"""Сообщение и его содержимое без знания о Postgres, HTTP и Kafka.

Порядок задаёт серверный ``conversation_seq``, а повтор запроса узнаётся по
``client_message_id``. Это разные идентификаторы с разными обязанностями:
первый упорядочивает историю, второй не позволяет таймауту создать дубль.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from messenger.domain.ids import (
    ClientMessageId,
    ConversationId,
    ConversationSeq,
    MessageId,
    UserId,
)

MAX_TEXT_LENGTH = 4096


class MessageKind(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    VOICE = "voice"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class MessagePayload:
    """Содержимое, которое хранится в Postgres и отдельном Kafka-топике."""

    text: str | None = None
    duration_ms: int | None = None

    def __post_init__(self) -> None:
        if self.text is not None and len(self.text) > MAX_TEXT_LENGTH:
            raise ValueError(f"текст не может быть длиннее {MAX_TEXT_LENGTH} символов")
        if self.duration_ms is not None and self.duration_ms <= 0:
            raise ValueError("длительность должна быть положительной")


@dataclass(frozen=True, slots=True)
class Message:
    message_id: MessageId
    conversation_id: ConversationId
    conversation_seq: ConversationSeq
    sender_id: UserId
    client_message_id: ClientMessageId
    kind: MessageKind
    payload: MessagePayload
    created_at: datetime
    reply_to_message_id: MessageId | None = None
    edited_at: datetime | None = None
    deleted_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.conversation_seq < 1:
            raise ValueError("порядковый номер сообщения должен быть положительным")
        if self.edited_at is not None and self.edited_at < self.created_at:
            raise ValueError("правка не может предшествовать созданию")
        if self.deleted_at is not None and self.deleted_at < self.created_at:
            raise ValueError("удаление не может предшествовать созданию")
        if self.deleted_at is not None and self.payload != MessagePayload():
            raise ValueError("удалённое сообщение не должно хранить содержимое")
        if self.deleted_at is not None:
            return

        if self.kind is MessageKind.TEXT and not (self.payload.text or "").strip():
            raise ValueError("текстовое сообщение требует непустой текст")
        if self.kind is MessageKind.VOICE and self.payload.duration_ms is None:
            raise ValueError("голосовое сообщение требует длительность")
        if self.kind is not MessageKind.VOICE and self.payload.duration_ms is not None:
            raise ValueError("длительность разрешена только для голосового сообщения")

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    @property
    def visible_payload(self) -> MessagePayload | None:
        """Надгробие остаётся в истории, но содержимое наружу не выходит."""

        return None if self.is_deleted else self.payload

    @property
    def is_client_kind(self) -> bool:
        """``system`` создаёт только сервер, но хранится той же сущностью."""

        return self.kind is not MessageKind.SYSTEM
