"""Вход: обмен у Keycloak и приведение своего состояния в порядок.

Разложено на три шага, каждый из которых уже существует отдельно: обмен
(`adapters/keycloak`), проверка выданного токена (`adapters/oidc`)
и зеркалирование профиля с сессией (`services/identity`).

Выданный токен проверяется, хотя только что получен от самого Keycloak.
Это не паранойя, а единственный способ узнать `sub` и `sid` — то, чем
подписан вход, — не доверяя разбору без проверки подписи. Заодно
настройки издателя и аудитории проверяются на живом токене при каждом
входе, а не однажды при настройке.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import asyncpg

from messenger.adapters import keycloak, oidc
from messenger.domain.ids import DeviceId
from messenger.domain.session import Session
from messenger.domain.user import User
from messenger.services import identity
from messenger.telemetry import logging as logging_envelope
from messenger.telemetry import metrics

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LoginResult:
    """Что отдать браузеру, или причина отказа.

    Токен обновления здесь есть, но в тело ответа он не попадает: его
    место — cookie `HttpOnly`, и решает это обработчик, а не сервис.
    """

    access_token: str | None = None
    refresh_token: str | None = None
    expires_in: int = 0
    refresh_expires_in: int = 0
    user: User | None = None
    session: Session | None = None
    device_id: DeviceId | None = None
    user_created: bool = False
    # Внутренняя причина: в журнал и в метку `error_class`, не в ответ.
    error_class: str | None = None
    # Отличает «клиент не прав» от «мы не смогли»: 401 против 503.
    upstream_failed: bool = False

    @property
    def ok(self) -> bool:
        return self.access_token is not None


@dataclass(frozen=True, slots=True)
class LoginSettings:
    """Всё, что нужно для входа. Собирается один раз при старте."""

    tokens: keycloak.TokenSettings
    oidc: oidc.OidcSettings


async def login_with_code(
    conn: asyncpg.Connection,
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    settings: LoginSettings,
    keys: oidc.JwksCache,
    device_id: DeviceId | None = None,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> LoginResult:
    """Первый вход или вход после выхода: код меняется на токены."""
    exchange = await keycloak.exchange_code(
        code=code,
        code_verifier=code_verifier,
        redirect_uri=redirect_uri,
        settings=settings.tokens,
    )
    return await _finish(
        conn,
        exchange=exchange,
        settings=settings,
        keys=keys,
        device_id=device_id,
        user_agent=user_agent,
        now=now,
        operation="login",
    )


async def refresh_access(
    conn: asyncpg.Connection,
    *,
    refresh_token: str,
    settings: LoginSettings,
    keys: oidc.JwksCache,
    device_id: DeviceId | None = None,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> LoginResult:
    """Перезагрузка страницы: cookie меняется на новый токен доступа.

    Несколько вкладок обменивают каждая свой, и одновременные запросы —
    норма, а не признак подбора. Реалм выдаёт одноразовые токены
    обновления, поэтому каждый обмен возвращает новый.
    """
    exchange = await keycloak.refresh(refresh_token=refresh_token, settings=settings.tokens)
    return await _finish(
        conn,
        exchange=exchange,
        settings=settings,
        keys=keys,
        device_id=device_id,
        user_agent=user_agent,
        now=now,
        operation="refresh",
    )


async def _finish(
    conn: asyncpg.Connection,
    *,
    exchange: keycloak.ExchangeResult,
    settings: LoginSettings,
    keys: oidc.JwksCache,
    device_id: DeviceId | None,
    user_agent: str | None,
    now: datetime | None,
    operation: str,
) -> LoginResult:
    if not exchange.ok or exchange.tokens is None:
        failure = exchange.failure or keycloak.ExchangeFailure.INVALID_GRANT
        metrics.login_failed(operation, failure.value)
        return LoginResult(
            error_class=failure.value,
            upstream_failed=failure is keycloak.ExchangeFailure.UPSTREAM_UNAVAILABLE,
        )

    tokens = exchange.tokens
    auth = await identity.authenticate(
        conn,
        token=tokens.access_token,
        keys=keys,
        settings=settings.oidc,
        device_id=device_id,
        user_agent=user_agent,
        now=now,
    )
    if not auth.ok or auth.user is None:
        # Keycloak выдал токен, а мы его не приняли. Это либо расхождение
        # настроек (аудитория, издатель), либо отозванный вход, о котором
        # Keycloak ещё не знает, - и различить их можно только по причине.
        reason = auth.rejection.value if auth.rejection else "unknown"
        metrics.login_failed(operation, reason)
        log.warning(
            "выданный токен не прошёл собственную проверку",
            extra={"event": "login_rejected", "operation": operation,
                   "result": "failed", "error_code": reason,
                   "log_stream": logging_envelope.STREAM_SECURITY},
        )
        return LoginResult(error_class=reason)

    metrics.login_succeeded(operation)
    return LoginResult(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
        refresh_expires_in=tokens.refresh_expires_in,
        user=auth.user,
        session=auth.session,
        device_id=auth.device.device_id if auth.device else None,
        user_created=auth.user_created,
    )
