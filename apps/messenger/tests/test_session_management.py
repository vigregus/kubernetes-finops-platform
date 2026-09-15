"""Сервис сессий: удостоверение связывается только со своими строками."""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from messenger.domain.identity import Claims, TokenRejection
from messenger.domain.ids import DeviceId, SessionId, UserId
from messenger.domain.session import Device, RevocationReason, Session
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


class FakeRealtime:
    """Записывает разрывы и публикации, отвечая успехом, как настроено."""

    def __init__(self, *, ok: bool = True):
        self.ok = ok
        self.disconnected_users: list[str] = []
        self.disconnected_clients: list[tuple[str, str]] = []
        self.published: list[tuple[str, dict]] = []

    async def disconnect_user(self, user_id, *, code, reason):
        self.disconnected_users.append(user_id)
        return self.ok

    async def disconnect_client(self, user_id, client_id, *, code, reason):
        self.disconnected_clients.append((user_id, client_id))
        return self.ok

    async def publish(self, channel, data):
        self.published.append((channel, data))
        return self.ok


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


def test_выход_везде_ограничен_пользователем_из_токена(monkeypatch):
    async def _authenticate(*args, **kwargs):
        return _auth()

    seen = {}

    async def _revoke_all(conn, **kwargs):
        seen.update(kwargs)
        return [
            service.sessions.RevokedSession(session_id=SESSION_ID),
            service.sessions.RevokedSession(session_id=OTHER_ID),
        ]

    monkeypatch.setattr(identity, "authenticate", _authenticate)
    monkeypatch.setattr(service.sessions, "revoke_user_sessions", _revoke_all)

    result = asyncio.run(service.revoke_all_for_token(
        None, token="token", keys=None, settings=None
    ))
    assert result.ok and result.revoked == 2
    assert seen["user_id"] == USER_ID
    assert seen["reason"] is RevocationReason.LOGOUT_ALL


def test_выход_на_устройстве_рвёт_только_это_соединение_и_шлёт_событие(monkeypatch):
    async def _authenticate(*args, **kwargs):
        return _auth()

    async def _revoke(conn, **kwargs):
        return service.sessions.RevokedSession(
            session_id=OTHER_ID, realtime_client_ids=("client-other", "client-tab-2")
        )

    realtime = FakeRealtime()
    monkeypatch.setattr(identity, "authenticate", _authenticate)
    monkeypatch.setattr(service.sessions, "revoke_session", _revoke)

    result = asyncio.run(service.revoke_for_token(
        None, token="token", target=OTHER_ID, keys=None, settings=None,
        realtime=realtime,
    ))
    assert result.ok and result.revoked
    # Весь user рвать нельзя: только соединение отозванного session_id,
    # адресуемое парой (user, client).
    assert realtime.disconnected_clients == [
        (str(USER_ID), "client-other"),
        (str(USER_ID), "client-tab-2"),
    ]
    assert realtime.disconnected_users == []
    assert realtime.published == [
        (f"user:{USER_ID}", {"type": "session.revoked", "session_id": str(OTHER_ID)})
    ]


def test_выход_на_устройстве_без_client_id_рвётся_только_событием(monkeypatch):
    async def _authenticate(*args, **kwargs):
        return _auth()

    async def _revoke(conn, **kwargs):
        return service.sessions.RevokedSession(
            session_id=OTHER_ID
        )

    realtime = FakeRealtime()
    monkeypatch.setattr(identity, "authenticate", _authenticate)
    monkeypatch.setattr(service.sessions, "revoke_session", _revoke)

    result = asyncio.run(service.revoke_for_token(
        None, token="token", target=OTHER_ID, keys=None, settings=None,
        realtime=realtime,
    ))
    assert result.ok and result.revoked
    # Соединение не сообщало свой client — рвать нечего, но событие уходит.
    assert realtime.disconnected_clients == []
    assert realtime.published == [
        (f"user:{USER_ID}", {"type": "session.revoked", "session_id": str(OTHER_ID)})
    ]


def test_выход_везде_рвёт_весь_user_и_шлёт_событие_на_каждую(monkeypatch):
    async def _authenticate(*args, **kwargs):
        return _auth()

    async def _revoke_all(conn, **kwargs):
        return [
            service.sessions.RevokedSession(
                session_id=SESSION_ID, realtime_client_ids=("client-a",)
            ),
            service.sessions.RevokedSession(
                session_id=OTHER_ID, realtime_client_ids=("client-b",)
            ),
        ]

    realtime = FakeRealtime()
    monkeypatch.setattr(identity, "authenticate", _authenticate)
    monkeypatch.setattr(service.sessions, "revoke_user_sessions", _revoke_all)

    result = asyncio.run(service.revoke_all_for_token(
        None, token="token", keys=None, settings=None, realtime=realtime,
    ))
    assert result.ok and result.revoked == 2
    assert realtime.disconnected_users == [str(USER_ID)]
    assert realtime.disconnected_clients == []
    assert realtime.published == [
        (f"user:{USER_ID}", {"type": "session.revoked", "session_id": str(SESSION_ID)}),
        (f"user:{USER_ID}", {"type": "session.revoked", "session_id": str(OTHER_ID)}),
    ]


def test_без_realtime_отзыв_проходит_без_разрыва(monkeypatch):
    async def _authenticate(*args, **kwargs):
        return _auth()

    async def _revoke(conn, **kwargs):
        return True

    monkeypatch.setattr(identity, "authenticate", _authenticate)
    monkeypatch.setattr(service.sessions, "revoke_session", _revoke)

    # realtime не передан вовсе — как при не настроенном Centrifugo.
    result = asyncio.run(service.revoke_for_token(
        None, token="token", target=OTHER_ID, keys=None, settings=None,
    ))
    assert result.ok and result.revoked


def test_недоступный_realtime_не_отменяет_отзыв(monkeypatch):
    async def _authenticate(*args, **kwargs):
        return _auth()

    async def _revoke(conn, **kwargs):
        return service.sessions.RevokedSession(
            session_id=OTHER_ID, realtime_client_ids=("client-other",)
        )

    realtime = FakeRealtime(ok=False)
    monkeypatch.setattr(identity, "authenticate", _authenticate)
    monkeypatch.setattr(service.sessions, "revoke_session", _revoke)

    result = asyncio.run(service.revoke_for_token(
        None, token="token", target=OTHER_ID, keys=None, settings=None,
        realtime=realtime,
    ))
    assert result.ok and result.revoked


def test_отклонённый_токен_не_меняет_базу_при_выходе_везде(monkeypatch):
    async def _authenticate(*args, **kwargs):
        return identity.AuthResult(rejection=TokenRejection.BAD_SIGNATURE)

    async def _не_вызывать(*args, **kwargs):
        raise AssertionError("репозиторий вызван после отказа токена")

    monkeypatch.setattr(identity, "authenticate", _authenticate)
    monkeypatch.setattr(service.sessions, "revoke_user_sessions", _не_вызывать)
    result = asyncio.run(service.revoke_all_for_token(
        None, token="bad", keys=None, settings=None
    ))
    assert not result.ok
    assert result.rejection is TokenRejection.BAD_SIGNATURE
