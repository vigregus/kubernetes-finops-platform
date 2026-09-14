"""HTTP-контракт создания беседы один-на-один."""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from messenger.api import main
from messenger.api.main import app
from messenger.domain.conversation import Conversation, ConversationType
from messenger.domain.errors import Reason
from messenger.domain.identity import TokenRejection
from messenger.domain.ids import ConversationId, ConversationSeq, UserId
from messenger.domain.user import User
from messenger.services import conversations as service
from messenger.services import identity

ACTOR_ID = UserId(uuid.UUID("11111111-1111-1111-1111-111111111111"))
OTHER_ID = UserId(uuid.UUID("22222222-2222-2222-2222-222222222222"))
CONVERSATION_ID = ConversationId(uuid.UUID("33333333-3333-3333-3333-333333333333"))
NOW = datetime(2026, 9, 15, tzinfo=UTC)


class Runtime:
    keys = None
    oidc_settings = None

    @asynccontextmanager
    async def connection(self):
        yield None


def _user(user_id: UserId, name: str) -> User:
    return User(
        user_id=user_id,
        external_id=f"kc-{user_id}",
        display_name=name,
        email=f"{user_id}@example.org",
        email_verified=True,
        created_at=NOW,
        updated_at=NOW,
    )


def _success(*, created: bool) -> service.CreateDirectResult:
    return service.CreateDirectResult(
        conversation=Conversation(
            conversation_id=CONVERSATION_ID,
            type=ConversationType.DIRECT,
            direct_key="a:b",
            last_seq=ConversationSeq(0),
            created_at=NOW,
            updated_at=NOW,
        ),
        participants=(_user(ACTOR_ID, "Аня"), _user(OTHER_ID, "Борис")),
        created=created,
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
        return identity.AuthResult(user=_user(ACTOR_ID, "Аня"))

    monkeypatch.setattr(main, "_current", _current)


def test_создание_возвращает_201_и_участников(client, monkeypatch):
    authenticated(monkeypatch)

    async def _create(*args, **kwargs):
        assert kwargs["participant_id"] == OTHER_ID
        return _success(created=True)

    monkeypatch.setattr(service, "create_direct", _create)
    response = client.post(
        "/conversations",
        json={"participant_id": str(OTHER_ID)},
        headers={"Authorization": "Bearer token"},
    )
    assert response.status_code == 201
    assert response.json() == {
        "conversation_id": str(CONVERSATION_ID),
        "type": "direct",
        "participants": [
            {"user_id": str(ACTOR_ID), "display_name": "Аня"},
            {"user_id": str(OTHER_ID), "display_name": "Борис"},
        ],
        "created_at": "2026-09-15T00:00:00Z",
    }


def test_повтор_возвращает_200(client, monkeypatch):
    authenticated(monkeypatch)

    async def _create(*args, **kwargs):
        return _success(created=False)

    monkeypatch.setattr(service, "create_direct", _create)
    response = client.post(
        "/conversations",
        json={"participant_id": str(OTHER_ID)},
        headers={"Authorization": "Bearer token"},
    )
    assert response.status_code == 200
    assert response.json()["conversation_id"] == str(CONVERSATION_ID)


@pytest.mark.parametrize(
    ("reason", "status"),
    [
        (Reason.BLOCKED, 403),
        (Reason.EMAIL_UNVERIFIED, 403),
        (Reason.SELF_CONVERSATION, 403),
        (Reason.USER_NOT_FOUND, 404),
    ],
)
def test_ожидаемые_отказы_переводятся_в_контракт(client, monkeypatch, reason, status):
    authenticated(monkeypatch)

    async def _create(*args, **kwargs):
        return service.CreateDirectResult(rejection=reason)

    monkeypatch.setattr(service, "create_direct", _create)
    response = client.post(
        "/conversations",
        json={"participant_id": str(OTHER_ID)},
        headers={"Authorization": "Bearer token"},
    )
    assert response.status_code == status


def test_без_токена_сервис_не_вызывается(client, monkeypatch):
    async def _не_вызывать(*args, **kwargs):
        raise AssertionError("создание началось без удостоверения")

    monkeypatch.setattr(service, "create_direct", _не_вызывать)
    response = client.post("/conversations", json={"participant_id": str(OTHER_ID)})
    assert response.status_code == 401


def test_недоступные_ключи_дают_503(client, monkeypatch):
    async def _current(*args, **kwargs):
        return identity.AuthResult(rejection=TokenRejection.KEYS_UNAVAILABLE)

    monkeypatch.setattr(main, "_current", _current)
    response = client.post(
        "/conversations",
        json={"participant_id": str(OTHER_ID)},
        headers={"Authorization": "Bearer token"},
    )
    assert response.status_code == 503


def test_не_uuid_отклоняется_до_сервиса(client, monkeypatch):
    async def _не_вызывать(*args, **kwargs):
        raise AssertionError("сервис получил невалидный UUID")

    monkeypatch.setattr(service, "create_direct", _не_вызывать)
    response = client.post(
        "/conversations",
        json={"participant_id": "not-a-uuid"},
        headers={"Authorization": "Bearer token"},
    )
    assert response.status_code == 422
