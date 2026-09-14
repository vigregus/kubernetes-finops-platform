"""Centrifugo: connect-токены, события и принудительный разрыв соединений.

Две роли, и обе живут здесь. Первая — выдача connect-токена: HS256 JWT,
подписанный тем же ключом, что держит Centrifugo. Вторая — серверный HTTP
API (`/publish`, `/disconnect`), по которому отзыв сессии немедленно рвёт
открытые соединения и рассылает сигнал о выходе.

Разрыв — best-effort: источник истины об отзыве лежит в Postgres, и HTTP
уже отрезал отозванную сессию. Недоступный Centrifugo не должен отменять
сам отзыв, поэтому методы возвращают `False`, а не бросают — вызывающий
фиксирует неудачу в журнале и метрике и идёт дальше. Это то же решение,
что в `ratelimit.py`: поведение при отказе зависимости задаёт вызывающий.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import jwt

log = logging.getLogger(__name__)

# Код close-кадра, который получает клиент при отзыве сессии. Не 1000
# (нормальное завершение) намеренно: клиент должен отличать «сервер выгнал
# за отзыв доступа» от «пользователь закрыл вкладку» и не пытаться
# переподключиться в первом случае.
DISCONNECT_CODE_SESSION_REVOKED = 3000
REASON_SESSION_REVOKED = "session_revoked"


@dataclass(frozen=True, slots=True)
class CentrifugoSettings:
    """Адрес и ключи Centrifugo. Ключи из секрета, в git их нет."""

    # База серверного HTTP API: под него подставляются `/publish`, `/disconnect`.
    api_url: str
    # Ключ серверного API. Centrifugo принимает его в `X-API-Key`.
    api_key: str
    # Секрет подписи connect-токена. Тот же, что у Centrifugo в
    # `token_hmac_secret_key`, — иначе токен подписан не тем ключом.
    token_hmac_secret_key: str
    # Короткий: разрыв не на критическом пути, ждать его дольше запроса
    # бессмысленно.
    request_timeout_seconds: float = 2.0
    # Сколько живёт connect-токен. Клиент переподключается со свежим.
    token_ttl_seconds: int = 120


@dataclass(slots=True)
class CentrifugoClient:
    settings: CentrifugoSettings

    def issue_token(
        self,
        user_id: str,
        channels: list[str],
        *,
        ttl_seconds: int | None = None,
    ) -> tuple[str, datetime]:
        """Connect-токен на подписку на перечисленные каналы.

        `sub` — идентификатор пользователя, `channels` — явный список.
        Клиент не выбирает канал сам: иначе подписка на чужую беседу
        сводится к знанию её идентификатора (то же правило, что в
        `contracts/websocket/channels.json`).
        """
        ttl = ttl_seconds or self.settings.token_ttl_seconds
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl)
        token = jwt.encode(
            {
                "sub": user_id,
                "exp": int(expires_at.timestamp()),
                "channels": channels,
            },
            self.settings.token_hmac_secret_key,
            algorithm="HS256",
        )
        return token, expires_at

    async def publish(self, channel: str, data: dict) -> bool:
        """Публикует событие в канал. `False` при недоступности Centrifugo."""
        return await self._call("publish", {"channel": channel, "data": data})

    async def disconnect_user(
        self, user_id: str, *, code: int, reason: str
    ) -> bool:
        """Рвёт все соединения пользователя — «выйти везде»."""
        return await self._call(
            "disconnect",
            {"user": user_id, "disconnect": {"code": code, "reason": reason}},
        )

    async def disconnect_client(
        self, user_id: str, client_id: str, *, code: int, reason: str
    ) -> bool:
        """Рвёт одно соединение по `client` — «выйти на этом устройстве».

        `user` обязателен даже при указании `client`: фильтр Centrifugo ищет
        соединение внутри пользователя, а не по всему кластеру. Весь
        `user_id` при этом не рвётся — фильтр сужен до одного `client`.
        """
        return await self._call(
            "disconnect",
            {
                "user": user_id,
                "client": client_id,
                "disconnect": {"code": code, "reason": reason},
            },
        )

    async def _call(self, method: str, payload: dict) -> bool:
        """Один запрос к серверному API. Недоступность — `False`, не падение."""
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.request_timeout_seconds
            ) as http:
                response = await http.post(
                    f"{self.settings.api_url}/{method}",
                    json=payload,
                    headers={"X-API-Key": self.settings.api_key},
                )
                response.raise_for_status()
                return True
        except (httpx.HTTPError, OSError) as exc:
            log.warning(
                "Centrifugo недоступен",
                extra={
                    "event": "centrifugo_unavailable",
                    "result": "failed",
                    "error_code": type(exc).__name__,
                    "dependency": "centrifugo",
                    "method": method,
                },
            )
            return False
