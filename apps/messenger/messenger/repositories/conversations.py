"""Хранение бесед и членства. Сценарии создания живут уровнем выше."""
from __future__ import annotations

import asyncpg

from messenger.domain.conversation import (
    Conversation,
    ConversationMember,
    ConversationType,
    EnsureConversationResult,
    MemberRole,
)
from messenger.domain.ids import ConversationId, ConversationSeq, UserId


def _to_conversation(row: asyncpg.Record) -> Conversation:
    return Conversation(
        conversation_id=ConversationId(row["conversation_id"]),
        type=ConversationType(row["type"]),
        direct_key=row["direct_key"],
        last_seq=ConversationSeq(row["last_seq"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _to_member(row: asyncpg.Record) -> ConversationMember:
    return ConversationMember(
        conversation_id=ConversationId(row["conversation_id"]),
        user_id=UserId(row["user_id"]),
        role=MemberRole(row["role"]),
        joined_at=row["joined_at"],
        left_at=row["left_at"],
    )


async def insert_conversation(
    conn: asyncpg.Connection,
    *,
    conversation_id: ConversationId,
    type: ConversationType,
    direct_key: str | None = None,
) -> Conversation:
    """Записывает подготовленную беседу, не принимая продуктовых решений.

    Вычислить пару участников, проверить блокировку и решить, вернуть ли
    существующую беседу, обязан сервис G1-010/G1-011. Репозиторий делает
    одну вставку и оставляет ограничениям Postgres право отклонить дубль.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO conversations (conversation_id, type, direct_key)
        VALUES ($1, $2, $3)
        RETURNING conversation_id, type, direct_key, last_seq,
                  created_at, updated_at
        """,
        conversation_id,
        type.value,
        direct_key,
    )
    if row is None:
        raise RuntimeError("вставленная беседа не вернулась из Postgres")
    return _to_conversation(row)


async def fetch_conversation(
    conn: asyncpg.Connection, *, conversation_id: ConversationId
) -> Conversation | None:
    row = await conn.fetchrow(
        """
        SELECT conversation_id, type, direct_key, last_seq,
               created_at, updated_at
          FROM conversations
         WHERE conversation_id = $1
        """,
        conversation_id,
    )
    return _to_conversation(row) if row else None


async def fetch_direct_conversation(
    conn: asyncpg.Connection, *, direct_key: str
) -> Conversation | None:
    """Беседа по канонической паре участников."""
    row = await conn.fetchrow(
        """
        SELECT conversation_id, type, direct_key, last_seq,
               created_at, updated_at
          FROM conversations
         WHERE direct_key = $1
        """,
        direct_key,
    )
    return _to_conversation(row) if row else None


async def fetch_last_seq(
    conn: asyncpg.Connection, *, conversation_id: ConversationId
) -> ConversationSeq | None:
    """Последний выданный номер беседы — неподвижная точка синхронизации.

    Отдельная функция, хотя номер есть и у `fetch_conversation`, потому что
    читается она в своё время: **до** страницы догрузки, а не как её часть.
    `last_seq` становится видимым ровно в момент коммита транзакции, которая
    держала блокировку строки беседы, — значит `last_seq = k` наблюдаемо
    тогда и только тогда, когда наблюдаемо и сообщение `k`. Заморозив
    границу первой и прочитав страницу `seq <= границы` второй, мы не
    получаем дыр. В обратном порядке между двумя чтениями помещается чужое
    сообщение `k`: страница отдаёт `has_more=false` до `k−1`, клиент считает
    синхронизацию завершённой — и не видит `k` ни по REST, ни по потоку.
    Дефект этот на спокойной беседе не воспроизводится.
    """
    value = await conn.fetchval(
        "SELECT last_seq FROM conversations WHERE conversation_id = $1",
        conversation_id,
    )
    return ConversationSeq(value) if value is not None else None


async def ensure_direct_conversation(
    conn: asyncpg.Connection,
    *,
    conversation_id: ConversationId,
    direct_key: str,
) -> EnsureConversationResult:
    """Вставляет беседу пары или возвращает победителя встречной гонки.

    `ON CONFLICT`, а не предварительный SELECT: два процесса могут прочитать
    отсутствие одновременно. Проигравшая вставка ждёт коммита победителя,
    затем следующий запрос в READ COMMITTED видит уже готовую строку.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO conversations (conversation_id, type, direct_key)
        VALUES ($1, 'direct', $2)
        ON CONFLICT (direct_key) WHERE direct_key IS NOT NULL DO NOTHING
        RETURNING conversation_id, type, direct_key, last_seq,
                  created_at, updated_at
        """,
        conversation_id,
        direct_key,
    )
    if row is not None:
        return EnsureConversationResult(
            conversation=_to_conversation(row), created=True
        )

    existing = await fetch_direct_conversation(conn, direct_key=direct_key)
    if existing is None:
        raise RuntimeError("беседа исчезла после конфликта уникальности")
    return EnsureConversationResult(conversation=existing, created=False)


async def creation_blocked_between(
    conn: asyncpg.Connection, *, first: UserId, second: UserId
) -> bool:
    """Есть ли блокировка в любую сторону.

    Запрет записи симметричен: заблокированный не пишет заблокировавшему,
    но и заблокировавший не получает односторонний канал преследования.
    """
    return bool(
        await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                  FROM blocks
                 WHERE (blocker_id = $1 AND blocked_id = $2)
                    OR (blocker_id = $2 AND blocked_id = $1)
            )
            """,
            first,
            second,
        )
    )


async def add_member(
    conn: asyncpg.Connection,
    *,
    conversation_id: ConversationId,
    user_id: UserId,
    role: MemberRole = MemberRole.MEMBER,
) -> ConversationMember:
    """Добавляет членство. Повтор — нарушение уникальности, не обновление.

    Молчаливый upsert воскресил бы бывшего участника и стёр время выхода.
    Возврат в группу — отдельное доменное действие, которого в v1 нет.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO conversation_members (conversation_id, user_id, role)
        VALUES ($1, $2, $3)
        RETURNING conversation_id, user_id, role, joined_at, left_at
        """,
        conversation_id,
        user_id,
        role.value,
    )
    if row is None:
        raise RuntimeError("вставленное членство не вернулось из Postgres")
    return _to_member(row)


async def fetch_member(
    conn: asyncpg.Connection,
    *,
    conversation_id: ConversationId,
    user_id: UserId,
) -> ConversationMember | None:
    """Возвращает и действующего, и бывшего участника.

    `None` и `left_at != NULL` имеют разную семантику доступа к истории.
    Скрывать бывшего здесь значило бы потерять эту границу до authorize().
    """
    row = await conn.fetchrow(
        """
        SELECT conversation_id, user_id, role, joined_at, left_at
          FROM conversation_members
         WHERE conversation_id = $1 AND user_id = $2
        """,
        conversation_id,
        user_id,
    )
    return _to_member(row) if row else None


async def list_active_members(
    conn: asyncpg.Connection, *, conversation_id: ConversationId
) -> list[ConversationMember]:
    rows = await conn.fetch(
        """
        SELECT conversation_id, user_id, role, joined_at, left_at
          FROM conversation_members
         WHERE conversation_id = $1 AND left_at IS NULL
         ORDER BY joined_at, user_id
        """,
        conversation_id,
    )
    return [_to_member(row) for row in rows]


async def list_active_user_conversations(
    conn: asyncpg.Connection, *, user_id: UserId
) -> list[Conversation]:
    """Действующие беседы пользователя, недавно изменённые первыми."""
    rows = await conn.fetch(
        """
        SELECT c.conversation_id, c.type, c.direct_key, c.last_seq,
               c.created_at, c.updated_at
          FROM conversation_members cm
          JOIN conversations c ON c.conversation_id = cm.conversation_id
         WHERE cm.user_id = $1 AND cm.left_at IS NULL
         ORDER BY c.updated_at DESC, c.conversation_id
        """,
        user_id,
    )
    return [_to_conversation(row) for row in rows]


async def list_active_conversation_ids(
    conn: asyncpg.Connection, *, user_id: UserId, limit: int = 200
) -> list[ConversationId]:
    """Беседы, в которых человек состоит сейчас. Свежие сверху.

    Нужны для выдачи каналов в токене Centrifugo: клиент не выбирает
    канал сам — сервер перечисляет разрешённые явно, иначе подписка
    на чужую беседу сводится к знанию её идентификатора.

    Предел не формальность: список каналов уезжает в подписанный токен,
    и человек с тысячей бесед получил бы токен, который не проходит
    ни в один заголовок. Беседы сверх предела доберутся при следующей
    выдаче — она короткая и происходит на каждое соединение.
    """
    rows = await conn.fetch(
        """
        SELECT c.conversation_id
          FROM conversation_members m
          JOIN conversations c ON c.conversation_id = m.conversation_id
         WHERE m.user_id = $1
           AND m.left_at IS NULL
         ORDER BY c.updated_at DESC
         LIMIT $2
        """,
        user_id,
        limit,
    )
    return [ConversationId(row["conversation_id"]) for row in rows]
