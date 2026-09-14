"""Вход: проверенный токен превращается в профиль.

Два шага, и оба обязательны. Проверка токена отвечает на вопрос «кто это
и кем подтверждено»; приведение профиля — на вопрос «есть ли он у нас».
Первый шаг не ходит в базу, второй не разбирает подписи, и держать их
в одной функции значит не суметь проверить ни то, ни другое отдельно.

Отказ входа — значение, а не исключение: `401` описан в контракте.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg

from messenger.adapters import oidc
from messenger.domain.identity import Claims, TokenRejection
from messenger.domain.ids import DeviceId, SessionId
from messenger.domain.session import Device, Session
from messenger.domain.user import User
from messenger.repositories import sessions, users
from messenger.telemetry import metrics

# Сколько наша строка считает вход действующим, если из него не приходило
# запросов. Совпадает с idle-таймаутом реалма: строка только отражает
# сессию Keycloak, и расходиться с ним ей незачем.
SESSION_IDLE = timedelta(days=7)

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AuthResult:
    """Вошедший или причина отказа. Заполнено ровно одно поле."""

    user: User | None = None
    claims: Claims | None = None
    session: Session | None = None
    device: Device | None = None
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
    device_id: DeviceId | None = None,
    user_agent: str | None = None,
    now: datetime | None = None,
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

    user = result.user
    if claims.session_state is None:
        # Токен без `sid` — служебная учётная запись: у неё нет ни входа,
        # ни устройства, и заводить их нечего.
        return AuthResult(user=user, claims=claims)

    session_id = SessionId(uuid.UUID(claims.session_state))
    moment = now or datetime.now(UTC)

    # Отозванный вход отсекается до всякой записи. Иначе «выйти везде»
    # переставало бы работать ровно в тот момент, когда им пользуются:
    # первый же запрос с ещё живым токеном продлевал бы сессию.
    existing = await sessions.fetch_session(conn, session_id=session_id)
    if existing is not None and existing.is_revoked:
        metrics.token_rejected(TokenRejection.SESSION_REVOKED.value)
        log.info(
            "вход отозван",
            extra={
                "event": "session_revoked_access",
                "result": "failed",
                "error_code": TokenRejection.SESSION_REVOKED.value,
            },
        )
        return AuthResult(rejection=TokenRejection.SESSION_REVOKED)

    device = await _device_for(conn, user_id=user.user_id, device_id=device_id,
                              user_agent=user_agent)
    session = await sessions.ensure_session(
        conn,
        session_id=session_id,
        user_id=user.user_id,
        device_id=device.device_id,
        expires_at=moment + SESSION_IDLE,
    )
    return AuthResult(user=user, claims=claims, session=session, device=device)


async def _device_for(
    conn: asyncpg.Connection,
    *,
    user_id,
    device_id: DeviceId | None,
    user_agent: str | None,
) -> Device:
    """Устройство пользователя; чужой идентификатор заменяется новым.

    Идентификатор приходит от клиента, значит, подобрать чужой можно.
    Репозиторий такую привязку не делает и возвращает `None`; здесь это
    не ошибка, а повод выдать новый — человек просто получит новую строку
    в списке «где я вошёл», а чужое устройство останется чужим.
    """
    if device_id is not None:
        device = await sessions.ensure_device(
            conn, device_id=device_id, user_id=user_id, user_agent=user_agent
        )
        if device is not None:
            return device
        log.info(
            "идентификатор устройства занят другим пользователем",
            extra={"event": "device_id_rejected", "result": "failed"},
        )

    fresh = await sessions.ensure_device(
        conn, device_id=DeviceId(uuid.uuid4()), user_id=user_id, user_agent=user_agent
    )
    if fresh is None:
        # Только что созданный uuid4 не может принадлежать другому.
        raise RuntimeError("не удалось завести устройство")
    return fresh
