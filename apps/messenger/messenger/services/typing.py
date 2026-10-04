"""«Печатает»: решение по публикации клиента (publish-proxy Centrifugo).

Почему через сервер, а не напрямую, как сказано в `01-architecture.md`:
ограничение частоты публикаций у самого Centrifugo — возможность его платной
редакции, а открытая отдаёт канал «как есть». Без прокси клиент заливает
события набора без предела (`RT-002`) и подписывает их чужим автором
(`RT-003`). Сама архитектура называет прокси публикации вариантом для
строгого контура; здесь он выбран потому, что **без него обе проверки
невыполнимы**, а не из осторожности.

Цена названа: каждое сердцебиение проходит через API (членство — из кеша пода
на 15 с, в Postgres только при промахе). Для стенда это
единицы в секунду; на ёмкости из `01-architecture.md` (7 500 /с) выбор
пересматривается — либо прямая публикация с лимитом PRO, либо более редкое
сердцебиение, либо отдельная лёгкая нагрузка под этот маршрут.
"""
from __future__ import annotations

import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import asyncpg

from messenger.adapters.ratelimit import OnFailure, RateLimiter
from messenger.domain import typing_indicator as domain
from messenger.domain.ids import ConversationId, UserId
from messenger.repositories import conversations as conversation_repo
from messenger.telemetry import metrics


@dataclass(frozen=True, slots=True)
class PublishDecision:
    # Что отдать Centrifugo вместо тела клиента.
    payload: dict[str, object] | None = None
    # Код отказа, если публикация не допущена: 400 тело, 403 канал, 429 флуд,
    # 503 возможность выключена.
    error_code: int | None = None
    result: str = "published"

    @property
    def allowed(self) -> bool:
        return self.error_code is None


def enabled() -> bool:
    """Аварийный выключатель «печатает». Выключается первым, потери нет.

    Пока значение берётся из окружения процесса — это названный зазор:
    `CFG-001` требует применять выключатель **без выкатки**, а для этого нужно
    хранилище флагов (G5). Здесь стоит то, что есть, и оно честно требует
    перезапуска.
    """
    return os.getenv("TYPING_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


# Член ли пользователь беседы. Читает Postgres — вызывается только при промахе
# кеша, поэтому приходит параметром, а не импортируется: сервису нечего знать
# про соединения.
IsMember = Callable[[str, uuid.UUID], Awaitable[bool]]

_MEMBERS: dict[tuple[str, uuid.UUID], tuple[bool, float]] = {}
_MEMBERS_MAX = 10_000


async def is_active_member(
    conn: asyncpg.Connection, user_id: str, conversation_id: uuid.UUID
) -> bool:
    """Действующий участник: запись есть и `left_at` пуст."""
    try:
        uid = UserId(uuid.UUID(user_id))
    except ValueError:
        return False
    member = await conversation_repo.fetch_member(
        conn, conversation_id=ConversationId(conversation_id), user_id=uid
    )
    return member is not None and member.left_at is None


def reset_membership_cache() -> None:
    _MEMBERS.clear()


async def _member(
    user_id: str, conversation_id: uuid.UUID, is_member: IsMember, now: Callable[[], float]
) -> bool:
    """Членство с кешем в памяти пода на `MEMBERSHIP_TTL_SECONDS`.

    Подписку на `typing:{id}` Centrifugo выдаёт по тикету, но при включённом
    publish-proxy он **делегирует** право публикации прокси и не требует
    подписки (измерено: не участник публиковал в канал). Поэтому членство
    проверяет сам прокси — без этого `SEC-008` не выполнен.
    """
    key = (user_id, conversation_id)
    cached = _MEMBERS.get(key)
    moment = now()
    if cached is not None and cached[1] > moment:
        return cached[0]
    answer = await is_member(user_id, conversation_id)
    if len(_MEMBERS) >= _MEMBERS_MAX:
        _MEMBERS.clear()
    _MEMBERS[key] = (answer, moment + domain.MEMBERSHIP_TTL_SECONDS)
    return answer


async def authorize_publish(
    *,
    limiter: RateLimiter,
    user_id: str,
    channel: str,
    data: object,
    is_member: IsMember,
    now: Callable[[], float] = time.monotonic,
) -> PublishDecision:
    decision = await _decide(
        limiter=limiter, user_id=user_id, channel=channel, data=data,
        is_member=is_member, now=now,
    )
    metrics.typing_event(decision.result)
    return decision


async def _decide(
    *,
    limiter: RateLimiter,
    user_id: str,
    channel: str,
    data: object,
    is_member: IsMember,
    now: Callable[[], float],
) -> PublishDecision:
    if not enabled():
        return PublishDecision(error_code=503, result="disabled")

    conversation_id = domain.parse_typing_channel(channel)
    if conversation_id is None:
        return PublishDecision(error_code=403, result="forbidden")

    state = domain.parse_client_message(data)
    if state is None:
        return PublishDecision(error_code=400, result="invalid")

    # Сначала дешёвое и общее: предел на пользователя по всем беседам. Он
    # ограничивает число походов в базу за членством, если кто-то перебирает
    # случайные каналы.
    overall = await limiter.take(
        f"typing:{user_id}",
        limit=domain.USER_RATE_LIMIT,
        window_seconds=domain.RATE_WINDOW_SECONDS,
        on_failure=OnFailure.DENY,
    )
    if not overall.allowed:
        return PublishDecision(error_code=429, result="rate_limited")

    if not await _member(user_id, conversation_id, is_member, now):
        return PublishDecision(error_code=403, result="forbidden")

    decision = await limiter.take(
        f"typing:{user_id}:{conversation_id}",
        limit=domain.RATE_LIMIT,
        window_seconds=domain.RATE_WINDOW_SECONDS,
        # Потерять набор нормально; недоступный счётчик — не повод открыть флуд.
        on_failure=OnFailure.DENY,
    )
    if not decision.allowed:
        return PublishDecision(error_code=429, result="rate_limited")

    return PublishDecision(payload=domain.event_payload(user_id, state))
