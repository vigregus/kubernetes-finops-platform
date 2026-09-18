"""Сборка сообщения из двух событий и защита от повторной доставки.

Факт и содержимое физически разнесены по разным потокам (`SEC-010`),
и приходят они независимо: порядок гарантирован внутри партиции, а не
между топиками. Значит, потребитель realtime обязан дождаться обеих
половин, и половина, пришедшая первой, должна где-то полежать.

Здесь же живёт дедупликация. Транспорт выбран at-least-once сознательно:
отправитель может умереть между публикацией и отметкой, потребитель —
между обработкой и фиксацией смещения. Раз повтор неизбежен, каждый
получатель обязан его пережить, и переживает он его отметкой «это
сообщение я уже отдал», а не надеждой.

Redis, а не память процесса: реплик потребителя может быть несколько,
и половинка, прилетевшая в одну, должна найтись из другой. Срок жизни
короткий - это не хранилище: то, что не собралось за несколько минут,
клиент всё равно догрузит историей при переподключении.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import RedisError

log = logging.getLogger(__name__)

# Сколько половина ждёт свою пару. Пять минут — тот же срок, что
# у кеша восстановления в канале беседы: дольше ждать бессмысленно,
# клиент к этому времени уже дочитает историю через REST.
PAIR_TTL_SECONDS = 300

# Насколько долго помним, что сообщение уже отдано. Дольше, чем живёт
# пара: повтор приходит именно тогда, когда что-то пошло не так,
# то есть с задержкой.
DELIVERED_TTL_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class CacheSettings:
    url: str
    timeout_seconds: float = 1.0


@dataclass(slots=True)
class EventCache:
    settings: CacheSettings
    _client: aioredis.Redis | None = field(default=None)

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

    async def remember_half(
        self, *, message_id: str, half: str, body: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Кладёт половину и возвращает вторую, если та уже здесь.

        Возврат второй половины, а не признак «пара собралась»: иначе
        вызывающему пришлось бы сходить в Redis ещё раз, и между двумя
        обращениями половина успела бы истечь.
        """
        client = self.client()
        other = "content" if half == "fact" else "fact"
        pipe = client.pipeline()
        pipe.set(f"pair:{message_id}:{half}", json.dumps(body, ensure_ascii=False),
                 ex=PAIR_TTL_SECONDS)
        pipe.get(f"pair:{message_id}:{other}")
        _, found = await pipe.execute()
        return json.loads(found) if found else None

    async def mark_delivered(self, *, message_id: str) -> bool:
        """`True` — отдать можно, это первый раз.

        `SET NX` атомарен, поэтому две реплики, получившие один и тот же
        повтор одновременно, не отдадут его дважды: вторая получит
        отказ, а не «ключа ещё нет».
        """
        client = self.client()
        created = await client.set(
            f"delivered:{message_id}", "1", ex=DELIVERED_TTL_SECONDS, nx=True
        )
        return bool(created)

    async def forget(self, *, message_id: str) -> None:
        """Убирает собранные половины: пара больше не нужна."""
        client = self.client()
        await client.delete(f"pair:{message_id}:fact", f"pair:{message_id}:content")

    async def healthy(self) -> bool:
        try:
            await self.client().ping()
        except (RedisError, OSError) as exc:
            log.warning(
                "кеш событий недоступен",
                extra={"event": "event_cache", "result": "failed",
                       "error_code": type(exc).__name__, "dependency": "redis"},
            )
            return False
        return True
