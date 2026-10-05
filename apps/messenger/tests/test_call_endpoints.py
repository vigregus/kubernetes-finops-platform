"""HTTP-контракт звонков: коды, кто что может, формат ответа (CALL-001…008)."""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from messenger.api import main
from messenger.api.main import app
from messenger.domain.call import Call, CallKind, CallState, EndReason
from messenger.domain.errors import Reason
from messenger.domain.ids import UserId
from messenger.domain.user import User
from messenger.services import calls as service
from messenger.services import identity

ME = UserId(uuid.UUID("11111111-1111-1111-1111-111111111111"))
PEER = UserId(uuid.UUID("22222222-2222-2222-2222-222222222222"))
CONV = uuid.UUID("33333333-3333-3333-3333-333333333333")
CALL_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
NOW = datetime(2026, 10, 5, tzinfo=UTC)


def make_call(*, caller=ME, callee=PEER, state=CallState.RINGING, reason=None) -> Call:
    return Call(
        call_id=CALL_ID, conversation_id=CONV, caller_id=caller, callee_id=callee,
        kind=CallKind.VIDEO, state=state, version=3, signal_seq=0, end_reason=reason,
        created_at=NOW, accepted_at=None, active_at=None, ended_at=None, last_keepalive_at=NOW,
    )


class Runtime:
    keys = None
    oidc_settings = None
    limiter = object()
    object_store = None
    centrifugo = None
    turn = object()

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


def authenticated(monkeypatch):
    async def _current(*args, **kwargs):
        user = User(user_id=ME, external_id="kc", display_name="Я", email="me@e.org",
                    email_verified=True, created_at=NOW, updated_at=NOW)
        return identity.AuthResult(user=user)

    monkeypatch.setattr(main, "_current", _current)


def anonymous(monkeypatch):
    async def _current(*args, **kwargs):
        return identity.AuthResult()

    monkeypatch.setattr(main, "_current", _current)


def result(call=None, rejection=None, retry=None):
    return service.CallResult(call=call, rejection=rejection, retry_after_seconds=retry)


def patch(monkeypatch, name, value):
    async def fake(conn, **kwargs):
        fake.kwargs = kwargs
        return value

    monkeypatch.setattr(service, name, fake)
    return fake


ENDPOINTS = [
    ("post", "/calls", {"conversation_id": str(CONV), "kind": "audio"}),
    ("get", "/calls/current", None),
    ("post", f"/calls/{CALL_ID}/accept", None),
    ("post", f"/calls/{CALL_ID}/decline", None),
    ("post", f"/calls/{CALL_ID}/hangup", None),
    ("post", f"/calls/{CALL_ID}/fail", None),
    ("post", f"/calls/{CALL_ID}/connected", {"connection_type": "direct"}),
    ("post", f"/calls/{CALL_ID}/keepalive", None),
    ("post", f"/calls/{CALL_ID}/signals", {"type": "offer", "sdp": "v=0"}),
    ("get", f"/calls/{CALL_ID}/ice-servers", None),
]


@pytest.mark.parametrize(("method", "path", "body"), ENDPOINTS)
def test_без_входа_ничего_не_доступно(client, monkeypatch, method, path, body):
    anonymous(monkeypatch)
    response = getattr(client, method)(path, **({"json": body} if body is not None else {}))
    assert response.status_code == 401


def test_звонок_создаётся_и_отдаётся_с_моей_ролью(client, monkeypatch):
    authenticated(monkeypatch)
    fake = patch(monkeypatch, "start_call", result(make_call()))
    r = client.post("/calls", json={"conversation_id": str(CONV), "kind": "video"})
    assert r.status_code == 201
    body = r.json()
    assert body["call_id"] == str(CALL_ID)
    assert body["state"] == "ringing"
    assert body["kind"] == "video"
    assert body["role"] == "caller"
    assert body["peer_user_id"] == str(PEER)
    assert body["version"] == 3
    # звонящий — тот, кто вошёл, а не тот, кого назвали в теле
    assert fake.kwargs["caller_id"] == ME


def test_завершённый_при_создании_звонок_отдаёт_причину(client, monkeypatch):
    authenticated(monkeypatch)
    ended = make_call(state=CallState.ENDED, reason=EndReason.BUSY)
    patch(monkeypatch, "start_call", result(ended))
    r = client.post("/calls", json={"conversation_id": str(CONV), "kind": "audio"})
    assert r.status_code == 201
    assert r.json()["state"] == "ended" and r.json()["end_reason"] == "busy"


@pytest.mark.parametrize(
    "body",
    [{}, {"conversation_id": "x", "kind": "audio"},
     {"conversation_id": str(CONV), "kind": "hologram"},
     {"conversation_id": str(CONV)}],
)
def test_негодное_тело_создания_отвергается(client, monkeypatch, body):
    authenticated(monkeypatch)
    patch(monkeypatch, "start_call", result(make_call()))
    assert client.post("/calls", json=body).status_code == 422


@pytest.mark.parametrize(
    ("reason", "status", "code"),
    [
        (Reason.CALLS_UNAVAILABLE, 503, "calls_unavailable"),
        (Reason.ALREADY_IN_CALL, 409, "already_in_call"),
        (Reason.BLOCKED, 403, "forbidden"),
        (Reason.NOT_A_MEMBER, 404, "resource_not_found"),
        (Reason.CONVERSATION_NOT_FOUND, 404, "resource_not_found"),
    ],
)
def test_отказы_создания(client, monkeypatch, reason, status, code):
    authenticated(monkeypatch)
    patch(monkeypatch, "start_call", result(rejection=reason))
    r = client.post("/calls", json={"conversation_id": str(CONV), "kind": "audio"})
    assert r.status_code == status
    assert r.json()["code"] == code


@pytest.mark.parametrize("action", ["accept", "decline", "hangup", "fail", "keepalive"])
def test_действия_над_звонком(client, monkeypatch, action):
    authenticated(monkeypatch)
    fake = patch(monkeypatch, action, result(make_call(callee=ME, caller=PEER)))
    r = client.post(f"/calls/{CALL_ID}/{action}")
    assert r.status_code == 200
    assert r.json()["role"] == "callee"
    assert fake.kwargs["user_id"] == ME
    assert fake.kwargs["call_id"] == CALL_ID


@pytest.mark.parametrize(
    ("reason", "status", "code"),
    [(Reason.CALL_NOT_FOUND, 404, "resource_not_found"), (Reason.CALL_ENDED, 409, "call_ended")],
)
def test_отказы_действий(client, monkeypatch, reason, status, code):
    authenticated(monkeypatch)
    patch(monkeypatch, "accept", result(rejection=reason))
    r = client.post(f"/calls/{CALL_ID}/accept")
    assert (r.status_code, r.json()["code"]) == (status, code)


def test_принятие_передаёт_метку_вкладки(client, monkeypatch):
    authenticated(monkeypatch)
    fake = patch(monkeypatch, "accept", result(make_call(callee=ME, caller=PEER)))
    assert client.post(f"/calls/{CALL_ID}/accept", json={"tab_id": "tab-7"}).status_code == 200
    assert fake.kwargs["tab"] == "tab-7"
    assert client.post(f"/calls/{CALL_ID}/accept").status_code == 200
    assert fake.kwargs["tab"] is None


def test_проигравшая_вкладка_получает_409_call_taken(client, monkeypatch):
    authenticated(monkeypatch)
    patch(monkeypatch, "accept", result(rejection=Reason.CALL_TAKEN))
    r = client.post(f"/calls/{CALL_ID}/accept", json={"tab_id": "tab-2"})
    assert (r.status_code, r.json()["code"]) == (409, "call_taken")


def test_негодная_метка_вкладки_отвергается(client, monkeypatch):
    authenticated(monkeypatch)
    patch(monkeypatch, "accept", result(make_call()))
    assert client.post(f"/calls/{CALL_ID}/accept", json={"tab_id": ""}).status_code == 422
    assert client.post(f"/calls/{CALL_ID}/accept", json={"tab_id": "x" * 65}).status_code == 422


def test_медиа_пошло_несёт_путь_соединения(client, monkeypatch):
    authenticated(monkeypatch)
    fake = patch(monkeypatch, "report_connected", result(make_call(state=CallState.ACTIVE)))
    r = client.post(f"/calls/{CALL_ID}/connected", json={"connection_type": "relay"})
    assert r.status_code == 200
    assert fake.kwargs["connection_type"] == "relay"


def test_неизвестный_путь_соединения_отвергается(client, monkeypatch):
    authenticated(monkeypatch)
    patch(monkeypatch, "report_connected", result(make_call()))
    r = client.post(f"/calls/{CALL_ID}/connected", json={"connection_type": "carrier-pigeon"})
    assert r.status_code == 422


def test_сигнал_принимается_без_тела_ответа(client, monkeypatch):
    authenticated(monkeypatch)
    fake = patch(monkeypatch, "send_signal", result(make_call()))
    r = client.post(f"/calls/{CALL_ID}/signals", json={"type": "offer", "sdp": "v=0"})
    assert r.status_code == 204
    assert fake.kwargs["data"] == {"type": "offer", "sdp": "v=0"}
    assert fake.kwargs["user_id"] == ME


def test_слишком_частые_сигналы_получают_retry_after(client, monkeypatch):
    authenticated(monkeypatch)
    patch(monkeypatch, "send_signal", result(rejection=Reason.RATE_LIMITED, retry=7))
    r = client.post(f"/calls/{CALL_ID}/signals", json={"type": "offer", "sdp": "v=0"})
    assert r.status_code == 429
    assert r.headers["retry-after"] == "7"


def test_негодный_сигнал_отвечает_400(client, monkeypatch):
    authenticated(monkeypatch)
    patch(monkeypatch, "send_signal", result(rejection=Reason.INVALID_SIGNAL))
    r = client.post(f"/calls/{CALL_ID}/signals", json={"type": "bye"})
    assert (r.status_code, r.json()["code"]) == (400, "invalid_signal")


def test_сигнал_не_объект_отвергается_до_сервиса(client, monkeypatch):
    authenticated(monkeypatch)
    patch(monkeypatch, "send_signal", result(make_call()))
    assert client.post(f"/calls/{CALL_ID}/signals", json=[1, 2]).status_code == 422


def test_текущий_звонок_есть_и_его_нет(client, monkeypatch):
    authenticated(monkeypatch)
    patch(monkeypatch, "current", make_call(callee=ME, caller=PEER))
    r = client.get("/calls/current")
    assert r.status_code == 200 and r.json()["call"]["role"] == "callee"

    patch(monkeypatch, "current", None)
    r = client.get("/calls/current")
    assert r.status_code == 200 and r.json() == {"call": None}


def test_ice_серверы_отдаются_с_сроком(client, monkeypatch):
    authenticated(monkeypatch)
    patch(monkeypatch, "ice_servers", service.IceResult(servers=[{"urls": ["stun:x"]}]))
    r = client.get(f"/calls/{CALL_ID}/ice-servers")
    assert r.status_code == 200
    assert r.json() == {"ice_servers": [{"urls": ["stun:x"]}], "ttl_seconds": 600}
    assert r.headers["cache-control"] == "no-store"


def test_ice_серверы_постороннему_404(client, monkeypatch):
    authenticated(monkeypatch)
    patch(monkeypatch, "ice_servers", service.IceResult(rejection=Reason.CALL_NOT_FOUND))
    assert client.get(f"/calls/{CALL_ID}/ice-servers").status_code == 404


def test_me_называет_звонки_возможностью_только_когда_они_включены(client, monkeypatch):
    authenticated(monkeypatch)
    monkeypatch.setenv("CALLS_ENABLED", "true")
    assert "calls" in client.get("/me").json()["capabilities"]
    monkeypatch.setenv("CALLS_ENABLED", "false")
    assert "calls" not in client.get("/me").json()["capabilities"]
