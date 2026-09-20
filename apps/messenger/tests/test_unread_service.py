"""Применение события: повтор, разрыв, потерянная строка и порядок шагов.

Соединение здесь фейковое и **умеет** `transaction()` — в отличие от
соседнего файла квитанций, где его отсутствие само по себе было проверкой.
Здесь транзакция обязательна, и фейк считает её открытия: тест
`test_событие_применяется_в_транзакции` ронится от удаления обёртки,
потому что без неё замок чекпойнта отпускается сразу после чтения,
и гонка с квитанцией снова открывается.
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
