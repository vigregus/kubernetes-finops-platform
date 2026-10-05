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
