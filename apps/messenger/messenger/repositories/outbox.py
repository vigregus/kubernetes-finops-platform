"""Транзакционный outbox: только запись, без сетевых вызовов."""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import asyncpg

from messenger.domain.ids import EventId, MessageId


async def insert_event(
    conn: asyncpg.Connection,
    *,
    aggregate_id: MessageId,
    event_id: EventId,
    event_type: str,
    partition_key: str,
    payload: Mapping[str, Any],
) -> None:
    await conn.execute(
        """
        INSERT INTO outbox (
            aggregate_type, aggregate_id, event_type, event_version,
            partition_key, event_id, payload
        )
        VALUES ('message', $1, $2, 1, $3, $4, $5::jsonb)
        """,
        aggregate_id,
        event_type,
        partition_key,
        event_id,
        json.dumps(payload, ensure_ascii=False),
    )

