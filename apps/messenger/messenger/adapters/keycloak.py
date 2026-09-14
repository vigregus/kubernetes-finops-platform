"""Обмен у Keycloak: код на токены и токен обновления на новый доступ.

Отдельно от `oidc.py` намеренно. Там — проверка чужой подписи, операция
без состояния и без секретов; здесь — обращение к чужой службе, которое
может не ответить, ответить медленно или отказать. Смешать их значит
получить проверку токена, ходящую в сеть.

Обмен делает сервер, а не браузер: только так токен обновления попадает
в cookie `HttpOnly` и остаётся недоступным скрипту на странице (ADR 0005).
Клиент при этом публичный и с PKCE — секрет, положенный в страницу,
секретом быть перестаёт, а без PKCE перехваченный код обменивает кто угодно.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

import httpx

log = logging.getLogger(__name__)


class ExchangeFailure(str, Enum):
    """Почему обмен не удался. Наружу не уходит — только в журнал и метку."""

    # Код или токен обновления неверен, просрочен или уже использован.
    INVALID_GRANT = "invalid_grant"
    # Запрос собран неправильно: не тот redirect_uri, не тот verifier.
    INVALID_REQUEST = "invalid_request"
    # Keycloak не ответил. Это 503, а не 401: клиент ни в чём не виноват.
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"


@dataclass(frozen=True, slots=True)
class TokenSettings:
    """Куда обращаться за токенами и от чьего имени."""

    # Внутренний адрес: тот, что достижим из пода. Издатель в выданном
    # токене при этом останется внешним — см. `adapters/oidc.py`.
    token_url: str
    client_id: str
    request_timeout_seconds: float = 5.0


@dataclass(frozen=True, slots=True)
class TokenPair:
    """Что вернул Keycloak. Ни одно поле не попадает в журнал."""

    access_token: str
    refresh_token: str | None
    expires_in: int
    # Срок жизни самого токена обновления. Нужен для Max-Age cookie:
    # cookie, живущая дольше токена внутри, оставляет человека с ощущением
    # действующего входа, который на самом деле уже не работает.
    refresh_expires_in: int = 0


@dataclass(frozen=True, slots=True)
class ExchangeResult:
    """Пара токенов или причина отказа. Заполнено ровно одно поле."""

    tokens: TokenPair | None = None
    failure: ExchangeFailure | None = None

    @property
    def ok(self) -> bool:
        return self.tokens is not None


async def exchange_code(
    *, code: str, code_verifier: str, redirect_uri: str, settings: TokenSettings
) -> ExchangeResult:
    """Меняет код авторизации на пару токенов.

    `redirect_uri` передаётся снова, хотя перенаправление уже случилось:
    Keycloak сверяет его с тем, для которого код выдан. Без этой сверки
    код, утёкший через журнал прокси, обменивается на токен в другом месте.
    """
    return await _post(
        {
            "grant_type": "authorization_code",
            "client_id": settings.client_id,
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
        },
        settings=settings,
        operation="exchange_code",
    )


async def refresh(*, refresh_token: str, settings: TokenSettings) -> ExchangeResult:
    """Меняет токен обновления на новый токен доступа.

    Реалм настроен на одноразовые токены обновления, поэтому в ответе
    приходит новый — и старый перестаёт работать. Перехваченный токен
    живёт до первого обмена, а не до истечения срока.
    """
    return await _post(
        {
            "grant_type": "refresh_token",
            "client_id": settings.client_id,
            "refresh_token": refresh_token,
        },
        settings=settings,
        operation="refresh_token",
    )


async def _post(form: dict[str, str], *, settings: TokenSettings, operation: str) -> ExchangeResult:
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as http:
            response = await http.post(settings.token_url, data=form)
    except httpx.HTTPError as exc:
        log.warning(
            "Keycloak не ответил на обмен",
            extra={
                "event": "token_exchange",
                "operation": operation,
                "result": "failed",
                "error_code": type(exc).__name__,
                "dependency": "keycloak",
            },
        )
        return ExchangeResult(failure=ExchangeFailure.UPSTREAM_UNAVAILABLE)

    if response.status_code == 200:
        body = response.json()
        return ExchangeResult(
            tokens=TokenPair(
                access_token=body["access_token"],
                refresh_token=body.get("refresh_token"),
                expires_in=int(body.get("expires_in", 0)),
                refresh_expires_in=int(body.get("refresh_expires_in", 0)),
            )
        )

    # Тело ответа в журнал не идёт целиком: при неверном запросе Keycloak
    # повторяет в описании то, что прислали, а прислали там код авторизации.
    failure = ExchangeFailure.INVALID_GRANT
    try:
        code = response.json().get("error")
    except ValueError:
        code = None
    if code and code != "invalid_grant":
        failure = ExchangeFailure.INVALID_REQUEST
    if response.status_code >= 500:
        failure = ExchangeFailure.UPSTREAM_UNAVAILABLE

    log.info(
        "обмен отклонён",
        extra={
            "event": "token_exchange",
            "operation": operation,
            "result": "failed",
            "error_code": failure.value,
            "status": response.status_code,
        },
    )
    return ExchangeResult(failure=failure)
