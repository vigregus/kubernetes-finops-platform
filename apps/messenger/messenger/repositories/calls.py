"""Звонки и их участники; транзакцией владеет сервис.

`call_participants` хранит **только живые** звонки (миграция `0016`): строка
появляется вместе со звонком и исчезает в той же транзакции, что завершает его.
Поэтому «один живой звонок на человека» — ограничение базы, а подметальщик
ищет по таблице, размер которой равен числу звонящих сейчас.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

import asyncpg

from messenger.domain.call import Call, CallKind, CallState, EndReason

_COLUMNS = """
    call_id, conversation_id, caller_id, callee_id, kind, state, version,
    signal_seq, end_reason, created_at, accepted_at, active_at, ended_at,
    last_keepalive_at, accepted_by
"""


class LiveCallExists(Exception):
    """Нарушено `UNIQUE (user_id)`: у кого-то из двоих уже есть живой звонок."""


def _to_call(row: asyncpg.Record) -> Call:
    return Call(
        call_id=row["call_id"],
        conversation_id=row["conversation_id"],
        caller_id=row["caller_id"],
        callee_id=row["callee_id"],
        kind=CallKind(row["kind"]),
        state=CallState(row["state"]),
        version=row["version"],
        signal_seq=row["signal_seq"],
        end_reason=EndReason(row["end_reason"]) if row["end_reason"] else None,
        created_at=row["created_at"],
        accepted_at=row["accepted_at"],
        active_at=row["active_at"],
        ended_at=row["ended_at"],
        last_keepalive_at=row["last_keepalive_at"],
        accepted_by=row["accepted_by"],
    )


async def callee_reachable(
    conn: asyncpg.Connection, *, user_id: uuid.UUID, window: timedelta
) -> bool:
    """Есть ли у человека живое **соединение звонков**.

    По реестру соединений, а не по присутствию «был в сети»: звонок в пустую
    вкладку звонил бы 30 секунд впустую. И именно соединение звонков, а не любое:
    у звонка свой канал без истории, и входящий, опубликованный при живом сокете
    беседы и мёртвом сокете звонков, потерялся бы навсегда.
    """
    row = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM realtime_connections
             WHERE user_id = $1 AND calls AND refreshed_at >= now() - $2::interval
        )
        """,
        user_id,
        window,
    )
    return bool(row)


async def insert_live(
    conn: asyncpg.Connection,
    *,
    call_id: uuid.UUID,
    conversation_id: uuid.UUID,
    caller_id: uuid.UUID,
    callee_id: uuid.UUID,
    kind: CallKind,
) -> Call:
    """Новый звонок и обе строки участников. Занятый участник — `LiveCallExists`."""
    try:
        row = await conn.fetchrow(
            f"""
            INSERT INTO calls (call_id, conversation_id, caller_id, callee_id, kind)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING {_COLUMNS}
            """,  # noqa: S608 - подставляется только _COLUMNS
            call_id, conversation_id, caller_id, callee_id, kind.value,
        )
        await conn.execute(
            """
            INSERT INTO call_participants (call_id, user_id, role)
            VALUES ($1, $2, 'caller'), ($1, $3, 'callee')
            """,
            call_id, caller_id, callee_id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise LiveCallExists from exc
    if row is None:
        raise RuntimeError("вставленный звонок не вернулся из Postgres")
    return _to_call(row)


async def insert_ended(
    conn: asyncpg.Connection,
    *,
    call_id: uuid.UUID,
    conversation_id: uuid.UUID,
    caller_id: uuid.UUID,
    callee_id: uuid.UUID,
    kind: CallKind,
    reason: EndReason,
) -> Call:
    """Звонок, закончившийся, не начавшись («занято», «не в сети»): участников нет."""
    row = await conn.fetchrow(
        f"""
        INSERT INTO calls (call_id, conversation_id, caller_id, callee_id, kind,
                           state, end_reason, ended_at)
        VALUES ($1, $2, $3, $4, $5, 'ended', $6, now())
        RETURNING {_COLUMNS}
        """,  # noqa: S608
        call_id, conversation_id, caller_id, callee_id, kind.value, reason.value,
    )
    if row is None:
        raise RuntimeError("вставленный звонок не вернулся из Postgres")
    return _to_call(row)


async def fetch(conn: asyncpg.Connection, call_id: uuid.UUID, *, lock: bool = False) -> Call | None:
    row = await conn.fetchrow(
        f"SELECT {_COLUMNS} FROM calls WHERE call_id = $1" + (" FOR UPDATE" if lock else ""),  # noqa: S608
        call_id,
    )
    return _to_call(row) if row else None


async def fetch_live_for_user(conn: asyncpg.Connection, user_id: uuid.UUID) -> Call | None:
    row = await conn.fetchrow(
        f"""
        SELECT {', '.join('c.' + c.strip() for c in _COLUMNS.split(','))}
          FROM call_participants p
          JOIN calls c ON c.call_id = p.call_id
         WHERE p.user_id = $1
        """,  # noqa: S608
        user_id,
    )
    return _to_call(row) if row else None


async def apply(
    conn: asyncpg.Connection,
    call_id: uuid.UUID,
    *,
    state: CallState,
    reason: EndReason | None = None,
    accepted_by: str | None = None,
) -> Call:
    """Записывает переход: состояние, время, номер; при конце — снимает участников."""
    row = await conn.fetchrow(
        f"""
        UPDATE calls
           SET state = $2,
               end_reason = $3,
               version = version + 1,
               accepted_at = CASE WHEN $2 = 'accepted' THEN now() ELSE accepted_at END,
               active_at = CASE WHEN $2 = 'active' THEN now() ELSE active_at END,
               ended_at = CASE WHEN $2 = 'ended' THEN now() ELSE ended_at END,
               last_keepalive_at = now(),
               accepted_by = CASE WHEN $2 = 'accepted' THEN $4 ELSE accepted_by END
         WHERE call_id = $1
        RETURNING {_COLUMNS}
        """,  # noqa: S608
        call_id, state.value, reason.value if reason else None, accepted_by,
    )
    if row is None:
        raise RuntimeError("звонок исчез во время перехода")
    if state is CallState.ENDED:
        await conn.execute("DELETE FROM call_participants WHERE call_id = $1", call_id)
    return _to_call(row)


async def next_signal_seq(conn: asyncpg.Connection, call_id: uuid.UUID) -> int:
    """Выдаёт следующий номер сигнала. Сервер, а не клиент: клиенту порядок не доверяем."""
    value = await conn.fetchval(
        "UPDATE calls SET signal_seq = signal_seq + 1 WHERE call_id = $1 RETURNING signal_seq",
        call_id,
    )
    if value is None:
        raise RuntimeError("звонок исчез до выдачи номера сигнала")
    return int(value)


async def touch(conn: asyncpg.Connection, call_id: uuid.UUID) -> None:
    await conn.execute("UPDATE calls SET last_keepalive_at = now() WHERE call_id = $1", call_id)


async def set_connection_type(conn: asyncpg.Connection, call_id: uuid.UUID, value: str) -> None:
    await conn.execute(
        "UPDATE calls SET connection_type = $2 WHERE call_id = $1 AND connection_type IS NULL",
        call_id, value,
    )


async def set_summary(conn: asyncpg.Connection, call_id: uuid.UUID, message_id: uuid.UUID) -> None:
    await conn.execute(
        "UPDATE calls SET summary_message_id = $2 WHERE call_id = $1", call_id, message_id
    )


async def claim_live(conn: asyncpg.Connection, *, limit: int) -> list[Call]:
    """Живые звонки для проверки сроков; чужие блокировки пропускаются.

    Читаются только строки из `call_participants` — то есть живые звонки, а не
    вся история. `SKIP LOCKED` позволяет двум подметальщикам не мешать друг
    другу и не ждать звонок, который сейчас принимает человек.
    """
    rows = await conn.fetch(
        f"""
        SELECT {', '.join('c.' + c.strip() for c in _COLUMNS.split(','))}
          FROM calls c
         WHERE c.call_id IN (SELECT call_id FROM call_participants)
         ORDER BY c.created_at
         LIMIT $1
         FOR UPDATE OF c SKIP LOCKED
        """,  # noqa: S608
        limit,
    )
    return [_to_call(row) for row in rows]
