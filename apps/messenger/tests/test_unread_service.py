"""Применение события: повтор, разрыв, потерянная строка и порядок шагов.

Соединение здесь фейковое и **умеет** `transaction()` — в отличие от
соседнего файла квитанций, где его отсутствие само по себе было проверкой.
Здесь транзакция обязательна, и фейк считает её открытия: тест
`test_событие_применяется_в_транзакции` ронится от удаления обёртки,
потому что без неё замок чекпойнта отпускается сразу после чтения,
и гонка с квитанцией снова открывается.

Вторая половина файла — про чтение списка (`restore_lost_counts`), и это
не другая тема: потеря проекции лечится ровно тем же чтением источника
истины, что и разрыв номеров, а порядок «замок беседы, потом запись» —
тот же, что у потребителя и у квитанции. Стенд у неё свой: читающий путь
не спрашивает ни состава получателей, ни `read_states`, и общий фейк
с ручками на оба пути скрыл бы, что спрашивается лишнее.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from messenger.domain.ids import ConversationId, ConversationSeq, UserId
from messenger.domain.unread import (
    EVENT_TYPE,
    UnreadCount,
    UnreadDelta,
    UnreadOutcomeKind,
)
from messenger.services import unread as service

ANYA = UserId(uuid.UUID("11111111-1111-1111-1111-111111111111"))
BORIS = UserId(uuid.UUID("22222222-2222-2222-2222-222222222222"))
VIKA = UserId(uuid.UUID("55555555-5555-5555-5555-555555555555"))
CONVERSATION = ConversationId(uuid.UUID("33333333-3333-3333-3333-333333333333"))
OTHER = ConversationId(uuid.UUID("66666666-6666-6666-6666-666666666666"))
THIRD = ConversationId(uuid.UUID("77777777-7777-7777-7777-777777777777"))
MESSAGE = uuid.UUID("44444444-4444-4444-4444-444444444444")


class _Transaction:
    async def __aenter__(self) -> _Transaction:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class Connection:
    """Соединение с транзакцией и журналом вызовов.

    Журнал ведётся, потому что предмет проверки здесь — **порядок**:
    чекпойнт занимается первым, сдвигается последним, а пересборка
    не соседствует с приращением. Последовательность вызовов,
    а не только их набор, и есть то, что ломается при перестановке.
    """

    def __init__(self) -> None:
        self.transactions = 0
        self.calls: list[str] = []

    def transaction(self) -> _Transaction:
        self.transactions += 1
        self.calls.append("transaction")
        return _Transaction()


class Stand:
    """Подменённые репозитории: что вернули и что записали."""

    def __init__(self) -> None:
        self.deltas: list[UnreadDelta] = []
        self.offsets: list[int] = []
        self.rebuilds: list[dict[str, object]] = []
        self.asked: list[tuple[UserId, ...]] = []


def _stand(
    monkeypatch: pytest.MonkeyPatch,
    *,
    watermark: int = 0,
    reads: dict[UserId, int] | None = None,
    missing: tuple[UserId, ...] = (),
    restored: tuple[UserId, ...] = (),
) -> tuple[Connection, Stand]:
    conn = Connection()
    stand = Stand()
    state = {"reads": reads or {}, "missing": missing, "restored": restored}

    async def _lock(conn_: Connection, *, conversation_id: ConversationId):
        conn_.calls.append("lock_offsets")
        return ConversationSeq(watermark)

    async def _set(conn_: Connection, *, conversation_id, applied_through_seq):
        conn_.calls.append("set_offsets")
        stand.offsets.append(int(applied_through_seq))
        return applied_through_seq

    async def _missing(conn_: Connection, *, conversation_id, user_ids):
        conn_.calls.append("missing_projection")
        stand.asked.append(tuple(user_ids))
        return state["missing"]

    async def _bump(conn_: Connection, *, conversation_id, deltas):
        conn_.calls.append("bump_unread")
        stand.deltas.extend(deltas)
        # Живой запрос возвращает число записанных строк: нулевые
        # приращения в него не входят (`WHERE EXCLUDED.unread_count <> 0`).
        return sum(1 for delta in deltas if delta.delta != 0)

    async def _rebuild(conn_: Connection, *, conversation_id, through_seq, user_ids=None):
        conn_.calls.append("rebuild")
        stand.rebuilds.append(
            {"through_seq": int(through_seq), "user_ids": user_ids}
        )
        names = state["restored"] if user_ids is None else tuple(user_ids)
        return {user: UnreadCount(0) for user in names}

    async def _reads(conn_: Connection, *, conversation_id, user_ids):
        conn_.calls.append("fetch_read_states")
        return {
            user: ConversationSeq(state["reads"][user])
            for user in user_ids
            if user in state["reads"]
        }

    monkeypatch.setattr(service, "lock_offsets", _lock)
    monkeypatch.setattr(service, "set_offsets", _set)
    monkeypatch.setattr(service, "missing_projection", _missing)
    monkeypatch.setattr(service, "bump_unread", _bump)
    monkeypatch.setattr(service, "rebuild", _rebuild)
    monkeypatch.setattr(service.read_states, "fetch_read_states", _reads)
    return conn, stand


def _body(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "event_id": str(MESSAGE),
        "event_type": EVENT_TYPE,
        "conversation_id": str(CONVERSATION),
        "conversation_seq": 5,
        "sender_id": str(BORIS),
        "recipient_ids": [str(ANYA)],
    }
    values.update(overrides)
    return values


def _apply(conn: Connection, **overrides: object):
    return asyncio.run(service.apply_event(conn, body=_body(**overrides)))


# ---------------------------------------------------------------------------
# Повтор и разрыв
# ---------------------------------------------------------------------------


def test_повтор_не_меняет_ничего(monkeypatch):
    """`CONS-003`: повтор события не меняет счётчик.

    Ронит замену `<=` на `<` в проверке чекпойнта: номер, **равный**
    чекпойнту, уже применён — чекпойнт и есть наибольший применённый.
    На строгом сравнении повтор последнего события прошёл бы дальше
    и прибавил бы единицу второй раз.
    """
    conn, stand = _stand(monkeypatch, watermark=5)

    outcome = _apply(conn, conversation_seq=5)

    assert outcome.kind is UnreadOutcomeKind.DUPLICATE
    assert stand.offsets == []
    assert stand.deltas == []


def test_отставшее_событие_не_двигает_чекпойнт(monkeypatch):
    """Ронит «чекпойнт перезаписывается номером события».

    Чекпойнт — наибольший применённый номер, поэтому событие ниже него
    обязано оставить его на месте: запись назад открыла бы повторную
    обработку всего, что между ними.
    """
    conn, stand = _stand(monkeypatch, watermark=9)

    outcome = _apply(conn, conversation_seq=7)

    assert outcome.kind is UnreadOutcomeKind.DUPLICATE
    assert stand.offsets == []


def test_разрыв_пересобирает_проекцию(monkeypatch):
    """Ронит приращение на разрыве.

    `applied_through_seq = 3` и событие с номером 5 значат, что событие 4
    не применялось. Прибавить единицу к счётчику значило бы навсегда
    потерять четвёртое: источник истины про него знает, а проекция — нет,
    и узнает только если её спросить целиком.
    """
    conn, stand = _stand(monkeypatch, watermark=3, restored=(ANYA, BORIS))

    outcome = _apply(conn, conversation_seq=5)

    assert outcome.kind is UnreadOutcomeKind.REBUILT
    assert outcome.reason == service.REASON_GAP
    assert stand.rebuilds == [{"through_seq": 5, "user_ids": None}]
    assert stand.deltas == []
    assert stand.offsets == [5]


def test_пересборка_идёт_до_номера_события_а_не_до_чекпойнта(monkeypatch):
    """Ронит пересборку до прежнего чекпойнта.

    Верхняя граница — номер события: событие уже лежит в источнике истины,
    и пересборка до чекпойнта оставила бы его неучтённым, а `set_offsets`
    объявил бы применённым. Обнаружилось бы это не сразу: счётчик был бы
    меньше настоящего ровно на одно сообщение, до следующего события.
    """
    conn, stand = _stand(monkeypatch, watermark=3, restored=(ANYA,))

    _apply(conn, conversation_seq=5)

    assert stand.rebuilds[0]["through_seq"] == 5


# ---------------------------------------------------------------------------
# Обычный шаг
# ---------------------------------------------------------------------------


def test_чужому_единица_своему_ноль(monkeypatch):
    """`UNR-003` и существование строки отправителя разом.

    Отправитель входит в набор, но получает ноль — это делает предикат,
    а не отдельная ветка. Не будь его в наборе, строка не создалась бы,
    и список бесед показывал бы у него «неизвестно».
    """
    conn, stand = _stand(monkeypatch, watermark=4)

    outcome = _apply(conn, conversation_seq=5)

    assert outcome.kind is UnreadOutcomeKind.APPLIED
    assert {(d.user_id, int(d.delta)) for d in stand.deltas} == {
        (ANYA, 1),
        (BORIS, 0),
    }
    assert outcome.affected == 1  # записана одна строка: нулевая — не запись
    assert stand.offsets == [5]


def test_прочитанное_не_прибавляется(monkeypatch):
    # Квитанция сдвинула `last_read_seq` до пяти, событие пятое. Счётчик
    # обязан остаться прежним, и это тот же предикат, что у `UNR-001`.
    conn, stand = _stand(monkeypatch, watermark=4, reads={ANYA: 5})

    outcome = _apply(conn, conversation_seq=5)

    assert outcome.kind is UnreadOutcomeKind.APPLIED
    assert int(stand.deltas[0].delta) == 0
    assert outcome.affected == 0


def test_чекпойнт_сдвигается_последним(monkeypatch):
    """Порядок шагов: замок первым, чекпойнт — после записи проекции.

    Ронит перестановку `set_offsets` выше записи. `applied_through_seq`
    означает «до этого номера проекция верна»; сдвинутый раньше записи,
    он объявил бы верным то, чего ещё нет, и отказ между шагами оставил бы
    расхождение навсегда: повтор события не прошёл бы проверку чекпойнта
    и вернул бы `duplicate`, а проекция осталась бы отставшей.
    """
    conn, _ = _stand(monkeypatch, watermark=4)

    _apply(conn, conversation_seq=5)

    assert conn.calls == [
        "transaction",
        "lock_offsets",
        "missing_projection",
        "fetch_read_states",
        "bump_unread",
        "set_offsets",
    ]


def test_ноль_получателей_не_останавливает_счётчик(monkeypatch):
    # Беседа, из которой все вышли: получателей нет, но событие
    # обрабатывается как все прочие, и чекпойнт двигается — иначе
    # следующее событие пришло бы с разрывом и пересобрало беседу зря.
    conn, stand = _stand(monkeypatch, watermark=4)

    outcome = _apply(conn, conversation_seq=5, recipient_ids=[])

    assert outcome.kind is UnreadOutcomeKind.APPLIED
    assert {(d.user_id, int(d.delta)) for d in stand.deltas} == {(BORIS, 0)}
    assert stand.offsets == [5]


# ---------------------------------------------------------------------------
# Состава нет
# ---------------------------------------------------------------------------


def test_отсутствие_состава_пересобирает_беседу(monkeypatch):
    """Ронит приращение по пустому составу.

    Поле необязательно по схеме, поэтому событие без него — не негодное.
    Состав берётся из `conversation_members`, то есть пересборкой;
    прибавить единицу «получателям из события» значило бы прибавить
    её никому, а событие объявить применённым.
    """
    conn, stand = _stand(monkeypatch, watermark=4, restored=(ANYA, BORIS))

    outcome = _apply(conn, conversation_seq=5, recipient_ids=None)

    assert outcome.kind is UnreadOutcomeKind.REBUILT
    assert outcome.reason == service.REASON_NO_RECIPIENTS
    assert stand.rebuilds == [{"through_seq": 5, "user_ids": None}]
    assert stand.deltas == []
    assert stand.offsets == [5]


# ---------------------------------------------------------------------------
# Потерянная строка проекции
# ---------------------------------------------------------------------------


def test_потерянная_строка_пересобирается_а_не_получает_единицу(monkeypatch):
    """Ронит выбрасывание предпроверки.

    `bump_unread` на отсутствующей строке **вставит** её со значением
    приращения: получившему пять непрочитанных запишется единица, и
    строка будет выглядеть правдоподобной. Поймать это можно было бы
    только сверкой и только у того, кто проекцию не терял сам.
    """
    conn, stand = _stand(monkeypatch, watermark=4, missing=(ANYA,), restored=(ANYA,))

    outcome = _apply(conn, conversation_seq=5)

    assert outcome.kind is UnreadOutcomeKind.REBUILT
    assert outcome.reason == service.REASON_MISSING_ROWS
    assert stand.rebuilds == [{"through_seq": 5, "user_ids": (ANYA,)}]
    # Уцелевшему приращение всё равно положено: пересборка считает только
    # потерянных, и без этой половины счётчик отстал бы на текущее событие.
    assert {(d.user_id, int(d.delta)) for d in stand.deltas} == {(BORIS, 0)}
    assert stand.offsets == [5]


def test_уцелевшие_строки_не_пересобираются(monkeypatch):
    """Ронит пересборку всей беседы из-за одной пропавшей строки.

    На беседе в пятьсот участников это пятьсот новых версий строк и
    пятьсот проходов по индексу ради одной потерянной записи, причём
    на каждом событии, пока она не найдётся, — а не находится она ровно
    потому, что пишется только при пересборке.
    """
    conn, stand = _stand(monkeypatch, watermark=4, missing=(ANYA,), restored=(ANYA,))
    body = _body(recipient_ids=[str(ANYA), str(VIKA)])

    asyncio.run(service.apply_event(conn, body=body))

    assert stand.rebuilds[0]["user_ids"] == (ANYA,)
    # Проверка спрашивается про всех затронутых: суженный отбор — это
    # следствие ответа, а не способ задать вопрос.
    assert stand.asked == [(ANYA, VIKA, BORIS)]


# ---------------------------------------------------------------------------
# До транзакции: чужое и негодное
# ---------------------------------------------------------------------------


def test_чужой_тип_события_не_занимает_ничего(monkeypatch):
    """Ронит проверку типа события внутри транзакции.

    Постороннее событие пропускается, и занимать под это соединение,
    замок беседы и место в очереди соседей незачем. Проверка идёт
    до транзакции, поэтому `transactions` здесь ноль.
    """
    conn, stand = _stand(monkeypatch)

    outcome = asyncio.run(
        service.apply_event(conn, body=_body(event_type="message.content"))
    )

    assert outcome.kind is UnreadOutcomeKind.IGNORED
    assert conn.transactions == 0
    assert conn.calls == []
    assert stand.offsets == []


def test_негодное_событие_отвергается_значением_а_не_исключением(monkeypatch):
    """Ронит исключение вместо исхода на негодном событии.

    Негодное событие — ожидаемый отказ: воркер обязан пропустить его
    и зафиксировать пачку. Исключение встало бы навсегда — очереди
    отклонённых записей у нас нет, — и потребитель остановился бы
    на одной испорченной записи в топике.
    """
    conn, _ = _stand(monkeypatch)

    outcome = asyncio.run(service.apply_event(conn, body=_body(conversation_seq=0)))

    assert outcome.kind is UnreadOutcomeKind.INVALID
    assert outcome.reason == "bad_seq"
    assert conn.transactions == 0


def test_событие_применяется_в_транзакции(monkeypatch):
    """Ронит удаление обёртки `conn.transaction()`.

    Без неё `lock_offsets` отпускает строку чекпойнта сразу после чтения,
    и промежуток между чтением и записью проекции снова открывается:
    квитанция, вставшая в него, пересчитает счётчик по устаревшему
    `applied_through_seq` и затрёт приращение потребителя.
    """
    conn, _ = _stand(monkeypatch, watermark=4)

    _apply(conn, conversation_seq=5)

    assert conn.transactions == 1


def test_негодное_событие_не_трогает_чекпойнт(monkeypatch):
    # Обратная сторона того же: пропуск негодного события не должен
    # выглядеть применением. Иначе испорченная запись в топике сдвинула бы
    # чекпойнт, и настоящий разрыв перестал бы обнаруживаться.
    conn, stand = _stand(monkeypatch)

    _apply(conn, conversation_seq=0)

    assert stand.offsets == []


def test_разрыв_не_прибавляет_единицу(monkeypatch):
    # Обратная сторона разрыва: пересборка и приращение исключают друг
    # друга, иначе текущее событие было бы учтено дважды — один раз
    # в пересобранном числе, второй раз дельтой.
    conn, stand = _stand(monkeypatch, watermark=3, restored=(ANYA, BORIS))

    _apply(conn, conversation_seq=5)

    assert stand.deltas == []


# ---------------------------------------------------------------------------
# Восстановление потерянных строк при чтении списка
# ---------------------------------------------------------------------------


class RestoreStand:
    """Что восстановление спросило и что записало.

    Свой, а не общий `Stand`: у восстановления нет ни состава получателей,
    ни `read_states`, ни предпроверки строк. Оно спрашивает ровно три вещи
    — уцелевшие числа, чекпойнт беседы и её голову — и пишет две: число
    читателя и, если голова ушла вперёд, чекпойнт. Общий стенд с восемью
    ручками читался бы как «всё со всем», а проверяется здесь
    **адресация**: какая беседа пересобрана, до какого номера и что
    записано в чекпойнт.
    """

    def __init__(self) -> None:
        self.projection: list[tuple[UserId, tuple[ConversationId, ...]]] = []
        self.rebuilds: list[dict[str, object]] = []
        self.offsets: list[tuple[ConversationId, int]] = []


def _restore_stand(
    monkeypatch: pytest.MonkeyPatch,
    *,
    present: dict[ConversationId, int] | None = None,
    watermarks: dict[ConversationId, int] | None = None,
    heads: dict[ConversationId, int] | None = None,
    rows: dict[ConversationId, dict[UserId, int]] | None = None,
) -> tuple[Connection, RestoreStand]:
    """Восстановление на подставленных источниках.

    `heads` по умолчанию повторяет чекпойнты: обычный случай — потребитель
    догнал поток. Беседа, которой нет в словаре голов, отвечает `None` —
    это и есть «беседа исчезла». `rows` — то, что вернула бы пересборка
    по беседе; читателя, которого в них нет, не будет и в ответе, потому
    что живая пересборка отбирает участников по `conversation_members`.
    """
    conn = Connection()
    stand = RestoreStand()
    kept = present or {}
    marks = watermarks or {}
    ends = marks if heads is None else heads
    rebuilt = rows or {}

    async def _lock(conn_: Connection, *, conversation_id: ConversationId):
        conn_.calls.append("lock_offsets")
        return ConversationSeq(marks[conversation_id])

    async def _projection(conn_: Connection, *, user_id, conversation_ids):
        conn_.calls.append("fetch_projection")
        stand.projection.append((user_id, tuple(conversation_ids)))
        return {
            conversation_id: UnreadCount(kept[conversation_id])
            for conversation_id in conversation_ids
            if conversation_id in kept
        }

    async def _head(conn_: Connection, *, conversation_id: ConversationId):
        conn_.calls.append("fetch_last_seq")
        if conversation_id not in ends:
            return None
        return ConversationSeq(ends[conversation_id])

    async def _rebuild(
        conn_: Connection, *, conversation_id, through_seq, user_ids=None
    ):
        conn_.calls.append("rebuild")
        stand.rebuilds.append(
            {
                "conversation_id": conversation_id,
                "through_seq": int(through_seq),
                "user_ids": user_ids,
            }
        )
        members = rebuilt.get(conversation_id, {})
        if user_ids is None:
            return {user: UnreadCount(count) for user, count in members.items()}
        return {
            user: UnreadCount(members[user]) for user in user_ids if user in members
        }

    async def _set(conn_: Connection, *, conversation_id, applied_through_seq):
        conn_.calls.append("set_offsets")
        stand.offsets.append((conversation_id, int(applied_through_seq)))
        return applied_through_seq

    monkeypatch.setattr(service, "lock_offsets", _lock)
    monkeypatch.setattr(service, "fetch_projection", _projection)
    monkeypatch.setattr(service.conversations, "fetch_last_seq", _head)
    monkeypatch.setattr(service, "rebuild", _rebuild)
    monkeypatch.setattr(service, "set_offsets", _set)
    return conn, stand


def _restore(conn: Connection, conversation_ids: list[ConversationId]):
    return asyncio.run(
        service.restore_lost_counts(
            conn, viewer_id=ANYA, conversation_ids=conversation_ids
        )
    )


def test_потерянная_строка_восстанавливается_чтением(monkeypatch):
    """Ронит возврат проекции как есть.

    Это и есть требование гейта: потеря проекции не должна менять
    существенный ответ системы. Без восстановления список на вопрос
    «сколько непрочитанного» ответил бы «неизвестно» ровно там, где
    источник истины отвечает, — беседа молча потеряла бы признак, и
    заметно это стало бы только в клиенте.
    """
    conn, stand = _restore_stand(
        monkeypatch,
        present={CONVERSATION: 2},
        watermarks={CONVERSATION: 4, OTHER: 4},
        rows={OTHER: {ANYA: 3, BORIS: 0}},
    )

    counts = _restore(conn, [CONVERSATION, OTHER])

    # Проекция спрашивается пачкой по странице — тем же правилом, что
    # и раньше: поход в базу на беседу был бы ровно тем read
    # amplification, ради устранения которого проекция и заведена.
    assert stand.projection == [(ANYA, (CONVERSATION, OTHER))]
    assert counts[CONVERSATION] == 2
    assert counts[OTHER] == 3


def test_потерянная_строка_чинится_в_транзакции(monkeypatch):
    """Ронит восстановление без транзакции и замок, взятый до неё.

    Замок беседы без транзакции отпускается сразу после чтения, и между
    пересборкой и записью чекпойнта помещается событие потребителя: оно
    попадёт и в пересобранное число, и в приращение, которое применится
    после. Транзакция здесь не про атомарность двух вставок, а про то,
    что замок живёт до конца чтения источника истины. Журнал поэтому
    и проверяется целиком: `lock_offsets` обязан стоять **внутри**
    транзакции, а не перед ней.
    """
    conn, _ = _restore_stand(
        monkeypatch,
        watermarks={OTHER: 4},
        rows={OTHER: {ANYA: 3}},
    )

    _restore(conn, [OTHER])

    assert conn.transactions == 1
    assert conn.calls == [
        "fetch_projection",
        "transaction",
        "lock_offsets",
        "fetch_last_seq",
        "rebuild",
    ]


def test_чекпойнт_объявляется_головой_беседы(monkeypatch):
    """Ронит пересборку до чекпойнта и пропуск записи чекпойнта.

    Число берётся из источника истины целиком до головы, и она же
    объявляется применённой: `applied_through_seq` значит «до этого
    номера проекция верна», после пересборки это верно до головы.
    Не сдвинуть его значило бы оставить в потоке события, которые
    потребитель применит второй раз, — их уже посчитала пересборка.
    Обратная ошибка того же корня — считать до чекпойнта: числа бы
    сошлись, а события между чекпойнтом и головой потерялись бы навсегда.
    """
    conn, stand = _restore_stand(
        monkeypatch,
        watermarks={OTHER: 4},
        heads={OTHER: 9},
        rows={OTHER: {ANYA: 5, BORIS: 1}},
    )

    counts = _restore(conn, [OTHER])

    assert stand.rebuilds == [
        {"conversation_id": OTHER, "through_seq": 9, "user_ids": None}
    ]
    assert stand.offsets == [(OTHER, 9)]
    assert counts[OTHER] == 5


def test_голова_не_ушла_вперёд_значит_одна_строка(monkeypatch):
    """Ронит пересборку всей беседы и запись чекпойнта без нужды.

    Когда голова не больше чекпойнта, число читателя известно и без
    остальных: чинить нечего, объявлять нечего. Писать горячую строку
    беседы значило бы платить новой её версией и работой автоочистке
    за то, что и так верно, — а на странице из двадцати бесед это
    двадцать таких записей на каждое открытие списка.
    """
    conn, stand = _restore_stand(
        monkeypatch,
        watermarks={OTHER: 9},
        heads={OTHER: 9},
        rows={OTHER: {ANYA: 7, BORIS: 4}},
    )

    counts = _restore(conn, [OTHER])

    # Пересборка сужена до читателя, границы те же: числа до чекпойнта
    # и до головы совпадают там, где голова не больше.
    assert stand.rebuilds == [
        {"conversation_id": OTHER, "through_seq": 9, "user_ids": (ANYA,)}
    ]
    assert stand.offsets == []
    assert counts[OTHER] == 7


def test_потерянные_беседы_обходятся_по_возрастанию(monkeypatch):
    """Ронит обход потерянных в порядке страницы.

    Порядок обхода больше не защита от взаимоблокировки — транзакция
    беседная, и держит она не больше одного замка, — но он остался ради
    детерминированности: порядок ремонта виден в журнале и не зависит
    от того, в каком порядке беседы пришли из страницы. У двух
    одновременных списков порядок страницы разный, и без сортировки
    журнал ремонта у них тоже был бы разный — по нему уже не отличить
    «потерялась беседа» от «её не было в списке».
    """
    page = [THIRD, OTHER, CONVERSATION]
    conn, stand = _restore_stand(
        monkeypatch,
        watermarks={THIRD: 0, OTHER: 0, CONVERSATION: 0},
        rows={THIRD: {ANYA: 1}, OTHER: {ANYA: 2}, CONVERSATION: {ANYA: 3}},
    )

    counts = _restore(conn, page)

    assert [rebuild["conversation_id"] for rebuild in stand.rebuilds] == [
        CONVERSATION,
        OTHER,
        THIRD,
    ]
    # По транзакции на беседу — ни одной страничной.
    assert conn.transactions == 3
    # Порядок в ответе — порядок вопроса: страница собирается по нему.
    assert counts[THIRD] == 1 and counts[CONVERSATION] == 3


def test_беседа_не_удерживает_замок_чужого_ремонта(monkeypatch):
    """Ронит одну транзакцию на всю страницу.

    Границы согласованности у ремонта и у потребителя совпадают, но она
    **беседная**: при одной транзакции на всех замок первой беседы
    держался бы, пока пересобраны остальные, и потребитель с квитанцией
    этой беседы ждали бы ремонта последней. В начальной загрузке, где
    теряется сразу вся страница, это обычный случай, а не редкий. Журнал
    поэтому и читается целиком: транзакция обязана закрыться до того,
    как взят следующий замок, — тогда в каждой транзакции ровно один
    `lock_offsets`, и удерживать чужой замок нечем.
    """
    conn, stand = _restore_stand(
        monkeypatch,
        watermarks={CONVERSATION: 0, OTHER: 0},
        rows={CONVERSATION: {ANYA: 1}, OTHER: {ANYA: 2}},
    )

    counts = _restore(conn, [OTHER, CONVERSATION])

    assert conn.calls == [
        "fetch_projection",
        "transaction",
        "lock_offsets",
        "fetch_last_seq",
        "rebuild",
        "transaction",
        "lock_offsets",
        "fetch_last_seq",
        "rebuild",
    ]
    assert counts == {CONVERSATION: 1, OTHER: 2}


def test_уцелевшая_проекция_не_пересобирается(monkeypatch):
    """Ронит восстановление на каждом чтении.

    Обычный путь обязан остаться чтением. Если пересборка пойдёт по всей
    странице всегда, проекция перестанет что-либо экономить, и цена списка
    станет ценой `COUNT` по каждой беседе — ровно то, ради отказа от чего
    она и заведена.
    """
    conn, stand = _restore_stand(
        monkeypatch,
        present={CONVERSATION: 2, OTHER: 0},
        watermarks={CONVERSATION: 9, OTHER: 9},
    )

    counts = _restore(conn, [CONVERSATION, OTHER])

    # Журнал целиком: ни замка, ни головы, ни пересборки — только чтение.
    assert conn.calls == ["fetch_projection"]
    assert stand.rebuilds == [] and stand.offsets == []
    assert counts == {CONVERSATION: 2, OTHER: 0}


def test_отставшая_строка_не_догоняет_потребителя(monkeypatch):
    """Ронит сверку головы с чекпойнтом на каждом чтении.

    Случаев потери два, и различие между ними не тонкость. Уцелевшая
    строка — это **отставание потребителя**, то есть состояние, которое
    он исправит сам: число в проекции меньше источника истины ровно на
    ещё не применённое, и это согласованность в конечном счёте, а не
    потеря. Соблазн дочинить здесь велик — голова-то впереди, — а цена
    у него ровно та, ради отказа от которой проекция и заведена:
    сравнение головы с чекпойнтом на каждом `GET` вернуло бы `COUNT`
    по беседе на каждое чтение списка, то есть список, подменяющий
    потребителя. Поэтому восстановление открывается **отсутствием
    строки**, а не отставанием: голову здесь не спрашивают вовсе,
    пересборки нет, и ответ остаётся прежним — 3, а не 4 из источника
    истины. Меньшее число в списке при живом потребителе — это норма,
    а не то, что список обязан чинить.
    """
    conn, stand = _restore_stand(
        monkeypatch,
        present={CONVERSATION: 3},
        watermarks={CONVERSATION: 10},
        heads={CONVERSATION: 11},
        rows={CONVERSATION: {ANYA: 4}},
    )

    counts = _restore(conn, [CONVERSATION])

    # Сначала существенное: ни пересборки, ни записи чекпойнта, и число
    # осталось прежним. Источник истины при этом дал бы 4, и он в стенде
    # лежит: 3 в ответе — не невозможность, а решение. Порядок проверок
    # поэтому такой: семантику роняет дефект «принял отставание за
    # потерю», а журнал обращений ниже — уже следствие, и он называет
    # цену: голова намеренно впереди чекпойнта и не читается вовсе.
    assert stand.rebuilds == [] and stand.offsets == []
    assert counts == {CONVERSATION: 3}
    assert conn.calls == ["fetch_projection"]


def test_отсутствие_числа_у_источника_истины_не_становится_нулём(monkeypatch):
    """Ронит подстановку нуля вместо отсутствия.

    Читателя нет в составе беседы, и источник истины числа ему не даёт.
    Ноль здесь — уверенное «всё прочитано», то есть ложь, которую клиент
    не отличит от правды; отсутствие ключа — «спросить не у кого», и оно
    остаётся единственным случаем, когда ключа нет: потеря проекции
    к нему больше не приводит. Восстановление при этом **состоялось** —
    иначе проверка доказывала бы пропуск ремонта, а не ответ источника.
    """
    conn, stand = _restore_stand(
        monkeypatch,
        watermarks={OTHER: 4},
        rows={OTHER: {BORIS: 0}},
    )

    counts = _restore(conn, [OTHER])

    assert stand.rebuilds != []
    assert OTHER not in counts


def test_исчезнувшая_беседа_роняет_восстановление(monkeypatch):
    """Ронит молчаливое восстановление не того числа.

    Отсутствие головы у существующей беседы невозможно: строку чекпойнта
    завела вставка выше, а она ссылается на беседу внешним ключом. Но
    невозможное состояние обязано быть громким: ответ, посчитанный не по
    тому, чего нет, от верного неотличим, и заметить его было бы нечем.
    """
    conn, _ = _restore_stand(monkeypatch, watermarks={OTHER: 4}, heads={})

    with pytest.raises(RuntimeError):
        _restore(conn, [OTHER])
