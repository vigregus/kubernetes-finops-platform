"""HTTP-контракт `POST /telemetry/browser`: коды ответа и форма пачки.

Ни базы, ни Prometheus: сервис (`browser_telemetry_service.ingest`)
подменяется, тем же приёмом, что `test_receipts_endpoints.py` — здесь
проверяется обработчик (аутентификация, форма 422, какие события до него
доезжают), а не то, что он с ними дальше делает (`test_browser_telemetry.py`).
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from messenger.api import main
from messenger.api.main import app
from messenger.domain.user import User
from messenger.services import browser_telemetry as service
from messenger.services import identity

ACTOR_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
NOW = datetime(2026, 9, 19, tzinfo=UTC)
URL = "/telemetry/browser"


class Runtime:
    oidc_settings = None
    keys = None

    def __init__(self) -> None:
        self.opened = 0

    @asynccontextmanager
    async def connection(self, mode=None):
        self.opened += 1
        yield None


def _user() -> User:
    return User(
        user_id=ACTOR_ID,
        external_id="kc-1",
        display_name="Аня",
        email="anya@example.org",
        email_verified=True,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def runtime():
    original = app.state.runtime
    подделка = Runtime()
    app.state.runtime = подделка
    yield подделка
    app.state.runtime = original


def authenticated(monkeypatch):
    async def _current(*args, **kwargs):
        return identity.AuthResult(user=_user())

    monkeypatch.setattr(main, "_current", _current)


def принято(monkeypatch):
    """Подменяет сервис и возвращает список событий, которые до него доехали."""
    получено = []

    async def _ingest(conn, *, events):
        получено.extend(events)

    monkeypatch.setattr(service, "ingest", _ingest)
    return получено


def test_без_токена_не_принимает(client, monkeypatch, отказ):
    async def _current(*args, **kwargs):
        return identity.AuthResult()

    monkeypatch.setattr(main, "_current", _current)

    event = {"type": "ws_connected", "occurred_at": "2026-09-19T00:00:00Z"}
    r = client.post(URL, json={"events": [event]})
    отказ(r, status=401, code="unauthenticated")


def test_пустая_пачка_принимается(client, monkeypatch):
    authenticated(monkeypatch)
    получено = принято(monkeypatch)

    r = client.post(URL, json={"events": []})

    assert r.status_code == 202
    assert получено == []


def test_событие_без_message_id_доезжает_до_сервиса(client, monkeypatch):
    authenticated(monkeypatch)
    получено = принято(monkeypatch)

    r = client.post(
        URL,
        json={"events": [{"type": "ws_reconnected", "occurred_at": "2026-09-19T00:00:00Z"}]},
    )

    assert r.status_code == 202
    assert len(получено) == 1
    assert получено[0].event_type == "ws_reconnected"
    assert получено[0].message_id is None


def test_delivery_ack_без_message_id_отвергается_422(client, monkeypatch):
    authenticated(monkeypatch)
    принято(monkeypatch)

    r = client.post(
        URL,
        json={"events": [{"type": "delivery_ack", "occurred_at": "2026-09-19T00:00:00Z"}]},
    )

    assert r.status_code == 422


def test_неизвестный_тип_события_отвергается_422(client, monkeypatch):
    authenticated(monkeypatch)
    принято(monkeypatch)

    r = client.post(
        URL,
        json={"events": [{"type": "message.выдуманное", "occurred_at": "2026-09-19T00:00:00Z"}]},
    )

    assert r.status_code == 422


def test_больше_пятидесяти_событий_отвергается_422(client, monkeypatch):
    authenticated(monkeypatch)
    принято(monkeypatch)

    events = [{"type": "ws_connected", "occurred_at": "2026-09-19T00:00:00Z"}] * 51
    r = client.post(URL, json={"events": events})

    assert r.status_code == 422


def test_delivery_ack_с_message_id_доезжает_с_полем(client, monkeypatch):
    authenticated(monkeypatch)
    получено = принято(monkeypatch)
    message_id = str(uuid.uuid4())

    r = client.post(
        URL,
        json={
            "events": [
                {
                    "type": "delivery_ack",
                    "occurred_at": "2026-09-19T00:00:00Z",
                    "message_id": message_id,
                }
            ]
        },
    )

    assert r.status_code == 202
    assert str(получено[0].message_id) == message_id
