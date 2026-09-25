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
from messenger.domain.conversation_list import (
    ActivityCursor,
    ConversationPage,
    ConversationSummary,
)
from messenger.domain.ids import ConversationId, ConversationSeq, UserId
from messenger.domain.receipts import ParticipantReadState, ReadState
from messenger.domain.user import UserSummary


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


# Тело выборки одно на три варианта курсора: различаются только условие
# и число параметров, поэтому у них общий текст, а не три копии боковых
# соединений.
#
# Участники собираются массивами, а не `json_agg`: `json` драйвер
# отдаёт строкой, и разбирать её пришлось бы рядом со сборкой модели —
# там же, где ошибка разбора и обнаружилась бы. `uuid[]`, `text[]`
# и `bigint[]` он отдаёт готовыми списками. Порядок `joined_at, user_id` —
# тот же, что у `list_active_members`: своего порядка у участников нет,
# и выдача без него менялась бы между одинаковыми запросами. Число этих
# массивов здесь не названо намеренно — оно названо там, где из него
# следует обязательство (`_to_summary`, `zip(..., strict=True)`), и
# второе такое место разошлось бы с первым на первой же правке.
#
# `COALESCE` здесь не перестраховка. `array_agg` по нулю строк даёт NULL,
# а не пустой массив, и беседа без действующих участников уронила бы
# сборку ответа обходом `None`. По условию отбора ниже зритель сам
# действующий участник, поэтому ветка недостижима; оставлена по той же
# причине, что `payload or {}` в `_to_message`: неверный ответ вместо
# падения нельзя допускать даже там, куда сегодня попасть нельзя.
_SELECT = """
    SELECT c.conversation_id, c.type, c.direct_key, c.last_seq,
           c.created_at, c.updated_at,
           participants.ids   AS participant_ids,
           participants.names AS participant_names,
           participants.seen  AS participant_last_seen,
           participants.read_seqs      AS participant_read_seqs,
           participants.delivered_seqs AS participant_delivered_seqs
      FROM conversation_members cm
      JOIN conversations c ON c.conversation_id = cm.conversation_id
      LEFT JOIN LATERAL (
          SELECT COALESCE(
                     array_agg(u.user_id ORDER BY cm2.joined_at, cm2.user_id),
                     ARRAY[]::uuid[]
                 ) AS ids,
                 COALESCE(
                     array_agg(u.display_name ORDER BY cm2.joined_at, cm2.user_id),
                     ARRAY[]::text[]
                 ) AS names,
                 -- Отметка «был в сети» едет с участником, а не отдельным
                 -- запросом: роли участников уже соединены здесь, и второй
                 -- поход в базу за той же колонкой тех же строк был бы
                 -- лишним кругом ровно на том маршруте, ради цены которого
                 -- всё остальное здесь и сведено к двум запросам.
                 --
                 -- `array_agg` сохраняет NULL-элементы (`COALESCE` нужен
                 -- только против NULL-массива у беседы без участников),
                 -- поэтому `zip(..., strict=True)` в `_to_summary` не
                 -- собьётся на человеке, который ни разу не был в сети.
                 COALESCE(
                     array_agg(u.last_seen_at ORDER BY cm2.joined_at, cm2.user_id),
                     ARRAY[]::timestamptz[]
                 ) AS seen,
                 -- Состояние чтения едет тем же подзапросом и по тому же
                 -- доводу, что и отметка: строки участников уже соединены,
                 -- а третий круг за двумя числами на беседу был бы платой
                 -- за то, что здесь уже лежит.
                 --
                 -- Соединение **левое**, и в этом весь смысл: строки
                 -- `read_states` у человека может не быть вовсе, и она
                 -- отличается от строки из двух нулей. `JOIN` выбросил бы
                 -- такого человека из участников, а `COALESCE(..., 0)`
                 -- выдал бы за него квитанцию, которой он не присылал.
                 -- `LEFT JOIN` оставляет в колонке NULL, а `array_agg`
                 -- NULL-элементы сохраняет — значит различить их можно
                 -- уже в `_to_summary`, что там и делается.
                 --
                 -- Сортировка та же, что у остальных массивов: состояния
                 -- обязаны идти в порядке участников, иначе `zip` свёл бы
                 -- номер одного человека с идентификатором другого.
                 COALESCE(
                     array_agg(rs.last_read_seq ORDER BY cm2.joined_at, cm2.user_id),
                     ARRAY[]::bigint[]
                 ) AS read_seqs,
                 COALESCE(
                     array_agg(rs.last_delivered_seq ORDER BY cm2.joined_at, cm2.user_id),
                     ARRAY[]::bigint[]
                 ) AS delivered_seqs
            FROM conversation_members cm2
            JOIN users u ON u.user_id = cm2.user_id
            LEFT JOIN read_states rs
                   ON rs.conversation_id = c.conversation_id
                  AND rs.user_id = cm2.user_id
           WHERE cm2.conversation_id = c.conversation_id
             AND cm2.left_at IS NULL
      ) AS participants ON TRUE
     WHERE cm.user_id = $1
       AND cm.left_at IS NULL
"""

# Порядок обязан совпадать с порядком компонентов курсора: сравнение строк
# `(updated_at, conversation_id) < (...)` верно ровно тогда, когда оба
# компонента сортируются в одну сторону. Смешай направления — и предикат
# пришлось бы писать двумя условиями, а знак в одном из них однажды
# перепутать.
#
# Внешний `ORDER BY` повторяет сортировку подзапроса намеренно: порядок
# подзапроса не наследуется, а «страница правильная, порядок случайный» —
# это ровно то, что проверяет LIST-001.
_ORDER = "\n     ORDER BY c.updated_at DESC, c.conversation_id DESC"
_WITHOUT_CURSOR = _ORDER + "\n     LIMIT $2"
# Предикат только парный. Варианта «строго старше отметки» здесь нет
# намеренно: он теряет беседы с равным `updated_at`, и держать заведомо
# lossy запрос в репозитории значило бы оставлять его кому-то под рукой.
_WITH_PAIR = (
    "\n       AND (c.updated_at, c.conversation_id) < ($2::timestamptz, $3::uuid)"
    + _ORDER
    + "\n     LIMIT $4"
)


def _to_summary(row: asyncpg.Record) -> ConversationSummary:
    """Строка списка → беседа с участниками.

    Сущность собирается тем же `_to_conversation`, что и у одиночной
    выдачи: вторая сборка разошлась бы с первой, и заметно это стало бы
    не здесь, а на проверках `Conversation.__post_init__` — то есть
    отказом маршрута вместо ответа.

    `zip(..., strict=True)` — страховка от расхождения длин массивов:
    молча укороченный список участников выглядел бы как беседа, из
    которой кто-то вышел, а не как испорченный запрос. Пять массивов
    собираются одним подзапросом с одной сортировкой, поэтому расхождение
    возможно только при испорченном запросе — и тогда лучше отказ, чем
    участник не с тем временем.

    Числа называются здесь так, как лежат в таблице, а не так, как их
    зовёт домен: `last_read_seq` против `ReadState.read_seq`. Перевод
    происходит один раз и выше (`api/main._conversation_body`), по тому же
    правилу, по которому `_to_read_state` переводит имена базы в имена
    домена: репозиторий стоит на стороне базы и говорит её именами.

    Состояния чтения приходят **разреженными**, и `zip` их не выравнивает:
    `LEFT JOIN read_states` для человека без строки оставляет `NULL`
    в обеих колонках, и такой элемент в результат не попадает. Подставить
    здесь ноль (`COALESCE(read_seqs[i], 0)`) значило бы объявить квитанцию
    у того, кто её не присылал, — различие, ради которого в `_SELECT`
    соединение и сделано левым.
    """
    return ConversationSummary(
        conversation=_to_conversation(row),
        participants=tuple(
            UserSummary(
                user_id=UserId(user_id),
                display_name=display_name,
                last_seen_at=last_seen_at,
            )
            for user_id, display_name, last_seen_at in zip(
                row["participant_ids"],
                row["participant_names"],
                row["participant_last_seen"],
                strict=True,
            )
        ),
        read_states=tuple(
            ParticipantReadState(
                user_id=UserId(user_id),
                state=ReadState(
                    delivered_seq=ConversationSeq(delivered_seq),
                    read_seq=ConversationSeq(read_seq),
                ),
            )
            for user_id, read_seq, delivered_seq in zip(
                row["participant_ids"],
                row["participant_read_seqs"],
                row["participant_delivered_seqs"],
                strict=True,
            )
            if read_seq is not None
        ),
    )


def _conversation_page(
    rows: list[asyncpg.Record], limit: int
) -> ConversationPage:
    """Лишняя строка — единственное доказательство «есть ещё».

    Режется здесь и только здесь: `limit + 1` не покидает репозиторий.
    """
    return ConversationPage(
        items=tuple(_to_summary(row) for row in rows[:limit]),
        has_more=len(rows) > limit,
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


async def blocked_with(
    conn: asyncpg.Connection,
    *,
    viewer: UserId,
    conversation_id: ConversationId | None = None,
) -> frozenset[UserId]:
    """С кем у зрителя блокировка — в любую сторону, одним запросом.

    Симметрия здесь — **новое решение**, а не чтение записанного правила
    (`docs/messenger/04-decisions.md`, privacy-инвариант `G3-007`). В
    `08-authorization.md` симметрия объявлена только у записи сообщений
    (`:101`, раздел «Блокировка симметрична по записи»); у присутствия
    названо одно направление — `blocked` не видит `blocker` (`:87`, `:97`),
    — а про заблокировавшего сказано лишь, что ему разрешено читать беседу
    (`:100`). Правило «блокировка в любую сторону ↔ метаданных активности
    нет» шире документа и записано как новое.

    Довод тот же, что у `creation_blocked_between`, и он не про таблицу:
    асимметричная маска оставила бы односторонний канал преследования в
    **обе** стороны. Движение `last_seen_at` заблокированного — такой же
    сигнал, как движение `last_seen_at` заблокировавшего, а
    `11-threat-model.md` называет угрозу «слежка за присутствием», не
    уточняя, кто за кем следит.

    Форма одна на два вопроса, потому что правило одно. Без
    `conversation_id` возвращаются все, с кем зритель в блокировке, — так
    маскируется тело беседы (присутствие и `read_states` каждого участника,
    срез 3). С ним — только те, кто состоит в названной беседе сейчас, и
    это ответ на другой вопрос: уйдёт ли событие в канал (`message.read`
    не публикуется, если блокировка есть с любым участником, — рассылка
    адресата не имеет). Два вопроса, одно правило, один предикат.

    Цена названа: `OR` в предикате — скан `blocks`. Первичный ключ
    `(blocker_id, blocked_id)` покрывает прямое направление, обратное — нет,
    и запрос такой же формы уже ходит на пути создания беседы, то есть
    ступень не новая. Индекс на `blocked_id` потребовал бы `CONCURRENTLY`,
    несовместимого с `psql --single-transaction` (`db/migrate.sh`), — та же
    стена, что у обещанного индекса `0012`; а таблица мала: её размер равен
    числу блокировок.

    Левый участник пары — всегда **другой**: `CASE` берёт сторону, не
    равную зрителю, поэтому себя в ответе он не увидит. Отбор участников
    повторяет список активных (`left_at IS NULL`) — тот же, по которому
    собирается беседа: маска обязана накрывать ровно тех, кто в теле
    ответа, иначе одного из двоих она пропустит.
    """
    rows = await conn.fetch(
        """
        SELECT DISTINCT CASE WHEN b.blocker_id = $1
                             THEN b.blocked_id
                             ELSE b.blocker_id
                        END AS other
          FROM blocks b
          LEFT JOIN conversation_members m
            ON m.user_id = CASE WHEN b.blocker_id = $1
                                THEN b.blocked_id
                                ELSE b.blocker_id
                           END
           AND m.conversation_id = $2
           AND m.left_at IS NULL
         WHERE (b.blocker_id = $1 OR b.blocked_id = $1)
           AND ($2::uuid IS NULL OR m.user_id IS NOT NULL)
        """,
        viewer,
        conversation_id,
    )
    return frozenset(UserId(row["other"]) for row in rows)


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


async def list_user_conversations(
    conn: asyncpg.Connection,
    *,
    user_id: UserId,
    cursor: ActivityCursor | None,
    limit: int,
) -> ConversationPage:
    """Действующие беседы пользователя, свежие первыми, страницами.

    Отбор здесь и есть право: беседы не называет клиент, их задаёт
    субъект. Ресурса, о котором надо было бы спросить у авторизации, в
    запросе нет — поэтому в сервисе над этим вызовом нет `authorize`,
    и это решение, а не пропуск.

    Два варианта предиката, а не один с необязательным условием.
    `($2 IS NULL OR ...)` планировщик читает как «условия нет» и снимает
    индекс целиком — тот же довод, что у `_BELOW_MAX`/`_BELOW_CURSOR`
    в `messages.py`. Число параметров от этого меняется, поэтому запрос
    собирается здесь, а не передаётся одним текстом с дырами.

    Строгое `<`, а не `<=`: курсор исключает собственный элемент, иначе
    последняя беседа страницы попала бы ещё и в следующую. Нестрогое
    сравнение выглядит спасительным для группы с равным `updated_at`, но
    спасает только её: продублировав ровно те беседы, ради которых
    заведено, оно повторяет их на каждой странице.
    """
    if cursor is None:
        query, parameters = _SELECT + _WITHOUT_CURSOR, (user_id, limit + 1)
    else:
        query, parameters = (
            _SELECT + _WITH_PAIR,
            (user_id, cursor.updated_at, cursor.conversation_id, limit + 1),
        )
    rows = await conn.fetch(query, *parameters)
    return _conversation_page(rows, limit)


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
