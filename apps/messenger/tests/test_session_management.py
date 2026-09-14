"""Сервис сессий: удостоверение связывается только со своими строками."""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from messenger.domain.identity import Claims, TokenRejection
from messenger.domain.ids import DeviceId, SessionId, UserId
from messenger.domain.session import Device, Session
from messenger.domain.user import User
from messenger.services import identity
from messenger.services import session_management as service

USER_ID = UserId(uuid.UUID("15283722-3214-438d-bd2a-04f0f22f19c4"))
DEVICE_ID = DeviceId(uuid.UUID("a16399d3-d16f-4e6a-8d6a-5be1848f8911"))
SESSION_ID = SessionId(uuid.UUID("cb886f0f-e9d9-49c6-8202-57e62f46ca9d"))
OTHER_ID = SessionId(uuid.UUID("195d2aef-fbbd-4d53-bb04-e4038483cfa1"))
NOW = datetime(2026, 9, 14, tzinfo=UTC)


def _auth() -> identity.AuthResult:
    return identity.AuthResult(
        user=User(
            user_id=USER_ID,
            external_id="keycloak-user",
            display_name="Аня",
            email="anya@example.org",
            email_verified=True,
            created_at=NOW,
            updated_at=NOW,
        ),
        claims=Claims(
            subject="keycloak-user",
            email="anya@example.org",
            email_verified=True,
            display_name="Аня",
            session_state="keycloak-session",
            expires_at=NOW + timedelta(minutes=5),
        ),
        session=Session(
            session_id=SESSION_ID,
            user_id=USER_ID,
            device_id=DEVICE_ID,
            created_at=NOW,
            expires_at=NOW + timedelta(days=7),
        ),
        device=Device(
            device_id=DEVICE_ID,
            user_id=USER_ID,
            user_agent="Firefox/143",
            created_at=NOW,
            last_seen_at=NOW,
        ),
    )


def test_отзыв_ограничен_пользователем_из_токена(monkeypatch):
    async def _authenticate(*args, **kwargs):
        return _auth()

    seen = {}

    async def _revoke(conn, **kwargs):
        seen.update(kwargs)
        return True

    monkeypatch.setattr(identity, "authenticate", _authenticate)
    monkeypatch.setattr(service.sessions, "revoke_session", _revoke)

    result = asyncio.run(service.revoke_for_token(
        None, token="token", target=OTHER_ID, keys=None, settings=None
    ))
    assert result.ok and result.revoked and not result.current
    assert seen["session_id"] == OTHER_ID
    assert seen["user_id"] == USER_ID


def test_текущая_сессия_определяется_из_токена(monkeypatch):
    async def _authenticate(*args, **kwargs):
        return _auth()

    async def _revoke(*args, **kwargs):
        return True

    monkeypatch.setattr(identity, "authenticate", _authenticate)
    monkeypatch.setattr(service.sessions, "revoke_session", _revoke)
    result = asyncio.run(service.revoke_for_token(
        None, token="token", target=SESSION_ID, keys=None, settings=None
    ))
    assert result.ok and result.current


def test_отклонённый_токен_не_меняет_базу(monkeypatch):
    async def _authenticate(*args, **kwargs):
        return identity.AuthResult(rejection=TokenRejection.BAD_SIGNATURE)

    async def _не_вызывать(*args, **kwargs):
        raise AssertionError("репозиторий вызван после отказа токена")

    monkeypatch.setattr(identity, "authenticate", _authenticate)
    monkeypatch.setattr(service.sessions, "revoke_session", _не_вызывать)
    result = asyncio.run(service.revoke_for_token(
        None, token="bad", target=SESSION_ID, keys=None, settings=None
    ))
    assert not result.ok
    assert result.rejection is TokenRejection.BAD_SIGNATURE
