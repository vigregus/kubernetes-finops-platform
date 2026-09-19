"""API мессенджера: пробы, метрики, журналы и жизненный цикл зависимостей.

Бизнес-логики здесь нет и не должно быть: обработчик разбирает запрос и
отдаёт код ответа, всё остальное — ниже по слоям. Правило проверяется
автоматически (`scripts/check-layers.py`), и потому `api` не видит
репозиториев: обработчик, ходящий в базу напрямую, однажды сделает это
мимо транзакции, которой владеет сервис.
"""
from __future__ import annotations

import hmac
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import parse_qs

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field
from starlette.requests import Request

from messenger.domain.errors import Problem, Reason, to_problem
from messenger.domain.history import DEFAULT_PAGE_SIZE, validate_cursors
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
from messenger.services import history as history_service
from messenger.services import identity as identity_service
from messenger.services import login as login_service
from messenger.services import messages as message_service
from messenger.services import realtime as realtime_service
from messenger.services import runtime as runtime_service
from messenger.services import session_management as session_service
from messenger.services import verification as verification_service
from messenger.telemetry import logging as logging_envelope
from messenger.telemetry import metrics, trace, tracing
from messenger.telemetry.logging import configure

# Настройка - один раз на процесс; журнал - свой у модуля.
# Корневой журнал в поле `logger` назвался бы `root`, и по нему
# нельзя понять, чей это модуль.
configure()
log = logging.getLogger(__name__)

# Стадия конвейера. По ней политика хвостовой выборки различает порог
# задержки для веб-слоя и для доставки: у них разная норма, и общий
# порог удерживал бы всё, что медленно по меркам одного из них.
STAGE = "api"

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
    # Здесь, а не при импорте: настроенный раньше провайдер (тест,
    # подменивший экспортёр) не должен быть затёрт. `configure`
    # идемпотентна и второй раз ничего не делает.
    tracing.configure()
    await app.state.runtime.start()
    # Событие старта — отдельная запись: по ней видно, когда процесс
    # действительно принял конфигурацию, а не когда kubelet создал под.
    log.info("сервис поднят", extra={"event": "service_start", "result": "success"})
    try:
        yield
    finally:
        await app.state.runtime.stop()
        # Досылка остатка спанов. Ограничена по времени: выключение
        # пода не должно ждать мёртвый коллектор.
        tracing.shutdown()


app = FastAPI(title="Messenger API", docs_url=None, redoc_url=None, lifespan=lifespan)

# Владелец соединений создаётся при импорте, а не внутри lifespan: тогда
# проверка может подменить его, не поднимая приложение целиком, и модульный
# тест готовности не идёт в сеть.
app.state.runtime = runtime_service.Runtime(
    settings=runtime_service.pool_settings_from_env(), application_name=SERVICE
)


# --- ответ об ошибке --------------------------------------------------------

# RFC 9457. Тип содержимого отдельный: посредник, шлюз или клиентская
# библиотека по нему отличает описание отказа от полезного ответа, не
# разбирая тело и не угадывая по коду состояния.
PROBLEM_MEDIA_TYPE = "application/problem+json"

# Пространство имён кодов. URI по спецификации — опознаватель вида отказа,
# а не обязательно страница: разыменование его не требуется. Собственное
# пространство выбрано вместо `about:blank`, потому что `about:blank`
# означает «вид не уточняется», и тогда единственным различителем видов
# остаётся код состояния — а `403` у нас выдают три разные причины.
PROBLEM_TYPE_BASE = "https://app.finops.local/errors"


def _problem_body(problem: Problem) -> dict[str, object]:
    """Тело отказа по RFC 9457. Единственное место, где оно собирается.

    `code` остаётся рядом с `type`, хотя спецификация его не требует:
    по нему клиент различает виды отказа, и убрать его значило бы
    заставить разбирать хвост URI. Схема `Problem` дополнительные
    свойства допускает, так что это не расхождение с контрактом.

    `trace_id` кладётся в тело, а не только в заголовок `X-Trace-Id`:
    в поддержку приходят со скриншотом ответа, а не с заголовками, и
    журнал по нему ищется тем же запросом, что и по заголовку.
    """
    body: dict[str, object] = {
        "type": f"{PROBLEM_TYPE_BASE}/{problem.code}",
        "title": problem.title,
        "status": problem.status,
        "code": problem.code,
    }
    trace_id = trace.current_trace_id()
    if trace_id is not None:
        body["trace_id"] = trace_id
    return body


def _problem_response(problem: Problem, response: Response) -> Response:
    """Готовый ответ об отказе. Единственный способ его вернуть.

    Заголовки, выставленные обработчиком на внедрённом `response`,
    переносятся сюда вручную: FastAPI сливает их с ответом только тогда,
    когда обработчик вернул не `Response`. Без переноса пропали бы
    `Retry-After` у `429` и снятие cookie у неудачного обновления —
    то есть ровно то, без чего клиент повторяет вслепую.

    Обработчики, возвращающие этот ответ, объявляют `response_model`
    в декораторе явно. Вывести её из аннотации FastAPI больше не может —
    там объединение с `Response`, — а `response_model=None` отключил бы
    сериализацию и успешного тела тоже, и `datetime` уехал бы клиенту
    в другом написании, чем описано в контракте.
    """
    problem_response = JSONResponse(
        _problem_body(problem),
        status_code=problem.status,
        media_type=PROBLEM_MEDIA_TYPE,
    )
    problem_response.raw_headers.extend(response.raw_headers)
    return problem_response


# Отказы, которых нет в доменной таксономии: они случаются на границе HTTP,
# до всякой доменной операции, и заводить ради них доменную причину значило бы
# описывать в домене то, чего он не видит.
FORBIDDEN_ORIGIN = Problem(403, "forbidden", "Действие недоступно")
# Все три отказа logout-токена выглядят одинаково намеренно: чем именно плох
# токен, отправителю знать незачем — он либо свой и исправен, либо подбирает.
INVALID_LOGOUT_TOKEN = Problem(400, "invalid_logout_token", "Негодный logout-токен")
ALREADY_VERIFIED = Problem(409, "already_verified", "Адрес уже подтверждён")


@app.post("/internal/oidc/backchannel-logout", response_model=dict[str, object])
async def oidc_backchannel_logout(
    request: Request, response: Response
) -> dict[str, object] | Response:
    """Принимает подписанный logout-token от Keycloak.

    Точка внутренняя, но доверие строится не на сети: подпись, издатель,
    аудитория и тип события проверяются так же строго, как у access-токена.
    """
    try:
        form = parse_qs((await request.body()).decode("utf-8", errors="strict"))
    except UnicodeDecodeError:
        return _problem_response(INVALID_LOGOUT_TOKEN, response)
    token = form.get("logout_token", [None])[0]
    if not token:
        return _problem_response(INVALID_LOGOUT_TOKEN, response)
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
        return _problem_response(INVALID_LOGOUT_TOKEN, response)
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


def _login_failure(upstream_failed: bool) -> Problem:
    """Отказ входа. Недоступный Keycloak — не «неверное удостоверение».

    Различие видно только по коду состояния: `503` означает «повтори
    позже», `401` — «войди заново». Слить их в один ответ значило бы
    отправлять человека на повторный вход в момент, когда вход всё
    равно не работает.
    """
    if upstream_failed:
        return to_problem(Reason.UPSTREAM_UNAVAILABLE)
    return to_problem(Reason.UNAUTHENTICATED)


@app.post("/auth/callback", response_model=dict[str, object])
async def auth_callback(
    body: AuthorizationCode, request: Request, response: Response
) -> dict[str, object] | Response:
    """Обмен кода на токены. Делает сервер, а не браузер."""
    if not _origin_allowed(request):
        return _problem_response(FORBIDDEN_ORIGIN, response)

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
        return _problem_response(_login_failure(result.upstream_failed), response)
    if result.user_created and result.user is not None and not result.user.email_verified:
        # При verifyEmail=false Keycloak выдаёт токен неподтверждённому
        # пользователю, чтобы приложение могло дать ограниченный доступ.
        # Первое письмо отправляется ровно при создании нашего профиля;
        # неудача почты не отменяет вход — для повтора есть отдельная точка.
        await verification_service.send_initial_verification(
            user=result.user, admin=runtime.admin
        )
    return _respond(result, response)


@app.post("/auth/refresh", response_model=dict[str, object])
async def auth_refresh(request: Request, response: Response) -> dict[str, object] | Response:
    """Новый токен доступа по cookie. Вызывается при каждой загрузке вкладки."""
    if not _origin_allowed(request):
        return _problem_response(FORBIDDEN_ORIGIN, response)

    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        return _problem_response(to_problem(Reason.UNAUTHENTICATED), response)

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
        return _problem_response(_login_failure(result.upstream_failed), response)
    return _respond(result, response)


# --- сессии ----------------------------------------------------------------


def _bearer_token(request: Request) -> str | None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def _auth_failure(rejection: TokenRejection | None, response: Response) -> Response:
    """Одинаковый ответ для всех отказов токена; недоступные ключи — 503."""
    if rejection is TokenRejection.KEYS_UNAVAILABLE:
        return _problem_response(to_problem(Reason.UPSTREAM_UNAVAILABLE), response)
    return _problem_response(to_problem(Reason.UNAUTHENTICATED), response)


@app.get("/sessions", response_model=dict[str, object])
async def list_sessions(request: Request, response: Response) -> dict[str, object] | Response:
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
) -> Response:
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
async def revoke_all_sessions(request: Request, response: Response) -> Response:
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


@app.post("/realtime/token", response_model=dict[str, object])
async def issue_realtime_token(
    request: Request, response: Response
) -> dict[str, object] | Response:
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
) -> Response:
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

    # OWASP Logging Cheat Sheet называет source IP обязательным полем
    # для security-событий ("Where", рядом с "Who"/user_id и
    # "When"/timestamp) - его не было в конверте вовсе.
    #
    # Первое значение X-Forwarded-For, а не request.client.host: заголовок
    # проверки (`request.client.host`) внутри пода — это edge-gateway
    # (единственная точка входа снаружи, Gateway API на cilium-envoy,
    # gitops/02-infra/edge-gateway), а не браузер. Envoy по умолчанию
    # добавляет реальный адрес клиента в X-Forwarded-For на границе, а
    # не ретранслирует чужой, - тот же принцип доверия, что уже описан
    # для Keycloak (`proxy.headers: xforwarded`,
    # gitops/04-messenger/messenger-keycloak/manifests/keycloak.yaml).
    # Явно не проверено на живом кластере (нет внешнего запроса, которым
    # можно было бы это подтвердить) - если Envoy сконфигурирован иначе,
    # здесь окажется значение, которое сумеет подставить сам клиент.
    forwarded_for = request.headers.get("x-forwarded-for")
    client_ip = (
        forwarded_for.split(",")[0].strip()
        if forwarded_for
        else (request.client.host if request.client else None)
    )

    token = logging_envelope.REQUEST_ID.set(request_id)
    client_ip_token = logging_envelope.CLIENT_IP.set(client_ip)
    try:
        # Спан охватывает и обработку, и запись журнала обращений.
        # Закрыть его сразу после `call_next` - ошибка, которую не видно
        # глазами: запись об обращении пишется последней, и `span_id`
        # у неё - единственной, которая обязана его нести, - окажется
        # выдуманным, а сама запись останется на вид исправной.
        #
        # `anchor` помечает этот спан целью ссылки из outbox: отправитель
        # укажет на него как на работу, породившую событие.
        with tracing.span(
            request.method,
            kind=tracing.SERVER,
            parent=incoming,
            anchor=True,
            attributes={"messaging.pipeline.stage": STAGE},
        ) as span:
            try:
                response = await call_next(request)
            except Exception as exc:
                # Исключение, дошедшее сюда, обработчиком не превращено
                # в ответ, то есть наружу уйдёт 500. Помечаем явно:
                # хвостовая выборка решает по статусу спана, и без этой
                # строки самый интересный отказ сохранялся бы только
                # вероятностной политикой - то есть в пяти случаях
                # из сотни.
                #
                # Имя доводится до шаблона и здесь: отказавший запрос -
                # ровно тот, который в Tempo ищут, а односложное `POST`
                # не отвечает, куда он шёл. Шаблон к этому моменту уже
                # есть - исключение из обработчика приходит после
                # разбора маршрута; для отказа до разбора остаётся
                # заглушка `unmatched`, и она честна.
                span.update_name(f"{request.method} {_route_template(request)}")
                tracing.mark_failed(span, type(exc).__name__)
                raise

            # Тот же идентификатор уходит клиенту: без него в поддержке
            # спрашивают «когда это было», а не «какой у вас request id».
            response.headers["X-Request-Id"] = request_id
            # Трасса тоже: по ней видна та часть пути сообщения, что
            # случилась внутри запроса.
            response.headers["X-Trace-Id"] = span.trace_id
            route = _route_template(request)
            # Имя спана задаётся в два приёма, и это не выбор стиля:
            # шаблона маршрута на входе в посредник ещё нет - роутер
            # кладёт его в `scope` уже внутри `call_next`. Подставить
            # сырой путь значило бы завести кардинальность, от которой
            # этот же файл сознательно ушёл, заведя `_route_template`.
            span.update_name(f"{request.method} {route}")
            span.set_attribute("http.route", route)
            if response.status_code >= 500:
                # Отказ сервера помечается, отказ клиента - нет. 4xx
                # (401 от сканера портов в том числе) - законный исход,
                # а не сбой, и политика "ошибки хранить" обязана означать
                # настоящие ошибки. Иначе хранилище засоряется чужими
                # отказами, а разбирать их никто не идёт.
                #
                # Значение - код, а не слово: для HTTP семантические
                # конвенции предписывают именно его.
                tracing.mark_failed(span, str(response.status_code))
            elapsed = time.perf_counter() - started

            REQUESTS.labels(
                service=SERVICE,
                route=route,
                method=request.method,
                status_class=f"{response.status_code // 100}xx",
            ).inc()
            # exemplar: клик по точке на графике p99/p95 ведёт прямо
            # на пример трассы, а не в три хода (дашборд -> окно логов ->
            # клик по trace_id из строки). `span.trace_id` уже под рукой -
            # не нужен contextvar, как в telemetry/metrics.py._exemplar().
            DURATION.labels(service=SERVICE, route=route, method=request.method).observe(
                elapsed, exemplar={"trace_id": span.trace_id}
            )

            # Журнал обращений — отдельный поток со своим сроком хранения.
            #
            # Уровень поднят до WARNING при 5xx, а не оставлен INFO с
            # полем result=failed: `level:ERROR`/`level:WARN` - первый
            # фильтр, которым открывают VictoriaLogs при инциденте, и
            # раньше он не находил здесь ни одного отказа - все обращения
            # шли одним уровнем INFO, в отличие от адаптеров
            # (kafka.py, keycloak.py, centrifugo.py), которые корректно
            # используют warning/error. 4xx (в том числе 401 от сканера
            # портов) уровень не поднимает: это законный исход, не отказ.
            #
            # Вызов повторён на обеих ветках, а не собран через
            # `log_call = log.warning if ... else log.info` или через
            # вынесенный в переменную `extra=`: `scripts/check-log-streams.py`
            # разбирает AST и ждёт от `extra` дословный словарь прямо
            # в вызове `log.<уровень>(...)` — оба сокращения для него
            # невидимы, и каталог событий перестал бы проверять самую
            # частую запись в системе.
            if response.status_code >= 500:
                log.warning(
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
                        "result": "failed",
                    },
                )
            else:
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
                        "result": "success",
                    },
                )
            return response
    finally:
        logging_envelope.REQUEST_ID.reset(token)
        logging_envelope.CLIENT_IP.reset(client_ip_token)


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
    # Спан вокруг единственной точки разбора удостоверения: раз она одна,
    # его получают все маршруты бесплатно. Отказ помечается результатом,
    # но не статусом `ERROR`: 401 - законный исход, а не сбой, и политика
    # хвостовой выборки «ошибки хранить» обязана означать настоящие
    # ошибки, иначе каждый сканер портов засоряет хранилище.
    with tracing.span("auth.check") as span:
        result = await identity_service.authenticate(
            conn,
            token=token,
            keys=runtime.keys,
            settings=runtime.oidc_settings,
            device_id=_device_from(None, request),
            user_agent=request.headers.get("user-agent"),
        )
        if result.user is not None:
            span.set_attribute("enduser.id", str(result.user.user_id))
        span.set_attribute("messenger.auth.result", result.rejection.value
                           if result.rejection else "ok")
        return result


@app.get("/me", response_model=dict[str, object])
async def me(request: Request, response: Response) -> dict[str, object] | Response:
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
        return _problem_response(to_problem(Reason.UNAUTHENTICATED), response)

    user = auth.user
    return {
        "user_id": str(user.user_id),
        "display_name": user.display_name,
        "email": user.email,
        "email_verified": user.email_verified,
        "capabilities": sorted(c.value for c in capabilities_of(user)),
    }


@app.post("/conversations", response_model=dict[str, object])
async def create_direct_conversation(
    body: CreateDirectConversation, request: Request, response: Response
) -> dict[str, object] | Response:
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
        return _problem_response(to_problem(result.rejection), response)

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


def _invalid_payload(exc: ValueError) -> Problem:
    """Негодное содержимое: заголовком идёт причина отказа от домена.

    Текст приходит из доменной проверки и данных не содержит — он
    описывает нарушенное правило, а не то, что прислали.
    """
    return Problem(422, "invalid_payload", str(exc))


def _invalid_cursors(exc: ValueError) -> Problem:
    """Негодное сочетание курсоров: `400`, а не `422`.

    Сосед `_invalid_payload`, но код другой, и разница не в слое, а в
    предмете. Тело, не разобравшееся в модель, — это `422` от FastAPI;
    здесь тело разобралось полностью, негодна строка запроса. Контракт
    объявляет на этом маршруте `400` и перечисляет ровно эти нарушения,
    а `422` не объявлен в нём нигде — то есть `422` здесь был бы ответом,
    которого клиент не ждёт и в спецификации не найдёт.

    Новой `Reason` для этого не заводится. Таксономия `Reason` закрыта и
    описывает то, что произошло с данными или с правом; у `to_problem`
    заголовок статичный, и текст нарушенного правила — то единственное,
    по чему клиент поймёт, что не так с его запросом, — в него не
    поместится. Поэтому заголовком идёт он.

    Ноль и отрицательные значения проверяются здесь же, а не `Query(ge=..)`:
    ограничение в объявлении параметра вернуло бы `422` в чужом формате
    раньше, чем до проверки дошло бы дело, и объявленный `400` оказался бы
    недостижим — первый такой случай в API.
    """
    return Problem(400, "invalid_cursor", str(exc))


def _history_body(result: history_service.HistoryResult) -> dict[str, object]:
    """Страница в форме контракта. Все пять полей — всегда.

    Сообщения собираются тем же `_message_body`, что и в одиночной выдаче:
    вторая сборка тела рано или поздно разошлась бы с первой, и расхождение
    вылезло бы в самом заметном месте — в истории.

    Сравнение с `None` явное и только `is not None`. `sync_to_seq = 0` —
    законное значение («снимок есть, и он пуст»), и `if sync_to_seq:`
    превратил бы его в «снимка нет»: клиент перестал бы считать беседу
    синхронизированной и пошёл бы догонять то, что уже догнал.
    """
    return {
        "items": [_message_body(item) for item in result.page.items],
        "has_more": result.page.has_more,
        "next_before_seq": (
            int(result.next_before_seq)
            if result.next_before_seq is not None
            else None
        ),
        "next_after_seq": (
            int(result.next_after_seq) if result.next_after_seq is not None else None
        ),
        "sync_to_seq": (
            int(result.sync_to_seq) if result.sync_to_seq is not None else None
        ),
    }


@app.get("/conversations/{conversation_id}/messages", response_model=dict[str, object])
async def list_messages(
    conversation_id: uuid.UUID,
    request: Request,
    response: Response,
    before_seq: int | None = None,
    after_seq: int | None = None,
    through_seq: int | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
) -> dict[str, object] | Response:
    """История беседы: страницами назад или догрузкой вперёд.

    Два направления — две разные гарантии, а не два способа листать одно
    и то же. Назад (`before_seq`) страница устойчива к дописи в голову:
    уже загруженное не сдвигается. Вперёд (`after_seq`) — восстановление
    пропущенного после обрыва, и оно останавливается на границе снимка
    `through_seq`, потому что выше неё сообщения приходят потоком.
    """
    # Проверка до всего остального: негодная строка запроса не должна
    # занимать соединение с базой и, тем более, разбирать удостоверение.
    # Ответ на неё от ресурса не зависит, поэтому и порядок такой — как
    # у проверки содержимого в `send_message`.
    try:
        validate_cursors(
            before_seq=before_seq,
            after_seq=after_seq,
            through_seq=through_seq,
            limit=limit,
        )
    except ValueError as exc:
        return _problem_response(_invalid_cursors(exc), response)

    runtime = request.app.state.runtime
    # Два соединения, и не вложенных, — решение, а не небрежность.
    # Удостоверение читается всегда с писателя: `sessions` на отставшей
    # реплике оставила бы живой сессию, отозванную секунду назад, а
    # устаревшее состояние не расширяет права (И-1). Устаревание членства
    # для чтения санкционировано отдельно, устаревание сессии — нет.
    # Поэтому соединение под страницу берётся вторым и под свой режим.
    async with runtime.connection() as conn:
        auth = await _current(request, conn)
        if not auth.ok or auth.user is None:
            return _auth_failure(auth.rejection, response)

    mode = history_service.read_mode(before_seq=before_seq, after_seq=after_seq)
    async with runtime.connection(mode=mode) as conn:
        result = await history_service.list_messages(
            conn,
            viewer=auth.user,
            conversation_id=ConversationId(conversation_id),
            before_seq=before_seq,
            after_seq=after_seq,
            through_seq=through_seq,
            limit=limit,
        )

    if not result.ok:
        # Видимость отказа решает сервис, а не обработчик: он знает, пришёл
        # субъект по своей ссылке или подобрал идентификатор, и `Decision`
        # уже несёт ответ. Здесь он только доезжает до `to_problem` — иначе
        # объявленный контрактом `403` остался бы недостижимым.
        return _problem_response(
            to_problem(result.rejection or Reason.INTERNAL, result.visibility),
            response,
        )

    with tracing.span("response"):
        body_out = _history_body(result)
    return body_out


@app.post("/conversations/{conversation_id}/messages", response_model=dict[str, object])
async def send_message(
    conversation_id: uuid.UUID,
    body: SendMessage,
    request: Request,
    response: Response,
) -> dict[str, object] | Response:
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
            return _problem_response(to_problem(Reason.PAYLOAD_TOO_LARGE), response)
        return _problem_response(_invalid_payload(exc), response)

    runtime = request.app.state.runtime
    async with runtime.connection() as conn:
        auth = await _current(request, conn)
        if not auth.ok or auth.user is None:
            return _auth_failure(auth.rejection, response)

        # Пара берётся из якоря, а не из текущего спана. Сегодня текущий
        # спан на момент вызова действительно корневой, и хватило бы его
        # одного. Но это верно ровно до первого спана, добавленного
        # внутри обработчика, - и тогда ссылка из outbox молча уехала бы
        # на него.
        link = tracing.current_link() or (
            trace.current_trace_id(),
            trace.current_span_id(),
        )
        try:
            result = await message_service.send_message(
                conn,
                sender_id=auth.user.user_id,
                conversation_id=ConversationId(conversation_id),
                client_message_id=ClientMessageId(body.client_message_id),
                kind=kind,
                payload=payload,
                attachment_ids=tuple(AttachmentId(value) for value in body.attachment_ids),
                # Пара уезжает в outbox вместе с событием: contextvar
                # не переживает ни коммит, ни Kafka, и связать запрос
                # с доставкой можно только тем, что лежит в теле.
                trace_id=link[0],
                span_id=link[1],
            )
        except ValueError as exc:
            # Негодное сочетание вида и содержимого: голосовое без
            # длительности, текстовое без текста. Домен отвергает это
            # до обращения к базе, и ответ обязан быть 4xx, а не 500.
            return _problem_response(_invalid_payload(exc), response)

    if not result.ok or result.message is None:
        # Отсутствие членства наружу выглядит как отсутствие беседы:
        # `403` подтвердил бы, что она существует, и перебором
        # выяснялось бы, кто с кем переписывается.
        return _problem_response(to_problem(result.rejection or Reason.INTERNAL), response)

    response.status_code = 201 if result.created else 200
    with tracing.span("response"):
        body_out = _message_body(result.message)
    # Сквозной ключ на корневом спане запроса. После перехода на три
    # трассы поиск по `trace_id` находит только одну из трёх, поэтому
    # найти сообщение в Tempo можно ровно по этому атрибуту.
    tracing.set_anchor_attribute("messaging.message.id", str(result.message.message_id))
    return body_out


@app.post("/auth/verify-email/resend", response_model=dict[str, object])
async def resend_verification(
    request: Request, response: Response
) -> dict[str, object] | Response:
    """Отправить письмо о подтверждении ещё раз.

    Под лимитом: точка, рассылающая письмо по указанному адресу без
    ограничения, — готовый инструмент травли, потому что адрес указывает
    регистрирующийся, а письма приходят владельцу адреса.
    """
    runtime = request.app.state.runtime
    async with runtime.connection() as conn:
        auth = await _current(request, conn)

    if not auth.ok or auth.user is None:
        return _problem_response(to_problem(Reason.UNAUTHENTICATED), response)

    result = await verification_service.resend_verification(
        user=auth.user, limiter=runtime.limiter, admin=runtime.admin
    )

    if result.already_verified:
        return _problem_response(ALREADY_VERIFIED, response)
    if result.limited:
        # Без Retry-After клиент повторяет вслепую и упирается снова.
        response.headers["Retry-After"] = str(result.retry_after_seconds)
        return _problem_response(to_problem(Reason.RATE_LIMITED), response)
    if result.upstream_failed:
        return _problem_response(to_problem(Reason.UPSTREAM_UNAVAILABLE), response)

    response.status_code = 202
    return {"sent": True}
