"""Применение одного события `message.created` к проекции непрочитанного.

Сервис держит **порядок** шагов, и порядок здесь и есть решение. Шагов
четыре, и переставить их нельзя:

1. Чекпойнт беседы занимается строкой `unread_offsets` — и занимается
   **первым**. Тем же порядком ходит квитанция (`services/receipts.py`),
   и именно совпадение порядка делает взаимоблокировку невозможной.
2. Принятое решение о номере: повтор, разрыв или продолжение.
3. Запись проекции — приращением там, где прежнему значению можно верить,
   и абсолютным числом там, где нельзя.
4. Чекпойнт сдвигается **последним**, уже после записи проекции.

Последний пункт — не формальность. `applied_through_seq` означает «до
этого номера проекция верна»; сдвинув его раньше записи, мы объявили бы
верным то, чего ещё нет, и отказ между двумя шагами оставил бы проекцию
навсегда отставшей: повтор события не прошёл бы дальше проверки
`seq <= watermark` и просто вернул бы `duplicate`. Обрыв транзакции
откатывает оба шага вместе, поэтому такая последовательность безопасна —
но только внутри одной транзакции.

**Транзакцию открывает сервис, а не воркер.** Граница согласованности
здесь — это «замок плюс обе записи», и она обязана лежать там же, где
берётся замок. Воркер, забывший открыть транзакцию, не получил бы
никакой ошибки: `lock_offsets` отпустил бы строку сразу, а отказ между
записью проекции и сдвигом чекпойнта оставил бы расхождение, которое
нашлось бы только сверкой. Ошибиться в этом месте нечем — открытие
транзакции неотделимо от применения события.

**Три повода пересобрать — один исход.** Разрыв номеров, отсутствие
состава в событии и пропавшая строка проекции чинятся одинаково: число
берётся из источника истины целиком. Различает их `reason`, и различает
не для красоты журнала: разрыв означает, что потребитель терял события,
отсутствие состава — что событие пришло неполным, а пропавшая строка —
что проекция терялась снаружи. Три разных происшествия, у которых одна
реакция.

**Чего сервис не делает.** Он не решает, что делать с исключением, — это
воркер: применение идемпотентно по чекпойнту, поэтому повтор дешевле
любого разбирательства, и смещение не фиксируется. И он не пишет в
журнал — правило слоя; исход возвращается значением, а записывает его
тот, кто знает и топик, и номер попытки.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import asyncpg

from messenger.domain import unread
from messenger.domain.ids import UserId
from messenger.domain.unread import (
    UnreadCount,
    UnreadDelta,
    UnreadOutcome,
    UnreadOutcomeKind,
)
from messenger.repositories import read_states
from messenger.repositories.unread import (
    bump_unread,
    lock_offsets,
    missing_projection,
    rebuild,
    set_offsets,
)

# Почему пришлось пересобирать. Не перечисление, а короткие коды: их
# видно в журнале рядом с `result="rebuilt"`, и по ним отличают разрыв
# номеров от потерянной проекции. Отдельных исходов под них нет намеренно
# (`domain/unread.UnreadOutcomeKind.REBUILT`): реакция у всех трёх одна,
# и три ветки с одинаковым телом разошлись бы при первой правке.
REASON_GAP = "gap"
REASON_NO_RECIPIENTS = "no_recipients"
REASON_MISSING_ROWS = "missing_rows"


async def apply_event(
    conn: asyncpg.Connection, *, body: Mapping[str, Any]
) -> UnreadOutcome:
    """Применяет событие к проекции. Исход — значение, отказ — исключение.

    Тело принимается **разобранным**, а не `bytes`. Разбор JSON — не
    предметная область, а транспорт: `adapters/kafka.poll` сам делает
    `json.loads` и негодную запись пропускает с записью в журнал
    (`result="failed"`), поэтому сюда неразобранное не доходит и
    второй раз его разбирать нечего. Разбор здесь начинается там, где
    начинается контракт: тип события, обязательные поля, пределы номера.

    Порядок шагов и доводы к нему — в докстринге модуля. Коротко:
    тип события проверяется до транзакции (постороннее событие не должно
    занимать ни соединение, ни замок), негодное событие — тоже.
    """
    if unread.event_type_of(body) != unread.EVENT_TYPE:
        # Чужой тип не трогает ничего, включая чекпойнт: `applied_through_seq`
        # определён как наибольший применённый `conversation_seq`, а его
        # несёт только `message.created`. Сдвинуть чекпойнт чужим событием
        # значило бы объявить применённым то, чего не было.
        return UnreadOutcome(kind=UnreadOutcomeKind.IGNORED)

    try:
        event = unread.parse_message_created(body)
    except unread.InvalidEvent as exc:
        # Негодное событие — ожидаемый отказ, а не сбой: возвращается
        # исходом, потому что воркер обязан пропустить его и зафиксировать
        # пачку. Исключение здесь встало бы навсегда: очереди отклонённых
        # записей у нас нет.
        return UnreadOutcome(kind=UnreadOutcomeKind.INVALID, reason=exc.code)

    async with conn.transaction():
        watermark = await lock_offsets(
            conn, conversation_id=event.conversation_id
        )

        if event.conversation_seq <= watermark:
            # Повтор. `<=`, а не `<`: номер, равный чекпойнту, уже применён
            # — чекпойнт и есть «наибольший применённый». Строгое сравнение
            # пропустило бы повтор последнего события, и счётчик вырос бы
            # дважды на одном сообщении — ровно то, что проверяет `CONS-003`.
            return UnreadOutcome(
                kind=UnreadOutcomeKind.DUPLICATE,
                conversation_id=event.conversation_id,
                conversation_seq=event.conversation_seq,
            )

        if event.recipient_ids is None:
            # Состава нет. Схема объявляет поле необязательным, поэтому
            # назвать событие негодным нельзя — оно менее полное, а не
            # неправильное. Состав берётся из `conversation_members`, то
            # есть пересборкой.
            return await _rebuilt(conn, event, reason=REASON_NO_RECIPIENTS)

        if event.conversation_seq > watermark + 1:
            return await _rebuilt(conn, event, reason=REASON_GAP)

        users = unread.affected_users(
            recipients=event.recipient_ids, sender_id=event.sender_id
        )
        # Проверка идёт по тем, кого событие касается, и **до** приращения:
        # `bump_unread` на отсутствующей строке вставит её со значением
        # приращения, то есть получившему пять непрочитанных запишет один,
        # и строка будет выглядеть правильной. Поймать это можно было бы
        # только сверкой, и только у того, кто проекцию не терял.
        lost = await missing_projection(
            conn, conversation_id=event.conversation_id, user_ids=users
        )
        if lost:
            return await _rebuild_lost(
                conn, event, users=users, lost=lost
            )

        written = await _bump(conn, event, users=users)
        await set_offsets(
            conn,
            conversation_id=event.conversation_id,
            applied_through_seq=event.conversation_seq,
        )
        return UnreadOutcome(
            kind=UnreadOutcomeKind.APPLIED,
            conversation_id=event.conversation_id,
            conversation_seq=event.conversation_seq,
            affected=written,
        )


async def _bump(
    conn: asyncpg.Connection,
    event: unread.MessageCreated,
    *,
    users: Sequence[UserId],
) -> int:
    """Приращение на каждого названного: ноль или единица.

    Дельты считаются здесь, а не запросом: прочитанные номера получателей
    уже прочитаны одной пачкой, а на диапазоне из одного номера `COUNT`
    вырождается в три сравнения. Позвать `COUNT` на получателя значило бы
    до пятисот круглых поездок в базу внутри транзакции, то есть столько
    же тактов удержания замка беседы (`PERF-007`).

    Отправитель приходит в `users` и получает ноль — не потому, что
    исключён отдельной веткой, а потому, что `counts_as_unread` для
    собственного сообщения ложна. Исключение его здесь развело бы
    предикат на две редакции: «для получателей» и «для отправителя».
    """
    reads = await read_states.fetch_read_states(
        conn, conversation_id=event.conversation_id, user_ids=users
    )
    deltas = tuple(
        UnreadDelta(
            user_id=user,
            delta=(
                UnreadCount(1)
                if unread.counts_as_unread(
                    sender_id=event.sender_id,
                    reader_id=user,
                    conversation_seq=event.conversation_seq,
                    # Отсутствие читателя в ответе означает ноль прочитанного,
                    # и подстановка здесь честна: у числа непрочитанного
                    # третьего значения нет, «не присылал квитанций» и
                    # «прочитал до нуля» для счёта — одно и то же.
                    last_read_seq=reads.get(user, unread.NO_SEQ),
                    applied_through_seq=event.conversation_seq,
                )
                else unread.NO_UNREAD
            ),
        )
        for user in users
    )
    return await bump_unread(
        conn, conversation_id=event.conversation_id, deltas=deltas
    )


async def _rebuilt(
    conn: asyncpg.Connection,
    event: unread.MessageCreated,
    *,
    reason: str,
) -> UnreadOutcome:
    """Пересборка всей беседы до номера события включительно.

    Верхняя граница — номер события, а не прежний чекпойнт: событие
    уже входит в источник истины, и пересборка до чекпойнта оставила бы
    его неучтённым, а `set_offsets` объявил бы применённым. Сдвиг
    чекпойнта идёт последним шагом и только после того, как записано
    то, что он объявляет записанным.
    """
    restored = await rebuild(
        conn,
        conversation_id=event.conversation_id,
        through_seq=event.conversation_seq,
    )
    await set_offsets(
        conn,
        conversation_id=event.conversation_id,
        applied_through_seq=event.conversation_seq,
    )
    return UnreadOutcome(
        kind=UnreadOutcomeKind.REBUILT,
        conversation_id=event.conversation_id,
        conversation_seq=event.conversation_seq,
        affected=len(restored),
        reason=reason,
    )


async def _rebuild_lost(
    conn: asyncpg.Connection,
    event: unread.MessageCreated,
    *,
    users: Sequence[UserId],
    lost: Sequence[UserId],
) -> UnreadOutcome:
    """Потерянные строки — пересборкой, остальные — приращением.

    Сужение до потерянных намеренное: писать новые версии всех строк
    беседы из-за одной пропавшей значило бы платить за восстановление
    дороже, чем за пересчёт, и платить на каждом событии той беседы,
    где однажды потерялась одна строка.

    Обе половины сходятся к верному числу по построению. Пересборка
    считает абсолютное значение **включая** текущее событие; приращение
    прибавляет его же тем, кто уцелел. Поэтому верхняя граница пересборки
    здесь — номер события, а не чекпойнт, и добавить к пересобранным
    единицу сверху было бы двойным счётом.
    """
    restored = await rebuild(
        conn,
        conversation_id=event.conversation_id,
        through_seq=event.conversation_seq,
        user_ids=lost,
    )
    surviving = tuple(user for user in users if user not in set(lost))
    written = await _bump(conn, event, users=surviving)
    await set_offsets(
        conn,
        conversation_id=event.conversation_id,
        applied_through_seq=event.conversation_seq,
    )
    return UnreadOutcome(
        kind=UnreadOutcomeKind.REBUILT,
        conversation_id=event.conversation_id,
        conversation_seq=event.conversation_seq,
        affected=len(restored) + written,
        reason=REASON_MISSING_ROWS,
    )
