"""Вход: проверенный токен превращается в профиль.

Два шага, и оба обязательны. Проверка токена отвечает на вопрос «кто это
и кем подтверждено»; приведение профиля — на вопрос «есть ли он у нас».
Первый шаг не ходит в базу, второй не разбирает подписи, и держать их
в одной функции значит не суметь проверить ни то, ни другое отдельно.

Отказ входа — значение, а не исключение: `401` описан в контракте.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg

from messenger.adapters import oidc
from messenger.domain.identity import Claims, TokenRejection
from messenger.domain.user import User
from messenger.repositories import users
from messenger.telemetry import metrics

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AuthResult:
    """Вошедший или причина отказа. Заполнено ровно одно поле."""

    user: User | None = None
    claims: Claims | None = None
    rejection: TokenRejection | None = None

    @property
    def ok(self) -> bool:
        return self.user is not None


async def authenticate(
    conn: asyncpg.Connection,
    *,
    token: str,
    keys: oidc.JwksCache,
    settings: oidc.OidcSettings,
) -> AuthResult:
    """Проверяет токен и приводит профиль к тому, что в нём написано.

    Соединение передаётся, а не берётся изнутри: вход бывает частью
    большей транзакции — например, когда первый запрос нового человека
    одновременно заводит профиль и создаёт беседу.

    Профиль обновляется на каждом входе намеренно. Имя и признак
    подтверждённого адреса меняются в Keycloak, и узнать об этом больше
    неоткуда: обратного уведомления он не шлёт, а читать его
    административное API на каждый запрос было бы дороже самой проверки.
    """
    check = await oidc.verify_access_token(token, keys=keys, settings=settings)
    if not check.ok:
        rejection = check.rejection or TokenRejection.MALFORMED
        metrics.token_rejected(rejection.value)
        log.info(
            "токен не принят",
            extra={
                "event": "token_rejected",
                "result": "failed",
                # Причина — в журнал и в метку метрики, но не в ответ:
                # разница между «подпись неверна» и «ключ неизвестен»
                # экономит время тому, кто подбирает.
                "error_code": rejection.value,
            },
        )
        return AuthResult(rejection=rejection)

    claims = check.claims
    if claims is None:
        # Недостижимо по построению TokenCheck, но `assert` здесь был бы
        # хуже: с -O он исчезает, и недостижимое становится падением
        # по None именно в проде.
        return AuthResult(rejection=TokenRejection.MALFORMED)

    result = await users.ensure_user(
        conn,
        external_id=claims.subject,
        display_name=claims.display_name,
        email=claims.email,
        email_verified=claims.email_verified,
    )
    if result.created:
        metrics.user_created()
        log.info(
            "заведена учётная запись",
            extra={"event": "user_created", "result": "success"},
        )
    return AuthResult(user=result.user, claims=claims)
