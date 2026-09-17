"""AUTH-005: подписанный logout-token отзывает локальную сессию."""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from messenger.domain.identity import LogoutClaims, LogoutTokenCheck, TokenRejection
from messenger.domain.ids import DeviceId, SessionId, UserId
from messenger.domain.session import Session
from messenger.repositories.sessions import RevokedSession
from messenger.services import backchannel

USER_ID = UserId(uuid.UUID("11111111-1111-1111-1111-111111111111"))
SESSION_ID = SessionId(uuid.UUID("22222222-2222-2222-2222-222222222222"))


def session() -> Session:
    now = datetime.now(UTC)
    return Session(
        session_id=SESSION_ID,
        user_id=USER_ID,
        device_id=DeviceId(uuid.uuid4()),
        created_at=now,
        expires_at=now + timedelta(days=1),
    )


def call(**kwargs):
    return asyncio.run(
        backchannel.handle_logout(
            object(),
            token="logout-token",
            keys=None,
            settings=None,
            audience="messenger-web",
            realtime=None,
            **kwargs,
        )
    )


def test_sid_отзывает_ровно_одну_сессию_и_соединения(monkeypatch):
    async def _verify(*args, **kwargs):
        return LogoutTokenCheck(claims=LogoutClaims(None, "external-sid"))

    async def _fetch(*args, **kwargs):
        return session()

    async def _revoke(*args, **kwargs):
        return RevokedSession(SESSION_ID, ("client-1",))

    dropped = []

    async def _drop(*args, **kwargs):
        dropped.append(kwargs)

    monkeypatch.setattr(backchannel.oidc, "verify_logout_token", _verify)
    monkeypatch.setattr(backchannel.sessions, "fetch_session", _fetch)
    monkeypatch.setattr(backchannel.sessions, "revoke_session", _revoke)
    monkeypatch.setattr(backchannel.session_management, "drop_connections", _drop)
    result = call()
    assert result.accepted and result.revoked == 1
    assert dropped[0]["disconnect_by_user"] is False


def test_повторный_logout_идемпотентен(monkeypatch):
    async def _verify(*args, **kwargs):
        return LogoutTokenCheck(claims=LogoutClaims(None, "external-sid"))

    async def _missing(*args, **kwargs):
        return None

    monkeypatch.setattr(backchannel.oidc, "verify_logout_token", _verify)
    monkeypatch.setattr(backchannel.sessions, "fetch_session", _missing)
    result = call()
    assert result.accepted and result.revoked == 0


def test_неподписанное_событие_не_ходит_в_базу(monkeypatch):
    async def _verify(*args, **kwargs):
        return LogoutTokenCheck(rejection=TokenRejection.BAD_SIGNATURE)

    async def _не_вызывать(*args, **kwargs):
        raise AssertionError("база вызвана до проверки logout-token")

    monkeypatch.setattr(backchannel.oidc, "verify_logout_token", _verify)
    monkeypatch.setattr(backchannel.sessions, "fetch_session", _не_вызывать)
    result = call()
    assert not result.accepted and result.rejection is TokenRejection.BAD_SIGNATURE
