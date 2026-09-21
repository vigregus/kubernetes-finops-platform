"""Хранение проекции непрочитанного и чекпойнта потребителя.

Слой репозиториев держит здесь ту часть работы, которую нельзя решить,
не глядя в базу: блокировку строки чекпойнта, счёт по диапазону номеров,
пересборку из источника истины и пакетное чтение проекции.

**Предикат непрочитанного — один.** Он собирается функцией
`_unread_predicate` и подставляется в **два** запроса: пересчёт квитанции
и пересборку. Тот же предикат есть на Python в
`domain/unread.counts_as_unread` — им считается приращение, — и тождество
этих двух половин не постулируется: интеграционная сверка
`reconciliation` доводит проекцию событиями, затем пересобирает её из
источника истины до того же чекпойнта и сравнивает числа поимённо.
Разойтись они могут только вместе с этим тестом.

Четвёртое место, где предикат нужен, — восстановление потерянной строки
на чтении списка (`services/unread.restore_lost_counts`) — **новой копии
не завело**: оно зовёт ту же пересборку с той же границей. Копий
по-прежнему две, и это то, что вообще делает сверку осмысленной: три
SQL-редакции одного правила разошлись бы при первой правке.

**Проекция — производная.** Отсюда право её удалить: полная пересборка
даёт те же числа, что и приращение, и это тоже свойство проверяемое
(`unread_check.py`, сценарии `projection_deleted_*`). И отсюда же
обязанность её восстанавливать: потерянная строка чинится потребителем
(пересборкой на следующем событии беседы) и списком (пересборкой на
первом чтении), а для беседы, чьи события ушли по сроку хранения,
второй путь — единственный.

Транзакцию репозиторий не открывает — её открывает сервис: обработка
события связывает две таблицы (проекцию и чекпойнт), и граница
согласованности проходит между коммитами, а не между операторами.
"""
from __future__ import annotations

from collections.abc import Sequence

import asyncpg

from messenger.domain.ids import ConversationId, ConversationSeq, UserId
from messenger.domain.unread import UnreadCount, UnreadDelta


def _unread_predicate(
    *, above: str, through: str, reader: str
) -> str:
    """Единственный экземпляр предиката непрочитанного на SQL.

    Функция, а не константа, и это выбор с причиной: имена параметров
    в трёх запросах разные (`set_unread_count` нумерует границы сам,
    `rebuild` берёт читателя из соединения — `cm.user_id`, — потому что
    участников там много), а предикат обязан остаться **одним**. Собери
    его константой с конкретными `$2`/`$3`, и третья копия появилась бы
    в тот день, когда понадобился бы четвёртый запрос.

    Отступ у второй и третьей строк — одиннадцать пробелов, ровно под
    `AND` вызывающего запроса. SQL к пробелам равнодушен, но текст
    запроса читают глазами и в журнале базы: разъехавшийся отступ
    читался бы как отдельное от предиката условие.

    Надгробие (`m.deleted_at`) здесь **не** упоминается — и это
    решение, а не пропуск. Считаются чужие **позиции** `conversation_seq`
    выше `last_read_seq`, а не сообщения с доступным содержимым; удаление
    содержимого счётчик само по себе не уменьшает, а события удаления
    в потоке нет вовсе, поэтому исключение удалённых из пересчёта сделало
    бы пересборку строго меньше приращения. Разбор — в
    `domain/unread.counts_as_unread`.
    """
    return (
        f"m.conversation_seq > {above}\n"
        f"           AND m.conversation_seq <= {through}\n"
        f"           AND m.sender_id IS DISTINCT FROM {reader}"
    )


async def lock_offsets(
    conn: asyncpg.Connection, *, conversation_id: ConversationId
) -> ConversationSeq | None:
    """Возвращает чекпойнт беседы, занимая его строку на время транзакции.

    Два оператора вместо одного `ON CONFLICT DO UPDATE … RETURNING`,
    и различие не стилистическое. `DO UPDATE` пишет новую версию строки
    на **каждое** событие — включая повторы, то есть на самом дешёвом
    пути: горячая строка беседы обрастала бы мёртвыми версиями, которые
    потом убирает автоочистка. `DO NOTHING` на существующей строке не
    пишет ничего, а `SELECT … FOR UPDATE` даёт ту же блокировку.

    Гонка «вставил кто-то другой» разрешается сама: `DO NOTHING` дожидается
    чужой транзакции и не вставляет, а `SELECT … FOR UPDATE` под
    `READ COMMITTED` после ожидания перечитывает последнюю
    зафиксированную версию.

    Нулевая строка — не особый случай: чекпойнт начинается с нуля, и
    «строка только что заведена» значит ровно то же, что «не применено
    ничего».

    `None` — беседы больше нет, и это **ответ, а не отказ**.

    Прежняя редакция этого места считала исчезновение беседы
    недостижимым («удалять беседы в v1 нечем») и падала внешним ключом.
    Живой прогон показал, что удалять есть чем: уборка интеграционной
    проверки удаляет созданные ею беседы, а поток хранит их события
    дальше. И падение это не разовое, а **вечное**: смещение не
    фиксируется, пачка возвращается в поток (`Subscriber.rewind`)
    и упирается в ту же запись — пятнадцать часов и 749 отказов
    `ForeignKeyViolationError` в журнале базы, ни одного применённого
    события.

    Проверка существования не стоит лишней поездки: `INSERT … SELECT …
    WHERE EXISTS` — по-прежнему один оператор. Пустая строка после него
    значит именно «беседы нет»: чекпойнта у удалённой беседы не остаётся,
    его уносит каскад `unread_offsets_conversation_id_fkey` (`0011`).
    Гонка с удалением, попавшая в окно между чтением и вставкой, даёт
    одну ошибку ключа — и на следующей попытке тот же `EXISTS` уже
    отвечает `None`, потому что вечной эта ошибка быть перестала.
    """
    await conn.execute(
        """
        INSERT INTO unread_offsets (conversation_id, applied_through_seq)
        SELECT $1, 0
         WHERE EXISTS (
                   SELECT 1
                     FROM conversations
                    WHERE conversation_id = $1
               )
        ON CONFLICT (conversation_id) DO NOTHING
        """,
        conversation_id,
    )
    value = await conn.fetchval(
        """
        SELECT applied_through_seq
          FROM unread_offsets
         WHERE conversation_id = $1
           FOR UPDATE
        """,
        conversation_id,
    )
    if value is None:
        return None
    return ConversationSeq(value)


async def set_offsets(
    conn: asyncpg.Connection,
    *,
    conversation_id: ConversationId,
    applied_through_seq: ConversationSeq,
) -> ConversationSeq:
    """Двигает чекпойнт вперёд и возвращает то, что записано.

    Монотонность здесь не проверяется — в отличие от `upsert_read_state`,
    где она держится `GREATEST` против хранимой строки. Причина в том,
    что проверять нечего: строка уже занята вызывающим
    (`lock_offsets` вызывается первым шагом той же транзакции), поэтому
    конкурента нет, а единственный вызывающий двигает чекпойнт только
    вперёд — событие с номером не выше чекпойнта до этой функции не
    доходит. Ограждение `WHERE applied_through_seq < $2` защищало бы не
    от гонки, а от ошибки в вызывающем, и защищало бы молча: чекпойнт
    остался бы на месте, а исход события был бы записан как применённый.
    """
    value = await conn.fetchval(
        """
        UPDATE unread_offsets
           SET applied_through_seq = $2,
               updated_at = now()
         WHERE conversation_id = $1
        RETURNING applied_through_seq
        """,
        conversation_id,
        applied_through_seq,
    )
    if value is None:
        raise RuntimeError("чекпойнт исчез между блокировкой и записью")
    return ConversationSeq(value)


async def missing_projection(
    conn: asyncpg.Connection,
    *,
    conversation_id: ConversationId,
    user_ids: Sequence[UserId],
) -> tuple[UserId, ...]:
    """Кого из названных нет в проекции беседы.

    Нужна перед приращением, и это не перестраховка. `bump_unread`
    на отсутствующей строке **вставит** её со значением приращения —
    то есть получившему пять непрочитанных запишет единицу, и строка
    будет выглядеть правильной: ненулевой, в пределах диапазона.
    Расхождение обнаружилось бы только на сверке и только у того, кто
    не удалял проекцию сам.

    Отбор идёт по первичному ключу проекции `(user_id, conversation_id)`,
    но сравнивается с массивом, поэтому это `NOT EXISTS` по индексу
    на каждого названного, а не сканирование беседы.
    """
    if not user_ids:
        return ()
    rows = await conn.fetch(
        """
        SELECT u.user_id
          FROM unnest($2::uuid[]) AS u (user_id)
         WHERE NOT EXISTS (
                   SELECT 1
                     FROM unread_projection p
                    WHERE p.user_id = u.user_id
                      AND p.conversation_id = $1
               )
        """,
        conversation_id,
        list(user_ids),
    )
    return tuple(UserId(row["user_id"]) for row in rows)


async def bump_unread(
    conn: asyncpg.Connection,
    *,
    conversation_id: ConversationId,
    deltas: Sequence[UnreadDelta],
) -> int:
    """Прибавляет приращения — одной вставкой на беседу, а не на участника.

    Цикл по получателям был бы столько же круглых поездок в базу внутри
    транзакции, то есть столько же тактов удержания замка беседы; при
    пятистах участниках это и есть тот N+1, ради отсутствия которого
    проекция заведена (`PERF-007`).

    `DO UPDATE` условный: `WHERE EXCLUDED.unread_count <> 0`. Нулевое
    приращение бывает законным — это собственное сообщение отправителя, —
    и безусловная запись оставляла бы новую версию его строки на каждом
    его же сообщении. Ноль при этом не выбрасывается из набора, а
    вставляется: `DO NOTHING` на отсутствующей строке создал бы её
    значением приращения, а строка отправителя нужна списку бесед —
    иначе собственные беседы показывали бы «неизвестно» до первой
    квитанции.

    `GREATEST` здесь не нужен и был бы вреден: под замком конкурента нет,
    поэтому соревноваться не с чем, а законное уменьшение делает
    `recount_unread` — абсолютной записью, и GREATEST отменил бы её.

    Возвращается число **записанных** строк, а не названных: строка
    отправителя, у которой нечего было менять, в это число не входит,
    и «тронуто» значит здесь «изменено или создано».
    """
    if not deltas:
        # Пустой набор — законный случай: собеседник у беседы один.
        return 0
    rows = await conn.fetch(
        """
        INSERT INTO unread_projection (
            user_id, conversation_id, unread_count, updated_at
        )
        SELECT u.user_id, $1, u.delta, now()
          FROM unnest($2::uuid[], $3::bigint[]) AS u (user_id, delta)
        ON CONFLICT (user_id, conversation_id) DO UPDATE
           SET unread_count = unread_projection.unread_count
                            + EXCLUDED.unread_count,
               updated_at = now()
         WHERE EXCLUDED.unread_count <> 0
        RETURNING user_id
        """,
        conversation_id,
        [str(delta.user_id) for delta in deltas],
        [int(delta.delta) for delta in deltas],
    )
    return len(rows)


async def recount_unread(
    conn: asyncpg.Connection,
    *,
    conversation_id: ConversationId,
    user_id: UserId,
    above_seq: ConversationSeq,
    through_seq: ConversationSeq,
) -> UnreadCount:
    """Считает непрочитанное и записывает его абсолютным значением.

    Счёт и запись — **одним** оператором, и это требование, а не
    оптимизация. Между отдельным `SELECT count(*)` и последующей записью
    помещается чужое приращение, и абсолютное значение затрёт его целиком:
    событие, применённое в этот промежуток, потеряется не на единицу,
    а всем своим числом. Разница видна на гонке квитанции с потребителем
    (`receipt_race` в `unread_check.py`) и не видна ни на одном
    последовательном прогоне.

    Сигнатуры с готовым `count` здесь поэтому нет: число, пришедшее
    параметром, пришло бы из другого оператора, то есть из того самого
    промежутка.

    Верхняя граница — чекпойнт потребителя, а не голова беседы. Сообщение,
    уже лежащее в Postgres, но потребителем ещё не применённое, в счёт
    не входит: иначе квитанция обнулила бы счётчик, а потребитель,
    догнав, прибавил бы единицу по устаревшему `last_read_seq`. Через
    чекпойнт это невыразимо: он и есть граница между «уже учтено
    приращением» и «ещё не учтено никем».
    """
    row = await conn.fetchrow(
        f"""
        WITH counted AS (
            SELECT count(*) AS value
              FROM messages m
             WHERE m.conversation_id = $1
               AND {_unread_predicate(above="$3", through="$4", reader="$2")}
        )
        INSERT INTO unread_projection (
            user_id, conversation_id, unread_count, updated_at
        )
        SELECT $2, $1, counted.value, now()
          FROM counted
        ON CONFLICT (user_id, conversation_id) DO UPDATE
           SET unread_count = EXCLUDED.unread_count,
               updated_at = now()
        RETURNING unread_count
        """,  # noqa: S608 - подставляется только предикат, данных в тексте нет
        conversation_id,
        user_id,
        above_seq,
        through_seq,
    )
    if row is None:
        raise RuntimeError("записанный счётчик не вернулся из Postgres")
    return UnreadCount(row["unread_count"])


# Тело пересборки одно на два случая, и различаются они только отбором
# участников. Отбор вынесен во внешний текст, а не сделан условием
# `($3::uuid[] IS NULL OR …)`: предикат по массиву в `WHERE` планировщик
# читает как «условия нет» и снимает индекс PK по `conversation_id` —
# тот же довод, по которому у `list_user_conversations` два варианта
# запроса, а не один с необязательным условием.
_REBUILD = """
    WITH counted AS (
        SELECT cm.user_id, count(m.conversation_seq) AS value
          FROM conversation_members cm
          LEFT JOIN read_states rs
            ON rs.conversation_id = cm.conversation_id
           AND rs.user_id = cm.user_id
          LEFT JOIN messages m
            ON m.conversation_id = cm.conversation_id
           AND {predicate}
           AND cm.joined_at <= m.created_at
           AND (cm.left_at IS NULL OR cm.left_at > m.created_at)
         WHERE cm.conversation_id = $1
           AND cm.left_at IS NULL
           {filter}
         GROUP BY cm.user_id
    )
    INSERT INTO unread_projection (
        user_id, conversation_id, unread_count, updated_at
    )
    SELECT user_id, $1, value, now()
      FROM counted
    ON CONFLICT (user_id, conversation_id) DO UPDATE
       SET unread_count = EXCLUDED.unread_count,
           updated_at = now()
    RETURNING user_id, unread_count
"""


async def rebuild(
    conn: asyncpg.Connection,
    *,
    conversation_id: ConversationId,
    through_seq: ConversationSeq,
    user_ids: Sequence[UserId] | None = None,
) -> dict[UserId, UnreadCount]:
    """Пересобирает проекцию беседы из источника истины.

    Число берётся целиком, а не приращением: пересборка вызывается тогда,
    когда прежнему значению верить нельзя — им и был бы испорчен результат.

    Четыре решения в этом запросе, и каждое видно только на живых данных:

    * **`count(m.conversation_seq)`, а не `count(*)`.** Соединение внешнее:
      у участника без непрочитанного строка одна и NULL-ная, и `count(*)`
      дал бы ему единицу — то есть ровно у тех, у кого счётчик обязан
      обнулиться, он бы вырос.
    * **Условия членства — в `ON`, а не в `WHERE`.** Перенесённые в
      `WHERE` (`cm.joined_at <= m.created_at AND (cm.left_at IS NULL OR
      cm.left_at > m.created_at)`) они превратили бы внешнее соединение
      во внутреннее и выкинули бы участников без сообщений — тех самых,
      кого пересборка обязана обнулить.
    * **`LEFT JOIN read_states` и `COALESCE(..., 0)`.** Не присылавший
      квитанций прочитал ноль, и для счёта это не «неизвестно», а именно
      ноль: третьего значения у числа непрочитанного нет. Различие
      «нет строки» и «ноль в строке» важно там, где спрашивают о наличии
      состояния (`read_states.fetch_read_states`, `fetch_projection`),
      а не там, где считают.
    * **`cm.left_at IS NULL` в отборе.** Пересборка пишет ровно те строки,
      которые пишет приращение: состав берётся из события, а в нём
      действующие на момент отправки. Сегодня `left_at` не проставляет
      никто, поэтому разницы нет; в день, когда появится выход из беседы,
      строка вышедшего останется нетронутой — недостижимой для списка
      бесед, потому что тот фильтрует по тому же условию. Это названный
      зазор, а не забытый случай.

    `through_seq` — верхняя граница, и вызывается пересборка с двумя
    разными: с чекпойнтом (когда подозрительна строка проекции, а
    применённое верно) и с номером события (когда подозрителен сам
    чекпойнт — разрыв номеров). Разница не косметическая: в первом
    случае событие ещё не учтено и учитывается приращением, во втором
    оно уже входит в счёт.

    `user_ids=None` — вся беседа. Список сужает отбор до названных, и
    нужен это для потерянной строки: писать новые версии всех строк
    беседы из-за одной пропавшей значило бы платить за восстановление
    дороже, чем за сам счёт.
    """
    if user_ids is None:
        query = _REBUILD.format(
            predicate=_unread_predicate(
                above="COALESCE(rs.last_read_seq, 0)",
                through="$2",
                reader="cm.user_id",
            ),
            filter="",
        )
        parameters: tuple[object, ...] = (conversation_id, through_seq)
    else:
        if not user_ids:
            # Пустой список — это «никого», а не «всех»: сходить в базу
            # за нулём строк значило бы вернуть проекцию беседы тому,
            # кто просил починки одной.
            return {}
        query = _REBUILD.format(
            predicate=_unread_predicate(
                above="COALESCE(rs.last_read_seq, 0)",
                through="$2",
                reader="cm.user_id",
            ),
            filter="AND cm.user_id = ANY($3::uuid[])",
        )
        parameters = (conversation_id, through_seq, list(user_ids))
    rows = await conn.fetch(query, *parameters)  # noqa: S608 - только предикат
    return {
        UserId(row["user_id"]): UnreadCount(row["unread_count"]) for row in rows
    }


async def fetch_projection(
    conn: asyncpg.Connection,
    *,
    user_id: UserId,
    conversation_ids: Sequence[ConversationId],
) -> dict[ConversationId, UnreadCount]:
    """Счётчики названных бесед одного пользователя — одним запросом.

    Префикс первичного ключа проекции — `user_id`, и отбор идёт по нему
    с добором по `conversation_id`: страница списка бесед читается
    целиком, а не по запросу на беседу.

    Отсутствие беседы в ответе означает **не прочитано**, а не ноль:
    это отчёт о таблице, а не ответ пользователю. Подставить сюда ноль
    значило бы показать уверенную ложь — «всё прочитано» вместо «сюда
    не смотрели», — и потому отсутствие строки наверх не выходит: его
    чинит `services/unread.restore_lost_counts`, читая источник истины.
    Ноль остаётся тем, чем и был: числом, которое кто-то посчитал.
    """
    if not conversation_ids:
        # Пустая страница — законный ответ, а не повод сходить в базу.
        return {}
    rows = await conn.fetch(
        """
        SELECT conversation_id, unread_count
          FROM unread_projection
         WHERE user_id = $1
           AND conversation_id = ANY($2::uuid[])
        """,
        user_id,
        list(conversation_ids),
    )
    return {
        ConversationId(row["conversation_id"]): UnreadCount(row["unread_count"])
        for row in rows
    }
