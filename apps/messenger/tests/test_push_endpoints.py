"""HTTP-контракт подписок Web Push: коды, кто и чью подписку меняет."""
from __future__ import annotations

import base64
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from messenger.api import main
from messenger.api.main import app
from messenger.domain.ids import DeviceId, UserId
from messenger.domain.session import Device
from messenger.domain.user import User
from messenger.services import identity
from messenger.services import push as push_service

ACTOR = UserId(uuid.UUID("11111111-1111-1111-1111-111111111111"))
DEVICE = DeviceId(uuid.UUID("33333333-3333-3333-3333-333333333333"))
NOW = datetime(2026, 10, 4, tzinfo=UTC)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


BODY = {
    "endpoint": "https://fcm.googleapis.com/fcm/send/abc",
    "keys": {"p256dh": b64(b"\x04" + b"\x01" * 64), "auth": b64(b"\x02" * 16)},
}


class Runtime:
    keys = None
    oidc_settings = None
    limiter = object()
    object_store = None

    @asynccontextmanager
    async def connection(self, mode=None):
        yield None


@pytest.fixture(autouse=True)
def runtime():
    original = app.state.runtime
    app.state.runtime = Runtime()
    yield
    app.state.runtime = original


@pytest.fixture
def client():
    return TestClient(app)


def authenticated(monkeypatch, *, device=True):
    async def _current(*args, **kwargs):
        user = User(user_id=ACTOR, external_id="kc", display_name="А", email="a@e.org",
                    email_verified=True, created_at=NOW, updated_at=NOW)
        dev = Device(device_id=DEVICE, user_id=ACTOR, user_agent=None,
                     created_at=NOW, last_seen_at=NOW) if device else None
        return identity.AuthResult(user=user, device=dev)

    monkeypatch.setattr(main, "_current", _current)


def anonymous(monkeypatch):
    async def _current(*args, **kwargs):
        return identity.AuthResult()

    monkeypatch.setattr(main, "_current", _current)


def test_без_входа_ничего_из_подписок_не_доступно(client, monkeypatch):
    anonymous(monkeypatch)
    assert client.get("/push/vapid-public-key").status_code == 401
    assert client.put("/me/push-subscription", json=BODY).status_code == 401
    assert client.delete("/me/push-subscription").status_code == 401


def test_открытый_ключ_отдаётся_а_без_настройки_уведомления_недоступны(client, monkeypatch):
    authenticated(monkeypatch)
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "BPublicKey")
    monkeypatch.setenv("PUSH_ENABLED", "true")
    r = client.get("/push/vapid-public-key")
    assert r.status_code == 200 and r.json() == {"public_key": "BPublicKey"}

    monkeypatch.delenv("VAPID_PUBLIC_KEY")
    assert client.get("/push/vapid-public-key").status_code == 503


def test_подписка_принимается_и_идёт_на_устройство_входа(client, monkeypatch):
    authenticated(monkeypatch)
    seen: dict = {}

    async def subscribe(conn, *, device_id, user_id, data):
        seen.update(device_id=device_id, user_id=user_id, data=data)
        return push_service.SubscribeResult(ok=True)

    monkeypatch.setattr(push_service, "subscribe", subscribe)
    r = client.put("/me/push-subscription", json=BODY)
    assert r.status_code == 204
    # Своё устройство берётся из входа, а не из тела: чужое не назвать.
    assert seen["device_id"] == DEVICE and seen["user_id"] == ACTOR
    assert seen["data"] == BODY


@pytest.mark.parametrize(
    ("reason", "status", "code"),
    [("invalid", 400, "invalid_subscription"), ("unavailable", 503, "push_unavailable")],
)
def test_отказы_подписки(client, monkeypatch, reason, status, code):
    authenticated(monkeypatch)

    async def subscribe(conn, **kwargs):
        return push_service.SubscribeResult(reason=reason)

    monkeypatch.setattr(push_service, "subscribe", subscribe)
    r = client.put("/me/push-subscription", json=BODY)
    assert r.status_code == status
    assert r.json()["code"] == code


def test_вход_без_устройства_подписку_не_оформляет(client, monkeypatch):
    authenticated(monkeypatch, device=False)
    assert client.put("/me/push-subscription", json=BODY).status_code == 401


def test_отписка_идемпотентна(client, monkeypatch):
    authenticated(monkeypatch)
    calls: list = []

    async def unsubscribe(conn, *, device_id, user_id):
        calls.append((device_id, user_id))

    monkeypatch.setattr(push_service, "unsubscribe", unsubscribe)
    assert client.delete("/me/push-subscription").status_code == 204
    assert client.delete("/me/push-subscription").status_code == 204
    assert calls == [(DEVICE, ACTOR), (DEVICE, ACTOR)]
