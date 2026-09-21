"""Realtime-аутентификация через Centrifugo connect/refresh proxy."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg

from messenger.adapters import oidc
from messenger.adapters.centrifugo import CentrifugoClient
from messenger.domain.identity import TokenRejection
from messenger.domain.ids import DeviceId, SessionId, UserId
from messenger.repositories import conversations as conversation_repo
from messenger.repositories import sessions, users
from messenger.services import identity


@dataclass(frozen=True, slots=True)
class RealtimeTokenResult:
    token: str = ""
    expires_at: datetime | None = None
    rejection: TokenRejection | None = None

    @property
    def ok(self) -> bool:
        return self.rejection is None


@dataclass(frozen=True, slots=True)
class RegisterConnectionResult:
    registered: bool = False
    rejection: TokenRejection | None = None

    @property
    def ok(self) -> bool:
        return self.rejection is None


@dataclass(frozen=True, slots=True)
class ProxyConnectResult:
    accepted: bool = False
    user_id: str = ""
    session_id: str = ""
    channels: tuple[str, ...] = ()
    expire_at: int = 0


async def issue_token_for_user(
    conn: asyncpg.Connection,
    *,
    token: str,
    keys: oidc.JwksCache,
    settings: oidc.OidcSettings,
    realtime: CentrifugoClient | None,
    device_id: DeviceId | None = None,
    user_agent: str | None = None,
) -> RealtimeTokenResult:
    """Выдаёт короткий ticket, который проверит connect-proxy."""
    auth = await identity.authenticate(
        conn,
        token=token,
        keys=keys,
        settings=settings,
        device_id=device_id,
        user_agent=user_agent,
    )
    if not auth.ok or auth.user is None or auth.session is None:
        return RealtimeTokenResult(
            rejection=auth.rejection or TokenRejection.MISSING_CLAIM
        )
    if realtime is None:
        return RealtimeTokenResult(rejection=TokenRejection.KEYS_UNAVAILABLE)

    user_id = str(auth.user.user_id)
    # Каналы бесед перечисляются явно и на момент выдачи. Токен короткий
    # именно поэтому: исключённый из беседы теряет подписку при следующем
    # соединении, а не когда-нибудь. Долгоживущий токен со списком каналов
    # означал бы, что выход из беседы ничего не меняет до его истечения.
    conversations = await conversation_repo.list_active_conversation_ids(
        conn, user_id=auth.user.user_id
    )
    channels = [f"user:{user_id}"]
    channels += [f"conversation:{conversation_id}" for conversation_id in conversations]
    issued, expires_at = realtime.issue_token(
        user_id,
        str(auth.session.session_id),
        channels=channels,
    )
    return RealtimeTokenResult(token=issued, expires_at=expires_at)


async def connect_from_ticket(
    conn: asyncpg.Connection,
    *,
    ticket: str,
    client_id: str,
    realtime: CentrifugoClient | None,
) -> ProxyConnectResult:
    """Проверяет ticket и регистрирует соединение до допуска Centrifugo."""
    if realtime is None:
        return ProxyConnectResult()
    claims = realtime.verify_ticket(ticket)
    if claims is None:
        return ProxyConnectResult()
    try:
        user_id = UserId(uuid.UUID(str(claims["sub"])))
        session_id = SessionId(uuid.UUID(str(claims["sid"])))
    except (KeyError, ValueError):
        return ProxyConnectResult()
    channels = tuple(str(channel) for channel in claims["channels"])

    # Регистрация соединения и отметка «был в сети» — одна транзакция.
    # Не ради аккуратности: `now()` в Postgres постоянен внутри неё, и оба
    # значения берутся из одного момента, поэтому отметка равна в точности
    # времени подтверждённого продления. Врозь они разошлись бы на время
    # между запросами, и тождество «last_seen_at = MAX(refreshed_at)»
    # перестало бы быть точным.
    async with conn.transaction():
        accepted = await sessions.register_realtime_connection(
            conn,
            session_id=session_id,
            user_id=user_id,
            client_id=client_id,
        )
        if not accepted:
            return ProxyConnectResult()
        # Отметка ставится только подтверждённой жизни: отказ регистрации
        # означает, что сессия отозвана или истекла, и «был в сети» в этот
        # момент было бы выдумкой.
        await users.touch_last_seen(conn, user_id=user_id)
    expire_at = int(
        (datetime.now(UTC) + timedelta(seconds=realtime.settings.token_ttl_seconds)).timestamp()
    )
    return ProxyConnectResult(
        accepted=True,
        user_id=str(user_id),
        session_id=str(session_id),
        channels=channels,
        expire_at=expire_at,
    )


async def refresh_connection(
    conn: asyncpg.Connection,
    *,
    user_id: str,
    session_id: str,
    client_id: str,
    realtime: CentrifugoClient | None,
) -> int | None:
    """Продлевает соединение либо просит Centrifugo закрыть его.

    Продление — тот же heartbeat, что и подключение: именно оно двигает
    `refreshed_at`, по которому считается присутствие, и оно же подтверждает
    жизнь для отметки «был в сети». `None` в ответе означает «продлевать
    нечего» — сессия отозвана, истекла или строку соединения успел убрать
    уборщик; прокси отвечает `expired`, и Centrifugo закрывает соединение.
    Это честный исход: раз клиент не продлевался дольше окна, сервер
    перестал за него ручаться, а клиент переподключится новым ticket'ом.
    """
    if realtime is None:
        return None
    try:
        uid = UserId(uuid.UUID(user_id))
        sid = SessionId(uuid.UUID(session_id))
    except ValueError:
        return None
    async with conn.transaction():
        alive = await sessions.refresh_realtime_connection(
            conn, session_id=sid, user_id=uid, client_id=client_id
        )
        if alive:
            await users.touch_last_seen(conn, user_id=uid)
    if not alive:
        return None
    return int(
        (datetime.now(UTC) + timedelta(seconds=realtime.settings.token_ttl_seconds)).timestamp()
    )


async def register_connection(
    conn: asyncpg.Connection,
    *,
    token: str,
    client_id: str,
    keys: oidc.JwksCache,
    settings: oidc.OidcSettings,
    device_id: DeviceId | None = None,
    user_agent: str | None = None,
) -> RegisterConnectionResult:
    """Совместимый старый endpoint; новые клиенты используют connect-proxy."""
    auth = await identity.authenticate(
        conn,
        token=token,
        keys=keys,
        settings=settings,
        device_id=device_id,
        user_agent=user_agent,
    )
    if not auth.ok or auth.user is None or auth.session is None:
        return RegisterConnectionResult(
            rejection=auth.rejection or TokenRejection.MISSING_CLAIM
        )
    # Транзакция здесь по той же причине, что и у connect-proxy: отметка
    # и `refreshed_at` обязаны прийти из одного `now()`, иначе тождество
    # «`last_seen_at` есть максимум подтверждённых продлений» перестаёт
    # быть точным — а проверить его на этом входе тоже обязано быть можно.
    async with conn.transaction():
        registered = await sessions.register_realtime_connection(
            conn,
            session_id=auth.session.session_id,
            user_id=auth.user.user_id,
            client_id=client_id,
        )
        if registered:
            # Тот же путь подтверждения жизни, что и у connect-proxy: маршрут
            # регистрации — второй вход в ту же таблицу, и разойтись отметкам
            # на двух входах значило бы, что «был в сети» зависит от того,
            # каким маршрутом клиент подключился.
            await users.touch_last_seen(conn, user_id=auth.user.user_id)
    return RegisterConnectionResult(registered=registered)
