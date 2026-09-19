"""Хранение сообщений; транзакцией и порядком действий владеет сервис."""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import asyncpg

from messenger.domain.history import MAX_SEQ, MessagePage
from messenger.domain.ids import (
    ClientMessageId,
    ConversationId,
    ConversationSeq,
    MessageId,
    UserId,
)
from messenger.domain.message import Message, MessageKind, MessagePayload
from messenger.telemetry import trace

# Список колонок один на все запросы модуля: он обязан совпадать с тем,
# что читает `_to_message`. Разъехавшиеся копии ловятся не здесь и не
# сразу: `_to_message` читает строку по именам, поэтому колонка, забытая
# в одной из четырёх выборок, — это `KeyError` при ответе на конкретном
# маршруте, а не отказ при импорте. Один список убирает и копии, и
# вопрос «а та же ли тут форма строки, что в соседней функции».
_COLUMNS = """
    message_id, conversation_id, conversation_seq, sender_id,
    client_message_id, type, payload, reply_to_message_id,
    created_at, edited_at, deleted_at
"""

# Две верхние границы для обратной выборки, и различие между ними — не
# косметика. Курсор клиента **исключает** свой номер: на нестрогом последний
# элемент страницы попал бы ещё и в следующую, и `HIST-001` покраснел бы на
# стыке. А «самые новые» — наоборот, **включают** максимум: строгая граница
# на `MAX_SEQ` отсекла бы законный номер `2**63 - 1`, который схема не
# запрещает, и сообщение с ним исчезло бы из истории молча — страница просто
# оказалась бы короче на один.
#
# Оба условия — простое сравнение по индексируемой колонке, а не
# `COALESCE($2, $3)` или `($2 IS NULL OR ...)`: там планировщик читает индекс
# целиком и отбрасывает лишнее после, а на
# `messages_conversation_seq_idx (conversation_id, conversation_seq DESC)`
# обратная страница обязана сниматься одним проходом.
_BELOW_MAX = "conversation_seq <= $2"
_BELOW_CURSOR = "conversation_seq < $2"


def _to_message(row: asyncpg.Record) -> Message:
    raw_payload = row["payload"]
    # `payload IS NULL` — не редкий случай, а гарантия схемы: ограничение
    # `messages_payload_matches_state` (db/migrations/0002_erasure.sql)
    # требует ровно этого от надгробия. Без `or {}` чтение удалённого
    # сообщения падает на `None.get(...)` — то есть история ломается
    # ровно там, где обязана показать, что сообщение удалено.
    payload: Mapping[str, Any] = (
        json.loads(raw_payload)
        if isinstance(raw_payload, str)
        else (raw_payload or {})
    )
    # `sender_id` тоже потерял `NOT NULL` (0002_erasure.sql), и `UserId`
    # этого не поймает: `NewType` не проверяет ничего и молча пропустит
    # `None`, который уедет в ответ строкой при `format: uuid` в контракте.
    # Сегодня недостижимо — пути стирания учётной записи в коде нет, —
    # и потому оставлено здесь словами, а не тихим умолчанием.
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


def _page(rows: list[asyncpg.Record], limit: int) -> MessagePage:
    """Лишняя строка — единственное доказательство «есть ещё».

    Режется здесь и только здесь: `limit + 1` не покидает репозиторий.
    Если взять курсором пробную строку, клиент перескочит `limit`-й
    элемент и потеряет его навсегда — на короткой беседе это не
    воспроизводится. `len(items) == limit` признаком не является: ровно
    исчерпанный диапазон объявил бы «есть ещё», и клиент сделал бы лишний
    пустой запрос.
    """
    return MessagePage(
        items=tuple(_to_message(row) for row in rows[:limit]),
        has_more=len(rows) > limit,
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

    Текст запроса несёт `/* trace_id=... */` первым комментарием — это
    единственное место в репозиториях, где он есть: promtail уже умеет
    вынимать такой комментарий из `log_min_duration_statement`
    (../../gitops/02-infra/observability-objects/promtail/values.yaml),
    но до сих пор не находил его ни в одном запросе мессенджера. Выбран
    именно этот запрос, а не обёртка над всем `conn`: он единственная
    блокировка на пути сообщения (`SELECT ... FOR UPDATE`), и без
    trace_id в его логе ожидание чужой блокировки неотличимо от
    медленного диска ни в логе, ни в спане (docs/messenger/
    06-observability.md, «Чего в этих спанах нет»). Комментарий с разным
    trace_id на каждый вызов делает текст запроса не-кешируемым
    подготовленным выражением asyncpg (`Connection._stmt_cache` ключуется
    точным текстом) - цена принята сознательно для этого одного запроса,
    а не распространена на весь `conn`, где она ударила бы по каждой
    вставке в конвейере сообщения без такой же отдачи.
    """
    trace_id = trace.current_trace_id()
    # Ровно один пробел после `*/`, не перенос строки: его и только его
    # ждёт регэксп promtail (`/\* trace_id=... \*/ `, буквально с одним
    # пробелом) - `query.strip()` убирает отступ и переносы шаблона,
    # чтобы после конкатенации текст начинался как раз с "SELECT",
    # а не с пустой строки перед ним.
    comment = f"/* trace_id={trace_id} */ " if trace_id else ""
    query = """
        SELECT c.last_seq
          FROM conversations c
          JOIN conversation_members cm
            ON cm.conversation_id = c.conversation_id
           AND cm.user_id = $2
           AND cm.left_at IS NULL
         WHERE c.conversation_id = $1
           FOR UPDATE OF c
    """
    value = await conn.fetchval(
        comment + query.strip(),
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
        f"""
        SELECT {_COLUMNS}
          FROM messages
         WHERE conversation_id = $1
           AND sender_id = $2
           AND client_message_id = $3
        """,  # noqa: S608 - подставляется только _COLUMNS, данных в тексте нет
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
        f"""
        INSERT INTO messages (
            message_id, conversation_id, conversation_seq, sender_id,
            client_message_id, type, payload, reply_to_message_id
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)
        RETURNING {_COLUMNS}
        """,  # noqa: S608 - подставляется только _COLUMNS, данных в тексте нет
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


async def fetch_page_backward(
    conn: asyncpg.Connection,
    *,
    conversation_id: ConversationId,
    before_seq: ConversationSeq | None,
    limit: int,
) -> MessagePage:
    """Страница от новых к старым; `before_seq` **исключает** свой номер.

    Строгое `<`, а не `<=`, — это и есть `HIST-001`: на нестрогом последний
    элемент страницы попадёт ещё и в следующую, и клиент увидит дубль на
    стыке, не заметив пропуска.

    Отсутствие курсора — не «нет условия», а «условие от максимума»: см.
    `_BELOW_MAX` и `_BELOW_CURSOR` о том, почему граница включается только
    здесь.

    Надгробия не отфильтрованы намеренно. Удалённое сообщение занимает свой
    слот и в нумерации, и в размере страницы (ADR 0004); `deleted_at IS NULL`
    в `WHERE` дал бы страницу короче `limit`, и клиент не отличил бы
    «обрезано удалением» от «конец истории».
    """
    predicate = _BELOW_CURSOR if before_seq is not None else _BELOW_MAX
    bound = before_seq if before_seq is not None else MAX_SEQ
    rows = await conn.fetch(
        f"""
        SELECT {_COLUMNS}
          FROM messages
         WHERE conversation_id = $1
           AND {predicate}
         ORDER BY conversation_seq DESC
         LIMIT $3
        """,  # noqa: S608 - подставляются _COLUMNS и оператор, данных в тексте нет
        conversation_id,
        bound,
        limit + 1,
    )
    return _page(rows, limit)


async def fetch_page_forward(
    conn: asyncpg.Connection,
    *,
    conversation_id: ConversationId,
    after_seq: ConversationSeq,
    through_seq: ConversationSeq,
    limit: int,
) -> MessagePage:
    """Страница по возрастанию; `after_seq` исключён, `through_seq` включён.

    Три оператора здесь разные, и каждый на своём месте. `>` на `>=` вернул
    бы первый элемент страницы дважды — при догрузке это выглядит как
    повтор доставленного сообщения. `<=` на `<` потерял бы саму границу
    снимка, а вместе с ней и случай `through_seq == after_seq` — законный
    пустой ответ на повтор после обрыва связи, а не ошибка.

    `through_seq` — не удобство, а замороженная граница: между страницами
    догрузки в беседу пишут, и без верхней границы `has_more` остался бы
    истинным вечно, а клиент не отличил бы «догнал» от «отстаю».

    `after_seq` не может быть `None`: направление «вперёд» без нижней
    границы не существует — это была бы первая страница листания назад,
    и вызывающий обязан выбрать её явно.
    """
    rows = await conn.fetch(
        f"""
        SELECT {_COLUMNS}
          FROM messages
         WHERE conversation_id = $1
           AND conversation_seq > $2
           AND conversation_seq <= $3
         ORDER BY conversation_seq ASC
         LIMIT $4
        """,  # noqa: S608 - подставляется только _COLUMNS, данных в тексте нет
        conversation_id,
        after_seq,
        through_seq,
        limit + 1,
    )
    return _page(rows, limit)


async def fetch_latest_by_conversation(
    conn: asyncpg.Connection, *, conversation_ids: Sequence[ConversationId]
) -> dict[ConversationId, Message]:
    """Последнее сообщение каждой из названных бесед — одним запросом.

    `DISTINCT ON (conversation_id)` с `ORDER BY conversation_id,
    conversation_seq DESC` повторяет ключ индекса
    `messages_conversation_seq_idx`, то есть даёт одну строку на беседу
    одним проходом, а не по запросу на беседу: список бесед — ровно тот
    случай, где «по запросу на элемент» превращается в N+1.

    Соблазн добавить `AND deleted_at IS NULL` выглядит улучшением, а на
    деле сдвинул бы «последнее» на предыдущее сообщение. Удалённое
    занимает свой номер (`ADR 0004`), поэтому беседа, в которой удалили
    последнее, обязана показать именно его — иначе список «оживёт»
    и расскажет про сообщение, которого клиент уже не увидит.

    Собирается общим `_to_message`: второй разбор строки разошёлся бы
    с первым ровно на надгробии — `payload IS NULL` уронил бы наивный
    `None.get(...)`.
    """
    if not conversation_ids:
        # Пустая страница — законный ответ, а не повод сходить в базу.
        return {}
    rows = await conn.fetch(
        f"""
        SELECT DISTINCT ON (conversation_id) {_COLUMNS}
          FROM messages
         WHERE conversation_id = ANY($1::uuid[])
         ORDER BY conversation_id, conversation_seq DESC
        """,  # noqa: S608 - подставляется только _COLUMNS, данных в тексте нет
        list(conversation_ids),
    )
    return {
        ConversationId(row["conversation_id"]): _to_message(row) for row in rows
    }

