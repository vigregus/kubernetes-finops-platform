"""HTTP-контракт отправки сообщения.

Ни базы, ни Kafka: сервис подменяется, потому что проверяется не он,
а обработчик — коды ответа, форма тела и то, что негодный запрос
отвергается до обращения к хранилищу.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from messenger.api import main
from messenger.api.main import app
from messenger.domain.errors import Reason
from messenger.domain.ids import (
    ClientMessageId,
    ConversationId,
    ConversationSeq,
    MessageId,
    UserId,
)
from messenger.domain.message import Message, MessageKind, MessagePayload
from messenger.domain.user import User
from messenger.services import identity
from messenger.services import messages as service

ACTOR_ID = UserId(uuid.UUID("11111111-1111-1111-1111-111111111111"))
CONVERSATION_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
MESSAGE_ID = MessageId(uuid.UUID("44444444-4444-4444-4444-444444444444"))
CLIENT_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
NOW = datetime(2026, 9, 18, tzinfo=UTC)

URL = f"/conversations/{CONVERSATION_ID}/messages"
ТЕЛО = {"client_message_id": str(CLIENT_ID), "type": "text", "payload": {"text": "привет"}}


class Runtime:
    keys = None
    oidc_settings = None

    @asynccontextmanager
    async def connection(self):
        yield None


def _user() -> User:
    return User(
        user_id=ACTOR_ID,
        external_id="kc-1",
        display_name="Аня",
        email="anya@example.org",
        email_verified=True,
        created_at=NOW,
        updated_at=NOW,
    )


def _message(seq: int = 1) -> Message:
    return Message(
        message_id=MESSAGE_ID,
        conversation_id=ConversationId(CONVERSATION_ID),
        conversation_seq=ConversationSeq(seq),
        sender_id=ACTOR_ID,
        client_message_id=ClientMessageId(CLIENT_ID),
        kind=MessageKind.TEXT,
        payload=MessagePayload(text="привет"),
        created_at=NOW,
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


def authenticated(monkeypatch):
    async def _current(*args, **kwargs):
        return identity.AuthResult(user=_user())

    monkeypatch.setattr(main, "_current", _current)


def отвечает(monkeypatch, result):
    async def _send(conn, **kwargs):
        return result

    monkeypatch.setattr(service, "send_message", _send)


def test_без_токена_не_принимает(client, monkeypatch, отказ):
    async def _current(*args, **kwargs):
        return identity.AuthResult()

    monkeypatch.setattr(main, "_current", _current)
    отказ(client.post(URL, json=ТЕЛО), status=401, code="unauthenticated")


def test_принятое_сообщение_возвращает_201_и_форму_контракта(client, monkeypatch):
    authenticated(monkeypatch)
    отвечает(monkeypatch, service.SendMessageResult(message=_message(), created=True))

    r = client.post(URL, json=ТЕЛО)
    assert r.status_code == 201
    body = r.json()
    for поле in ("message_id", "conversation_id", "seq", "sender_id",
                 "client_message_id", "type", "payload", "created_at"):
        assert поле in body, f"нет поля {поле}"
    assert body["seq"] == 1
    assert body["payload"]["text"] == "привет"


def test_повтор_возвращает_200_и_то_же_сообщение(client, monkeypatch):
    """Различить коды клиенту нужно: `201` — «принято сейчас», `200` —
    «было принято раньше, и первый запрос всё-таки дошёл»."""
    authenticated(monkeypatch)
    отвечает(monkeypatch, service.SendMessageResult(message=_message(), created=False))

    r = client.post(URL, json=ТЕЛО)
    assert r.status_code == 200
    assert r.json()["message_id"] == str(MESSAGE_ID)


def test_слишком_длинный_текст_отвергается_до_базы(client, monkeypatch, отказ):
    """Повтор такого запроса бессмыслен без правки текста, поэтому код
    отдельный — и проверка идёт до всякого обращения к хранилищу."""
    authenticated(monkeypatch)

    async def _не_должно_вызваться(conn, **kwargs):
        raise AssertionError("сервис вызван с негодным содержимым")

    monkeypatch.setattr(service, "send_message", _не_должно_вызваться)
    r = client.post(URL, json={**ТЕЛО, "payload": {"text": "я" * 5000}})
    отказ(r, status=413, code="payload_too_large")


def test_голосовое_без_длительности_не_принимается(client, monkeypatch, отказ):
    authenticated(monkeypatch)
    отвечает(monkeypatch, service.SendMessageResult(message=_message()))
    r = client.post(URL, json={**ТЕЛО, "type": "voice", "payload": {}})
    отказ(r, status=422, code="invalid_payload")


def test_системный_вид_не_принимается_снаружи(client, monkeypatch):
    """`system` — вид, который порождает сервер, а не человек."""
    authenticated(monkeypatch)
    r = client.post(URL, json={**ТЕЛО, "type": "system"})
    assert r.status_code == 422


def test_чужая_беседа_неотличима_от_несуществующей(client, monkeypatch, отказ):
    """`403` подтвердил бы, что беседа есть, и перебором выяснялось бы,
    кто с кем переписывается."""
    authenticated(monkeypatch)
    отвечает(monkeypatch, service.SendMessageResult(rejection=Reason.NOT_A_MEMBER))
    r = client.post(URL, json=ТЕЛО)
    отказ(r, status=404, code="resource_not_found")
