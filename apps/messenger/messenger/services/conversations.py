"""Пользовательские сценарии бесед.

G1-011 превращает конфликт двух встречных запросов в один общий результат:
уникальный индекс выбирает победителя, а проигравший читает его строку.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import asyncpg

from messenger.domain.authorization import Action, ResourceRef, Subject
from messenger.domain.conversation import Conversation
from messenger.domain.errors import Reason
from messenger.domain.ids import ConversationId, UserId, direct_key
from messenger.domain.user import User
from messenger.repositories import conversations
from messenger.services import authorization


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
    async with conn.transaction():
        decision = await authorization.authorize(
            conn,
            subject=Subject(actor),
            resource=ResourceRef.user(participant_id),
            action=Action.CREATE_CONVERSATION,
        )
        if not decision.allowed or decision.target_user is None:
            return CreateDirectResult(rejection=decision.reason or Reason.INTERNAL)
        participant = decision.target_user

        key = direct_key(actor.user_id, participant.user_id)
        ensured = await conversations.ensure_direct_conversation(
            conn,
            conversation_id=ConversationId(uuid.uuid4()),
            direct_key=key,
        )
        conversation = ensured.conversation
        if not ensured.created:
            return CreateDirectResult(
                conversation=conversation,
                participants=(actor, participant),
                created=False,
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
