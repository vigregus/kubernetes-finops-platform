"""Транзакционный outbox: запись, аренда пачки и отметка об отправке.

Сетевых вызовов здесь нет и быть не может. Публикация в Kafka живёт
в адаптере, и вызывается она **после** того, как транзакция аренды
закрыта: сетевой вызов внутри транзакции держал бы блокировки строк
ровно столько, сколько длится таймаут брокера.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

import asyncpg

from messenger.domain.ids import EventId, MessageId
from messenger.domain.outbox import OutboxRecord


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


def _to_record(row: asyncpg.Record) -> OutboxRecord:
    return OutboxRecord(
        id=row["id"],
        event_id=EventId(row["event_id"]),
        event_type=row["event_type"],
        event_version=row["event_version"],
        partition_key=row["partition_key"],
        payload=json.loads(row["payload"]),
        attempts=row["attempts"],
        created_at=row["created_at"],
    )


async def claim_batch(
    conn: asyncpg.Connection,
    *,
    owner: str,
    limit: int,
    lease_seconds: int,
) -> list[OutboxRecord]:
    """Берёт в аренду пачку неопубликованных записей.

    `FOR UPDATE SKIP LOCKED`: параллельные отправители не ждут друг друга
    и не берут одни и те же записи. Без `SKIP LOCKED` второй отправитель
    вставал бы в очередь за первым, и две реплики работали бы медленнее
    одной.

    Аренда, а не блокировка на время отправки: транзакция закрывается
    сразу, и сетевой вызов происходит уже без неё. Если отправитель умрёт
    между арендой и отметкой, запись вернётся в очередь по истечении
    `lease_until` — и будет опубликована повторно. Это осознанная цена
    «как минимум один раз»: дубль в Kafka отсеивает потребитель
    по `event_id`.

    Порядок по `id`: события одной беседы обязаны уйти в том же порядке,
    в каком записаны, иначе содержимое обгонит факт своего появления.
    """
    rows = await conn.fetch(
        """
        WITH claimed AS (
            SELECT id
              FROM outbox
             WHERE published_at IS NULL
               AND (lease_until IS NULL OR lease_until < now())
             ORDER BY id
             FOR UPDATE SKIP LOCKED
             LIMIT $2
        )
        UPDATE outbox
           SET lease_owner = $1,
               lease_until = now() + make_interval(secs => $3)
         WHERE id IN (SELECT id FROM claimed)
        RETURNING id, event_id, event_type, event_version, partition_key,
                  payload::text AS payload, attempts, created_at
        """,
        owner,
        limit,
        lease_seconds,
    )
    return sorted((_to_record(row) for row in rows), key=lambda r: r.id)


async def mark_published(
    conn: asyncpg.Connection, *, ids: Sequence[int], owner: str
) -> int:
    """Отмечает записи отправленными. Возвращает число отмеченных.

    `lease_owner` в условии — не формальность: если аренда успела истечь
    и запись забрал другой отправитель, отмечать её от своего имени
    нельзя. Иначе тот, второй, опубликует её ещё раз уже после отметки,
    и дубль уйдёт в поток без всякого следа в базе.
    """
    if not ids:
        return 0
    rows = await conn.fetch(
        """
        UPDATE outbox
           SET published_at = now(),
               lease_owner = NULL,
               lease_until = NULL,
               last_error = NULL
         WHERE id = ANY($1::bigint[])
           AND published_at IS NULL
           AND lease_owner = $2
        RETURNING id
        """,
        list(ids),
        owner,
    )
    return len(rows)


async def record_failure(
    conn: asyncpg.Connection,
    *,
    record_id: int,
    owner: str,
    error: str,
    retry_after: timedelta,
) -> None:
    """Считает неудачу и отодвигает следующую попытку.

    Аренда не снимается, а продлевается на время отсрочки: свободная
    запись была бы немедленно взята этим же отправителем в следующем
    же цикле, и отсрочка не значила бы ничего.

    Текст ошибки обрезается: `last_error` читают глазами, а не
    разбирают, и мегабайтная трасса в строке таблицы мешает и тому,
    и другому.
    """
    await conn.execute(
        """
        UPDATE outbox
           SET attempts = attempts + 1,
               last_error = left($3, 500),
               lease_until = now() + make_interval(secs => $4)
         WHERE id = $1
           AND lease_owner = $2
           AND published_at IS NULL
        """,
        record_id,
        owner,
        error,
        retry_after.total_seconds(),
    )


async def oldest_pending_age_seconds(conn: asyncpg.Connection) -> float:
    """Возраст самой старой неотправленной записи.

    Главный показатель здоровья отправителя, и именно он, а не число
    записей: очередь из тысячи событий возрастом в секунду — норма,
    одна запись возрастом в час — отказ.
    """
    value = await conn.fetchval(
        """
        SELECT EXTRACT(EPOCH FROM (now() - MIN(created_at)))
          FROM outbox
         WHERE published_at IS NULL
        """
    )
    return float(value or 0.0)


async def pending_count(conn: asyncpg.Connection) -> int:
    count = await conn.fetchval(
        "SELECT count(*) FROM outbox WHERE published_at IS NULL"
    )
    return int(count or 0)
