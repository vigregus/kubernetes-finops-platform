"""Доменные состояния беседы и членства без базы."""
from __future__ import annotations

import uuid
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone

import pytest

from messenger.domain.conversation import (
    Conversation,
    ConversationMember,
    ConversationType,
    MemberRole,
)
from messenger.domain.conversation_list import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    ActivityCursor,
    ConversationPage,
    validate_activity_cursors,
)
from messenger.domain.history import InvalidCursor
from messenger.domain.ids import ConversationId, ConversationSeq, UserId

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


def _conversation(**overrides) -> Conversation:
    base = {
        "conversation_id": ConversationId(uuid.uuid4()),
        "type": ConversationType.DIRECT,
        "direct_key": "a:b",
        "last_seq": ConversationSeq(0),
        "created_at": NOW,
        "updated_at": NOW,
    }
    return Conversation(**{**base, **overrides})


def _member(**overrides) -> ConversationMember:
    base = {
        "conversation_id": ConversationId(uuid.uuid4()),
        "user_id": UserId(uuid.uuid4()),
        "role": MemberRole.MEMBER,
        "joined_at": NOW,
    }
    return ConversationMember(**{**base, **overrides})


def test_прямая_беседа_требует_ключ_пары():
    with pytest.raises(ValueError):
        _conversation(direct_key=None)
    with pytest.raises(ValueError):
        _conversation(direct_key="")


def test_группа_не_может_иметь_ключ_пары():
    for value in ("a:b", ""):
        with pytest.raises(ValueError):
            _conversation(type=ConversationType.GROUP, direct_key=value)


def test_номер_сообщения_не_бывает_отрицательным():
    with pytest.raises(ValueError):
        _conversation(last_seq=ConversationSeq(-1))


def test_тип_беседы_назван_явно():
    assert _conversation().is_direct
    assert not _conversation(type=ConversationType.GROUP, direct_key=None).is_direct


def test_бывший_участник_отличим_от_действующего():
    assert _member().is_active
    assert not _member(left_at=NOW + timedelta(hours=1)).is_active


def test_выход_не_может_предшествовать_вступлению():
    with pytest.raises(ValueError):
        _member(left_at=NOW - timedelta(seconds=1))


def test_беседа_и_членство_неизменяемы():
    with pytest.raises(FrozenInstanceError):
        _conversation().last_seq = ConversationSeq(1)  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        _member().role = MemberRole.ADMIN  # type: ignore[misc]


# --- правила списка бесед ---------------------------------------------------


def _bounds(**overrides) -> dict:
    base = {
        "before_activity_at": None,
        "before_conversation_id": None,
        "limit": DEFAULT_PAGE_SIZE,
    }
    return {**base, **overrides}


@pytest.mark.parametrize("limit", [1, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE])
def test_размер_страницы_в_объявленном_диапазоне_законен(limit):
    validate_activity_cursors(**_bounds(limit=limit))


@pytest.mark.parametrize("limit", [0, -1, MAX_PAGE_SIZE + 1])
def test_размер_страницы_вне_диапазона_отвергается(limit):
    with pytest.raises(InvalidCursor):
        validate_activity_cursors(**_bounds(limit=limit))


def test_пустой_курсор_законен():
    # Первая страница: границы нет вовсе. Отличать это от «граница
    # не задана» нечем и не нужно — выдача начинается с головы списка.
    validate_activity_cursors(**_bounds())


def test_одиночная_отметка_активности_законна():
    # Объявлена контрактом сегодня и означает строго «старше отметки».
    # Отвергать её значило бы сузить уже обещанное поведение.
    validate_activity_cursors(**_bounds(before_activity_at=NOW))


def test_второй_компонент_без_первого_отвергается():
    # Сам по себе идентификатор границы не задаёт: он не «граница
    # по идентификатору», а половина пары, и второй половины нет.
    with pytest.raises(InvalidCursor):
        validate_activity_cursors(
            **_bounds(before_conversation_id=ConversationId(uuid.uuid4()))
        )


def test_пара_курсора_законна():
    validate_activity_cursors(
        **_bounds(
            before_activity_at=NOW,
            before_conversation_id=ConversationId(uuid.uuid4()),
        )
    )


def test_наивная_отметка_отвергается():
    # Наивную дату `asyncpg` истолкует по местной зоне процесса, а не
    # по UTC: ответ был бы посчитан не на тот вопрос, который задал
    # клиент, и заметно это стало бы только на машине с другой зоной.
    with pytest.raises(InvalidCursor):
        validate_activity_cursors(
            **_bounds(before_activity_at=datetime(2026, 9, 14, 12, 0))
        )


def test_отметка_в_любой_зоне_кроме_utc_законна():
    # Требование — смещение, а не именно UTC: один и тот же момент
    # в другой зоне остаётся тем же моментом.
    validate_activity_cursors(
        **_bounds(before_activity_at=NOW.astimezone(timezone(timedelta(hours=3))))
    )


def test_страница_по_умолчанию_пуста_и_без_продолжения():
    page = ConversationPage()
    assert page.items == () and not page.has_more


def test_курсор_без_второго_компонента_различим():
    # Разные варианты — разные состояния, а не «пара с пустым
    # идентификатором»: одиночный курсор означает «строго старше
    # отметки», и смешивать их нельзя.
    пара = ActivityCursor(
        updated_at=NOW, conversation_id=ConversationId(uuid.uuid4())
    )
    одиночный = ActivityCursor(updated_at=NOW)
    assert пара.conversation_id is not None
    assert одиночный.conversation_id is None
    assert пара != одиночный
