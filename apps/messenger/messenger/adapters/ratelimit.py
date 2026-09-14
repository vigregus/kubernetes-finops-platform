"""Ограничение частоты на Redis.

В Redis, а не в памяти процесса: счётчик внутри пода умножается на число
реплик, то есть при трёх подах разрешает втрое больше обещанного. При
выкатке он к тому же обнуляется, и лимит обходится перезапуском.

Окно фиксированное, а не скользящее. Скользящее точнее на границе, но
стоит списка отметок на каждый ключ; для «не чаще раза в минуту» разница
в том, что на стыке окон можно успеть дважды, — и это приемлемо для
писем, но не будет приемлемо для отправки сообщений, где окно понадобится
другое.

**Поведение при недоступном Redis задаёт вызывающий**, и это главное
решение модуля. `CACHE-004` требует, чтобы при недоступном Redis чат
работал — значит, лимит на пути сообщения обязан пропускать. Но повторная
отправка письма при том же отказе обязана, наоборот, отказывать: иначе
недоступный Redis превращается в способ разослать почту. Одного значения
по умолчанию на оба случая не существует, поэтому его и нет.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

import redis.asyncio as aioredis
from redis.exceptions import RedisError

log = logging.getLogger(__name__)


class OnFailure(str, Enum):
    """Что делать, когда счётчик недоступен."""

    # Пропускать: отказ Redis не должен останавливать основной путь.
    ALLOW = "allow"
    # Отказывать: действие дороже отказа — рассылка почты, выдача ссылок.
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class LimitSettings:
    # БД 2 — роль security. Realtime (0) принадлежит Centrifugo,
    # app-cache (1) — приложению; счётчики лимитов не должны вымываться
    # вместе с кешем.
    url: str
    # Короткий: лимитер не на критическом пути, и ждать его дольше,
    # чем длится сам запрос, бессмысленно.
    timeout_seconds: float = 0.5


@dataclass(frozen=True, slots=True)
class LimitDecision:
    allowed: bool
    # Сколько ждать до следующей попытки. Уходит в заголовок `Retry-After`:
    # без него клиент повторяет вслепую и упирается снова.
    retry_after_seconds: int = 0
    # Решение принято без счётчика. Отдельный признак, потому что всплеск
    # таких решений — это отказ Redis, а не поведение пользователей.
    degraded: bool = False


@dataclass(slots=True)
class RateLimiter:
    settings: LimitSettings
    _client: aioredis.Redis | None = None

    def client(self) -> aioredis.Redis:
        if self._client is None:
            self._client = aioredis.from_url(
                self.settings.url,
                socket_timeout=self.settings.timeout_seconds,
                socket_connect_timeout=self.settings.timeout_seconds,
                decode_responses=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def take(
        self, key: str, *, limit: int, window_seconds: int, on_failure: OnFailure
    ) -> LimitDecision:
        """Считает попытку и говорит, разрешена ли она.

        Счёт идёт до проверки намеренно: иначе между чтением и увеличением
        успевает вклиниться второй запрос, и оба проходят. `INCR` атомарен,
        и первый же ответ говорит, каким по счёту оказался вызов.

        Срок ставится только на первой попытке окна. `EXPIRE` на каждой
        сдвигал бы конец окна вперёд, и окно никогда бы не заканчивалось —
        лимит превращался бы в блокировку навсегда.
        """
        try:
            client = self.client()
            # INCR и EXPIRE одним скриптом: между двумя командами процесс
            # мог умереть, оставив ключ без срока и вечную блокировку.
            used, ttl = await client.eval(
                """
                local used = redis.call('INCR', KEYS[1])
                if used == 1 then
                    redis.call('EXPIRE', KEYS[1], ARGV[1])
                end
                return {used, redis.call('TTL', KEYS[1])}
                """,
                1,
                key,
                window_seconds,
            )
            if int(used) <= limit:
                return LimitDecision(allowed=True)
            wait = int(ttl) if ttl and ttl > 0 else window_seconds
            return LimitDecision(allowed=False, retry_after_seconds=max(wait, 1))
        except (RedisError, OSError) as exc:
            log.warning(
                "счётчик лимитов недоступен",
                extra={
                    "event": "rate_limit_degraded",
                    "result": "failed",
                    "error_code": type(exc).__name__,
                    "dependency": "redis",
                    "decision": on_failure.value,
                },
            )
            return LimitDecision(
                allowed=on_failure is OnFailure.ALLOW,
                retry_after_seconds=window_seconds,
                degraded=True,
            )
