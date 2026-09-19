"""Запись квитанции: порядок шагов, отказ против исключения, транзакции нет."""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest

from messenger.domain.authorization import Decision
from messenger.domain.errors import Reason, Visibility
from messenger.domain.ids import ConversationId, ConversationSeq, UserId
from messenger.domain.receipts import InvalidReceipt, ReadState, Receipts
from messenger.domain.user import User
from messenger.services import receipts as service

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
VIEWER_ID = UserId(uuid.UUID("11111111-1111-1111-1111-111111111111"))
CONVERSATION = ConversationId(uuid.UUID("33333333-3333-3333-3333-333333333333"))


class Connection:
    """Соединение без транзакции: сервису она не нужна, и это проверяется.

    Заготовка намеренно **не** даёт `transaction()`. Если однажды в сервисе
    появится обёртка «для единообразия с сообщениями», здешние вызовы
    упадут `AttributeError` — и это верный сигнал: обёртка не даёт на
    READ COMMITTED ничего, зато выглядит гарантией, которой не является.
    """


def _user() -> User:
    return User(
        user_id=VIEWER_ID,
        external_id="kc-1",
        display_name="Аня",
        email="anya@example.org",
        email_verified=True,
        created_at=NOW,
        updated_at=NOW,
    )


def _set(conn, **overrides):
    values = {
        "viewer": _user(),
        "conversation_id": CONVERSATION,
        "receipts": Receipts(read_seq=3),
    }
    return asyncio.run(service.set_receipts(conn, **{**values, **overrides}))


def test_пустая_квитанция_отвергается_до_всякой_работы(monkeypatch):
    # Порядок здесь и есть предмет проверки: проверка пределов отвечает
    # без базы, поэтому обязана идти до авторизации и до соединения.
    async def _must_not_run(*args, **kwargs):
        raise AssertionError("негодная квитанция дошла до работы")

    monkeypatch.setattr(service.authorization, "authorize", _must_not_run)
    monkeypatch.setattr(service.conversations, "fetch_last_seq", _must_not_run)
    monkeypatch.setattr(service.read_states, "upsert_read_state", _must_not_run)

    with pytest.raises(InvalidReceipt):
        _set(Connection(), receipts=Receipts())


def test_посторонний_не_читает_голову_беседы(monkeypatch):
    # Не только «отказ вместо записи»: неучастник не должен получить
    # прочитанную за него голову. Поэтому `fetch_last_seq` здесь падает,
    # а не возвращает число, — иначе проверка была бы про запись, а не
    # про порядок.
    async def _отказать(conn, **kwargs):
        return Decision.deny(Reason.NOT_A_MEMBER)

    async def _must_not_run(*args, **kwargs):
        raise AssertionError("голова беседы прочитана для постороннего")

    monkeypatch.setattr(service.authorization, "authorize", _отказать)
    monkeypatch.setattr(service.conversations, "fetch_last_seq", _must_not_run)
    monkeypatch.setattr(service.read_states, "upsert_read_state", _must_not_run)

    result = _set(Connection())
    assert result.rejection is Reason.NOT_A_MEMBER
    assert not result.ok


def test_отказ_несёт_видимость_решения(monkeypatch):
    # `KNOWN` сегодня недостижим в бою, но сервис обязан донести значение,
    # а не заменить его умолчанием: иначе объявленный `403` станет
    # недостижимым навсегда, и заметят это при первом разборе чужой беседы.
    async def _отказать(conn, **kwargs):
        return Decision.deny(Reason.NOT_A_MEMBER, visibility=Visibility.KNOWN)

    monkeypatch.setattr(service.authorization, "authorize", _отказать)

    result = _set(Connection())
    assert result.rejection is Reason.NOT_A_MEMBER
    assert result.visibility is Visibility.KNOWN


def test_номер_выше_головы_отвергается_и_не_пишется(monkeypatch):
    async def _разрешить(conn, **kwargs):
        return Decision.allow()

    async def _голова(conn, *, conversation_id):
        return ConversationSeq(5)

    async def _must_not_run(*args, **kwargs):
        raise AssertionError("отвергнутая квитанция дошла до записи")

    monkeypatch.setattr(service.authorization, "authorize", _разрешить)
    monkeypatch.setattr(service.conversations, "fetch_last_seq", _голова)
    monkeypatch.setattr(service.read_states, "upsert_read_state", _must_not_run)

    with pytest.raises(InvalidReceipt):
        _set(Connection(), receipts=Receipts(delivered_seq=6))


def test_пропавшая_беседа_не_роняет_сервис(monkeypatch):
    # Между решением о праве и чтением головы беседу могли удалить.
    # Сегодня это недостижимо, но `None` сравнился бы с числом и дал бы
    # `500` вместо честного `404`.
    async def _разрешить(conn, **kwargs):
        return Decision.allow()

    async def _нет_беседы(conn, *, conversation_id):
        return None

    monkeypatch.setattr(service.authorization, "authorize", _разрешить)
    monkeypatch.setattr(service.conversations, "fetch_last_seq", _нет_беседы)

    result = _set(Connection())
    assert result.rejection is Reason.CONVERSATION_NOT_FOUND


def test_запись_получает_нормализованную_пару(monkeypatch):
    # Предмет проверки — что уезжает в SQL. Присланное «прочитано 5»
    # без доставки обязано стать парой (5, 5): доставленное поднимается
    # до прочитанного, и это `RCP-004`.
    записанное: dict[str, object] = {}

    async def _разрешить(conn, **kwargs):
        return Decision.allow()

    async def _голова(conn, *, conversation_id):
        return ConversationSeq(9)

    async def _записать(conn, **kwargs):
        записанное.update(kwargs)
        return ReadState(
            delivered_seq=ConversationSeq(5), read_seq=ConversationSeq(5)
        )

    monkeypatch.setattr(service.authorization, "authorize", _разрешить)
    monkeypatch.setattr(service.conversations, "fetch_last_seq", _голова)
    monkeypatch.setattr(service.read_states, "upsert_read_state", _записать)

    result = _set(Connection(), receipts=Receipts(read_seq=5))
    assert result.ok
    assert записанное["delivered_seq"] == 5
    assert записанное["read_seq"] == 5
    assert записанное["user_id"] == VIEWER_ID
    assert записанное["conversation_id"] == CONVERSATION


def test_сервис_возвращает_состояние_из_базы_а_не_присланное(monkeypatch):
    # Ответ на отставшую квитанцию несёт текущее значение. Присланное
    # клиент счёл бы применённым — именно этим «применено» и отличается
    # от «проигнорировано».
    async def _разрешить(conn, **kwargs):
        return Decision.allow()

    async def _голова(conn, *, conversation_id):
        return ConversationSeq(9)

    async def _записать(conn, **kwargs):
        return ReadState(
            delivered_seq=ConversationSeq(9), read_seq=ConversationSeq(9)
        )

    monkeypatch.setattr(service.authorization, "authorize", _разрешить)
    monkeypatch.setattr(service.conversations, "fetch_last_seq", _голова)
    monkeypatch.setattr(service.read_states, "upsert_read_state", _записать)

    result = _set(Connection(), receipts=Receipts(read_seq=2))
    assert result.state is not None
    assert result.state.read_seq == 9


def test_транзакции_нет():
    # Соединение в этих тестах не умеет `transaction()` вовсе, поэтому
    # успешные прогоны выше уже это доказывают. Тест называет свойство
    # вслух — иначе «обёртки нет» выглядит как недосмотр, и её добавят
    # «для единообразия с сообщениями».
    assert not hasattr(Connection(), "transaction")
