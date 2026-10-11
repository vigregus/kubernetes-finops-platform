"""Диагностика сети по событию (RES-012): сессия, данные TURN, приём результата."""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from messenger.adapters.ratelimit import LimitDecision
from messenger.adapters.turn import CoturnProvider
from messenger.api import main
from messenger.api.main import app
from messenger.domain.ids import UserId
from messenger.domain.user import User
from messenger.services import diagnostics as service
from messenger.services import identity
from messenger.telemetry.network import NetworkContext

ME = UserId(uuid.UUID("11111111-1111-1111-1111-111111111111"))
OTHER = UserId(uuid.UUID("22222222-2222-2222-2222-222222222222"))
NOW = 1_800_000_000.0
SECRET = b"diag-secret"


def run(coro):
    return asyncio.run(coro)


class Limiter:
    """Счётчик по ключам: ровно та семантика, что у Redis-лимитера."""

    def __init__(self, deny_prefix: str | None = None):
        self.used: dict[str, int] = {}
        self.deny_prefix = deny_prefix

    async def take(self, key, *, limit, window_seconds, on_failure):
        self.used[key] = self.used.get(key, 0) + 1
        if self.deny_prefix and key.startswith(self.deny_prefix):
            return LimitDecision(allowed=False, retry_after_seconds=120)
        return LimitDecision(allowed=self.used[key] <= limit, retry_after_seconds=60)


def coturn():
    return CoturnProvider(
        secret="turn-secret",
        urls=("stun:s:3478", "turn:t:3478?transport=udp", "turns:t:443?transport=tcp"),
        now=lambda: NOW,
    )


def report(**overrides):
    base = dict(phase="failure", trigger="ice_slow", setup_result="failed")
    base.update(overrides)
    return service.Report(**base)


@pytest.fixture
def seen(monkeypatch):
    records = {"probes": [], "diagnostics": [], "rescue": [], "ice": []}
    monkeypatch.setattr(service.metrics, "network_probe", lambda *a: records["probes"].append(a))
    monkeypatch.setattr(
        service.metrics, "network_diagnostic", lambda *a: records["diagnostics"].append(a)
    )
    monkeypatch.setattr(service.metrics, "network_rescue", lambda *a: records["rescue"].append(a))
    monkeypatch.setattr(service.metrics, "network_ice", lambda *a: records["ice"].append(a))
    return records


# --- сессия ---------------------------------------------------------------------------


def test_сессия_выдаёт_идентификатор_пробы_и_короткие_данные_turn():
    result = run(service.start_session(
        turn=coturn(), limiter=Limiter(), secret=SECRET, user_id=ME, now=NOW))
    assert result.rejection is None
    assert result.ttl_seconds == 45
    assert service.verify_id(SECRET, ME, result.diagnostic_id, NOW)
    turn_server = next(s for s in result.servers if any(u.startswith("turn:") for u in s["urls"]))
    # срок данных — ровно 45 секунд: проверка, а не канал
    assert turn_server["username"].split(":")[0] == str(int(NOW) + 45)


def test_данные_диагностики_и_звонка_у_человека_разные():
    """Диагностическая метка не совпадает с меткой звонка: квота и журнал раздельны."""
    diag = run(service.start_session(
        turn=coturn(), limiter=Limiter(), secret=SECRET, user_id=ME, now=NOW))
    call = run(coturn().ice_servers(ttl_seconds=45, expires_at=int(NOW) + 45, subject=str(ME)))
    diag_name = next(s["username"] for s in diag.servers if s.get("username"))
    assert diag_name != call[0].username
    assert str(ME) not in diag_name


def test_сессий_не_больше_лимита_в_час():
    result = run(service.start_session(
        turn=coturn(), limiter=Limiter(deny_prefix="diag-session:"), secret=SECRET,
        user_id=ME, now=NOW))
    assert result.rejection.value == "rate_limited" and result.retry_after_seconds == 120
    assert result.servers == []


def test_недоступный_turn_даёт_сессию_без_серверов():
    class Down:
        async def ice_servers(self, **kwargs):
            return None

    result = run(service.start_session(
        turn=Down(), limiter=Limiter(), secret=SECRET, user_id=ME, now=NOW))
    assert result.rejection is None and result.servers == []


# --- идентификатор --------------------------------------------------------------------


def test_идентификатор_чужой_просроченный_и_подделанный_неразличимы():
    token = service.issue_id(SECRET, ME, int(NOW) + 600)
    assert service.verify_id(SECRET, ME, token, NOW)
    assert not service.verify_id(SECRET, OTHER, token, NOW)  # чужой
    assert not service.verify_id(SECRET, ME, token, NOW + 601)  # просрочен
    assert not service.verify_id(b"another-secret", ME, token, NOW)  # другой секрет
    nonce, expires, sig = token.split(".")
    assert not service.verify_id(SECRET, ME, f"{nonce}.{int(expires) + 1000}.{sig}", NOW)
    for junk in ("", "a.b", "a.b.c", "x" * 300, "..", "a.99999999999.c"):
        assert not service.verify_id(SECRET, ME, junk, NOW)


# --- приём результата -----------------------------------------------------------------


def accept(limiter, token, rep, now=NOW, network=None, user=ME):
    return run(service.accept_report(
        limiter=limiter, secret=SECRET, user_id=user, diagnostic_id=token, report=rep,
        network=network, now=now))


def test_результат_пишет_пробы_и_исход_в_метрики(seen):
    token = service.issue_id(SECRET, ME, int(NOW) + 600)
    rep = report(
        probes=(service.Probe("turn_udp", "timeout"), service.Probe("turn_tls", "ok")),
        ice_gathering_ms=800, ice_connection_ms=12000, failure_reason="ice_connect_timeout")
    net = NetworkContext(country="RU", net_class="mobile")
    assert accept(Limiter(), token, rep, network=net).rejection is None
    assert seen["probes"] == [
        ("failure", "turn_udp", "timeout", "RU", "mobile"),
        ("failure", "turn_tls", "ok", "RU", "mobile"),
    ]
    assert seen["diagnostics"] == [("failure", "ice_slow", "failed", "ice_connect_timeout")]
    assert seen["ice"] == [("gathering", 0.8), ("connection", 12.0)]
    assert seen["rescue"] == []


def test_что_спасло_звонок_после_отказа_других_путей(seen):
    token = service.issue_id(SECRET, ME, int(NOW) + 600)
    rep = report(phase="recovery", trigger="fallback_enabled", setup_result="connected",
                 selected_candidate="relay", selected_relay_protocol="tls")
    accept(Limiter(), token, rep, network=NetworkContext(country="RU", net_class="mobile"))
    assert seen["rescue"] == [("tls", "RU", "mobile")]


def test_прямой_путь_после_восстановления_это_direct(seen):
    token = service.issue_id(SECRET, ME, int(NOW) + 600)
    accept(Limiter(), token, report(
        phase="recovery", setup_result="connected", selected_candidate="srflx"))
    assert seen["rescue"] == [("direct", "unknown", "unknown")]


def test_неудачное_восстановление_ничего_не_спасло(seen):
    token = service.issue_id(SECRET, ME, int(NOW) + 600)
    accept(Limiter(), token, report(phase="recovery", setup_result="failed"))
    assert seen["rescue"] == []


def test_фаза_принимается_один_раз(seen):
    token = service.issue_id(SECRET, ME, int(NOW) + 600)
    limiter = Limiter()
    assert accept(limiter, token, report()).rejection is None
    again = accept(limiter, token, report())
    assert again.rejection.value == "diagnostic_reported"
    assert len(seen["diagnostics"]) == 1
    # а «после» — отдельная фаза того же идентификатора
    assert accept(limiter, token, report(phase="recovery")).rejection is None


def test_чужой_идентификатор_отвергается_и_метрик_нет(seen):
    token = service.issue_id(SECRET, OTHER, int(NOW) + 600)
    assert accept(Limiter(), token, report()).rejection.value == "diagnostic_not_found"
    assert seen["probes"] == [] and seen["diagnostics"] == []


def test_повтор_пробы_в_одном_отчёте_считается_раз(seen):
    token = service.issue_id(SECRET, ME, int(NOW) + 600)
    accept(Limiter(), token, report(
        probes=(service.Probe("stun", "ok"), service.Probe("stun", "failed"))))
    assert len(seen["probes"]) == 1


def test_неизвестная_причина_не_становится_меткой(seen):
    token = service.issue_id(SECRET, ME, int(NOW) + 600)
    accept(Limiter(), token, report(failure_reason="carrier-pigeon"))
    assert seen["diagnostics"][0][3] == "none"


# --- HTTP -----------------------------------------------------------------------------


class Runtime:
    keys = None
    oidc_settings = None
    object_store = None
    centrifugo = None

    def __init__(self):
        self.limiter = Limiter()
        self.turn = coturn()
        self.diagnostics_secret = SECRET

    @asynccontextmanager
    async def connection(self, mode=None):
        yield None


@pytest.fixture
def runtime():
    original = app.state.runtime
    app.state.runtime = Runtime()
    yield app.state.runtime
    app.state.runtime = original


@pytest.fixture
def client(runtime):
    return TestClient(app)


def authenticated(monkeypatch):
    async def _current(*args, **kwargs):
        user = User(user_id=ME, external_id="kc", display_name="Я", email="me@e.org",
                    email_verified=True,
                    created_at=datetime(2026, 10, 5, tzinfo=UTC),
                    updated_at=datetime(2026, 10, 5, tzinfo=UTC))
        return identity.AuthResult(user=user)

    monkeypatch.setattr(main, "_current", _current)


def anonymous(monkeypatch):
    async def _current(*args, **kwargs):
        return identity.AuthResult()

    monkeypatch.setattr(main, "_current", _current)


def test_без_входа_401(client, monkeypatch):
    anonymous(monkeypatch)
    assert client.post("/network-diagnostics/session").status_code == 401
    body = {"phase": "failure", "trigger": "ice_slow", "setup_result": "failed"}
    assert client.post("/network-diagnostics/x/report", json=body).status_code == 401


def test_сессия_201_без_кэширования(client, monkeypatch):
    authenticated(monkeypatch)
    r = client.post("/network-diagnostics/session")
    assert r.status_code == 201 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["ttl_seconds"] == 45 and "turn" in str(body["ice_servers"])
    assert "stun" in body["probes"] and "turn_tls" in body["probes"]


def test_частые_сессии_429_с_retry_after(client, monkeypatch, runtime):
    authenticated(monkeypatch)
    runtime.limiter = Limiter(deny_prefix="diag-session:")
    r = client.post("/network-diagnostics/session")
    assert r.status_code == 429 and r.headers["retry-after"] == "120"


def test_полный_круг_сессия_отчёт_повтор(client, monkeypatch, seen):
    authenticated(monkeypatch)
    token = client.post("/network-diagnostics/session").json()["diagnostic_id"]
    body = {
        "phase": "failure", "trigger": "ice_slow", "setup_result": "timeout",
        "probes": [{"probe": "turn_udp", "result": "timeout"}],
        "ice_gathering_ms": 900, "failure_reason": "ice_connect_timeout",
    }
    assert client.post(f"/network-diagnostics/{token}/report", json=body).status_code == 204
    assert client.post(f"/network-diagnostics/{token}/report", json=body).status_code == 409
    assert seen["probes"][0][:3] == ("failure", "turn_udp", "timeout")


def test_чужой_идентификатор_404(client, monkeypatch, seen):
    authenticated(monkeypatch)
    token = service.issue_id(SECRET, OTHER, 4_000_000_000)
    body = {"phase": "failure", "trigger": "ice_slow", "setup_result": "failed"}
    assert client.post(f"/network-diagnostics/{token}/report", json=body).status_code == 404


@pytest.mark.parametrize("patch", [
    {"phase": "after"},
    {"trigger": "because"},
    {"setup_result": "maybe"},
    {"probes": [{"probe": "ping", "result": "ok"}]},
    {"probes": [{"probe": "stun", "result": "great"}]},
    {"ice_gathering_ms": -1},
    {"ice_connection_ms": 999_999},
    {"selected_candidate": "magic"},
    {"selected_relay_protocol": "quic"},
    {"ip": "203.0.113.5"},
])
def test_негодный_отчёт_422(client, monkeypatch, patch):
    authenticated(monkeypatch)
    token = service.issue_id(SECRET, ME, 4_000_000_000)
    body = {"phase": "failure", "trigger": "ice_slow", "setup_result": "failed", **patch}
    assert client.post(f"/network-diagnostics/{token}/report", json=body).status_code == 422
