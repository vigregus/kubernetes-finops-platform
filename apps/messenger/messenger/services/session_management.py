"""Список входов и отзыв одной сессии.

HTTP-слой не знает про SQL: он передаёт проверенный bearer-токен сюда,
а сервис связывает удостоверение с принадлежащими ему строками. Идентификатор
в URL считается недоверенным — ограничение по ``user_id`` остаётся в самом
``UPDATE``, поэтому подобрать и закрыть чужую сессию нельзя.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import asyncpg

from messenger.adapters import oidc
from messenger.domain.identity import TokenRejection
from messenger.domain.ids import DeviceId, SessionId
from messenger.domain.session import RevocationReason, SessionView
from messenger.repositories import sessions
from messenger.services import identity
from messenger.telemetry import metrics

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SessionListResult:
    """Активные входы либо причина, по которой токен не принят."""

    items: list[SessionView] = field(default_factory=list)
    rejection: TokenRejection | None = None

    @property
    def ok(self) -> bool:
        return self.rejection is None


@dataclass(frozen=True, slots=True)
class RevokeResult:
    """Результат отзыва одной принадлежащей пользователю сессии."""

    revoked: bool = False
    current: bool = False
    rejection: TokenRejection | None = None

    @property
    def ok(self) -> bool:
        return self.rejection is None


@dataclass(frozen=True, slots=True)
class RevokeAllResult:
    """Результат «выйти везде»: число закрытых сессий либо отказ токена."""

    revoked: int = 0
    rejection: TokenRejection | None = None

    @property
    def ok(self) -> bool:
        return self.rejection is None


async def list_for_token(
    conn: asyncpg.Connection,
    *,
    token: str,
    keys: oidc.JwksCache,
    settings: oidc.OidcSettings,
    device_id: DeviceId | None = None,
    user_agent: str | None = None,
) -> SessionListResult:
    """Возвращает только сессии владельца предъявленного токена."""
    auth = await identity.authenticate(
        conn,
        token=token,
        keys=keys,
        settings=settings,
        device_id=device_id,
        user_agent=user_agent,
    )
    if not auth.ok or auth.user is None or auth.session is None:
        return SessionListResult(
            rejection=auth.rejection or TokenRejection.MISSING_CLAIM
        )

    items = await sessions.list_active_sessions(
        conn, user_id=auth.user.user_id, current=auth.session.session_id
    )
    return SessionListResult(items=items)


async def revoke_for_token(
    conn: asyncpg.Connection,
    *,
    token: str,
    target: SessionId,
    keys: oidc.JwksCache,
    settings: oidc.OidcSettings,
    device_id: DeviceId | None = None,
    user_agent: str | None = None,
) -> RevokeResult:
    """Отзывает одну сессию, не раскрывая существование чужой.

    Неизвестная, уже отозванная и чужая цель выглядят одинаково: операция
    идемпотентно завершается, но ``revoked`` остаётся ложным. Наружу это
    различие не выходит, иначе UUID превращается в способ перечислять входы
    другого пользователя.
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
        return RevokeResult(rejection=auth.rejection or TokenRejection.MISSING_CLAIM)

    current = target == auth.session.session_id
    revoked = await sessions.revoke_session(
        conn,
        session_id=target,
        user_id=auth.user.user_id,
        reason=RevocationReason.LOGOUT_DEVICE,
    )
    if revoked:
        metrics.sessions_revoked(RevocationReason.LOGOUT_DEVICE.value)
        log.info(
            "сессия отозвана пользователем",
            extra={
                "event": "session_revoked",
                "result": "success",
                "reason": RevocationReason.LOGOUT_DEVICE.value,
                "current": current,
            },
        )
    return RevokeResult(revoked=revoked, current=current)


async def revoke_all_for_token(
    conn: asyncpg.Connection,
    *,
    token: str,
    keys: oidc.JwksCache,
    settings: oidc.OidcSettings,
    device_id: DeviceId | None = None,
    user_agent: str | None = None,
) -> RevokeAllResult:
    """Отзывает все действующие сессии владельца токена. Возвращает их число.

    Число — не украшение: «выйти везде» обязано породить событие о каждом
    закрытом входе, и сравнить число событий с числом строк — единственный
    способ заметить, что путь доставки потерял часть. До появления G1-008
    число фиксируется в журнале и метрике, чтобы этот счёт уже существовал.
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
        return RevokeAllResult(
            rejection=auth.rejection or TokenRejection.MISSING_CLAIM
        )

    revoked = await sessions.revoke_user_sessions(
        conn, user_id=auth.user.user_id, reason=RevocationReason.LOGOUT_ALL
    )
    metrics.sessions_revoked(RevocationReason.LOGOUT_ALL.value, revoked)
    log.info(
        "все сессии отозваны",
        extra={
            "event": "sessions_revoked_all",
            "result": "success",
            "reason": RevocationReason.LOGOUT_ALL.value,
            "revoked": revoked,
        },
    )
    return RevokeAllResult(revoked=revoked)
