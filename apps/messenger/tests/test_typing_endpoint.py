"""publish-proxy «печатает»: допуск, подмена автора, лимит (RT-002, RT-003, SEC-008)."""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from messenger.adapters.ratelimit import LimitDecision, OnFailure
from messenger.api.main import app
from messenger.services import typing as typing_service

USER = "11111111-1111-1111-1111-111111111111"
CONVERSATION = uuid.UUID("22222222-2222-2222-2222-222222222222")
HEADERS = {"X-Realtime-Proxy-Key": "proxy-secret"}


class Settings:
    api_key = "proxy-secret"


class Centrifugo:
    settings = Settings()


class Limiter:
    def __init__(self, allowed: bool = True):
        self.allowed = allowed
        self.calls: list[dict] = []

    async def take(self, key, *, limit, window_seconds, on_failure):
        self.calls.append(
            {"key": key, "limit": limit, "window": window_seconds, "on_failure": on_failure}
        )
        return LimitDecision(allowed=self.allowed, retry_after_seconds=3)


class Runtime:
    def __init__(self, limiter):
        self.centrifugo = Centrifugo()
        self.limiter = limiter

    @asynccontextmanager
    async def connection(self, mode=None):
        yield None


@pytest.fixture
def limiter():
    return Limiter()


class Membership:
    """Подмена походов в Postgres за членством: считает обращения."""

    def __init__(self):
        self.member = True
        self.lookups = 0


@pytest.fixture
def membership(monkeypatch):
    fake = Membership()

    async def is_active_member(conn, user_id, conversation_id):
        fake.lookups += 1
        return fake.member

    monkeypatch.setattr(typing_service, "is_active_member", is_active_member)
    typing_service.reset_membership_cache()
    yield fake
    typing_service.reset_membership_cache()


@pytest.fixture(autouse=True)
def runtime(limiter, membership):
    original = app.state.runtime
    app.state.runtime = Runtime(limiter)
    yield app.state.runtime
    app.state.runtime = original


@pytest.fixture
def client():
    return TestClient(app)


def publish(client, *, data, channel=f"typing:{CONVERSATION}", headers=HEADERS, user=USER):
    return client.post(
        "/internal/centrifugo/publish",
        json={"client": "c1", "user": user, "channel": channel, "data": data},
        headers=headers,
    )


def test_без_ключа_прокси_публикация_отклоняется(client):
    r = publish(client, data={"state": "typing"}, headers={})
    assert r.json()["error"]["code"] == 403


def test_допустимая_публикация_получает_автора_из_соединения(client):
    r = publish(client, data={"state": "typing"})
    assert r.json() == {
        "result": {
            "data": {"user_id": USER, "expires_in_ms": 5000},
            # «Печатает» не пишется в историю канала вовсе.
            "skip_history": True,
        }
    }


def test_подделка_автора_и_срока_в_теле_игнорируется(client):
    r = publish(client, data={"state": "typing", "user_id": "другой", "expires_in_ms": 99999})
    assert r.json()["result"]["data"] == {"user_id": USER, "expires_in_ms": 5000}


def test_остановка_гасит_индикатор_сразу(client):
    r = publish(client, data={"state": "stop"})
    assert r.json()["result"]["data"] == {"user_id": USER, "expires_in_ms": 0}


@pytest.mark.parametrize("data", [None, {}, {"state": "dancing"}, "typing", [1]])
def test_негодное_тело_отклоняется(client, limiter, data):
    r = publish(client, data=data)
    assert r.json()["error"]["code"] == 400
    # Негодное не стоит счётчика: лимит считает только годные публикации.
    assert limiter.calls == []


@pytest.mark.parametrize(
    "channel", [f"conversation:{CONVERSATION}", f"user:{USER}", "typing:не-uuid", "typing:"]
)
def test_чужое_пространство_каналов_отклоняется(client, channel):
    r = publish(client, data={"state": "typing"}, channel=channel)
    assert r.json()["error"]["code"] == 403


def test_флуд_обрывается_лимитом_до_публикации(client, limiter):
    limiter.allowed = False
    r = publish(client, data={"state": "typing"})
    assert r.json()["error"]["code"] == 429
    assert "result" not in r.json()


def test_лимит_по_пользователю_и_беседе_закрывается_при_отказе_счётчика(client, limiter):
    publish(client, data={"state": "typing"})
    overall, per_conversation = limiter.calls
    # Сначала общий предел на пользователя, затем — на пару пользователь-беседа.
    assert overall["key"] == f"typing:{USER}"
    assert (overall["limit"], overall["window"]) == (60, 5)
    assert per_conversation["key"] == f"typing:{USER}:{CONVERSATION}"
    assert (per_conversation["limit"], per_conversation["window"]) == (6, 5)
    # Потерять набор нормально, а недоступный счётчик не повод открывать флуд.
    assert overall["on_failure"] is OnFailure.DENY
    assert per_conversation["on_failure"] is OnFailure.DENY


def test_выключенный_набор_отвечает_отказом(client, monkeypatch):
    monkeypatch.setenv("TYPING_ENABLED", "false")
    r = publish(client, data={"state": "typing"})
    assert r.json()["error"]["code"] == 503


def test_не_участник_не_публикует_в_канал_набора(client, membership):
    membership.member = False
    r = publish(client, data={"state": "typing"})
    assert r.json()["error"]["code"] == 403


def test_членство_кешируется_на_ограниченное_время(client, membership):
    for _ in range(5):
        publish(client, data={"state": "typing"})
    # Сердцебиение не ходит в Postgres на каждое сообщение.
    assert membership.lookups == 1


def test_исключённый_перестаёт_публиковать_не_позже_ttl(client, membership, monkeypatch):
    import time as _time

    clock = {"now": 1000.0}
    monkeypatch.setattr(_time, "monotonic", lambda: clock["now"])
    # `authorize_publish` берёт часы по умолчанию при определении — подменяем сам кеш.
    assert publish(client, data={"state": "typing"}).json().get("result")
    membership.member = False
    typing_service._MEMBERS.clear()  # noqa: SLF001 - имитация истёкшего TTL
    assert publish(client, data={"state": "typing"}).json()["error"]["code"] == 403


def test_перебор_каналов_упирается_в_общий_предел_до_похода_в_базу(client, limiter, membership):
    limiter.allowed = False
    r = publish(client, data={"state": "typing"})
    assert r.json()["error"]["code"] == 429
    assert membership.lookups == 0
