"""Что приложение знает о вошедшем и на каком основании.

Пароли, вторая ступень и сброс живут в Keycloak. Сюда приходит уже
проверенный токен, и задача домена — назвать, что в нём обязано быть
и как называется каждый повод его не принять.

Отказ здесь — значение, а не исключение: `401` описан в контракте, то есть
это обычный ответ, а не поломка. Причина отказа названа отдельно от ответа
по той же причине, что и в таксономии ошибок: наружу уходит «требуется
вход», а в журнал и в метку метрики — что именно не сошлось.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class TokenRejection(str, Enum):
    """Почему токен не принят. Идёт в метку `error_class`, не в ответ.

    Различать эти случаи наружу нельзя — подсказка «подпись неверна»
    против «ключ неизвестен» экономит время тому, кто подбирает. Внутрь
    различать обязательно: всплеск `UNKNOWN_KEY` означает ротацию ключей,
    всплеск `EXPIRED` — сломанное обновление токена у клиентов, и это
    разные ночные звонки.
    """

    MALFORMED = "malformed"
    UNKNOWN_KEY = "unknown_key"
    BAD_SIGNATURE = "bad_signature"
    EXPIRED = "expired"
    WRONG_ISSUER = "wrong_issuer"
    WRONG_AUDIENCE = "wrong_audience"
    MISSING_CLAIM = "missing_claim"
    # Ключи недоступны — это не «токен плохой», а «проверить нечем».
    # Отдельная причина, потому что ответ на неё другой: 503, а не 401.
    KEYS_UNAVAILABLE = "keys_unavailable"


@dataclass(frozen=True, slots=True)
class Claims:
    """Содержимое проверенного токена, приведённое к нашим понятиям.

    `subject` — это `external_id` профиля. Не адрес почты: адрес меняется,
    и привязка к нему однажды разъедет учётную запись с её перепиской.
    """

    subject: str
    email: str | None
    email_verified: bool
    display_name: str | None
    # Идентификатор сессии в Keycloak. Понадобится для немедленного отзыва:
    # «выйти везде» обязано рвать соединения, а не ждать истечения токена.
    session_state: str | None
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class TokenCheck:
    """Результат проверки. Ровно одно из двух полей заполнено."""

    claims: Claims | None = None
    rejection: TokenRejection | None = None

    @property
    def ok(self) -> bool:
        return self.claims is not None

    @classmethod
    def accepted(cls, claims: Claims) -> TokenCheck:
        return cls(claims=claims)

    @classmethod
    def rejected(cls, rejection: TokenRejection) -> TokenCheck:
        return cls(rejection=rejection)
