"""API мессенджера: пробы, метрики, журналы и жизненный цикл зависимостей.

Бизнес-логики здесь нет и не должно быть: обработчик разбирает запрос и
отдаёт код ответа, всё остальное — ниже по слоям. Правило проверяется
автоматически (`scripts/check-layers.py`), и потому `api` не видит
репозиториев: обработчик, ходящий в базу напрямую, однажды сделает это
мимо транзакции, которой владеет сервис.
"""
from __future__ import annotations

import hmac
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import parse_qs

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field
from starlette.requests import Request

from messenger.domain.errors import Reason, to_problem
from messenger.domain.identity import TokenRejection
from messenger.domain.ids import (
    AttachmentId,
    ClientMessageId,
    ConversationId,
    DeviceId,
    SessionId,
    UserId,
)
from messenger.domain.message import (
    MessageKind,
    MessagePayload,
    validate_message_payload,
)
from messenger.domain.user import capabilities_of
from messenger.services import backchannel as backchannel_service
from messenger.services import conversations as conversation_service
from messenger.services import identity as identity_service
from messenger.services import login as login_service
from messenger.services import messages as message_service
from messenger.services import realtime as realtime_service
from messenger.services import runtime as runtime_service
from messenger.services import session_management as session_service
from messenger.services import verification as verification_service
from messenger.telemetry import logging as logging_envelope
from messenger.telemetry import metrics, trace
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


@app.post("/internal/oidc/backchannel-logout")
async def oidc_backchannel_logout(request: Request, response: Response) -> dict[str, object]:
    """Принимает подписанный logout-token от Keycloak.

    Точка внутренняя, но доверие строится не на сети: подпись, издатель,
    аудитория и тип события проверяются так же строго, как у access-токена.
    """
    try:
        form = parse_qs((await request.body()).decode("utf-8", errors="strict"))
    except UnicodeDecodeError:
        response.status_code = 400
        return {"code": "invalid_logout_token"}
    token = form.get("logout_token", [None])[0]
    if not token:
        response.status_code = 400
        return {"code": "invalid_logout_token"}
    runtime = request.app.state.runtime
    async with runtime.connection() as conn:
        result = await backchannel_service.handle_logout(
            conn,
            token=token,
            keys=runtime.keys,
            settings=runtime.oidc_settings,
            audience=runtime.backchannel_audience,
            realtime=runtime.centrifugo,
        )
    if not result.accepted:
        response.status_code = 400
        return {"code": "invalid_logout_token"}
    return {"accepted": True, "revoked": result.revoked}

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


class CreateDirectConversation(BaseModel):
    participant_id: uuid.UUID


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
    "/sessions/{session_id}", status_code=204, response_model=None
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
            realtime=runtime.centrifugo,
        )
    if not result.ok:
        return _auth_failure(result.rejection, response)

    final = Response(status_code=204)
    if result.current:
        # Cookie очищается даже при повторном запросе: локальный выход не
        # должен зависеть от того, успела ли строка уже стать отозванной.
        final.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)
    return final


@app.delete("/sessions", status_code=204, response_model=None)
async def revoke_all_sessions(request: Request, response: Response) -> Response | dict[str, str]:
    """Выход на всех устройствах: отзывает каждый действующий вход.

    Текущая сессия тоже отзывается, поэтому cookie снимается всегда, а не
    только когда счётчик непуст. Разрыв уже открытых WebSocket — отдельный
    путь доставки события, G1-008.
    """
    token = _bearer_token(request)
    if token is None:
        return _auth_failure(None, response)

    runtime = request.app.state.runtime
    async with runtime.connection() as conn:
        result = await session_service.revoke_all_for_token(
            conn,
            token=token,
            keys=runtime.keys,
            settings=runtime.oidc_settings,
            device_id=_device_from(None, request),
            user_agent=request.headers.get("user-agent"),
            realtime=runtime.centrifugo,
        )
    if not result.ok:
        return _auth_failure(result.rejection, response)

    final = Response(status_code=204)
    final.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)
    return final


# --- realtime ---------------------------------------------------------------


class RealtimeConnection(BaseModel):
    """Тело `POST /realtime/connections`: какой Centrifugo-идентификатор у
    только что открытого соединения. Значение недоверенное — Centrifugo сам
    генерирует `client` и отдаёт его клиенту, а клиент сообщает обратно."""

    client_id: str = Field(min_length=1, max_length=128)


class CentrifugoConnectRequest(BaseModel):
    """Внутренний запрос connect-proxy от Centrifugo."""

    client: str = Field(min_length=1, max_length=128)
    data: dict[str, object] = Field(default_factory=dict)


class CentrifugoRefreshRequest(BaseModel):
    """Внутренний запрос refresh-proxy от Centrifugo."""

    client: str = Field(min_length=1, max_length=128)
    user: str = Field(min_length=1, max_length=128)
    meta: dict[str, object] = Field(default_factory=dict)


def _centrifugo_proxy_authorized(request: Request) -> bool:
    runtime = request.app.state.runtime
    realtime = runtime.centrifugo
    if realtime is None:
        return False
    supplied = request.headers.get("x-realtime-proxy-key", "")
    return bool(supplied) and hmac.compare_digest(
        supplied, realtime.settings.api_key
    )


@app.post("/realtime/token")
async def issue_realtime_token(request: Request, response: Response) -> dict[str, object]:
    """Короткий ticket для подключения через connect-proxy.

    Тот же путь, что у клиента: проверенный bearer обменивается на токен
    Клиент передаёт его в поле `data.ticket`, а Centrifugo до допуска
    соединения сверяет ticket и живую сессию через внутренний proxy.
    """
    token = _bearer_token(request)
    if token is None:
        return _auth_failure(None, response)

    runtime = request.app.state.runtime
    async with runtime.connection() as conn:
        result = await realtime_service.issue_token_for_user(
            conn,
            token=token,
            keys=runtime.keys,
            settings=runtime.oidc_settings,
            realtime=runtime.centrifugo,
            device_id=_device_from(None, request),
            user_agent=request.headers.get("user-agent"),
        )
    if not result.ok:
        return _auth_failure(result.rejection, response)

    return {
        "token": result.token,
        "expires_at": result.expires_at.isoformat() if result.expires_at else None,
    }


@app.post("/internal/centrifugo/connect")
async def centrifugo_connect_proxy(
    body: CentrifugoConnectRequest, request: Request
) -> dict[str, object]:
    """Допускает WebSocket только после проверки живой сессии."""
    if not _centrifugo_proxy_authorized(request):
        return {"disconnect": {"code": 4501, "reason": "unauthorized"}}
    ticket = body.data.get("ticket")
    if not isinstance(ticket, str):
        return {"disconnect": {"code": 4501, "reason": "unauthorized"}}
    runtime = request.app.state.runtime
    async with runtime.connection() as conn:
        result = await realtime_service.connect_from_ticket(
            conn,
            ticket=ticket,
            client_id=body.client,
            realtime=runtime.centrifugo,
        )
    if not result.accepted:
        return {"disconnect": {"code": 4501, "reason": "session_revoked"}}
    return {
        "result": {
            "user": result.user_id,
            "channels": list(result.channels),
            "meta": {"session_id": result.session_id},
            "expire_at": result.expire_at,
        }
    }


@app.post("/internal/centrifugo/refresh")
async def centrifugo_refresh_proxy(
    body: CentrifugoRefreshRequest, request: Request
) -> dict[str, object]:
    """Не продлевает соединение отозванной или истёкшей сессии."""
    if not _centrifugo_proxy_authorized(request):
        return {"result": {"expired": True}}
    session_id = body.meta.get("session_id")
    if not isinstance(session_id, str):
        return {"result": {"expired": True}}
    runtime = request.app.state.runtime
    async with runtime.connection() as conn:
        expire_at = await realtime_service.refresh_connection(
            conn,
            user_id=body.user,
            session_id=session_id,
            client_id=body.client,
            realtime=runtime.centrifugo,
        )
    if expire_at is None:
        return {"result": {"expired": True}}
    return {"result": {"expire_at": expire_at}}


@app.post("/realtime/connections", status_code=204, response_model=None)
async def register_realtime_connection(
    body: RealtimeConnection, request: Request, response: Response
) -> Response | dict[str, str]:
    """Устаревший совместимый путь для клиентов предыдущей сборки.

    Новые клиенты не регистрируются после подключения: это делается
    connect-proxy до допуска WebSocket и без прежнего окна гонки.
    """
    token = _bearer_token(request)
    if token is None:
        return _auth_failure(None, response)

    runtime = request.app.state.runtime
    async with runtime.connection() as conn:
        result = await realtime_service.register_connection(
            conn,
            token=token,
            client_id=body.client_id,
            keys=runtime.keys,
            settings=runtime.oidc_settings,
            device_id=_device_from(None, request),
            user_agent=request.headers.get("user-agent"),
        )
    if not result.ok:
        return _auth_failure(result.rejection, response)

    return Response(status_code=204)

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

    # Идентификатор обращения: свой, если клиент его не прислал. Он
    # связывает все строки одного запроса - включая те, что пишет
    # сервисный слой, который про HTTP ничего не знает. Заголовок
    # принимается, потому что запрос к нам может быть продолжением
    # чужого, и разрывать цепочку на своей границе незачем.
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex

    # Трасса продолжается, если пришла, и начинается здесь, если нет.
    # `request_id` и `trace_id` — разные вещи, и нужны обе: первая
    # опознаёт обращение к нам, вторая — весь путь сообщения, который
    # после ответа клиенту продолжается в отправителе и потребителе.
    incoming = trace.parse(request.headers.get(trace.HEADER))

    token = logging_envelope.REQUEST_ID.set(request_id)
    try:
        # Привязка охватывает и обработку, и запись журнала обращений.
        # Закрыть её сразу после `call_next` - ошибка, которую не видно
        # глазами: запись об обращении пишется последней, и `trace_id`
        # у неё - единственной, которая обязана его нести, - окажется
        # пустым, а сама запись останется на вид исправной.
        with trace.bind(trace_id=incoming[0] if incoming else None) as trace_id:
            response = await call_next(request)

            # Тот же идентификатор уходит клиенту: без него в поддержке
            # спрашивают «когда это было», а не «какой у вас request id».
            response.headers["X-Request-Id"] = request_id
            # Трасса тоже: по ней виден весь путь сообщения, а не только
            # та его часть, что случилась внутри запроса.
            response.headers["X-Trace-Id"] = trace_id
            route = _route_template(request)
            elapsed = time.perf_counter() - started

            REQUESTS.labels(
                service=SERVICE,
                route=route,
                method=request.method,
                status_class=f"{response.status_code // 100}xx",
            ).inc()
            DURATION.labels(service=SERVICE, route=route, method=request.method).observe(
                elapsed
            )

            # Журнал обращений — отдельный поток со своим сроком хранения.
            log.info(
                "%s %s %s",
                request.method,
                route,
                response.status_code,
                extra={
                    "event": "http_request",
                    "log_stream": logging_envelope.STREAM_ACCESS,
                    "route": route,
                    "method": request.method,
                    "status": response.status_code,
                    "duration_ms": round(elapsed * 1000, 2),
                    "request_id": request_id,
                    "result": "success" if response.status_code < 500 else "failed",
                },
            )
            return response
    finally:
        logging_envelope.REQUEST_ID.reset(token)


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


@app.post("/conversations")
async def create_direct_conversation(
    body: CreateDirectConversation, request: Request, response: Response
) -> dict[str, object]:
    """Создаёт диалог от имени субъекта bearer-токена."""
    runtime = request.app.state.runtime
    async with runtime.connection() as conn:
        auth = await _current(request, conn)
        if not auth.ok or auth.user is None:
            return _auth_failure(auth.rejection, response)
        result = await conversation_service.create_direct(
            conn,
            actor=auth.user,
            participant_id=UserId(body.participant_id),
        )

    if not result.ok or result.conversation is None:
        problem = to_problem(result.rejection)
        response.status_code = problem.status
        return {"code": problem.code, "title": problem.title}

    response.status_code = 201 if result.created else 200
    conversation = result.conversation
    return {
        "conversation_id": str(conversation.conversation_id),
        "type": conversation.type.value,
        "participants": [
            {"user_id": str(user.user_id), "display_name": user.display_name}
            for user in result.participants
        ],
        "created_at": conversation.created_at,
    }


class SendMessagePayload(BaseModel):
    """Содержимое. Поля разные у разных видов — проверяет это домен."""

    text: str | None = None
    duration_ms: int | None = None


class SendMessage(BaseModel):
    """Тело `POST /conversations/{id}/messages`. Имена — из контракта."""

    # Генерируется клиентом и переиспользуется при повторе: именно он
    # делает отправку идемпотентной. Сервер такой идентификатор выдать
    # не может — повтор приходит как раз тогда, когда ответ сервера
    # до клиента не дошёл.
    client_message_id: uuid.UUID
    type: Literal["text", "image", "file", "voice"]
    payload: SendMessagePayload
    attachment_ids: list[uuid.UUID] = Field(default_factory=list)


def _message_body(message) -> dict[str, object]:
    """Сообщение в форме контракта.

    Содержимое удалённого не отдаётся: надгробие сохраняет номер
    и место в истории, но не текст.
    """
    payload: dict[str, object] = {}
    if message.deleted_at is None:
        if message.payload.text is not None:
            payload["text"] = message.payload.text
        if message.payload.duration_ms is not None:
            payload["duration_ms"] = message.payload.duration_ms
    return {
        "message_id": str(message.message_id),
        "conversation_id": str(message.conversation_id),
        "seq": int(message.conversation_seq),
        "sender_id": str(message.sender_id),
        "client_message_id": str(message.client_message_id),
        "type": message.kind.value,
        "payload": payload,
        "created_at": message.created_at,
        "edited_at": message.edited_at,
        "deleted_at": message.deleted_at,
    }


@app.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: uuid.UUID,
    body: SendMessage,
    request: Request,
    response: Response,
) -> dict[str, object]:
    """Принимает сообщение. Идемпотентно по `client_message_id`.

    Повтор возвращает то же сообщение с тем же `message_id` и `seq`
    и кодом `200`, а не создаёт второе. Различить их клиенту нужно:
    `201` означает «принято сейчас», `200` — «было принято раньше,
    и твой первый запрос всё-таки дошёл».

    В Kafka отсюда не ходят. Сообщение и оба события outbox ложатся
    одним коммитом, а отправкой занимается отдельная нагрузка — иначе
    сетевой вызов держал бы блокировку строки беседы столько же,
    сколько длится таймаут брокера.
    """
    try:
        kind = MessageKind(body.type)
        payload = MessagePayload(
            text=body.payload.text, duration_ms=body.payload.duration_ms
        )
        # Та же доменная проверка, что и в сервисе, но раньше: негодный
        # запрос не должен занимать соединение с базой и тем более ждать
        # блокировку строки беседы. Правило одно — вызывается дважды,
        # а не переписывается здесь своими словами.
        validate_message_payload(kind, payload)
    except ValueError as exc:
        # Предел длины описан в контракте отдельным кодом: клиенту важно
        # отличить «слишком длинно» от «поле не то», потому что в первом
        # случае повтор бессмыслен без правки текста.
        if "длиннее" in str(exc):
            problem = to_problem(Reason.PAYLOAD_TOO_LARGE)
            response.status_code = problem.status
            return {"code": problem.code, "title": problem.title}
        response.status_code = 422
        return {"code": "invalid_payload", "title": str(exc)}

    runtime = request.app.state.runtime
    async with runtime.connection() as conn:
        auth = await _current(request, conn)
        if not auth.ok or auth.user is None:
            return _auth_failure(auth.rejection, response)

        try:
            result = await message_service.send_message(
                conn,
                sender_id=auth.user.user_id,
                conversation_id=ConversationId(conversation_id),
                client_message_id=ClientMessageId(body.client_message_id),
                kind=kind,
                payload=payload,
                attachment_ids=tuple(AttachmentId(value) for value in body.attachment_ids),
                # Трасса уезжает в outbox вместе с событием: contextvar
                # не переживает ни коммит, ни Kafka, и связать запрос
                # с доставкой можно только тем, что лежит в теле.
                trace_id=trace.current_trace_id(),
            )
        except ValueError as exc:
            # Негодное сочетание вида и содержимого: голосовое без
            # длительности, текстовое без текста. Домен отвергает это
            # до обращения к базе, и ответ обязан быть 4xx, а не 500.
            response.status_code = 422
            return {"code": "invalid_payload", "title": str(exc)}

    if not result.ok or result.message is None:
        # Отсутствие членства наружу выглядит как отсутствие беседы:
        # `403` подтвердил бы, что она существует, и перебором
        # выяснялось бы, кто с кем переписывается.
        problem = to_problem(result.rejection or Reason.INTERNAL)
        response.status_code = problem.status
        return {"code": problem.code, "title": problem.title}

    response.status_code = 201 if result.created else 200
    return _message_body(result.message)


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
