"""Лимит частоты: атомарное окно и явная политика при отказе Redis."""
from __future__ import annotations

import asyncio

from redis.exceptions import RedisError

from messenger.adapters.ratelimit import LimitSettings, OnFailure, RateLimiter


class FakeRedis:
    def __init__(self, answer=None, error: Exception | None = None):
        self.answer = answer
        self.error = error
        self.calls = []

    async def eval(self, script, keys, key, window):
        self.calls.append((script, keys, key, window))
        if self.error:
            raise self.error
        return self.answer


def take(client: FakeRedis, *, on_failure: OnFailure = OnFailure.DENY):
    limiter = RateLimiter(LimitSettings("redis://unused"), _client=client)
    return asyncio.run(
        limiter.take("verify:user", limit=3, window_seconds=3600, on_failure=on_failure)
    )


def test_первые_три_попытки_разрешены_атомарным_скриптом():
    client = FakeRedis([3, 3400])
    decision = take(client)
    assert decision.allowed
    script, keys, key, window = client.calls[0]
    assert "INCR" in script and "EXPIRE" in script
    assert (keys, key, window) == (1, "verify:user", 3600)


def test_четвёртая_попытка_возвращает_оставшийся_срок():
    decision = take(FakeRedis([4, 127]))
    assert not decision.allowed
    assert decision.retry_after_seconds == 127


def test_отказ_redis_для_почты_закрывает_операцию():
    decision = take(FakeRedis(error=RedisError("нет связи")), on_failure=OnFailure.DENY)
    assert not decision.allowed
    assert decision.degraded


def test_политика_пропуска_при_том_же_отказе_задаётся_явно():
    decision = take(FakeRedis(error=RedisError("нет связи")), on_failure=OnFailure.ALLOW)
    assert decision.allowed
    assert decision.degraded
