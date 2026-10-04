"""«Печатает»: решение по публикации клиента (publish-proxy Centrifugo).

Почему через сервер, а не напрямую, как сказано в `01-architecture.md`:
ограничение частоты публикаций у самого Centrifugo — возможность его платной
редакции, а открытая отдаёт канал «как есть». Без прокси клиент заливает
события набора без предела (`RT-002`) и подписывает их чужим автором
(`RT-003`). Сама архитектура называет прокси публикации вариантом для
строгого контура; здесь он выбран потому, что **без него обе проверки
невыполнимы**, а не из осторожности.

Цена названа: каждое сердцебиение проходит через API. Для стенда это
единицы в секунду; на ёмкости из `01-architecture.md` (7 500 /с) выбор
пересматривается — либо прямая публикация с лимитом PRO, либо более редкое
сердцебиение, либо отдельная лёгкая нагрузка под этот маршрут.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from messenger.adapters.ratelimit import OnFailure, RateLimiter
from messenger.domain import typing_indicator as domain
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


async def authorize_publish(
    *,
    limiter: RateLimiter,
    user_id: str,
    channel: str,
    data: object,
) -> PublishDecision:
    decision = await _decide(limiter=limiter, user_id=user_id, channel=channel, data=data)
    metrics.typing_event(decision.result)
    return decision


async def _decide(
    *,
    limiter: RateLimiter,
    user_id: str,
    channel: str,
    data: object,
) -> PublishDecision:
    if not enabled():
        return PublishDecision(error_code=503, result="disabled")

    conversation_id = domain.parse_typing_channel(channel)
    if conversation_id is None:
        return PublishDecision(error_code=403, result="forbidden")

    state = domain.parse_client_message(data)
    if state is None:
        return PublishDecision(error_code=400, result="invalid")

    # Подписку на `typing:{id}` Centrifugo выдаёт по тикету и разрешает
    # публиковать только подписчику, поэтому членство здесь заново не
    # проверяется: это был бы поход в Postgres на каждое сердцебиение.
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
