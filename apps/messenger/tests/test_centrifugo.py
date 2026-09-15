"""Centrifugo: connect-токен и серверный HTTP API, best-effort."""
from __future__ import annotations

import asyncio

import httpx
import jwt

from messenger.adapters.centrifugo import (
    DISCONNECT_CODE_SESSION_REVOKED,
    REASON_SESSION_REVOKED,
    CentrifugoClient,
    CentrifugoSettings,
)


def _settings() -> CentrifugoSettings:
    return CentrifugoSettings(
        api_url="http://centrifugo:9000/api",
        api_key="api-secret",
        token_hmac_secret_key="hmac-secret",
    )


class FakeResponse:
    def __init__(self, body=None):
        self.body = body or {"result": {}}

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class FakeHTTP:
    """Подменяет httpx.AsyncClient: записывает вызовы, отвечает как настроено."""

    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls: list[tuple[str, dict, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, *, json, headers):
        self.calls.append((url, json, headers))
        if self.error:
            raise self.error
        return FakeResponse()


def test_connect_токен_подписан_hs256_и_несёт_sub_каналы_и_срок():
    client = CentrifugoClient(_settings())
    token, expires_at = client.issue_token(
        "user-1", "session-1", channels=["user:user-1"]
    )

    decoded = jwt.decode(
        token, "hmac-secret", algorithms=["HS256"],
        audience="centrifugo-connect-proxy", issuer="messenger-api",
    )
    assert decoded["sub"] == "user-1"
    assert decoded["sid"] == "session-1"
    assert decoded["channels"] == ["user:user-1"]
    # exp в токене — тот же момент, что возвращён вызывающему как срок.
    assert decoded["exp"] == int(expires_at.timestamp())


def test_connect_токен_уважает_собственный_ttl():
    client = CentrifugoClient(_settings())
    token, expires_at = client.issue_token(
        "u", "s", channels=[], ttl_seconds=60
    )

    decoded = jwt.decode(
        token, "hmac-secret", algorithms=["HS256"],
        audience="centrifugo-connect-proxy", issuer="messenger-api",
    )
    assert decoded["exp"] == int(expires_at.timestamp())


def test_publish_строит_верный_url_тело_и_заголовок(monkeypatch):
    fake = FakeHTTP()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake)

    ok = asyncio.run(CentrifugoClient(_settings()).publish(
        "user:1", {"type": "session.revoked", "session_id": "s1"}
    ))

    assert ok
    url, body, headers = fake.calls[0]
    assert url == "http://centrifugo:9000/api/publish"
    assert body == {
        "channel": "user:1",
        "data": {"type": "session.revoked", "session_id": "s1"},
    }
    assert headers == {"X-API-Key": "api-secret"}


def test_disconnect_user_шлёт_user_и_код_разрыва(monkeypatch):
    fake = FakeHTTP()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake)

    ok = asyncio.run(CentrifugoClient(_settings()).disconnect_user(
        "user-1", code=DISCONNECT_CODE_SESSION_REVOKED, reason=REASON_SESSION_REVOKED
    ))

    assert ok
    url, body, _ = fake.calls[0]
    assert url == "http://centrifugo:9000/api/disconnect"
    assert body == {
        "user": "user-1",
        "disconnect": {"code": DISCONNECT_CODE_SESSION_REVOKED,
                       "reason": REASON_SESSION_REVOKED},
    }


def test_disconnect_client_шлёт_user_client_и_код_разрыва(monkeypatch):
    fake = FakeHTTP()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake)

    ok = asyncio.run(CentrifugoClient(_settings()).disconnect_client(
        "user-1", "s1", code=DISCONNECT_CODE_SESSION_REVOKED, reason=REASON_SESSION_REVOKED
    ))

    assert ok
    url, body, _ = fake.calls[0]
    assert url == "http://centrifugo:9000/api/disconnect"
    assert body == {
        "user": "user-1",
        "client": "s1",
        "disconnect": {"code": DISCONNECT_CODE_SESSION_REVOKED,
                       "reason": REASON_SESSION_REVOKED},
    }


def test_недоступность_centrifugo_возвращает_false_а_не_падает(monkeypatch):
    fake = FakeHTTP(error=httpx.ConnectError("нет связи"))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake)

    ok = asyncio.run(CentrifugoClient(_settings()).disconnect_user(
        "user-1", code=DISCONNECT_CODE_SESSION_REVOKED, reason=REASON_SESSION_REVOKED
    ))
    assert not ok


def test_логическая_ошибка_при_http_200_не_считается_успехом(monkeypatch):
    class ErrorHTTP(FakeHTTP):
        async def post(self, url, *, json, headers):
            self.calls.append((url, json, headers))
            return FakeResponse({"error": {"code": 102, "message": "unknown channel"}})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: ErrorHTTP())
    ok = asyncio.run(CentrifugoClient(_settings()).publish("missing:x", {}))
    assert not ok
