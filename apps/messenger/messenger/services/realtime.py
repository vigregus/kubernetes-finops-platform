"""Выдача connect-токена и привязка realtime-соединения к сессии.

HTTP-слой не знает ни про Centrifugo, ни про формат токена: он передаёт
проверенный bearer-токен сюда, а сервис связывает удостоверение с профилем
и выдаёт токен на подписку на личный канал.

Связь session_id → соединение замыкается иначе, чем кажется: Centrifugo v6
сам генерирует идентификатор соединения и возвращает его клиенту в ответе
на подключение, задать его заранее нельзя. Поэтому клиент после подключения
сообщает его обратно через ``POST /realtime/connections``, и он ложится в
``sessions.realtime_client_id``. По нему отзыв на устройстве рвёт ровно это
соединение вызовом ``disconnect{user, client}``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import asyncpg

from messenger.adapters import oidc
from messenger.adapters.centrifugo import CentrifugoClient
from messenger.domain.identity import TokenRejection
from messenger.domain.ids import DeviceId
from messenger.repositories import sessions
from messenger.services import identity


@dataclass(frozen=True, slots=True)
class RealtimeTokenResult:
    """Connect-токен либо причина отказа."""

    token: str = ""
    expires_at: datetime | None = None
    rejection: TokenRejection | None = None

    @property
    def ok(self) -> bool:
        return self.rejection is None


@dataclass(frozen=True, slots=True)
class RegisterConnectionResult:
    """Итог привязки соединения к сессии либо причина отказа токена."""

    registered: bool = False
    rejection: TokenRejection | None = None

    @property
    def ok(self) -> bool:
        return self.rejection is None


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
    """Connect-токен на подписку на личный канал вошедшего.

    Отказ токена доходит как `rejection` и обрабатывается как любой отказ
    входа. Ненастроенный Centrifugo — это «подключиться некуда», то есть
    отказ системы (503), а не пользователя.
    """
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
    issued, expires_at = realtime.issue_token(
        user_id, channels=[f"user:{user_id}"]
    )
    return RealtimeTokenResult(token=issued, expires_at=expires_at)


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
    """Привязывает соединение Centrifugo к действующей своей сессии.

    ``client_id`` — значение, которое Centrifugo сам вернул клиенту, то есть
    недоверенное. Привязка идёт строго к собственной действующей сессии
    (условие `user_id` и `revoked_at IS NULL` в `UPDATE`), поэтому подобранным
    идентификатором нельзя пометить чужое соединение. Уже отозванная сессия
    не принимает привязку — ``registered`` остаётся ложным, но это не отказ
    токена: отзыв в Postgres уже отрезал доступ, и привязывать не к чему.
    """
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

    registered = await sessions.set_realtime_client_id(
        conn,
        session_id=auth.session.session_id,
        user_id=auth.user.user_id,
        client_id=client_id,
    )
    return RegisterConnectionResult(registered=registered)
