"""Пользовательские сценарии бесед.

G1-010 закрывает обычное и последовательное повторное создание. Два
одновременных встречных запроса всё ещё могут столкнуться на уникальном
индексе — обработка именно этой гонки остаётся изолированной в G1-011.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import asyncpg

from messenger.domain.conversation import Conversation, ConversationType
from messenger.domain.errors import Reason
from messenger.domain.ids import ConversationId, UserId, direct_key
from messenger.domain.user import Capability, User, can
from messenger.repositories import conversations, users


@dataclass(frozen=True, slots=True)
class CreateDirectResult:
    conversation: Conversation | None = None
    participants: tuple[User, ...] = field(default_factory=tuple)
    created: bool = False
    rejection: Reason | None = None

    @property
    def ok(self) -> bool:
        return self.conversation is not None and self.rejection is None


async def create_direct(
    conn: asyncpg.Connection, *, actor: User, participant_id: UserId
) -> CreateDirectResult:
    """Создаёт диалог атомарно или возвращает уже существующий.

    Участник передаётся внутренним идентификатором, но субъект всегда берётся
    из проверенного токена. Поэтому тело запроса не может создать беседу
    от имени другого пользователя.
    """
    if not can(actor, Capability.START_CONVERSATION):
        return CreateDirectResult(rejection=Reason.EMAIL_UNVERIFIED)
    if actor.user_id == participant_id:
        return CreateDirectResult(rejection=Reason.SELF_CONVERSATION)

    async with conn.transaction():
        participant = await users.fetch_user(conn, user_id=participant_id)
        if participant is None or participant.is_deleted:
            return CreateDirectResult(rejection=Reason.USER_NOT_FOUND)

        if await conversations.creation_blocked_between(
            conn, first=actor.user_id, second=participant.user_id
        ):
            return CreateDirectResult(rejection=Reason.BLOCKED)

        key = direct_key(actor.user_id, participant.user_id)
        existing = await conversations.fetch_direct_conversation(
            conn, direct_key=key
        )
        if existing is not None:
            return CreateDirectResult(
                conversation=existing,
                participants=(actor, participant),
                created=False,
            )

        conversation = await conversations.insert_conversation(
            conn,
            conversation_id=ConversationId(uuid.uuid4()),
            type=ConversationType.DIRECT,
            direct_key=key,
        )
        await conversations.add_member(
            conn,
            conversation_id=conversation.conversation_id,
            user_id=actor.user_id,
        )
        await conversations.add_member(
            conn,
            conversation_id=conversation.conversation_id,
            user_id=participant.user_id,
        )
        return CreateDirectResult(
            conversation=conversation,
            participants=(actor, participant),
            created=True,
        )
