"""Выдача доступа к TURN (CALL-004)."""
from __future__ import annotations

import asyncio

import httpx

from messenger.adapters import turn

URLS = ['stun:stun.cloudflare.com:3478', 'turns:turn.cloudflare.com:443?transport=tcp']


def run(coro):
    return asyncio.run(coro)


def cloudflare(handler) -> turn.CloudflareProvider:
    return turn.CloudflareProvider(
        key_id="key123", api_token="secret-token", transport=httpx.MockTransport(handler)
    )


def test_cloudflare_выдаёт_краткоживущие_данные():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.read()
        return httpx.Response(
            200,
            json={
                "iceServers": {
                    "urls": URLS,
                    "username": "u",
                    "credential": "c",
                }
            },
        )

    servers = run(cloudflare(handler).ice_servers(ttl_seconds=600))

    assert servers is not None and len(servers) == 1
    assert servers[0].as_dict() == {
        "urls": URLS,
        "username": "u",
        "credential": "c",
    }
    assert "/turn/keys/key123/" in seen["url"]
    assert seen["auth"] == "Bearer secret-token"
    assert b'"ttl":600' in seen["body"].replace(b" ", b"")


def test_недоступный_cloudflare_даёт_none_а_не_падение():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    assert run(cloudflare(handler).ice_servers(ttl_seconds=600)) is None


def test_мусорный_ответ_cloudflare_даёт_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"iceServers": "oops"})

    assert run(cloudflare(handler).ice_servers(ttl_seconds=600)) is None


def test_ключ_и_токен_не_попадают_в_журнал(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom secret-token key123")

    with caplog.at_level("WARNING"):
        run(cloudflare(handler).ice_servers(ttl_seconds=600))
    assert "secret-token" not in caplog.text
    assert "key123" not in caplog.text


def test_без_настроек_выдаётся_пустой_список(monkeypatch):
    monkeypatch.delenv("TURN_PROVIDER", raising=False)
    monkeypatch.delenv("TURN_STUN_URLS", raising=False)
    provider = turn.provider_from_env()
    assert run(provider.ice_servers(ttl_seconds=600)) == []


def test_stun_из_окружения(monkeypatch):
    monkeypatch.delenv("TURN_PROVIDER", raising=False)
    monkeypatch.setenv("TURN_STUN_URLS", "stun:a:3478, stun:b:3478")
    servers = run(turn.provider_from_env().ice_servers(ttl_seconds=600))
    assert servers is not None
    assert servers[0].urls == ("stun:a:3478", "stun:b:3478")


def test_cloudflare_без_ключей_откатывается_на_stun(monkeypatch):
    monkeypatch.setenv("TURN_PROVIDER", "cloudflare")
    monkeypatch.delenv("TURN_CLOUDFLARE_KEY_ID", raising=False)
    monkeypatch.delenv("TURN_CLOUDFLARE_API_TOKEN", raising=False)
    assert isinstance(turn.provider_from_env(), turn.StaticProvider)


# --- свой coturn ----------------------------------------------------------------------


def coturn(now=1_800_000_000.0) -> turn.CoturnProvider:
    return turn.CoturnProvider(
        secret="shared-secret", urls=("turn:t.example:3478?transport=udp",), now=lambda: now
    )


def test_coturn_выдаёт_данные_по_схеме_use_auth_secret():
    import base64
    import hashlib
    import hmac

    servers = run(coturn().ice_servers(ttl_seconds=600))
    assert servers is not None and len(servers) == 1
    server = servers[0]
    assert server.urls == ("turn:t.example:3478?transport=udp",)
    # Имя — «срок:метка»; срок — ровно сейчас + 10 минут.
    expires, _, label = (server.username or "").partition(":")
    assert expires == str(1_800_000_000 + 600) and label
    # Пароль — HMAC-SHA1 от имени общим секретом: ровно то, что проверит сам coturn.
    expected = base64.b64encode(
        hmac.new(b"shared-secret", server.username.encode(), hashlib.sha1).digest()
    ).decode()
    assert server.credential == expected


def test_coturn_метка_случайная_и_не_несёт_личности():
    first = run(coturn().ice_servers(ttl_seconds=600))[0].username
    second = run(coturn().ice_servers(ttl_seconds=600))[0].username
    assert first != second


def test_coturn_не_ходит_в_сеть_и_не_падает():
    assert run(coturn().ice_servers(ttl_seconds=60)) is not None


def test_coturn_из_окружения(monkeypatch):
    monkeypatch.setenv("TURN_PROVIDER", "coturn")
    monkeypatch.setenv("TURN_COTURN_SECRET", "s")
    monkeypatch.setenv("TURN_COTURN_URLS", "turn:a:3478, turns:b:443?transport=tcp")
    provider = turn.provider_from_env()
    assert isinstance(provider, turn.CoturnProvider)
    assert provider.urls == ("turn:a:3478", "turns:b:443?transport=tcp")


def test_coturn_без_секрета_или_адресов_откатывается_на_stun(monkeypatch):
    monkeypatch.setenv("TURN_PROVIDER", "coturn")
    monkeypatch.delenv("TURN_COTURN_SECRET", raising=False)
    monkeypatch.setenv("TURN_COTURN_URLS", "turn:a:3478")
    assert isinstance(turn.provider_from_env(), turn.StaticProvider)
    monkeypatch.setenv("TURN_COTURN_SECRET", "s")
    monkeypatch.delenv("TURN_COTURN_URLS")
    assert isinstance(turn.provider_from_env(), turn.StaticProvider)
