"""Беседа и членство: состояние без сценариев создания и без базы.

В первой версии пользовательские беседы только один-на-один, но схема уже
умеет хранить группы. Поэтому тип и роль представлены явно: строка из базы
не должна незаметно превратиться в неизвестное состояние.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from messenger.domain.ids import ConversationId, ConversationSeq, UserId


class ConversationType(str, Enum):
    DIRECT = "direct"
    GROUP = "group"


class MemberRole(str, Enum):
    MEMBER = "member"
    ADMIN = "admin"


@dataclass(frozen=True, slots=True)
class Conversation:
    conversation_id: ConversationId
    type: ConversationType
    direct_key: str | None
    last_seq: ConversationSeq
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.last_seq < 0:
            raise ValueError("номер последнего сообщения не может быть отрицательным")
        if self.type is ConversationType.DIRECT and not self.direct_key:
            raise ValueError("direct_key обязателен только для беседы один-на-один")
        if self.type is ConversationType.GROUP and self.direct_key is not None:
            raise ValueError("direct_key обязателен только для беседы один-на-один")

    @property
    def is_direct(self) -> bool:
        return self.type is ConversationType.DIRECT


@dataclass(frozen=True, slots=True)
class ConversationMember:
    conversation_id: ConversationId
    user_id: UserId
    role: MemberRole
    joined_at: datetime
    left_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.left_at is not None and self.left_at < self.joined_at:
            raise ValueError("нельзя выйти из беседы до вступления")

    @property
    def is_active(self) -> bool:
        return self.left_at is None
