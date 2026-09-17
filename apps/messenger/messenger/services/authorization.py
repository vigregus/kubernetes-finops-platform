"""Единственная точка принятия решений о доступе.

Свежесть выбирает действие, а не вызывающий. Для записи и подписки кеш
никогда не читается: устаревшее разрешение не должно расширить доступ.
"""
from __future__ import annotations

import uuid
from typing import Protocol

import asyncpg

from messenger.domain.authorization import (
    Action,
    Decision,
    ResourceKind,
    ResourceRef,
    Subject,
)
from messenger.domain.conversation import ConversationMember
from messenger.domain.errors import Reason
from messenger.domain.ids import ConversationId, UserId
from messenger.domain.user import Capability, can
from messenger.repositories import authorization as authorization_repository
from messenger.repositories import conversations, users


class MembershipCache(Protocol):
    async def get(
        self, *, conversation_id: ConversationId, user_id: UserId
    ) -> ConversationMember | None: ...


async def authorize(
    conn: asyncpg.Connection,
    *,
    subject: Subject,
    resource: ResourceRef,
    action: Action,
    membership_cache: MembershipCache | None = None,
) -> Decision:
    """Возвращает решение; привилегированное разрешение сначала аудируется."""
    if action is Action.CREATE_CONVERSATION and resource.kind is ResourceKind.USER:
        return await _create_conversation(conn, subject=subject, resource=resource)

    if resource.kind is ResourceKind.CONVERSATION and action in {
        Action.READ_CONVERSATION,
        Action.WRITE_CONVERSATION,
        Action.SUBSCRIBE_CONVERSATION,
    }:
        return await _conversation(
            conn,
            subject=subject,
            resource=resource,
            action=action,
            membership_cache=membership_cache,
        )

    if action is Action.ADMIN_EXECUTE and resource.kind is ResourceKind.ADMIN_ACTION:
        decision = _admin(subject)
        # И разрешение, и отказ оставляют след. Ошибка INSERT не превращается
        # в разрешение: исключение останавливает вызывающую операцию.
        await authorization_repository.append_audit(
            conn,
            subject=subject,
            resource=resource,
            action=action,
            allowed=decision.allowed,
            reason=decision.reason,
        )
        return decision

    return Decision.deny(Reason.NOT_A_MEMBER)


async def _create_conversation(
    conn: asyncpg.Connection, *, subject: Subject, resource: ResourceRef
) -> Decision:
    actor = subject.user
    if not can(actor, Capability.START_CONVERSATION):
        return Decision.deny(Reason.EMAIL_UNVERIFIED)
    try:
        participant_id = UserId(uuid.UUID(resource.identifier))
    except ValueError:
        return Decision.deny(Reason.USER_NOT_FOUND)
    if actor.user_id == participant_id:
        return Decision.deny(Reason.SELF_CONVERSATION)

    participant = await users.fetch_user(conn, user_id=participant_id)
    if participant is None or participant.is_deleted:
        return Decision.deny(Reason.USER_NOT_FOUND)
    if await conversations.creation_blocked_between(
        conn, first=actor.user_id, second=participant.user_id
    ):
        return Decision.deny(Reason.BLOCKED)
    return Decision.allow(target_user=participant)


async def _conversation(
    conn: asyncpg.Connection,
    *,
    subject: Subject,
    resource: ResourceRef,
    action: Action,
    membership_cache: MembershipCache | None,
) -> Decision:
    try:
        conversation_id = ConversationId(uuid.UUID(resource.identifier))
    except ValueError:
        return Decision.deny(Reason.CONVERSATION_NOT_FOUND)

    conversation = await conversations.fetch_conversation(
        conn, conversation_id=conversation_id
    )
    if conversation is None:
        return Decision.deny(Reason.CONVERSATION_NOT_FOUND)

    # Только чтение вправе использовать ограниченно устаревший кеш.
    if action is Action.READ_CONVERSATION and membership_cache is not None:
        member = await membership_cache.get(
            conversation_id=conversation_id, user_id=subject.user.user_id
        )
    else:
        member = await conversations.fetch_member(
            conn, conversation_id=conversation_id, user_id=subject.user.user_id
        )
    if member is None or not member.is_active:
        return Decision.deny(Reason.NOT_A_MEMBER)
    return Decision.allow()


def _admin(subject: Subject) -> Decision:
    if "admin" not in subject.roles or not subject.second_factor_verified:
        return Decision.deny(Reason.NOT_A_MEMBER)
    return Decision.allow()
