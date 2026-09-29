"""Приём сообщения: порядок, идемпотентность и граница транзакции."""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest

from messenger.domain.conversation import ConversationMember, MemberRole
from messenger.domain.errors import Reason
from messenger.domain.ids import (
    ClientMessageId,
    ConversationId,
    ConversationSeq,
    MessageId,
    UserId,
)
from messenger.domain.message import Message, MessageKind, MessagePayload
from messenger.services import messages as service

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
SENDER = UserId(uuid.UUID("11111111-1111-1111-1111-111111111111"))
RECIPIENT = UserId(uuid.UUID("22222222-2222-2222-2222-222222222222"))
CONVERSATION = ConversationId(uuid.UUID("33333333-3333-3333-3333-333333333333"))
CLIENT_MESSAGE = ClientMessageId(uuid.UUID("44444444-4444-4444-4444-444444444444"))


class Transaction:
    def __init__(self) -> None:
        self.entered = False
        self.exited_with: type[BaseException] | None = None
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.exited_with = exc_type
        self.exited = True


class CommitFailsTransaction(Transaction):
    """Тело блока успешно, но сам COMMIT на выходе из __aexit__ падает.

    Так asyncpg ведёт себя, если запросы блока были только буферизованы
    и реальный COMMIT на проводе отклонён сервером уже после того, как
    тело успешно вернуло управление — именно тот сценарий, для которого
    `messages.py` больше не пишет `result="success"` внутри блока.
    """

    async def __aexit__(self, exc_type, exc, traceback):
        self.exited = True
        if exc_type is None:
            raise RuntimeError("commit failed")
        self.exited_with = exc_type


class Connection:
    def __init__(self, tx: Transaction | None = None) -> None:
        self.tx = tx if tx is not None else Transaction()

    def transaction(self) -> Transaction:
        return self.tx


def _message() -> Message:
    return Message(
        message_id=MessageId(uuid.uuid4()),
        conversation_id=CONVERSATION,
        conversation_seq=ConversationSeq(7),
        sender_id=SENDER,
        client_message_id=CLIENT_MESSAGE,
        kind=MessageKind.TEXT,
        payload=MessagePayload(text="привет"),
        created_at=NOW,
    )


def _member(user_id: UserId) -> ConversationMember:
    return ConversationMember(
        conversation_id=CONVERSATION,
        user_id=user_id,
        role=MemberRole.MEMBER,
        joined_at=NOW,
    )


def _send(conn: Connection, **overrides):
    values = {
        "sender_id": SENDER,
        "conversation_id": CONVERSATION,
        "client_message_id": CLIENT_MESSAGE,
        "kind": MessageKind.TEXT,
        "payload": MessagePayload(text="привет"),
    }
    return asyncio.run(service.send_message(conn, **{**values, **overrides}))


def test_system_не_принимается_от_клиента_до_транзакции():
    conn = Connection()
    result = _send(
        conn,
        kind=MessageKind.SYSTEM,
        payload=MessagePayload(text="служебное"),
    )
    assert result.rejection is Reason.UNSUPPORTED_MEDIA_TYPE
    assert not conn.tx.entered


def test_посторонний_не_получает_номер_сообщения(monkeypatch):
    async def _not_member(*args, **kwargs):
        return None

    async def _must_not_run(*args, **kwargs):
        raise AssertionError("после отказа попытались изменить беседу")

    monkeypatch.setattr(service.messages, "lock_active_conversation", _not_member)
    monkeypatch.setattr(service.messages, "allocate_sequence", _must_not_run)

    result = _send(Connection())
    assert result.rejection is Reason.NOT_A_MEMBER


def test_повтор_возвращает_ту_же_запись_без_нового_outbox(monkeypatch):
    expected = _message()

    async def _lock(*args, **kwargs):
        return ConversationSeq(7)

    async def _existing(*args, **kwargs):
        return expected

    async def _must_not_run(*args, **kwargs):
        raise AssertionError("повтор создал новый номер или событие")

    monkeypatch.setattr(service.messages, "lock_active_conversation", _lock)
    monkeypatch.setattr(service.messages, "fetch_by_client_id", _existing)
    monkeypatch.setattr(service.messages, "allocate_sequence", _must_not_run)
    monkeypatch.setattr(service.outbox, "insert_event", _must_not_run)

    result = _send(Connection())
    assert result.ok and not result.created and result.message == expected


def test_новое_сообщение_и_два_события_создаются_в_одной_транзакции(monkeypatch):
    expected = _message()
    events: list[dict] = []

    async def _lock(*args, **kwargs):
        return ConversationSeq(6)

    async def _missing(*args, **kwargs):
        return None

    async def _sequence(*args, **kwargs):
        return ConversationSeq(7)

    async def _insert(*args, **kwargs):
        assert kwargs["conversation_seq"] == 7
        return expected

    async def _members(*args, **kwargs):
        return [_member(SENDER), _member(RECIPIENT)]

    async def _event(*args, **kwargs):
        events.append(kwargs)

    monkeypatch.setattr(service.messages, "lock_active_conversation", _lock)
    monkeypatch.setattr(service.messages, "fetch_by_client_id", _missing)
    monkeypatch.setattr(service.messages, "allocate_sequence", _sequence)
    monkeypatch.setattr(service.messages, "insert_message", _insert)
    monkeypatch.setattr(service.conversations, "list_active_members", _members)
    monkeypatch.setattr(service.outbox, "insert_event", _event)

    conn = Connection()
    result = _send(conn, trace_id="a" * 32, span_id="b" * 16)
    assert result.ok and result.created and result.message == expected
    assert conn.tx.entered and conn.tx.exited_with is None
    assert [event["event_type"] for event in events] == [
        "message.created",
        "message.content",
    ]
    assert events[0]["partition_key"] == str(CONVERSATION)
    assert events[0]["payload"]["recipient_ids"] == [str(RECIPIENT)]
    assert "payload" not in events[0]["payload"]
    assert events[1]["payload"]["payload"]["text"] == "привет"
    # Контекст создателя уезжает в обе половины: потребитель непрочитанного
    # получает только факт, и без него его строки не связать с тем же
    # сообщением. Участок нужен наравне с трассой — отправитель ставит
    # ссылку на конкретный спан, а не на трассу целиком.
    for event in events:
        assert event["payload"]["trace_id"] == "a" * 32
        assert event["payload"]["span_id"] == "b" * 16


def test_ошибка_второго_outbox_выходит_через_границу_транзакции(monkeypatch):
    expected = _message()
    calls = 0

    async def _lock(*args, **kwargs):
        return ConversationSeq(6)

    async def _missing(*args, **kwargs):
        return None

    async def _sequence(*args, **kwargs):
        return ConversationSeq(7)

    async def _insert(*args, **kwargs):
        return expected

    async def _members(*args, **kwargs):
        return [_member(SENDER), _member(RECIPIENT)]

    async def _event(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("outbox недоступен")

    monkeypatch.setattr(service.messages, "lock_active_conversation", _lock)
    monkeypatch.setattr(service.messages, "fetch_by_client_id", _missing)
    monkeypatch.setattr(service.messages, "allocate_sequence", _sequence)
    monkeypatch.setattr(service.messages, "insert_message", _insert)
    monkeypatch.setattr(service.conversations, "list_active_members", _members)
    monkeypatch.setattr(service.outbox, "insert_event", _event)

    conn = Connection()
    with pytest.raises(RuntimeError, match="outbox недоступен"):
        _send(conn)
    assert conn.tx.exited_with is RuntimeError


def _happy_path_mocks(monkeypatch, expected: Message):
    async def _lock(*args, **kwargs):
        return ConversationSeq(6)

    async def _missing(*args, **kwargs):
        return None

    async def _sequence(*args, **kwargs):
        return ConversationSeq(7)

    async def _insert(*args, **kwargs):
        return expected

    async def _members(*args, **kwargs):
        return [_member(SENDER), _member(RECIPIENT)]

    async def _event(*args, **kwargs):
        return None

    monkeypatch.setattr(service.messages, "lock_active_conversation", _lock)
    monkeypatch.setattr(service.messages, "fetch_by_client_id", _missing)
    monkeypatch.setattr(service.messages, "allocate_sequence", _sequence)
    monkeypatch.setattr(service.messages, "insert_message", _insert)
    monkeypatch.setattr(service.conversations, "list_active_members", _members)
    monkeypatch.setattr(service.outbox, "insert_event", _event)


def test_success_метрика_пишется_только_после_реального_commit(monkeypatch):
    # Регрессия ровно на исправленный баг: раньше metrics.message_commit(
    # result="success") вызывался ВНУТРИ `async with conn.transaction()`,
    # то есть до того, как __aexit__ (реальный COMMIT) успел отработать.
    # Если этот порядок снова сломают - переносом метрики на пару строк
    # вверх, - тест это ловит: на момент вызова message_commit транзакция
    # уже обязана быть закрыта (conn.tx.exited is True).
    expected = _message()
    _happy_path_mocks(monkeypatch, expected)

    calls: list[tuple[float, str, bool]] = []
    conn = Connection()

    def _spy(duration_seconds, *, result):
        calls.append((duration_seconds, result, conn.tx.exited))

    monkeypatch.setattr(service.metrics, "message_commit", _spy)

    result = _send(conn)
    assert result.ok and result.created
    assert calls == [(calls[0][0], "success", True)]


def test_commit_не_прошедший_в_aexit_не_считается_success(monkeypatch):
    # Тело транзакции целиком успешно (все repo-вызовы отработали без
    # исключений), но сам COMMIT на выходе из `async with` отклонён -
    # asyncpg именно так себя ведёт, если запросы блока были только
    # буферизованы и реальный COMMIT на проводе не прошёл. До фикса
    # metrics.message_commit(result="success") уже был бы записан к
    # этому моменту (вызывался внутри блока, до __aexit__) - баг,
    # который и исправляет вынос метрики за пределы `async with`.
    expected = _message()
    _happy_path_mocks(monkeypatch, expected)

    calls: list[str] = []
    monkeypatch.setattr(
        service.metrics, "message_commit", lambda _d, *, result: calls.append(result)
    )

    conn = Connection(tx=CommitFailsTransaction())
    with pytest.raises(RuntimeError, match="commit failed"):
        _send(conn)

    assert calls == ["failed"]
