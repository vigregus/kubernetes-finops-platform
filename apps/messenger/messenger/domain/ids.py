"""Идентификаторы как разные типы.

`UserId` и `ConversationId` внутри одинаковые — оба UUID, — но перепутать их
в вызове нельзя. Проверка типов ловит это до запуска; `UUID` в сигнатуре
не ловит ничего, а два подряд идущих идентификатора однажды меняются местами,
и это сообщение, ушедшее не в ту беседу.

Инженерный стандарт требует собственных типов вместо `str` и `UUID` — здесь
они и заведены.
"""
from __future__ import annotations

import uuid
from typing import NewType

UserId = NewType("UserId", uuid.UUID)
DeviceId = NewType("DeviceId", uuid.UUID)
SessionId = NewType("SessionId", uuid.UUID)
ConversationId = NewType("ConversationId", uuid.UUID)
MessageId = NewType("MessageId", uuid.UUID)
AttachmentId = NewType("AttachmentId", uuid.UUID)
EventId = NewType("EventId", uuid.UUID)

# Идентификатор, который выдаёт клиент. Отдельный тип от MessageId
# намеренно: это разные вещи, и глоссарий стандарта называет их путаницу
# самой дорогой мелочью в проекте.
ClientMessageId = NewType("ClientMessageId", uuid.UUID)

# Номер внутри беседы. Не время создания: часы разъезжаются, а два
# сообщения могут прийти в одну миллисекунду.
ConversationSeq = NewType("ConversationSeq", int)


def new_message_id() -> MessageId:
    return MessageId(uuid.uuid4())


def new_event_id() -> EventId:
    return EventId(uuid.uuid4())


def direct_key(a: UserId, b: UserId) -> str:
    """Ключ беседы один-на-один: упорядоченная пара участников.

    Порядок участников не должен влиять на результат — иначе встречное
    создание даст два разных ключа и две беседы вместо одной. Уникальный
    индекс по этому значению и есть решение гонки `CONV-001`: вторая
    транзакция падает на ограничении, а не создаёт дубль.
    """
    if a == b:
        raise ValueError("беседа один-на-один с самим собой не создаётся")
    first, second = sorted((str(a), str(b)))
    return f"{first}:{second}"
