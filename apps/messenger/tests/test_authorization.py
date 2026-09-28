"""AUTHZ-003…005: свежесть, скрытие существования и обязательный аудит."""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

import pytest

from messenger.domain.authorization import Action, ResourceRef, Subject
from messenger.domain.conversation import (
    Conversation,
    ConversationMember,
    ConversationType,
    MemberRole,
)
from messenger.domain.errors import Reason, to_problem
from messenger.domain.ids import ConversationId, ConversationSeq, UserId
from messenger.domain.user import User
from messenger.services import authorization

NOW = datetime(2026, 9, 17, tzinfo=UTC)
USER_ID = UserId(uuid.UUID("11111111-1111-1111-1111-111111111111"))
OTHER_USER_ID = UserId(uuid.UUID("33333333-3333-3333-3333-333333333333"))
CONVERSATION_ID = ConversationId(uuid.UUID("22222222-2222-2222-2222-222222222222"))


def user(*, email_verified: bool = True) -> User:
    return User(
        user_id=USER_ID,
        external_id="kc-user",
        display_name="Аня",
        email="anya@example.org",
        email_verified=email_verified,
        created_at=NOW,
        updated_at=NOW,
    )


def conversation() -> Conversation:
    return Conversation(
        conversation_id=CONVERSATION_ID,
        type=ConversationType.GROUP,
        direct_key=None,
        last_seq=ConversationSeq(0),
        created_at=NOW,
        updated_at=NOW,
    )


def member(*, active: bool) -> ConversationMember:
    return ConversationMember(
        conversation_id=CONVERSATION_ID,
        user_id=USER_ID,
        role=MemberRole.MEMBER,
        joined_at=NOW,
        left_at=None if active else NOW,
    )


def run(action: Action, *, cache=None):
    return asyncio.run(
        authorization.authorize(
            object(),
            subject=Subject(user()),
            resource=ResourceRef.conversation(CONVERSATION_ID),
            action=action,
            membership_cache=cache,
        )
    )


def run_create(*, subject: Subject | None = None, resource: ResourceRef | None = None):
    return asyncio.run(
        authorization.authorize(
            object(),
            subject=subject if subject is not None else Subject(user()),
            resource=resource if resource is not None else ResourceRef.user(OTHER_USER_ID),
            action=Action.CREATE_CONVERSATION,
        )
    )


def test_устаревший_кеш_не_разрешает_запись(monkeypatch):
    """AUTHZ-003: кеш разрешает, но свежая база уже отозвала членство."""
    class StaleCache:
        called = False

        async def get(self, **kwargs):
            self.called = True
            return member(active=True)

    cache = StaleCache()

    async def _conversation(*args, **kwargs):
        return conversation()

    async def _former(*args, **kwargs):
        return member(active=False)

    monkeypatch.setattr(authorization.conversations, "fetch_conversation", _conversation)
    monkeypatch.setattr(authorization.conversations, "fetch_member", _former)
    decision = run(Action.WRITE_CONVERSATION, cache=cache)
    assert not decision.allowed and decision.reason is Reason.NOT_A_MEMBER
    assert cache.called is False


def test_чтение_может_использовать_ограниченный_кеш(monkeypatch):
    class Cache:
        async def get(self, **kwargs):
            return member(active=True)

    async def _conversation(*args, **kwargs):
        return conversation()

    async def _не_читать_базу(*args, **kwargs):
        raise AssertionError("чтение не воспользовалось кешем")

    monkeypatch.setattr(authorization.conversations, "fetch_conversation", _conversation)
    monkeypatch.setattr(authorization.conversations, "fetch_member", _не_читать_базу)
    assert run(Action.READ_CONVERSATION, cache=Cache()).allowed


def test_чужая_и_несуществующая_беседы_неразличимы(monkeypatch):
    """AUTHZ-004: наружу в обоих случаях уходит один и тот же Problem."""
    async def _missing(*args, **kwargs):
        return None

    monkeypatch.setattr(authorization.conversations, "fetch_conversation", _missing)
    missing = run(Action.READ_CONVERSATION)

    async def _conversation(*args, **kwargs):
        return conversation()

    monkeypatch.setattr(authorization.conversations, "fetch_conversation", _conversation)
    monkeypatch.setattr(authorization.conversations, "fetch_member", _missing)
    foreign = run(Action.READ_CONVERSATION)

    assert missing.reason is Reason.CONVERSATION_NOT_FOUND
    assert foreign.reason is Reason.NOT_A_MEMBER
    assert to_problem(missing.reason) == to_problem(foreign.reason)


def test_административное_разрешение_сначала_пишет_аудит(monkeypatch):
    calls = []

    async def _audit(*args, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(authorization.authorization_repository, "append_audit", _audit)
    decision = asyncio.run(
        authorization.authorize(
            object(),
            subject=Subject(
                user(), roles=frozenset({"admin"}), second_factor_verified=True
            ),
            resource=ResourceRef.admin_action("disable-user"),
            action=Action.ADMIN_EXECUTE,
        )
    )
    assert decision.allowed
    assert len(calls) == 1 and calls[0]["allowed"] is True


def test_без_аудита_административного_разрешения_нет(monkeypatch):
    """AUTHZ-005: ошибка хранилища аудита останавливает операцию."""
    async def _failed(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(authorization.authorization_repository, "append_audit", _failed)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        asyncio.run(
            authorization.authorize(
                object(),
                subject=Subject(
                    user(), roles=frozenset({"admin"}), second_factor_verified=True
                ),
                resource=ResourceRef.admin_action("disable-user"),
                action=Action.ADMIN_EXECUTE,
            )
        )


# --- authorization_denied: одна запись на каждый Decision.deny, независимо
# от причины и от того, где она возникла (`_create_conversation` или
# `_conversation`). Проверяется контракт события, а не отдельная строка на
# каждый `Reason` — логирование теперь централизовано в `authorize()`.


@pytest.fixture
def caplog_security(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO, logger=authorization.__name__)
    return caplog


def _authorization_denied_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event", None) == "authorization_denied"]


def _assert_denied_contract(
    record: logging.LogRecord, *, reason: Reason, action: Action, resource_kind, user_id: UserId
) -> None:
    assert record.log_stream == "security"
    assert record.result == "rejected"
    assert record.error_code == reason.value
    assert record.action == action.value
    assert record.resource_kind == resource_kind.value
    assert record.user_id == str(user_id)


def test_отказ_создания_беседы_пишет_authorization_denied(caplog_security):
    """`EMAIL_UNVERIFIED`: до похода в базу, `can()` отказывает первым."""
    decision = run_create(subject=Subject(user(email_verified=False)))
    assert decision.reason is Reason.EMAIL_UNVERIFIED

    records = _authorization_denied_records(caplog_security)
    assert len(records) == 1
    _assert_denied_contract(
        records[0],
        reason=Reason.EMAIL_UNVERIFIED,
        action=Action.CREATE_CONVERSATION,
        resource_kind=authorization.ResourceKind.USER,
        user_id=USER_ID,
    )


def test_отказ_беседы_с_собой_пишет_authorization_denied(caplog_security):
    decision = run_create(resource=ResourceRef.user(USER_ID))
    assert decision.reason is Reason.SELF_CONVERSATION

    records = _authorization_denied_records(caplog_security)
    assert len(records) == 1
    _assert_denied_contract(
        records[0],
        reason=Reason.SELF_CONVERSATION,
        action=Action.CREATE_CONVERSATION,
        resource_kind=authorization.ResourceKind.USER,
        user_id=USER_ID,
    )


def test_отказ_блокировки_пишет_authorization_denied(monkeypatch, caplog_security):
    async def _participant(*args, **kwargs):
        return user(email_verified=True)

    async def _blocked(*args, **kwargs):
        return True

    monkeypatch.setattr(authorization.users, "fetch_user", _participant)
    monkeypatch.setattr(authorization.conversations, "creation_blocked_between", _blocked)

    decision = run_create()
    assert decision.reason is Reason.BLOCKED

    records = _authorization_denied_records(caplog_security)
    assert len(records) == 1
    _assert_denied_contract(
        records[0],
        reason=Reason.BLOCKED,
        action=Action.CREATE_CONVERSATION,
        resource_kind=authorization.ResourceKind.USER,
        user_id=USER_ID,
    )


@pytest.mark.parametrize(
    "action",
    [Action.READ_CONVERSATION, Action.WRITE_CONVERSATION, Action.SUBSCRIBE_CONVERSATION],
)
def test_отказ_не_участнику_пишет_authorization_denied(monkeypatch, caplog_security, action):
    """`action` — часть наблюдаемого контракта, поэтому параметризован."""
    async def _conversation(*args, **kwargs):
        return conversation()

    async def _missing(*args, **kwargs):
        return None

    monkeypatch.setattr(authorization.conversations, "fetch_conversation", _conversation)
    monkeypatch.setattr(authorization.conversations, "fetch_member", _missing)

    decision = run(action)
    assert decision.reason is Reason.NOT_A_MEMBER

    records = _authorization_denied_records(caplog_security)
    assert len(records) == 1
    _assert_denied_contract(
        records[0],
        reason=Reason.NOT_A_MEMBER,
        action=action,
        resource_kind=authorization.ResourceKind.CONVERSATION,
        user_id=USER_ID,
    )


def test_админский_отказ_не_пишет_authorization_denied(monkeypatch, caplog_security):
    """`ADMIN_EXECUTE` остаётся audit-only — второй нормативный след не заводится."""
    async def _audit(*args, **kwargs):
        return None

    monkeypatch.setattr(authorization.authorization_repository, "append_audit", _audit)
    decision = asyncio.run(
        authorization.authorize(
            object(),
            subject=Subject(user()),
            resource=ResourceRef.admin_action("disable-user"),
            action=Action.ADMIN_EXECUTE,
        )
    )
    assert decision.reason is Reason.NOT_A_MEMBER
    assert _authorization_denied_records(caplog_security) == []
