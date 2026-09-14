"""Доменные состояния беседы и членства без базы."""
from __future__ import annotations

import uuid
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from messenger.domain.conversation import (
    Conversation,
    ConversationMember,
    ConversationType,
    MemberRole,
)
from messenger.domain.ids import ConversationId, ConversationSeq, UserId

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


def _conversation(**overrides) -> Conversation:
    base = {
        "conversation_id": ConversationId(uuid.uuid4()),
        "type": ConversationType.DIRECT,
        "direct_key": "a:b",
        "last_seq": ConversationSeq(0),
        "created_at": NOW,
        "updated_at": NOW,
    }
    return Conversation(**{**base, **overrides})


def _member(**overrides) -> ConversationMember:
    base = {
        "conversation_id": ConversationId(uuid.uuid4()),
        "user_id": UserId(uuid.uuid4()),
        "role": MemberRole.MEMBER,
        "joined_at": NOW,
    }
    return ConversationMember(**{**base, **overrides})


def test_прямая_беседа_требует_ключ_пары():
    with pytest.raises(ValueError):
        _conversation(direct_key=None)
    with pytest.raises(ValueError):
        _conversation(direct_key="")


def test_группа_не_может_иметь_ключ_пары():
    for value in ("a:b", ""):
        with pytest.raises(ValueError):
            _conversation(type=ConversationType.GROUP, direct_key=value)


def test_номер_сообщения_не_бывает_отрицательным():
    with pytest.raises(ValueError):
        _conversation(last_seq=ConversationSeq(-1))


def test_тип_беседы_назван_явно():
    assert _conversation().is_direct
    assert not _conversation(type=ConversationType.GROUP, direct_key=None).is_direct


def test_бывший_участник_отличим_от_действующего():
    assert _member().is_active
    assert not _member(left_at=NOW + timedelta(hours=1)).is_active


def test_выход_не_может_предшествовать_вступлению():
    with pytest.raises(ValueError):
        _member(left_at=NOW - timedelta(seconds=1))


def test_беседа_и_членство_неизменяемы():
    with pytest.raises(FrozenInstanceError):
        _conversation().last_seq = ConversationSeq(1)  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        _member().role = MemberRole.ADMIN  # type: ignore[misc]
