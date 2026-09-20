"""Хранение состояния прочтения. Решений здесь нет — только запись и разбор.

Имя модуля — по таблице (`read_states`), а сервиса и домена — по понятию
(`receipts`). Расхождение осознанное и записано миграцией `0009`: наружу
уходит `read_seq` — то, что сообщило устройство, — а хранится
`last_read_seq`, максимум сообщённого. Репозиторий стоит на стороне базы и
говорит её именами; переименование происходит один раз, в `_to_read_state`.

Транзакцию репозиторий не открывает и теперь, когда её открывает сервис:
`INSERT … ON CONFLICT DO UPDATE` атомарен сам, и обёртка вокруг **одного**
оператора выглядела бы гарантией, которой не является. Транзакция
квитанции (`G3-003`) оборачивает два шага в двух таблицах, и предмет её —
порядок блокировок, а не атомарность этой записи.
"""
from __future__ import annotations

from collections.abc import Sequence

import asyncpg

from messenger.domain.ids import ConversationId, ConversationSeq, UserId
from messenger.domain.receipts import ReadState


def _to_read_state(row: asyncpg.Record) -> ReadState:
    return ReadState(
        delivered_seq=ConversationSeq(row["last_delivered_seq"]),
        read_seq=ConversationSeq(row["last_read_seq"]),
    )


async def upsert_read_state(
    conn: asyncpg.Connection,
    *,
    conversation_id: ConversationId,
    user_id: UserId,
    delivered_seq: ConversationSeq,
    read_seq: ConversationSeq,
) -> ReadState:
    """Пишет состояние и возвращает то, что получилось.

    Монотонность держится здесь, а не проверкой перед записью, и это не
    перенос ради переноса. Два устройства одного пользователя пишут в одну
    строку одновременно, и всякая схема «прочитать, сравнить, записать»
    теряет обновление проигравшего: между чтением и записью помещается
    чужая запись. `GREATEST` против хранимой строки такой промежуток не
    оставляет.

    Опирается это на свойство `ON CONFLICT DO UPDATE` в READ COMMITTED:
    выражения `SET` считаются по **самой свежей закоммиченной** версии
    строки, а не по снимку оператора. Проигравший арбитраж вставки ждёт на
    строчной блокировке победителя и пересчитывает `SET` уже по его
    результату — ровно так же, как безопасен `UPDATE t SET n = n + 1`.
    Наивное `SET last_read_seq = EXCLUDED.last_read_seq` этот довод не
    ломает, а тихо теряет значение: последовательный пересказ квитанций
    проходит, расхождение видно только под гонкой.

    `updated_at` выставляется в обеих ветках, и триггера на колонке нет.
    Пропуск в ветке обновления оставил бы несвежую отметку, которую
    способна оставить **только** она, — а в ветке вставки отсутствие
    обошлось бы умолчанием колонки, то есть асимметрия маскировала бы
    себя с одной стороны. Семантика выбранная — «время последней записи»:
    отметка двигается и тогда, когда `GREATEST` ничего не сдвинул.
    Читателя у неё не появилось и с проекцией непрочитанных (`G3-003`):
    та читает `last_read_seq`, а отметку — нет. Вопрос «последняя запись
    или последнее изменение» поэтому остаётся открытым не потому, что его
    отложили, а потому, что задать его по-прежнему некому.

    Блокировка строки беседы не берётся — намеренно не так, как в
    `lock_active_conversation`. Квитанция беседу не читает ради изменения
    и не меняет её; `FOR UPDATE` на горячей строке беседы поставил бы
    квитанции в очередь за каждой выдачей номера, ничего не защитив.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO read_states (
            conversation_id, user_id, last_delivered_seq, last_read_seq, updated_at
        )
        VALUES ($1, $2, $3, $4, now())
        ON CONFLICT (conversation_id, user_id) DO UPDATE
           SET last_delivered_seq = GREATEST(
                   read_states.last_delivered_seq, EXCLUDED.last_delivered_seq
               ),
               last_read_seq = GREATEST(
                   read_states.last_read_seq, EXCLUDED.last_read_seq
               ),
               updated_at = now()
        RETURNING last_delivered_seq, last_read_seq
        """,
        conversation_id,
        user_id,
        delivered_seq,
        read_seq,
    )
    if row is None:
        raise RuntimeError("записанное состояние не вернулось из Postgres")
    return _to_read_state(row)


async def fetch_read_states(
    conn: asyncpg.Connection,
    *,
    conversation_id: ConversationId,
    user_ids: Sequence[UserId],
) -> dict[UserId, ConversationSeq]:
    """Прочитанные номера названных участников — одним запросом.

    Отсутствие пользователя в ответе означает ноль, и это **не** то же
    самое, что ноль в строке: `COALESCE` внутри запроса вернул бы строку и
    для того, кто квитанции не присылал, — то есть соврал бы о наличии
    состояния. Различие живёт у вызывающего, где оно и проверяемо, а здесь
    остаётся ровно то, что лежит в таблице.

    Условие отбора идёт по первичному ключу `(conversation_id, user_id)`,
    то есть по индексу, а не сканированием: на событие беседы читаются
    состояния всех её получателей сразу, и запрос на каждого был бы тем
    самым N+1, от которого уходит список бесед.
    """
    if not user_ids:
        # Пустой запрос — законный ответ, а не повод сходить в базу.
        return {}
    rows = await conn.fetch(
        """
        SELECT user_id, last_read_seq
          FROM read_states
         WHERE conversation_id = $1
           AND user_id = ANY($2::uuid[])
        """,
        conversation_id,
        list(user_ids),
    )
    return {
        UserId(row["user_id"]): ConversationSeq(row["last_read_seq"])
        for row in rows
    }
