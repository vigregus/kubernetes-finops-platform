"""Правила квитанции без базы: пределы, граница головы и нормализация."""
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from messenger.domain.history import MAX_SEQ
from messenger.domain.receipts import (
    InvalidReceipt,
    ReadState,
    Receipts,
    state_of,
    validate_receipt_bound,
    validate_receipts,
)


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
