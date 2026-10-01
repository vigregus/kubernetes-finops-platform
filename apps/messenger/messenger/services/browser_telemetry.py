"""Браузерная телеметрия (G3-008): best-effort приём, запись в метрики.

Эта точка не защищает ни одного инварианта продукта - она отвечает на
другой вопрос ("сколько это заняло", не "доставлено ли"; квитанция и
телеметрия - разные плоскости, IMPLEMENTATION-PLAN.md, G3-008). Поэтому
здесь нет транзакции и нет Problem/Reason: один негодный элемент пачки не
роняет остальные и не возвращает отказ транспорту, который всё равно
отправлен через `sendBeacon` и ответа не читает.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import asyncpg

from messenger.domain.ids import MessageId
from messenger.repositories import messages as messages_repo
from messenger.telemetry import metrics

log = logging.getLogger(__name__)

# Единственный тип события, для которого считается T_delivery. Остальные
# одиннадцать из 06-observability.md только инкрементируют BROWSER_EVENTS -
# разбирать их тело дальше нечем, они и не несут ничего, кроме типа и
# момента.
_DELIVERY_ACK = "delivery_ack"


@dataclass(frozen=True, slots=True)
class TelemetryEvent:
    event_type: str
    occurred_at: datetime
    message_id: MessageId | None


async def ingest(conn: asyncpg.Connection, *, events: list[TelemetryEvent]) -> None:
    for event in events:
        metrics.browser_event(event.event_type)
        if event.event_type == _DELIVERY_ACK:
            await _record_delivery(conn, event)


async def _record_delivery(conn: asyncpg.Connection, event: TelemetryEvent) -> None:
    if event.message_id is None:
        # Контракт уже отверг это 422-м на уровне транспорта (модель
        # требует message_id у delivery_ack) - сервис не доверяет ему
        # второй раз тем же правилом, что и `receipts_service`, но здесь
        # это защита от будущего вызова мимо API, а не ожидаемый путь.
        log.warning(
            "delivery_ack без message_id",
            extra={"event": "browser_telemetry", "result": "failed",
                   "error_code": "missing_message_id"},
        )
        return

    created_at = await messages_repo.fetch_created_at(conn, message_id=event.message_id)
    if created_at is None:
        # Сообщение не найдено - идентификатор устарел (ростер сообщений
        # не чистится отдельно от беседы) или сфабрикован. Не ошибка
        # транспорта и не повод отвечать отказом на best-effort пути,
        # ответа которого никто не читает (`sendBeacon`): тихое
        # игнорирование честнее придуманного кода.
        log.info(
            "delivery_ack на неизвестное сообщение",
            extra={"event": "browser_telemetry", "result": "success",
                   "message_id": str(event.message_id)},
        )
        return

    delta = (event.occurred_at - created_at).total_seconds()
    # Отрицательное значение - рассинхронизация часов браузера и сервера,
    # а не доставка быстрее отправки; тот же довод, что у серверного
    # приближения T_delivery (`services/realtime_delivery.py`).
    if delta >= 0:
        metrics.browser_delivery_duration(delta)
