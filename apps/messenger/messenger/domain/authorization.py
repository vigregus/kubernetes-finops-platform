"""Типы единой точки принятия решений о доступе."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from messenger.domain.errors import Reason, Visibility
from messenger.domain.ids import ConversationId, UserId
from messenger.domain.user import User


class Action(str, Enum):
    CREATE_CONVERSATION = "conversation.create"
    READ_CONVERSATION = "conversation.read"
    WRITE_CONVERSATION = "conversation.write"
    SUBSCRIBE_CONVERSATION = "conversation.subscribe"
    ADMIN_EXECUTE = "admin.execute"


class ResourceKind(str, Enum):
    USER = "user"
    CONVERSATION = "conversation"
    ADMIN_ACTION = "admin_action"


@dataclass(frozen=True, slots=True)
class Subject:
    user: User
    roles: frozenset[str] = field(default_factory=frozenset)
    second_factor_verified: bool = False


@dataclass(frozen=True, slots=True)
class ResourceRef:
    kind: ResourceKind
    identifier: str

    @classmethod
    def user(cls, user_id: UserId) -> ResourceRef:
        return cls(ResourceKind.USER, str(user_id))

    @classmethod
    def conversation(cls, conversation_id: ConversationId) -> ResourceRef:
        return cls(ResourceKind.CONVERSATION, str(conversation_id))

    @classmethod
    def admin_action(cls, name: str) -> ResourceRef:
        return cls(ResourceKind.ADMIN_ACTION, name)


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    reason: Reason | None = None
    visibility: Visibility = Visibility.HIDDEN
    # Проверенный ресурс можно передать следующему шагу без повторного SELECT.
    target_user: User | None = None

    @classmethod
    def allow(cls, *, target_user: User | None = None) -> Decision:
        return cls(True, target_user=target_user)

    @classmethod
    def deny(
        cls, reason: Reason, *, visibility: Visibility = Visibility.HIDDEN
    ) -> Decision:
        return cls(False, reason=reason, visibility=visibility)
