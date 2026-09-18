"""HTTP-контракт списка входов и выхода с одного устройства."""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from messenger.api import main
from messenger.api.main import app
from messenger.domain.identity import TokenRejection
from messenger.domain.ids import DeviceId, SessionId
from messenger.domain.session import SessionView
from messenger.services import realtime as realtime_service
from messenger.services import session_management as session_service

SID = uuid.UUID("71ed2ab8-5d25-493f-a83e-492079035fcc")
OTHER_SID = uuid.UUID("b6cb7698-34c2-466d-ad23-263e0a2e4081")
DEVICE = uuid.UUID("c95058ec-cf19-4c3b-813a-c8fd7c0b2679")


class FakeRuntime:
    keys = None
    oidc_settings = None
    centrifugo = SimpleNamespace(settings=SimpleNamespace(api_key="proxy-secret"))

    @asynccontextmanager
    async def connection(self):
        yield None


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def runtime():
    original = app.state.runtime
    app.state.runtime = FakeRuntime()
    yield
    app.state.runtime = original


def test_список_требует_bearer_и_не_ходит_ниже(client, monkeypatch, отказ):
    async def _не_вызывать(*args, **kwargs):
        raise AssertionError("сервис вызван без удостоверения")

    monkeypatch.setattr(session_service, "list_for_token", _не_вызывать)
    отказ(client.get("/sessions"), status=401, code="unauthenticated")


def test_список_показывает_текущую_сессию(client, monkeypatch):
    moment = datetime(2026, 9, 14, tzinfo=UTC)

    async def _list(*args, **kwargs):
        assert kwargs["token"] == "access-token"
        return session_service.SessionListResult(items=[SessionView(
            session_id=SessionId(SID),
            device_id=DeviceId(DEVICE),
            user_agent="Firefox/143",
            created_at=moment,
            last_seen_at=moment,
            current=True,
        )])

    monkeypatch.setattr(session_service, "list_for_token", _list)
    r = client.get("/sessions", headers={"Authorization": "Bearer access-token"})
    assert r.status_code == 200
    assert r.json()["items"] == [{
        "session_id": str(SID),
        "device_id": str(DEVICE),
        "user_agent": "Firefox/143",
        "created_at": "2026-09-14T00:00:00Z",
        "last_seen_at": "2026-09-14T00:00:00Z",
        "current": True,
    }]


def test_выход_с_текущего_устройства_снимает_cookie(client, monkeypatch):
    async def _revoke(*args, **kwargs):
        assert kwargs["target"] == SessionId(SID)
        return session_service.RevokeResult(revoked=True, current=True)

    monkeypatch.setattr(session_service, "revoke_for_token", _revoke)
    r = client.delete(
        f"/sessions/{SID}", headers={"Authorization": "Bearer access-token"}
    )
    assert r.status_code == 204
    assert "Max-Age=0" in r.headers["set-cookie"]
    assert f"Path={main.REFRESH_COOKIE_PATH}" in r.headers["set-cookie"]


def test_закрытие_другого_устройства_не_снимает_текущую_cookie(client, monkeypatch):
    async def _revoke(*args, **kwargs):
        return session_service.RevokeResult(revoked=True, current=False)

    monkeypatch.setattr(session_service, "revoke_for_token", _revoke)
    r = client.delete(
        f"/sessions/{OTHER_SID}", headers={"Authorization": "Bearer access-token"}
    )
    assert r.status_code == 204
    assert "set-cookie" not in r.headers


def test_выход_с_одного_устройства_требует_bearer(client, monkeypatch, отказ):
    async def _не_вызывать(*args, **kwargs):
        raise AssertionError("сервис вызван без удостоверения")

    monkeypatch.setattr(session_service, "revoke_for_token", _не_вызывать)
    отказ(client.delete(f"/sessions/{SID}"), status=401, code="unauthenticated")


def test_недоступные_ключи_это_503(client, monkeypatch, отказ):
    async def _list(*args, **kwargs):
        return session_service.SessionListResult(rejection=TokenRejection.KEYS_UNAVAILABLE)

    monkeypatch.setattr(session_service, "list_for_token", _list)
    r = client.get("/sessions", headers={"Authorization": "Bearer access-token"})
    отказ(r, status=503, code="upstream_unavailable")


def test_не_uuid_не_доходит_до_сервиса(client, monkeypatch):
    async def _не_вызывать(*args, **kwargs):
        raise AssertionError("сервис вызван с невалидным идентификатором")

    monkeypatch.setattr(session_service, "revoke_for_token", _не_вызывать)
    r = client.delete(
        "/sessions/not-a-uuid", headers={"Authorization": "Bearer access-token"}
    )
    assert r.status_code == 422


def test_выход_везде_требует_bearer(client, monkeypatch, отказ):
    async def _не_вызывать(*args, **kwargs):
        raise AssertionError("сервис вызван без удостоверения")

    monkeypatch.setattr(session_service, "revoke_all_for_token", _не_вызывать)
    отказ(client.delete("/sessions"), status=401, code="unauthenticated")


def test_выход_везде_снимает_cookie_и_отдаёт_204(client, monkeypatch):
    async def _revoke_all(*args, **kwargs):
        assert kwargs["token"] == "access-token"
        return session_service.RevokeAllResult(revoked=2)

    monkeypatch.setattr(session_service, "revoke_all_for_token", _revoke_all)
    r = client.delete("/sessions", headers={"Authorization": "Bearer access-token"})
    assert r.status_code == 204
    assert "Max-Age=0" in r.headers["set-cookie"]
    assert f"Path={main.REFRESH_COOKIE_PATH}" in r.headers["set-cookie"]


def test_выход_везде_при_недоступных_ключах_503(client, monkeypatch, отказ):
    async def _revoke_all(*args, **kwargs):
        return session_service.RevokeAllResult(rejection=TokenRejection.KEYS_UNAVAILABLE)

    monkeypatch.setattr(session_service, "revoke_all_for_token", _revoke_all)
    r = client.delete("/sessions", headers={"Authorization": "Bearer access-token"})
    отказ(r, status=503, code="upstream_unavailable")


def test_realtime_токен_требует_bearer(client, monkeypatch, отказ):
    async def _не_вызывать(*args, **kwargs):
        raise AssertionError("сервис вызван без удостоверения")

    monkeypatch.setattr(realtime_service, "issue_token_for_user", _не_вызывать)
    отказ(client.post("/realtime/token"), status=401, code="unauthenticated")


def test_realtime_токен_возвращает_token_и_срок(client, monkeypatch):
    moment = datetime(2026, 9, 14, tzinfo=UTC)

    async def _issue(*args, **kwargs):
        assert kwargs["token"] == "access-token"
        return realtime_service.RealtimeTokenResult(
            token="connect-token",
            expires_at=moment,
        )

    monkeypatch.setattr(realtime_service, "issue_token_for_user", _issue)
    r = client.post(
        "/realtime/token", headers={"Authorization": "Bearer access-token"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["token"] == "connect-token"
    assert body["expires_at"] == "2026-09-14T00:00:00+00:00"
    # Идентификатора соединения в ответе нет: Centrifugo назначает его сам.
    assert "client_id" not in body


def test_realtime_токен_при_недоступных_ключах_503(client, monkeypatch, отказ):
    async def _issue(*args, **kwargs):
        return realtime_service.RealtimeTokenResult(
            rejection=TokenRejection.KEYS_UNAVAILABLE
        )

    monkeypatch.setattr(realtime_service, "issue_token_for_user", _issue)
    r = client.post(
        "/realtime/token", headers={"Authorization": "Bearer access-token"}
    )
    отказ(r, status=503, code="upstream_unavailable")


def test_привязка_соединения_требует_bearer(client, monkeypatch, отказ):
    async def _не_вызывать(*args, **kwargs):
        raise AssertionError("сервис вызван без удостоверения")

    monkeypatch.setattr(realtime_service, "register_connection", _не_вызывать)
    отказ(
        client.post("/realtime/connections", json={"client_id": "c1"}),
        status=401,
        code="unauthenticated",
    )


def test_привязка_соединения_передаёт_client_id_и_отдаёт_204(client, monkeypatch):
    async def _register(*args, **kwargs):
        assert kwargs["token"] == "access-token"
        assert kwargs["client_id"] == "c1"
        return realtime_service.RegisterConnectionResult(registered=True)

    monkeypatch.setattr(realtime_service, "register_connection", _register)
    r = client.post(
        "/realtime/connections",
        json={"client_id": "c1"},
        headers={"Authorization": "Bearer access-token"},
    )
    assert r.status_code == 204


def test_привязка_соединения_с_пустым_client_id_это_422(client, monkeypatch):
    async def _не_вызывать(*args, **kwargs):
        raise AssertionError("сервис вызван с невалидным телом")

    monkeypatch.setattr(realtime_service, "register_connection", _не_вызывать)
    r = client.post(
        "/realtime/connections",
        json={"client_id": ""},
        headers={"Authorization": "Bearer access-token"},
    )
    assert r.status_code == 422


def test_привязка_соединения_при_недоступных_ключах_503(client, monkeypatch, отказ):
    async def _register(*args, **kwargs):
        return realtime_service.RegisterConnectionResult(
            rejection=TokenRejection.KEYS_UNAVAILABLE
        )

    monkeypatch.setattr(realtime_service, "register_connection", _register)
    r = client.post(
        "/realtime/connections",
        json={"client_id": "c1"},
        headers={"Authorization": "Bearer access-token"},
    )
    отказ(r, status=503, code="upstream_unavailable")


def test_connect_proxy_возвращает_user_каналы_и_meta(client, monkeypatch):
    async def _connect(*args, **kwargs):
        assert kwargs["ticket"] == "signed-ticket"
        assert kwargs["client_id"] == "centrifugo-client"
        return realtime_service.ProxyConnectResult(
            accepted=True,
            user_id=str(SID),
            session_id=str(OTHER_SID),
            channels=(f"user:{SID}",),
            expire_at=1_800_000_000,
        )

    monkeypatch.setattr(realtime_service, "connect_from_ticket", _connect)
    r = client.post(
        "/internal/centrifugo/connect",
        json={"client": "centrifugo-client", "data": {"ticket": "signed-ticket"}},
        headers={"X-Realtime-Proxy-Key": "proxy-secret"},
    )
    assert r.status_code == 200
    assert r.json() == {"result": {
        "user": str(SID),
        "channels": [f"user:{SID}"],
        "meta": {"session_id": str(OTHER_SID)},
        "expire_at": 1_800_000_000,
    }}


def test_connect_proxy_отклоняет_негодный_ticket(client, monkeypatch):
    async def _connect(*args, **kwargs):
        return realtime_service.ProxyConnectResult()

    monkeypatch.setattr(realtime_service, "connect_from_ticket", _connect)
    r = client.post(
        "/internal/centrifugo/connect",
        json={"client": "centrifugo-client", "data": {"ticket": "bad"}},
        headers={"X-Realtime-Proxy-Key": "proxy-secret"},
    )
    assert r.json()["disconnect"]["code"] == 4501


def test_refresh_proxy_закрывает_отозванную_сессию(client, monkeypatch):
    async def _refresh(*args, **kwargs):
        return None

    monkeypatch.setattr(realtime_service, "refresh_connection", _refresh)
    r = client.post(
        "/internal/centrifugo/refresh",
        json={
            "client": "centrifugo-client",
            "user": str(SID),
            "meta": {"session_id": str(OTHER_SID)},
        },
        headers={"X-Realtime-Proxy-Key": "proxy-secret"},
    )
    assert r.json() == {"result": {"expired": True}}
