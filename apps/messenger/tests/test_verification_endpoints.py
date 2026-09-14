"""HTTP-ответы профиля и повторной отправки подтверждения."""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from messenger.api import main
from messenger.api.main import app
from messenger.domain.ids import UserId
from messenger.domain.user import User
from messenger.services import identity, verification


class Runtime:
    limiter = object()
    admin = object()

    @asynccontextmanager
    async def connection(self):
        yield None


def user(*, verified: bool = False) -> User:
    now = datetime.now(UTC)
    return User(
        user_id=UserId(uuid.uuid4()),
        external_id="keycloak-user",
        display_name="Аня",
        email="anya@example.org",
        email_verified=verified,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def runtime():
    original = app.state.runtime
    app.state.runtime = Runtime()
    yield
    app.state.runtime = original


def authenticated(monkeypatch, *, verified=False):
    async def _current(*args, **kwargs):
        return identity.AuthResult(user=user(verified=verified))

    monkeypatch.setattr(main, "_current", _current)


def test_неподтверждённый_профиль_видит_явные_возможности(client, monkeypatch):
    authenticated(monkeypatch)
    body = client.get("/me", headers={"Authorization": "Bearer token"}).json()
    assert body["email_verified"] is False
    assert body["capabilities"] == ["read", "send_message"]


def test_повтор_письма_возвращает_202(client, monkeypatch):
    authenticated(monkeypatch)

    async def _resend(**kwargs):
        return verification.ResendResult(sent=True)

    monkeypatch.setattr(verification, "resend_verification", _resend)
    r = client.post("/auth/verify-email/resend", headers={"Authorization": "Bearer token"})
    assert r.status_code == 202 and r.json() == {"sent": True}


def test_лимит_возвращает_429_и_retry_after(client, monkeypatch):
    authenticated(monkeypatch)

    async def _resend(**kwargs):
        return verification.ResendResult(limited=True, retry_after_seconds=73)

    monkeypatch.setattr(verification, "resend_verification", _resend)
    r = client.post("/auth/verify-email/resend", headers={"Authorization": "Bearer token"})
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "73"


def test_подтверждённый_адрес_возвращает_409(client, monkeypatch):
    authenticated(monkeypatch, verified=True)

    async def _resend(**kwargs):
        return verification.ResendResult(already_verified=True)

    monkeypatch.setattr(verification, "resend_verification", _resend)
    assert client.post(
        "/auth/verify-email/resend", headers={"Authorization": "Bearer token"}
    ).status_code == 409


def test_отказ_keycloak_возвращает_503(client, monkeypatch):
    authenticated(monkeypatch)

    async def _resend(**kwargs):
        return verification.ResendResult(upstream_failed=True)

    monkeypatch.setattr(verification, "resend_verification", _resend)
    assert client.post(
        "/auth/verify-email/resend", headers={"Authorization": "Bearer token"}
    ).status_code == 503


def test_без_удостоверения_письмо_не_отправляется(client, monkeypatch):
    async def _current(*args, **kwargs):
        return identity.AuthResult()

    async def _не_вызывать(**kwargs):
        raise AssertionError("отправка началась без удостоверения")

    monkeypatch.setattr(main, "_current", _current)
    monkeypatch.setattr(verification, "resend_verification", _не_вызывать)
    assert client.post("/auth/verify-email/resend").status_code == 401
