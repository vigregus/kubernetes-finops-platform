"""Индикатор набора («печатает») без знания о Centrifugo и Redis.

Событие эфемерное: его не сохраняют, не доставляют офлайн и не
восстанавливают (`docs/messenger/01-architecture.md`, «Эфемерные события»).
Поэтому здесь нет ни идентификаторов сообщений, ни порядка — только то,
что решает сервер: кому можно верить в теле публикации (никому) и что
уйдёт получателям.
"""
from __future__ import annotations

import uuid
from enum import Enum

# Сколько получатель держит индикатор без нового сердцебиения. Состояние
# держит **получатель**, а не сервер: закрытая вкладка не шлёт `stop`, и
# индикатор обязан погаснуть сам (`RT-001`).
EXPIRES_IN_MS = 5000
# Как часто клиент шлёт сердцебиение, пока человек набирает.
HEARTBEAT_MS = 2500

# Не чаще шести публикаций за пять секунд на пользователя и беседу: при
# сердцебиении раз в 2,5 с нужно две-три, остальное — запас на старт и стоп.
# Всё сверх — флуд, и он обрывается здесь, **до** публикации (`RT-002`).
RATE_LIMIT = 6
RATE_WINDOW_SECONDS = 5

# Общий предел на пользователя по всем беседам: без него перебор случайных
# каналов заставил бы сервер ходить в базу за членством на каждую попытку.
USER_RATE_LIMIT = 60

# Сколько живёт ответ о членстве в памяти пода. Это и есть **допустимое время
# устаревания** права публиковать набор: исключённый из беседы перестаёт
# «печатать» в неё не позже чем через эту цифру.
MEMBERSHIP_TTL_SECONDS = 15

_PREFIX = "typing:"


class TypingState(str, Enum):
    TYPING = "typing"
    STOP = "stop"


def typing_channel(conversation_id: uuid.UUID) -> str:
    return f"{_PREFIX}{conversation_id}"


def parse_typing_channel(channel: str) -> uuid.UUID | None:
    """Идентификатор беседы из имени канала или `None`, если канал чужой."""
    if not channel.startswith(_PREFIX):
        return None
    try:
        return uuid.UUID(channel[len(_PREFIX):])
    except ValueError:
        return None


def parse_client_message(data: object) -> TypingState | None:
    """Что прислал клиент. Всё, кроме `{"state": "typing"|"stop"}`, — не разбирается.

    Остальные поля тела (в том числе `user_id` и срок) отбрасываются: клиент
    напишет туда что угодно, а автора и срок задаёт сервер (`RT-003`).
    """
    if not isinstance(data, dict):
        return None
    state = data.get("state")
    if not isinstance(state, str):
        return None
    try:
        return TypingState(state)
    except ValueError:
        return None


def event_payload(user_id: str, state: TypingState) -> dict[str, object]:
    """Событие для получателей (`typingEvent` в `channels.json`)."""
    return {
        "user_id": user_id,
        "expires_in_ms": EXPIRES_IN_MS if state is TypingState.TYPING else 0,
    }
