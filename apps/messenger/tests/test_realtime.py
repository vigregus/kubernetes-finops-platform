"""Сервис realtime: выдача connect-токена и привязка соединения к сессии."""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from messenger.domain.identity import Claims, TokenRejection
from messenger.domain.ids import DeviceId, SessionId, UserId
from messenger.domain.session import Device, Session
from messenger.domain.user import User
from messenger.repositories import conversations as conversation_repo
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
        self.issued: list[tuple[str, str, list[str]]] = []
        self.settings = SimpleNamespace(token_ttl_seconds=120)
        self.claims = {
            "sub": str(USER_ID),
            "sid": str(SESSION_ID),
            "channels": [f"user:{USER_ID}"],
        }

    def issue_token(self, user_id, session_id, channels, *, ttl_seconds=None):
        self.issued.append((user_id, session_id, channels))
        return "connect-token", NOW + timedelta(minutes=2)

    def verify_ticket(self, token):
        return self.claims if token == "connect-token" else None


def _беседы(monkeypatch, ids):
    """Подменяет список бесед: здесь проверяется выдача токена, не SQL."""
    async def _list(conn, *, user_id, limit=200):
        return list(ids)

    monkeypatch.setattr(conversation_repo, "list_active_conversation_ids", _list)


def test_выдача_токена_на_личный_канал(monkeypatch):
    async def _authenticate(*args, **kwargs):
        return _auth()

    realtime = FakeRealtime()
    monkeypatch.setattr(identity, "authenticate", _authenticate)
    _беседы(monkeypatch, [])

    result = asyncio.run(service.issue_token_for_user(
        None, token="token", keys=None, settings=None, realtime=realtime,
    ))
    assert result.ok
    assert result.token == "connect-token"
    assert result.expires_at is not None
    # Идентификатора соединения в результате больше нет.
    assert not hasattr(result, "client_id")
    assert realtime.issued == [
        (str(USER_ID), str(SESSION_ID), [f"user:{USER_ID}"])
    ]


def test_в_токен_попадают_каналы_бесед(monkeypatch):
    """Клиент не выбирает канал сам.

    Сервер перечисляет разрешённые явно, иначе подписка на чужую беседу
    сводится к знанию её идентификатора. Список берётся на момент выдачи,
    и токен короткий именно поэтому: исключённый из беседы теряет
    подписку при следующем соединении, а не когда-нибудь.
    """
    import uuid as _uuid

    беседа = _uuid.uuid4()

    async def _authenticate(*args, **kwargs):
        return _auth()

    realtime = FakeRealtime()
    monkeypatch.setattr(identity, "authenticate", _authenticate)
    _беседы(monkeypatch, [беседа])

    asyncio.run(service.issue_token_for_user(
        None, token="token", keys=None, settings=None, realtime=realtime,
    ))
    _, _, channels = realtime.issued[0]
    assert channels == [f"user:{USER_ID}", f"conversation:{беседа}"]


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
    monkeypatch.setattr(service.sessions, "register_realtime_connection", _set)

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
    monkeypatch.setattr(service.sessions, "register_realtime_connection", _не_вызывать)

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
    monkeypatch.setattr(service.sessions, "register_realtime_connection", _set)

    result = asyncio.run(service.register_connection(
        None, token="token", client_id="c1", keys=None, settings=None,
    ))
    # Не отказ токена: доступ уже отрезан отзывом, привязка просто не нужна.
    assert result.ok and not result.registered


def test_connect_proxy_регистрирует_client_до_допуска(monkeypatch):
    seen = {}

    async def _register(conn, **kwargs):
        seen.update(kwargs)
        return True

    monkeypatch.setattr(service.sessions, "register_realtime_connection", _register)
    result = asyncio.run(service.connect_from_ticket(
        None,
        ticket="connect-token",
        client_id="client-tab-1",
        realtime=FakeRealtime(),
    ))
    assert result.accepted
    assert result.user_id == str(USER_ID)
    assert result.session_id == str(SESSION_ID)
    assert result.channels == (f"user:{USER_ID}",)
    assert seen["client_id"] == "client-tab-1"


def test_connect_proxy_не_принимает_отозванную_сессию(monkeypatch):
    async def _register(conn, **kwargs):
        return False

    monkeypatch.setattr(service.sessions, "register_realtime_connection", _register)
    result = asyncio.run(service.connect_from_ticket(
        None,
        ticket="connect-token",
        client_id="client-after-logout",
        realtime=FakeRealtime(),
    ))
    assert not result.accepted


def test_connect_proxy_не_принимает_поддельный_ticket(monkeypatch):
    async def _never(*args, **kwargs):
        raise AssertionError("репозиторий вызван для поддельного ticket")

    monkeypatch.setattr(service.sessions, "register_realtime_connection", _never)
    result = asyncio.run(service.connect_from_ticket(
        None,
        ticket="forged",
        client_id="client",
        realtime=FakeRealtime(),
    ))
    assert not result.accepted
