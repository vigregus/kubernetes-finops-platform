"""Сообщение: неизменяемая модель и правила содержимого без базы."""
from __future__ import annotations

import uuid
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from messenger.domain.ids import (
    ClientMessageId,
    ConversationId,
    ConversationSeq,
    MessageId,
    UserId,
)
from messenger.domain.message import MAX_TEXT_LENGTH, Message, MessageKind, MessagePayload

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def _message(**overrides) -> Message:
    base = {
        "message_id": MessageId(uuid.uuid4()),
        "conversation_id": ConversationId(uuid.uuid4()),
        "conversation_seq": ConversationSeq(1),
        "sender_id": UserId(uuid.uuid4()),
        "client_message_id": ClientMessageId(uuid.uuid4()),
        "kind": MessageKind.TEXT,
        "payload": MessagePayload(text="привет"),
        "created_at": NOW,
    }
    return Message(**{**base, **overrides})


def test_порядковый_номер_сообщения_начинается_с_единицы():
    for value in (0, -1):
        with pytest.raises(ValueError, match="положительным"):
            _message(conversation_seq=ConversationSeq(value))


def test_текстовое_сообщение_требует_непустой_текст():
    for value in (None, "", "   "):
        with pytest.raises(ValueError, match="текст"):
            _message(payload=MessagePayload(text=value))


def test_текст_ограничен_контрактным_пределом():
    _message(payload=MessagePayload(text="а" * MAX_TEXT_LENGTH))
    with pytest.raises(ValueError, match="4096"):
        MessagePayload(text="а" * (MAX_TEXT_LENGTH + 1))


def test_голосовое_сообщение_требует_положительную_длительность():
    for value in (None, 0, -1):
        with pytest.raises(ValueError, match="длительность"):
            _message(kind=MessageKind.VOICE, payload=MessagePayload(duration_ms=value))


def test_длительность_не_принимается_для_не_голосового_сообщения():
    with pytest.raises(ValueError, match="голосового"):
        _message(payload=MessagePayload(text="привет", duration_ms=100))


def test_момент_правки_и_удаления_не_предшествует_созданию():
    with pytest.raises(ValueError, match="правка"):
        _message(edited_at=NOW - timedelta(seconds=1))
    with pytest.raises(ValueError, match="удаление"):
        _message(deleted_at=NOW - timedelta(seconds=1))


def test_удалённое_сообщение_не_выдаёт_содержимое():
    deleted = _message(payload=MessagePayload(), deleted_at=NOW + timedelta(seconds=1))
    assert deleted.is_deleted
    assert deleted.visible_payload is None

    with pytest.raises(ValueError, match="содержимое"):
        _message(deleted_at=NOW + timedelta(seconds=1))


def test_системное_сообщение_нельзя_принять_от_клиента():
    system = _message(kind=MessageKind.SYSTEM, payload=MessagePayload(text="вступил"))
    assert not system.is_client_kind
    assert _message().is_client_kind


def test_сообщение_и_payload_неизменяемы():
    message = _message()
    with pytest.raises(FrozenInstanceError):
        message.conversation_seq = ConversationSeq(2)  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        message.payload.text = "другое"  # type: ignore[misc]
