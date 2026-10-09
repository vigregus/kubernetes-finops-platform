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


def test_coturn_данные_стабильны_для_субъекта_и_срока():
    """Новый пользователь на каждый запрос выедал бы квоту coturn (`--user-quota`)."""
    provider = coturn()

    def issue(subject, expires_at=1_800_010_000):
        return run(provider.ice_servers(ttl_seconds=60, expires_at=expires_at, subject=subject))[0]

    first, again = issue("call-1:user-a"), issue("call-1:user-a")
    assert (first.username, first.credential) == (again.username, again.credential)
    assert first.username.startswith("1800010000:")
    other = issue("call-1:user-b")
    assert other.username != first.username
    assert other.username.split(":")[0] == "1800010000"


def test_метка_не_раскрывает_субъекта():
    server = run(coturn().ice_servers(ttl_seconds=60, expires_at=1, subject="call-1:user-a"))[0]
    assert "user-a" not in server.username and "call-1" not in server.username


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


# --- RES-006: несколько TURN ----------------------------------------------------------------


class Fake:
    def __init__(self, result=None, error=None, delay=0.0):
        self.result, self.error, self.delay = result, error, delay

    async def ice_servers(self, *, ttl_seconds, expires_at=None, subject=None):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.result


def server(url):
    return turn.IceServer(urls=(url,), username="u", credential="c")


def composite(*pairs, timeout=1.0, seen=None):
    return turn.CompositeTurnProvider(
        providers=pairs,
        timeout_seconds=timeout,
        observe=(lambda name, result: seen.append((name, result)))
        if seen is not None
        else (lambda *_: None),
    )


def test_composite_объединяет_серверы_обоих_провайдеров():
    seen: list = []
    provider = composite(
        ("a", Fake([server("turns:a:443?transport=tcp")])),
        ("b", Fake([server("turns:b:443?transport=tcp")])),
        seen=seen,
    )
    servers = run(provider.ice_servers(ttl_seconds=60))
    assert [s.urls[0] for s in servers] == [
        "turns:a:443?transport=tcp", "turns:b:443?transport=tcp"]
    assert seen == [("a", "ok"), ("b", "ok")]


def test_отказ_первого_не_лишает_звонок_второго():
    seen: list = []
    provider = composite(
        ("a", Fake(error=RuntimeError("down"))),
        ("b", Fake([server("turns:b:443?transport=tcp")])),
        seen=seen,
    )
    servers = run(provider.ice_servers(ttl_seconds=60))
    assert [s.urls[0] for s in servers] == ["turns:b:443?transport=tcp"]
    assert ("a", "unavailable") in seen and ("b", "ok") in seen


def test_медленный_провайдер_не_держит_выдачу_дольше_срока():
    seen: list = []
    provider = composite(
        ("slow", Fake([server("turns:slow:443")], delay=5.0)),
        ("b", Fake([server("turns:b:443")])),
        timeout=0.05,
        seen=seen,
    )
    servers = run(provider.ice_servers(ttl_seconds=60))
    assert [s.urls[0] for s in servers] == ["turns:b:443"]
    assert ("slow", "timeout") in seen


def test_все_недоступны_значит_нет_релея_а_не_ошибка():
    provider = composite(("a", Fake(None)), ("b", Fake(error=RuntimeError("x"))))
    assert run(provider.ice_servers(ttl_seconds=60)) is None


def test_два_coturn_из_окружения_собираются_в_composite(monkeypatch):
    monkeypatch.setenv("TURN_PROVIDER", "coturn,coturn-b")
    monkeypatch.setenv("TURN_COTURN_SECRET", "s1")
    monkeypatch.setenv("TURN_COTURN_URLS", "turns:a.example:443?transport=tcp")
    monkeypatch.setenv("TURN_COTURN_B_SECRET", "s2")
    monkeypatch.setenv("TURN_COTURN_B_URLS", "turns:b.example:443?transport=tcp")
    provider = turn.provider_from_env()
    assert isinstance(provider, turn.CompositeTurnProvider)
    assert [name for name, _ in provider.providers] == ["coturn", "coturn-b"]
    servers = run(provider.ice_servers(ttl_seconds=60))
    assert {s.urls[0] for s in servers} == {
        "turns:a.example:443?transport=tcp", "turns:b.example:443?transport=tcp"}
    # у каждого свой секрет: данные одного не подходят другому
    assert servers[0].credential != servers[1].credential


def test_ненастроенный_второй_пропускается_первый_работает(monkeypatch):
    monkeypatch.setenv("TURN_PROVIDER", "coturn,coturn-b")
    monkeypatch.setenv("TURN_COTURN_SECRET", "s1")
    monkeypatch.setenv("TURN_COTURN_URLS", "turns:a.example:443?transport=tcp")
    monkeypatch.delenv("TURN_COTURN_B_SECRET", raising=False)
    monkeypatch.delenv("TURN_COTURN_B_URLS", raising=False)
    provider = turn.provider_from_env()
    assert isinstance(provider, turn.CompositeTurnProvider)
    assert [name for name, _ in provider.providers] == ["coturn"]


def test_restricted_only_оставляет_только_turns():
    servers = [
        turn.IceServer(urls=("stun:s",)),
        turn.IceServer(
            urls=("turn:t:3478", "turns:t:443?transport=tcp"), username="u", credential="c"),
    ]
    out = turn.restricted_only(servers)
    assert [s.urls for s in out] == [("turns:t:443?transport=tcp",)]
    assert out[0].username == "u" and out[0].credential == "c"
