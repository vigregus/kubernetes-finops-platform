"""Сервис realtime: выдача connect-токена и привязка соединения к сессии."""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from messenger.domain.identity import Claims, TokenRejection
from messenger.domain.ids import DeviceId, SessionId, UserId
from messenger.domain.session import Device, Session
from messenger.domain.user import User
from messenger.services import identity
from messenger.services import realtime as service

USER_ID = UserId(uuid.UUID("15283722-3214-438d-bd2a-04f0f22f19c4"))
DEVICE_ID = DeviceId(uuid.UUID("a16399d3-d16f-4e6a-8d6a-5be1848f8911"))
SESSION_ID = SessionId(uuid.UUID("cb886f0f-e9d9-49c6-8202-57e62f46ca9d"))
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
    """Отдаёт фиксированный токен; срок записывается для проверки."""

    def __init__(self):
        self.issued: list[tuple[str, list[str]]] = []

    def issue_token(self, user_id, channels, *, ttl_seconds=None):
        self.issued.append((user_id, channels))
        return "connect-token", NOW + timedelta(minutes=2)


def test_выдача_токена_на_личный_канал(monkeypatch):
    async def _authenticate(*args, **kwargs):
        return _auth()

    realtime = FakeRealtime()
    monkeypatch.setattr(identity, "authenticate", _authenticate)

    result = asyncio.run(service.issue_token_for_user(
        None, token="token", keys=None, settings=None, realtime=realtime,
    ))
    assert result.ok
    assert result.token == "connect-token"
    assert result.expires_at is not None
    # Идентификатора соединения в результате больше нет.
    assert not hasattr(result, "client_id")
    assert realtime.issued == [(str(USER_ID), [f"user:{USER_ID}"])]


def test_выдача_токена_при_отказе_токена(monkeypatch):
    async def _authenticate(*args, **kwargs):
        return identity.AuthResult(rejection=TokenRejection.BAD_SIGNATURE)

    monkeypatch.setattr(identity, "authenticate", _authenticate)
    result = asyncio.run(service.issue_token_for_user(
        None, token="bad", keys=None, settings=None, realtime=FakeRealtime(),
    ))
    assert not result.ok
    assert result.rejection is TokenRejection.BAD_SIGNATURE


def test_выдача_токена_без_centrifugo_это_503(monkeypatch):
    async def _authenticate(*args, **kwargs):
        return _auth()

    monkeypatch.setattr(identity, "authenticate", _authenticate)
    result = asyncio.run(service.issue_token_for_user(
        None, token="token", keys=None, settings=None, realtime=None,
    ))
    assert not result.ok
    assert result.rejection is TokenRejection.KEYS_UNAVAILABLE


def test_привязка_соединения_к_своей_сессии(monkeypatch):
    async def _authenticate(*args, **kwargs):
        return _auth()

    seen = {}

    async def _set(conn, **kwargs):
        seen.update(kwargs)
        return True

    monkeypatch.setattr(identity, "authenticate", _authenticate)
    monkeypatch.setattr(service.sessions, "set_realtime_client_id", _set)

    result = asyncio.run(service.register_connection(
        None, token="token", client_id="centrifugo-client-1", keys=None, settings=None,
    ))
    assert result.ok and result.registered
    assert seen["session_id"] == SESSION_ID
    assert seen["user_id"] == USER_ID
    assert seen["client_id"] == "centrifugo-client-1"


def test_привязка_соединения_при_отказе_токена(monkeypatch):
    async def _authenticate(*args, **kwargs):
        return identity.AuthResult(rejection=TokenRejection.SESSION_REVOKED)

    async def _не_вызывать(*args, **kwargs):
        raise AssertionError("репозиторий вызван после отказа токена")

    monkeypatch.setattr(identity, "authenticate", _authenticate)
    monkeypatch.setattr(service.sessions, "set_realtime_client_id", _не_вызывать)

    result = asyncio.run(service.register_connection(
        None, token="bad", client_id="c1", keys=None, settings=None,
    ))
    assert not result.ok
    assert result.rejection is TokenRejection.SESSION_REVOKED


def test_привязка_к_уже_отозванной_сессии_не_ошибка(monkeypatch):
    async def _authenticate(*args, **kwargs):
        return _auth()

    async def _set(conn, **kwargs):
        return False  # строка уже отозвана — привязать не к чему

    monkeypatch.setattr(identity, "authenticate", _authenticate)
    monkeypatch.setattr(service.sessions, "set_realtime_client_id", _set)

    result = asyncio.run(service.register_connection(
        None, token="token", client_id="c1", keys=None, settings=None,
    ))
    # Не отказ токена: доступ уже отрезан отзывом, привязка просто не нужна.
    assert result.ok and not result.registered
