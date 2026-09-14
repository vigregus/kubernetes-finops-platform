"""Сессия и устройство: что именно у нас своё, а что живёт в Keycloak.

Keycloak владеет входом и знает про свою SSO-сессию. Наша строка — не её
копия ради копии: без неё «выйти везде» означает «подождите, пока истекут
все выданные токены», то есть ближайшие минуты чужой доступ сохраняется.
Отзыв обязан действовать сразу, а проверить его можно только у себя.

Поэтому идентификатор сессии у нас **тот же**, что у Keycloak: утверждение
`sid` из токена. Свой отдельный идентификатор потребовал бы таблицы
соответствия, которая существует ровно для того, чтобы её однажды
рассинхронизировать.

Две вкладки — это одно устройство и одна сессия; ноутбук и телефон —
разные. Устройство существует в первой версии не ради будущего мобильного
клиента, а потому что список «где я вошёл» показывают именно по ним.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from messenger.domain.ids import DeviceId, SessionId, UserId


class RevocationReason(str, Enum):
    """Почему сессия закончилась. Хранится и показывается в аудите.

    Причина — не украшение: «вышел сам» и «отключён администратором»
    разбираются по-разному, а `AUTHZ-005` требует, чтобы привилегированная
    операция оставляла след. Операция без следа считается несовершённой.
    """

    # Немедленные — событие обязано дойти до Centrifugo и разорвать
    # соединения. Путь доставки события — отдельная задача, G1-008.
    LOGOUT_DEVICE = "logout_device"
    LOGOUT_ALL = "logout_all"
    # noqa ниже — линтер видит слово «password» и считает это зашитым
    # паролем. Здесь это название причины отзыва, а не секрет.
    PASSWORD_CHANGE = "password_change"  # noqa: S105
    ADMIN_DISABLE = "admin_disable"
    # Отложенный: доступ пропадает при следующем обновлении токена.
    # Отдельная причина, потому что это не действие человека, а срок.
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class Device:
    """Откуда вошли. Две вкладки одного браузера — одно устройство."""

    device_id: DeviceId
    user_id: UserId
    user_agent: str | None
    created_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True, slots=True)
class Session:
    """Вход с устройства. `session_id` — это `sid` из токена Keycloak."""

    session_id: SessionId
    user_id: UserId
    device_id: DeviceId
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    revoked_reason: str | None = None

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def is_live_at(self, moment: datetime) -> bool:
        """Действует ли сессия в этот момент.

        Два условия, а не одно. Отозванная сессия мертва независимо от
        срока; не отозванная, но истёкшая — тоже. Проверять только
        `revoked_at` значит принимать токен, срок которого вышел час
        назад, если из этой сессии никто не выходил.
        """
        return not self.is_revoked and self.expires_at > moment


@dataclass(frozen=True, slots=True)
class SessionView:
    """Строка списка «где я вошёл». Ровно то, что отдаёт `GET /sessions`.

    Собирается из сессии и устройства: `user_agent` и `last_seen_at`
    принадлежат устройству, а не входу, — иначе каждая новая сессия
    заводила бы собственную копию строки браузера.
    """

    session_id: SessionId
    device_id: DeviceId
    user_agent: str | None
    created_at: datetime
    last_seen_at: datetime
    # Сессия, из которой сделан этот запрос. Без признака человек не
    # понимает, какую строку нельзя закрывать, и закрывает свою.
    current: bool = False
