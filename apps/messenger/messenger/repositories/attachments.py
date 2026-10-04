"""Хранение вложений; транзакцией и порядком действий владеет сервис."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

import asyncpg

from messenger.domain.attachment import Attachment, AttachmentState, RejectionCode
from messenger.domain.ids import AttachmentId, MessageId, UserId

_COLUMNS = """
    attachment_id, uploader_id, message_id, state, bucket, object_key,
    content_type, size_bytes, created_at, file_name, detected_content_type,
    rejection_reason, attempts, duration_ms, bitrate_kbps,
    thumbnail_key, width, height
"""


_PREFIXED = ", ".join("a." + c.strip() for c in _COLUMNS.split(",") if c.strip())


def _to_attachment(row: asyncpg.Record) -> Attachment:
    return Attachment(
        attachment_id=AttachmentId(row["attachment_id"]),
        uploader_id=UserId(row["uploader_id"]),
        message_id=MessageId(row["message_id"]) if row["message_id"] else None,
        state=AttachmentState(row["state"]),
        bucket=row["bucket"],
        object_key=row["object_key"],
        content_type=row["content_type"],
        size_bytes=row["size_bytes"],
        created_at=row["created_at"],
        file_name=row["file_name"],
        detected_content_type=row["detected_content_type"],
        rejection_reason=(
            RejectionCode(row["rejection_reason"]) if row["rejection_reason"] else None
        ),
        attempts=row["attempts"],
        duration_ms=row["duration_ms"],
        bitrate_kbps=row["bitrate_kbps"],
        thumbnail_key=row["thumbnail_key"],
        width=row["width"],
        height=row["height"],
    )


async def insert_pending(
    conn: asyncpg.Connection,
    *,
    attachment_id: AttachmentId,
    uploader_id: UserId,
    bucket: str,
    object_key: str,
    content_type: str,
    size_bytes: int,
    file_name: str | None,
    duration_ms: int | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO attachments (
            attachment_id, uploader_id, state, bucket, object_key,
            content_type, size_bytes, file_name, duration_ms
        )
        VALUES ($1, $2, 'pending', $3, $4, $5, $6, $7, $8)
        """,
        attachment_id,
        uploader_id,
        bucket,
        object_key,
        content_type,
        size_bytes,
        file_name,
        duration_ms,
    )


async def fetch(
    conn: asyncpg.Connection, *, attachment_id: AttachmentId, for_update: bool = False
) -> Attachment | None:
    row = await conn.fetchrow(
        f"SELECT {_COLUMNS} FROM attachments WHERE attachment_id = $1"  # noqa: S608
        + (" FOR UPDATE" if for_update else ""),
        attachment_id,
    )
    return _to_attachment(row) if row else None


async def mark_processing(
    conn: asyncpg.Connection,
    *,
    attachment_id: AttachmentId,
    size_bytes: int,
    bitrate_kbps: int | None = None,
) -> bool:
    """`pending → processing`. `False`, если вложение уже не `pending`.

    Размер записывается тот, что показало хранилище: заявленный клиентом —
    обещание, фактический — факт, и дальше живёт только факт.
    """
    result = await conn.execute(
        """
        UPDATE attachments
           SET state = 'processing', size_bytes = $2, bitrate_kbps = $3
         WHERE attachment_id = $1 AND state = 'pending'
        """,
        attachment_id,
        size_bytes,
        bitrate_kbps,
    )
    return result.endswith(" 1")


async def finish(
    conn: asyncpg.Connection,
    *,
    attachment_id: AttachmentId,
    state: AttachmentState,
    detected_content_type: str | None = None,
    rejection_reason: RejectionCode | None = None,
    thumbnail_key: str | None = None,
    width: int | None = None,
    height: int | None = None,
) -> None:
    """Итог обработки. Аренда снимается: у законченной строки владельца нет."""
    await conn.execute(
        """
        UPDATE attachments
           SET state = $2,
               detected_content_type = $3,
               rejection_reason = $4,
               thumbnail_key = $5,
               width = $6,
               height = $7,
               processed_at = now(),
               lease_owner = NULL,
               lease_until = NULL
         WHERE attachment_id = $1 AND state = 'processing'
        """,
        attachment_id,
        state.value,
        detected_content_type,
        rejection_reason.value if rejection_reason else None,
        thumbnail_key,
        width,
        height,
    )


async def claim_processing(
    conn: asyncpg.Connection, *, owner: str, lease: timedelta, limit: int
) -> list[Attachment]:
    """Берёт пачку на обработку в аренду. `SKIP LOCKED`, как у outbox."""
    rows = await conn.fetch(
        f"""
        WITH picked AS (
            SELECT attachment_id
              FROM attachments
             WHERE state = 'processing'
               AND (lease_until IS NULL OR lease_until < now())
             ORDER BY created_at
             LIMIT $3
               FOR UPDATE SKIP LOCKED
        )
        UPDATE attachments a
           SET lease_owner = $1,
               lease_until = now() + $2::interval,
               attempts = a.attempts + 1
          FROM picked
         WHERE a.attachment_id = picked.attachment_id
        RETURNING {_PREFIXED}
        """,  # noqa: S608 - подставляется только _COLUMNS
        owner,
        lease,
        limit,
    )
    return [_to_attachment(row) for row in rows]


async def release_with_delay(
    conn: asyncpg.Connection, *, attachment_id: AttachmentId, delay: timedelta
) -> None:
    """Вернуть в очередь не раньше чем через `delay`: сканер ещё не отвечает."""
    await conn.execute(
        """
        UPDATE attachments
           SET lease_owner = NULL, lease_until = now() + $2::interval
         WHERE attachment_id = $1 AND state = 'processing'
        """,
        attachment_id,
        delay,
    )


async def lock_ready_for_message(
    conn: asyncpg.Connection, *, ids: Sequence[AttachmentId], uploader_id: UserId
) -> list[Attachment]:
    """Готовые, свои и ещё не прикреплённые — заблокированные до коммита.

    Блокировка нужна, чтобы два сообщения не прикрепили одно вложение: второе
    дождётся первого и увидит `attached`.
    """
    rows = await conn.fetch(
        f"""
        SELECT {_COLUMNS}
          FROM attachments
         WHERE attachment_id = ANY($1::uuid[])
           AND uploader_id = $2
           AND state = 'ready'
           FOR UPDATE
        """,  # noqa: S608
        list(ids),
        uploader_id,
    )
    return [_to_attachment(row) for row in rows]


async def attach(
    conn: asyncpg.Connection, *, ids: Sequence[AttachmentId], message_id: MessageId
) -> None:
    await conn.execute(
        """
        UPDATE attachments SET state = 'attached', message_id = $2
         WHERE attachment_id = ANY($1::uuid[]) AND state = 'ready'
        """,
        list(ids),
        message_id,
    )


async def list_for_messages(
    conn: asyncpg.Connection, *, message_ids: Sequence[MessageId]
) -> dict[MessageId, list[Attachment]]:
    if not message_ids:
        return {}
    rows = await conn.fetch(
        f"""
        SELECT {_COLUMNS}
          FROM attachments
         WHERE message_id = ANY($1::uuid[]) AND state = 'attached'
         ORDER BY created_at, attachment_id
        """,  # noqa: S608
        list(message_ids),
    )
    grouped: dict[MessageId, list[Attachment]] = {}
    for row in rows:
        attachment = _to_attachment(row)
        assert attachment.message_id is not None  # noqa: S101 - гарантирует CHECK из 0013
        grouped.setdefault(attachment.message_id, []).append(attachment)
    return grouped


async def oldest_processing_age_seconds(conn: asyncpg.Connection) -> float:
    value = await conn.fetchval(
        """
        SELECT COALESCE(EXTRACT(EPOCH FROM now() - min(created_at)), 0)
          FROM attachments WHERE state = 'processing'
        """
    )
    return float(value)


async def claim_orphans(
    conn: asyncpg.Connection, *, older_than: timedelta, limit: int
) -> list[Attachment]:
    """Неприкреплённые старше срока: для уборки (`ATT-001`).

    Берутся `pending`, `ready`, `rejected`, `failed` — всё, что не стало
    частью сообщения. `processing` не трогается: его ведёт воркер.
    """
    rows = await conn.fetch(
        f"""
        SELECT {_COLUMNS}
          FROM attachments
         WHERE state IN ('pending', 'ready', 'rejected', 'failed')
           AND created_at < now() - $1::interval
         ORDER BY created_at
         LIMIT $2
           FOR UPDATE SKIP LOCKED
        """,  # noqa: S608
        older_than,
        limit,
    )
    return [_to_attachment(row) for row in rows]


async def mark_erased(conn: asyncpg.Connection, *, attachment_id: AttachmentId) -> None:
    await conn.execute(
        """
        UPDATE attachments
           SET state = 'erased', erased_at = now()
         WHERE attachment_id = $1 AND message_id IS NULL
        """,
        attachment_id,
    )
