"""API мессенджера: пробы, метрики, журналы и жизненный цикл зависимостей.

Бизнес-логики здесь нет и не должно быть: обработчик разбирает запрос и
отдаёт код ответа, всё остальное — ниже по слоям. Правило проверяется
автоматически (`scripts/check-layers.py`), и потому `api` не видит
репозиториев: обработчик, ходящий в базу напрямую, однажды сделает это
мимо транзакции, которой владеет сервис.
"""
from __future__ import annotations

import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field
from starlette.requests import Request

from messenger.domain.identity import TokenRejection
from messenger.domain.ids import DeviceId, SessionId
from messenger.domain.user import capabilities_of
from messenger.services import identity as identity_service
from messenger.services import login as login_service
from messenger.services import runtime as runtime_service
from messenger.services import session_management as session_service
from messenger.services import verification as verification_service
from messenger.telemetry import metrics
from messenger.telemetry.logging import configure

log = configure()

# Имя берётся из телеметрии, а не из `os.getenv` заново. Два чтения одной
# переменной с разными значениями по умолчанию дают метрики с `service="api"`
# и `service="unknown"` в одном процессе — ряд разъезжается, а дашборд
# показывает половину.
SERVICE = metrics.SERVICE
VERSION = os.getenv("SERVICE_VERSION", "unversioned")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Открывает пул при старте и закрывает при остановке.

    Неудача при открытии процесс не роняет: падение в момент подъёма даёт
    CrashLoopBackOff, и когда база возвращается, кластер держит под
    в экспоненциальной паузе ещё несколько минут. Вместо этого сервис
    поднимается не готовым — трафика он не получит, а перезапускать
    его не за что.
    """
    await app.state.runtime.start()
    # Событие старта — отдельная запись: по ней видно, когда процесс
    # действительно принял конфигурацию, а не когда kubelet создал под.
    log.info("сервис поднят", extra={"event": "service_start", "result": "success"})
    try:
        yield
    finally:
        await app.state.runtime.stop()


app = FastAPI(title="Messenger API", docs_url=None, redoc_url=None, lifespan=lifespan)

# Владелец соединений создаётся при импорте, а не внутри lifespan: тогда
# проверка может подменить его, не поднимая приложение целиком, и модульный
# тест готовности не идёт в сеть.
app.state.runtime = runtime_service.Runtime(
    settings=runtime_service.pool_settings_from_env(), application_name=SERVICE
)

# --- вход -------------------------------------------------------------------

# Имя и путь cookie. Путь узкий: cookie отправляется только на точки обмена,
# и на обычные запросы API браузер её не прикладывает. Это и есть причина,
# по которой подделка межсайтового запроса не работает на остальном API —
# заголовок `Authorization` браузер сам не ставит.
REFRESH_COOKIE = os.getenv("AUTH_COOKIE_NAME", "messenger_refresh")
REFRESH_COOKIE_PATH = os.getenv("AUTH_COOKIE_PATH", "/api/v1/auth")
# Источник, которому разрешено обращаться к точкам обмена. Проверяется,
# потому что cookie браузер прикладывает сам, — это WEBSEC-003.
WEB_ORIGIN = os.getenv("WEB_ORIGIN", "https://app.finops.local")
# Локально страница может открываться по HTTP, и тогда `Secure` означает,
# что cookie не будет установлена вовсе. Значение по умолчанию строгое.
COOKIE_SECURE = os.getenv("AUTH_COOKIE_SECURE", "true").lower() != "false"


class AuthorizationCode(BaseModel):
    """Тело `POST /auth/callback`. Имена совпадают с контрактом."""

    code: str = Field(min_length=1)
    code_verifier: str = Field(min_length=1)
    redirect_uri: str = Field(min_length=1)
    device_id: uuid.UUID | None = None


def _origin_allowed(request: Request) -> bool:
    """Источник запроса — свой.

    Заголовка может не быть вовсе: его ставит браузер, а сервер-к-серверу
    обращается без него. Отсутствие не считается нарушением — подделка
    межсайтового запроса возможна только из браузера, а он `Origin`
    на POST присылает всегда.
    """
    origin = request.headers.get("origin")
    return origin is None or origin == WEB_ORIGIN


def _device_from(value: uuid.UUID | None, request: Request) -> DeviceId | None:
    if value is not None:
        return DeviceId(value)
    header = request.headers.get("x-device-id")
    if not header:
        return None
    try:
        return DeviceId(uuid.UUID(header))
    except ValueError:
        # Мусор в заголовке — не повод отказывать во входе: сервер просто
        # выдаст новое устройство.
        return None


def _respond(result: login_service.LoginResult, response: Response) -> dict[str, object]:
    """Кладёт токен обновления в cookie, а в тело — только токен доступа.

    Токен обновления в теле означал бы, что скрипт на странице может его
    прочитать и унести, — ровно то, ради чего выбран `HttpOnly` (ADR 0005).
    """
    if result.refresh_token:
        response.set_cookie(
            REFRESH_COOKIE,
            result.refresh_token,
            max_age=result.refresh_expires_in or None,
            path=REFRESH_COOKIE_PATH,
            httponly=True,
            secure=COOKIE_SECURE,
            samesite="strict",
        )
    return {
        "access_token": result.access_token,
        "expires_in": result.expires_in,
        "device_id": str(result.device_id) if result.device_id else None,
    }


@app.post("/auth/callback")
async def auth_callback(
    body: AuthorizationCode, request: Request, response: Response
) -> dict[str, object]:
    """Обмен кода на токены. Делает сервер, а не браузер."""
    if not _origin_allowed(request):
        response.status_code = 403
        return {"code": "forbidden", "title": "Действие недоступно"}

    runtime = request.app.state.runtime
    async with runtime.connection() as conn:
        result = await login_service.login_with_code(
            conn,
            code=body.code,
            code_verifier=body.code_verifier,
            redirect_uri=body.redirect_uri,
            settings=runtime.login,
            keys=runtime.keys,
            device_id=_device_from(body.device_id, request),
            user_agent=request.headers.get("user-agent"),
        )

    if not result.ok:
        response.status_code = 503 if result.upstream_failed else 401
        return {"code": "unauthenticated", "title": "Требуется вход"}
    if result.user_created and result.user is not None and not result.user.email_verified:
        # При verifyEmail=false Keycloak выдаёт токен неподтверждённому
        # пользователю, чтобы приложение могло дать ограниченный доступ.
        # Первое письмо отправляется ровно при создании нашего профиля;
        # неудача почты не отменяет вход — для повтора есть отдельная точка.
        await verification_service.send_initial_verification(
            user=result.user, admin=runtime.admin
        )
    return _respond(result, response)


@app.post("/auth/refresh")
async def auth_refresh(request: Request, response: Response) -> dict[str, object]:
    """Новый токен доступа по cookie. Вызывается при каждой загрузке вкладки."""
    if not _origin_allowed(request):
        response.status_code = 403
        return {"code": "forbidden", "title": "Действие недоступно"}

    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        response.status_code = 401
        return {"code": "unauthenticated", "title": "Требуется вход"}

    runtime = request.app.state.runtime
    async with runtime.connection() as conn:
        result = await login_service.refresh_access(
            conn,
            refresh_token=token,
            settings=runtime.login,
            keys=runtime.keys,
            device_id=_device_from(None, request),
            user_agent=request.headers.get("user-agent"),
        )

    if not result.ok:
        # Cookie снимается: она больше не работает, и оставлять её значит
        # обрекать вкладку на повторные отказы при каждой перезагрузке.
        response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)
        response.status_code = 503 if result.upstream_failed else 401
        return {"code": "unauthenticated", "title": "Требуется вход"}
    return _respond(result, response)


# --- сессии ----------------------------------------------------------------


def _bearer_token(request: Request) -> str | None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def _auth_failure(rejection: TokenRejection | None, response: Response) -> dict[str, str]:
    """Одинаковый ответ для всех отказов токена; недоступные ключи — 503."""
    if rejection is TokenRejection.KEYS_UNAVAILABLE:
        response.status_code = 503
        return {"code": "upstream_unavailable", "title": "Временно недоступно"}
    response.status_code = 401
    return {"code": "unauthenticated", "title": "Требуется вход"}


@app.get("/sessions")
async def list_sessions(request: Request, response: Response) -> dict[str, object]:
    """Активные входы пользователя; текущий помечен явно."""
    token = _bearer_token(request)
    if token is None:
        return _auth_failure(None, response)

    runtime = request.app.state.runtime
    async with runtime.connection() as conn:
        result = await session_service.list_for_token(
            conn,
            token=token,
            keys=runtime.keys,
            settings=runtime.oidc_settings,
            device_id=_device_from(None, request),
            user_agent=request.headers.get("user-agent"),
        )
    if not result.ok:
        return _auth_failure(result.rejection, response)
    return {
        "items": [
            {
                "session_id": str(item.session_id),
                "device_id": str(item.device_id),
                "user_agent": item.user_agent,
                "created_at": item.created_at,
                "last_seen_at": item.last_seen_at,
                "current": item.current,
            }
            for item in result.items
        ]
    }


@app.delete(
    "/sessions/{session_id}", status_code=204, response_class=Response, response_model=None
)
async def revoke_session(
    session_id: uuid.UUID, request: Request, response: Response
) -> Response | dict[str, str]:
    """Закрывает один вход; чужой или уже закрытый не раскрывается."""
    token = _bearer_token(request)
    if token is None:
        return _auth_failure(None, response)

    runtime = request.app.state.runtime
    async with runtime.connection() as conn:
        result = await session_service.revoke_for_token(
            conn,
            token=token,
            target=SessionId(session_id),
            keys=runtime.keys,
            settings=runtime.oidc_settings,
            device_id=_device_from(None, request),
            user_agent=request.headers.get("user-agent"),
        )
    if not result.ok:
        return _auth_failure(result.rejection, response)

    final = Response(status_code=204)
    if result.current:
        # Cookie очищается даже при повторном запросе: локальный выход не
        # должен зависеть от того, успела ли строка уже стать отозванной.
        final.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)
    return final

# «Что сейчас запущено» — непрерывный ряд. Из него нельзя строить отметки
# на графиках: Grafana поставит отметку на каждой точке. Для «когда
# произошло изменение» есть эксплуатационное событие, и это другая вещь.
BUILD_INFO = Gauge(
    "messenger_build_info", "Запущенная версия сервиса", ["service", "version"]
)
BUILD_INFO.labels(service=SERVICE, version=VERSION).set(1)

# RED. Метка route — шаблон маршрута, а не путь: `/conversations/123/messages`
# в метке означал бы новый временной ряд на каждую беседу, то есть гибель
# хранилища метрик в момент роста нагрузки.
REQUESTS = Counter(
    "http_requests_total",
    "Запросы HTTP",
    ["service", "route", "method", "status_class"],
)
DURATION = Histogram(
    "http_request_duration_seconds",
    "Длительность обработки запроса",
    ["service", "route", "method"],
)

READY = Gauge("messenger_ready", "Готовность принимать трафик", ["service"])
READY.labels(service=SERVICE).set(0)


def _route_template(request: Request) -> str:
    """Шаблон маршрута из роутера, иначе — заглушка.

    Возврат сырого пути был бы ровно тем, что запрещено: неизвестный путь
    от сканера портов создал бы временной ряд на каждый его запрос.
    """
    route = request.scope.get("route")
    return getattr(route, "path", "unmatched")


@app.middleware("http")
async def observe(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    route = _route_template(request)
    elapsed = time.perf_counter() - started

    REQUESTS.labels(
        service=SERVICE,
        route=route,
        method=request.method,
        status_class=f"{response.status_code // 100}xx",
    ).inc()
    DURATION.labels(service=SERVICE, route=route, method=request.method).observe(elapsed)

    # Журнал обращений — отдельный поток со своим сроком хранения.
    log.info(
        "%s %s %s",
        request.method,
        route,
        response.status_code,
        extra={
            "event": "http_request",
            "stream": "access",
            "route": route,
            "method": request.method,
            "status": response.status_code,
            "duration_ms": round(elapsed * 1000, 2),
            "result": "success" if response.status_code < 500 else "failed",
        },
    )
    return response


@app.get("/livez")
async def livez() -> dict[str, str]:
    """Жив ли процесс. Зависимости здесь не проверяются намеренно.

    Проба живости, падающая из-за недоступной базы, устраивает каскад:
    kubelet перезапускает исправные поды, те стартуют, снова не видят базу
    и перезапускаются опять — пока база восстанавливается, сервис
    целенаправленно уничтожает сам себя.
    """
    return {"status": "ok"}


@app.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, object]:
    """Можно ли давать трафик. Вот здесь зависимости и проверяются.

    Проверка — настоящий запрос к базе тем же путём, каким пойдёт рабочий:
    через PgBouncer, от роли приложения. «Клиент создан» доказывает только
    то, что объект создан, — пул считается открытым, пока первый запрос
    не упрётся в закрытый порт.

    Каждая зависимость названа в ответе отдельно. `ready: false` без
    разбивки означает поход в журналы в момент, когда как раз некогда.
    """
    runtime = request.app.state.runtime
    report = await runtime_service.check_readiness(runtime)
    READY.labels(service=SERVICE).set(1 if report.ready else 0)
    if not report.ready:
        response.status_code = 503
    return {"ready": report.ready, "checks": report.checks}


@app.get("/metrics")
async def metrics() -> Response:
    """Метрики снимаются опросом, а не отправляются.

    Отправка сделала бы хранилище метрик участником критического пути:
    его недоступность замедляла бы обработку запроса.
    """
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# --- разбор удостоверения ----------------------------------------------------

UNAUTHENTICATED = {"code": "unauthenticated", "title": "Требуется вход"}


def _bearer(request: Request) -> str | None:
    """Токен из заголовка. Регистр схемы не фиксирован спецификацией."""
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


async def _current(request: Request, conn) -> identity_service.AuthResult:
    """Кто обращается. Единственное место разбора удостоверения в `api`.

    Полноценная точка проверки прав — `authorize()` из G1-012; здесь только
    «кто это», без единого решения о доступе. Разница существенная:
    решение о доступе, принятое в обработчике, однажды будет принято в нём
    иначе, чем в соседнем.
    """
    token = _bearer(request)
    if token is None:
        return identity_service.AuthResult()
    runtime = request.app.state.runtime
    return await identity_service.authenticate(
        conn,
        token=token,
        keys=runtime.keys,
        settings=runtime.oidc_settings,
        device_id=_device_from(None, request),
        user_agent=request.headers.get("user-agent"),
    )


@app.get("/me")
async def me(request: Request, response: Response) -> dict[str, object]:
    """Кто я и что мне сейчас доступно.

    Возможности отдаются списком, а не выводятся клиентом из
    `email_verified`. Правило одно и живёт в домене; продублированное
    в интерфейсе, оно однажды разойдётся с сервером — и человек увидит
    доступную кнопку, которая отвечает отказом.
    """
    runtime = request.app.state.runtime
    async with runtime.connection() as conn:
        auth = await _current(request, conn)

    if not auth.ok or auth.user is None:
        response.status_code = 401
        return dict(UNAUTHENTICATED)

    user = auth.user
    return {
        "user_id": str(user.user_id),
        "display_name": user.display_name,
        "email": user.email,
        "email_verified": user.email_verified,
        "capabilities": sorted(c.value for c in capabilities_of(user)),
    }


@app.post("/auth/verify-email/resend")
async def resend_verification(request: Request, response: Response) -> dict[str, object]:
    """Отправить письмо о подтверждении ещё раз.

    Под лимитом: точка, рассылающая письмо по указанному адресу без
    ограничения, — готовый инструмент травли, потому что адрес указывает
    регистрирующийся, а письма приходят владельцу адреса.
    """
    runtime = request.app.state.runtime
    async with runtime.connection() as conn:
        auth = await _current(request, conn)

    if not auth.ok or auth.user is None:
        response.status_code = 401
        return dict(UNAUTHENTICATED)

    result = await verification_service.resend_verification(
        user=auth.user, limiter=runtime.limiter, admin=runtime.admin
    )

    if result.already_verified:
        response.status_code = 409
        return {"code": "already_verified", "title": "Адрес уже подтверждён"}
    if result.limited:
        response.status_code = 429
        # Без Retry-After клиент повторяет вслепую и упирается снова.
        response.headers["Retry-After"] = str(result.retry_after_seconds)
        return {"code": "rate_limited", "title": "Слишком часто"}
    if result.upstream_failed:
        response.status_code = 503
        return {"code": "upstream_unavailable", "title": "Временно недоступно"}

    response.status_code = 202
    return {"sent": True}
