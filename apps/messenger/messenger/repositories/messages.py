"""Хранение сообщений; транзакцией и порядком действий владеет сервис."""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import asyncpg

from messenger.domain.ids import (
    ClientMessageId,
    ConversationId,
    ConversationSeq,
    MessageId,
    UserId,
)
from messenger.domain.message import Message, MessageKind, MessagePayload


def _to_message(row: asyncpg.Record) -> Message:
    raw_payload = row["payload"]
    payload: Mapping[str, Any] = (
        json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
    )
    return Message(
        message_id=MessageId(row["message_id"]),
        conversation_id=ConversationId(row["conversation_id"]),
        conversation_seq=ConversationSeq(row["conversation_seq"]),
        sender_id=UserId(row["sender_id"]),
        client_message_id=ClientMessageId(row["client_message_id"]),
        kind=MessageKind(row["type"]),
        payload=MessagePayload(
            text=payload.get("text"),
            duration_ms=payload.get("duration_ms"),
        ),
        reply_to_message_id=(
            MessageId(row["reply_to_message_id"])
            if row["reply_to_message_id"] is not None
            else None
        ),
        created_at=row["created_at"],
        edited_at=row["edited_at"],
        deleted_at=row["deleted_at"],
    )


async def lock_active_conversation(
    conn: asyncpg.Connection,
    *,
    conversation_id: ConversationId,
    sender_id: UserId,
) -> ConversationSeq | None:
    """Блокирует строку беседы и возвращает последний выданный номер.

    Одна строка — один счётчик. Поэтому две реплики API получают разные
    последовательные номера независимо от порядка прихода к процессам.
    Бывший участник и посторонний не проходят условие ``left_at IS NULL``.
    """
    value = await conn.fetchval(
        """
        SELECT c.last_seq
          FROM conversations c
          JOIN conversation_members cm
            ON cm.conversation_id = c.conversation_id
           AND cm.user_id = $2
           AND cm.left_at IS NULL
         WHERE c.conversation_id = $1
           FOR UPDATE OF c
        """,
        conversation_id,
        sender_id,
    )
    return ConversationSeq(value) if value is not None else None


async def fetch_by_client_id(
    conn: asyncpg.Connection,
    *,
    conversation_id: ConversationId,
    sender_id: UserId,
    client_message_id: ClientMessageId,
) -> Message | None:
    row = await conn.fetchrow(
        """
        SELECT message_id, conversation_id, conversation_seq, sender_id,
               client_message_id, type, payload, reply_to_message_id,
               created_at, edited_at, deleted_at
          FROM messages
         WHERE conversation_id = $1
           AND sender_id = $2
           AND client_message_id = $3
        """,
        conversation_id,
        sender_id,
        client_message_id,
    )
    return _to_message(row) if row else None


async def allocate_sequence(
    conn: asyncpg.Connection, *, conversation_id: ConversationId
) -> ConversationSeq:
    value = await conn.fetchval(
        """
        UPDATE conversations
           SET last_seq = last_seq + 1,
               updated_at = now()
         WHERE conversation_id = $1
        RETURNING last_seq
        """,
        conversation_id,
    )
    if value is None:
        raise RuntimeError("заблокированная беседа исчезла до выдачи номера")
    return ConversationSeq(value)


async def insert_message(
    conn: asyncpg.Connection,
    *,
    message_id: MessageId,
    conversation_id: ConversationId,
    conversation_seq: ConversationSeq,
    sender_id: UserId,
    client_message_id: ClientMessageId,
    kind: MessageKind,
    payload: MessagePayload,
    reply_to_message_id: MessageId | None = None,
) -> Message:
    row = await conn.fetchrow(
        """
        INSERT INTO messages (
            message_id, conversation_id, conversation_seq, sender_id,
            client_message_id, type, payload, reply_to_message_id
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)
        RETURNING message_id, conversation_id, conversation_seq, sender_id,
                  client_message_id, type, payload, reply_to_message_id,
                  created_at, edited_at, deleted_at
        """,
        message_id,
        conversation_id,
        conversation_seq,
        sender_id,
        client_message_id,
        kind.value,
        json.dumps(
            {key: value for key, value in {
                "text": payload.text,
                "duration_ms": payload.duration_ms,
            }.items() if value is not None},
            ensure_ascii=False,
        ),
        reply_to_message_id,
    )
    if row is None:
        raise RuntimeError("вставленное сообщение не вернулось из Postgres")
    return _to_message(row)

