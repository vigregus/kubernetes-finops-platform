"""Правила квитанции без базы: пределы, граница головы и нормализация."""
from __future__ import annotations

import uuid
from dataclasses import FrozenInstanceError

import pytest

from messenger.domain.history import MAX_SEQ
from messenger.domain.ids import ConversationSeq, UserId
from messenger.domain.receipts import (
    EMPTY_STATE,
    InvalidReceipt,
    ReadState,
    Receipts,
    advanced_from,
    read_event,
    state_of,
    validate_receipt_bound,
    validate_receipts,
)

READER_ID = UserId(uuid.UUID("11111111-1111-1111-1111-111111111111"))


def test_отказ_остаётся_ошибкой_клиента():
    # Не деталь: `InvalidReceipt` ловится в обработчике узко, и подмена
    # базового класса на `Exception` расширила бы этот `except` молча.
    assert issubclass(InvalidReceipt, ValueError)


def test_пустая_квитанция_отвергается():
    # Транспорт её не пропустил бы (`anyOf`), но домен, доверяющий
    # спецификации, защищён ровно до первого скрипта.
    with pytest.raises(InvalidReceipt):
        validate_receipts(Receipts())


@pytest.mark.parametrize(
    "квитанция",
    [
        Receipts(delivered_seq=0),
        Receipts(read_seq=0),
        Receipts(delivered_seq=0, read_seq=0),
        Receipts(delivered_seq=MAX_SEQ),
        Receipts(read_seq=MAX_SEQ),
    ],
)
def test_пределы_внутри_объявленного_законны(квитанция):
    validate_receipts(квитанция)


@pytest.mark.parametrize(
    "квитанция, поле",
    [
        (Receipts(delivered_seq=-1), "delivered_seq"),
        (Receipts(read_seq=-1), "read_seq"),
        (Receipts(delivered_seq=MAX_SEQ + 1), "delivered_seq"),
        (Receipts(read_seq=MAX_SEQ + 1), "read_seq"),
    ],
)
def test_пределы_вне_объявленного_отвергаются(квитанция, поле):
    # Верхняя граница обязательна не ради полноты: без неё `asyncpg`
    # отдал бы `DataError` на приведении к `bigint`, и объявленный
    # контрактом `400` превратился бы в `500`. Проверяется и текст —
    # клиент должен узнать, какое из двух полей он испортил.
    with pytest.raises(InvalidReceipt, match=поле):
        validate_receipts(квитанция)


def test_ноль_в_доставке_доезжает_до_состояния():
    # Ноль — законное число и всё содержимое свежей строки. Проверка
    # на ложность (`if delivered_seq`) его выбросила бы, и различить
    # это по другим тестам нельзя: там везде ненулевые числа.
    состояние = state_of(Receipts(delivered_seq=0))
    assert состояние.delivered_seq == 0
    assert состояние.read_seq == 0


def test_доставка_выше_головы_отвергается():
    validate_receipt_bound(Receipts(delivered_seq=15), head=15)
    with pytest.raises(InvalidReceipt, match="delivered_seq"):
        validate_receipt_bound(Receipts(delivered_seq=16), head=15)


def test_чтение_выше_головы_отвергается():
    validate_receipt_bound(Receipts(read_seq=15), head=15)
    with pytest.raises(InvalidReceipt, match="read_seq"):
        validate_receipt_bound(Receipts(read_seq=16), head=15)


def test_пустая_беседа_принимает_ноль_и_отвергает_единицу():
    # Голова 0 — беседа создана, сообщений нет. Ноль там осмыслен,
    # единица — нет: такого сообщения ещё не существует.
    validate_receipt_bound(Receipts(delivered_seq=0, read_seq=0), head=0)
    with pytest.raises(InvalidReceipt):
        validate_receipt_bound(Receipts(delivered_seq=1), head=0)


def test_отказ_называет_присланное_поле_а_не_нормализованное():
    # Порядок «граница до нормализации» — не стилистика. Нормализация
    # поднимает доставленное до прочитанного, поэтому `read_seq=16` при
    # голове 15 после неё выглядело бы как `delivered_seq=16`, и клиент
    # получил бы отказ про поле, которого не присылал.
    with pytest.raises(InvalidReceipt, match="read_seq"):
        validate_receipt_bound(Receipts(read_seq=16), head=15)


@pytest.mark.parametrize(
    "квитанция, ожидаемое",
    [
        (Receipts(read_seq=5), (5, 5)),
        (Receipts(delivered_seq=5), (5, 0)),
        (Receipts(delivered_seq=2, read_seq=7), (7, 7)),
        (Receipts(delivered_seq=7, read_seq=2), (7, 2)),
        (Receipts(delivered_seq=7, read_seq=0), (7, 0)),
        (Receipts(delivered_seq=0, read_seq=0), (0, 0)),
    ],
)
def test_нормализация(квитанция, ожидаемое):
    состояние = state_of(квитанция)
    assert (состояние.delivered_seq, состояние.read_seq) == ожидаемое


@pytest.mark.parametrize("head", [0, 1, 15, MAX_SEQ])
def test_состояние_никогда_не_нарушает_соотношение(head):
    # Тот самый инвариант, ради которого нормализация лежит здесь,
    # а не в SQL: `CHECK` из миграции `0009` не должен ловить ничего,
    # кроме ошибки в этой строке. Перебор, а не пример: соотношение
    # обязано держаться на всей области, а не на удобной паре.
    for delivered in (0, 1, head):
        for read in (0, 1, head):
            состояние = state_of(Receipts(delivered_seq=delivered, read_seq=read))
            assert состояние.read_seq <= состояние.delivered_seq


def test_состояние_неизменяемо():
    # Оно уезжает в ответ и в репозиторий; изменяемое читалось бы
    # как «сервис поправил уже посчитанное».
    состояние = ReadState(delivered_seq=3, read_seq=1)
    with pytest.raises(FrozenInstanceError):
        состояние.read_seq = 2  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Сдвиг: отсутствие — не ноль, но для сравнения — ноль
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "прежнее, записанное, сдвиг",
    [
        # Продвижение: `GREATEST` поднял строку.
        (ReadState(ConversationSeq(7), ConversationSeq(7)),
         ReadState(ConversationSeq(9), ConversationSeq(9)), True),
        # Отставка: записано то же, что лежало.
        (ReadState(ConversationSeq(9), ConversationSeq(9)),
         ReadState(ConversationSeq(9), ConversationSeq(9)), False),
        # Строки не было, а записалась пара нулей — законный первый запрос
        # `{read_seq: 0}`. Продвижения нет, и событие о нём было бы
        # утверждением, которого никто не делал.
        (None, ReadState(ConversationSeq(0), ConversationSeq(0)), False),
        # Строки не было, а записалось нечто — это уже сдвиг.
        (None, ReadState(ConversationSeq(1), ConversationSeq(1)), True),
        # Приведение — только для сравнения, а не замена нулём «на глаз»:
        # у существующей нулевой строки продвижение определяется так же.
        (ReadState(ConversationSeq(0), ConversationSeq(0)),
         ReadState(ConversationSeq(0), ConversationSeq(0)), False),
    ],
)
def test_сдвиг_сравнивается_с_приведённым_прежним(прежнее, записанное, сдвиг):
    """Ронит оба перевёрнутых сравнения — с присланным и без приведения.

    Первое (с присланным) объявляет сдвигом отставку и молчит на
    продвижении: обе строки перевёрнуты целиком, и в таблице это видно
    как пара соседних случаев. Второе (без приведения `None` к нулю)
    краснит ровно на третьей строке — на первом законном `{read_seq: 0}`.

    `EMPTY_STATE` здесь не подставляется вызывающим: приведение — часть
    правила, и проверяется оно на области, а не на удобной паре.
    """
    assert advanced_from(прежнее, записанное) is сдвиг


def test_отсутствие_строки_наружу_остаётся_отсутствием():
    """`None` приводится только для сравнения, а не для ответа.

    Оба вопроса — «что мы отдаём наружу» и «сдвинулось ли» — разные, и
    слить их значило бы вернуть клиенту пару нулей вместо «состояния
    нет»: то же правило «отсутствие ≠ ноль», что у `unread_count`.
    """
    assert EMPTY_STATE is not None
    assert advanced_from(None, EMPTY_STATE) is False
    # Сравниваемое приведено, а `previous` как значение никуда не делось:
    # это вызывающий решает, что отдать, — здесь же видно, что функция
    # не подменяет переданное.
    assert advanced_from(None, ReadState(ConversationSeq(0), ConversationSeq(0))) is False


def test_событие_несёт_номера_своими_именами():
    """Ронит переиспользование `seq` под номер квитанции.

    `seq` — номер сообщения, и его отсутствие в теле события не
    стилистика: клиент, не знающий о квитанциях, читает разрыв в `seq`
    как пропуск событий и уходит догружать историю. Второе значение
    в том же поле сломало бы этот детектор молча — то есть ровно то,
    что запрещает `CTR-003`.
    """
    тело = read_event(reader_id=READER_ID, state=ReadState(ConversationSeq(9), ConversationSeq(7)))

    assert тело == {
        "type": "message.read",
        "reader_id": str(READER_ID),
        "read_seq": 7,
        "delivered_seq": 9,
    }
    assert "seq" not in тело


def test_номера_в_событии_целые_а_не_доменные():
    """`ConversationSeq` — доменный тип с проверкой; в тело события едет
    число, потому что `json` доменного типа не знает, а `Centrifugo` на
    такой публикации откажет — и откажет молча (best-effort)."""
    тело = read_event(reader_id=READER_ID, state=ReadState(ConversationSeq(9), ConversationSeq(7)))

    assert type(тело["read_seq"]) is int
    assert type(тело["delivered_seq"]) is int
    assert type(тело["reader_id"]) is str
