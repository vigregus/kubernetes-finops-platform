"""Сценарий обычного создания direct-беседы; конкурентная гонка — G1-011."""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest

from messenger.domain.authorization import Decision
from messenger.domain.conversation import (
    Conversation,
    ConversationType,
    EnsureConversationResult,
)
from messenger.domain.errors import Reason
from messenger.domain.ids import ConversationId, ConversationSeq, UserId, direct_key
from messenger.domain.user import User
from messenger.services import conversations as service

NOW = datetime(2026, 9, 15, tzinfo=UTC)
ACTOR_ID = UserId(uuid.UUID("11111111-1111-1111-1111-111111111111"))
OTHER_ID = UserId(uuid.UUID("22222222-2222-2222-2222-222222222222"))


class Transaction:
    def __init__(self) -> None:
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.exited = True


class Connection:
    def __init__(self) -> None:
        self.tx = Transaction()

    def transaction(self) -> Transaction:
        return self.tx


def _user(user_id: UserId, *, verified: bool = True, deleted: bool = False) -> User:
    return User(
        user_id=user_id,
        external_id=f"kc-{user_id}",
        display_name="Аня" if user_id == ACTOR_ID else "Борис",
        email=f"{user_id}@example.org",
        email_verified=verified,
        created_at=NOW,
        updated_at=NOW,
        deleted_at=NOW if deleted else None,
    )


def _conversation() -> Conversation:
    return Conversation(
        conversation_id=ConversationId(uuid.uuid4()),
        type=ConversationType.DIRECT,
        direct_key=direct_key(ACTOR_ID, OTHER_ID),
        last_seq=ConversationSeq(0),
        created_at=NOW,
        updated_at=NOW,
    )


def test_неподтверждённый_не_может_начать_беседу(monkeypatch):
    async def _deny(*args, **kwargs):
        return Decision.deny(Reason.EMAIL_UNVERIFIED)

    monkeypatch.setattr(service.authorization, "authorize", _deny)
    result = asyncio.run(
        service.create_direct(
            Connection(), actor=_user(ACTOR_ID, verified=False), participant_id=OTHER_ID
        )
    )
    assert result.rejection is Reason.EMAIL_UNVERIFIED


def test_беседа_с_собой_отклоняется_до_базы(monkeypatch):
    async def _deny(*args, **kwargs):
        return Decision.deny(Reason.SELF_CONVERSATION)

    monkeypatch.setattr(service.authorization, "authorize", _deny)
    result = asyncio.run(
        service.create_direct(Connection(), actor=_user(ACTOR_ID), participant_id=ACTOR_ID)
    )
    assert result.rejection is Reason.SELF_CONVERSATION


@pytest.mark.parametrize("participant", [None, _user(OTHER_ID, deleted=True)])
def test_отсутствующий_или_удалённый_участник_скрыт(monkeypatch, participant):
    async def _deny(*args, **kwargs):
        return Decision.deny(Reason.USER_NOT_FOUND)

    monkeypatch.setattr(service.authorization, "authorize", _deny)
    result = asyncio.run(
        service.create_direct(Connection(), actor=_user(ACTOR_ID), participant_id=OTHER_ID)
    )
    assert result.rejection is Reason.USER_NOT_FOUND


def test_блокировка_в_любую_сторону_запрещает_создание(monkeypatch):
    async def _deny(*args, **kwargs):
        return Decision.deny(Reason.BLOCKED)

    monkeypatch.setattr(service.authorization, "authorize", _deny)
    result = asyncio.run(
        service.create_direct(Connection(), actor=_user(ACTOR_ID), participant_id=OTHER_ID)
    )
    assert result.rejection is Reason.BLOCKED


def test_первый_запрос_атомарно_создаёт_беседу_и_два_членства(monkeypatch):
    conn = Connection()
    expected = _conversation()
    members: list[UserId] = []

    async def _allow(*args, **kwargs):
        return Decision.allow(target_user=_user(OTHER_ID))

    async def _ensure(*args, **kwargs):
        assert kwargs["direct_key"] == direct_key(ACTOR_ID, OTHER_ID)
        return EnsureConversationResult(conversation=expected, created=True)

    async def _add(*args, **kwargs):
        members.append(kwargs["user_id"])

    monkeypatch.setattr(service.authorization, "authorize", _allow)
    monkeypatch.setattr(service.conversations, "ensure_direct_conversation", _ensure)
    monkeypatch.setattr(service.conversations, "add_member", _add)

    result = asyncio.run(
        service.create_direct(conn, actor=_user(ACTOR_ID), participant_id=OTHER_ID)
    )
    assert result.ok and result.created and result.conversation == expected
    assert members == [ACTOR_ID, OTHER_ID]
    assert conn.tx.entered and conn.tx.exited


def test_последовательный_повтор_возвращает_существующую(monkeypatch):
    expected = _conversation()

    async def _allow(*args, **kwargs):
        return Decision.allow(target_user=_user(OTHER_ID))

    async def _ensure(*args, **kwargs):
        return EnsureConversationResult(conversation=expected, created=False)

    async def _не_добавлять(*args, **kwargs):
        raise AssertionError("повтор попытался создать членство заново")

    monkeypatch.setattr(service.authorization, "authorize", _allow)
    monkeypatch.setattr(service.conversations, "ensure_direct_conversation", _ensure)
    monkeypatch.setattr(service.conversations, "add_member", _не_добавлять)

    result = asyncio.run(
        service.create_direct(Connection(), actor=_user(ACTOR_ID), participant_id=OTHER_ID)
    )
    assert result.ok and not result.created and result.conversation == expected
    assert [user.user_id for user in result.participants] == [ACTOR_ID, OTHER_ID]
