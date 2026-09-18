"""Доставка сообщения в канал беседы.

Потребитель получает две половины одного сообщения из разных потоков —
факт и содержимое — и отдаёт клиенту одно событие. Разделение нужно
не клиенту, а правам доступа (`SEC-010`): потребитель непрочитанных
получает факт и не получает текста. Собрать их обратно — работа того,
кто имеет право на оба.

Повтор переживается дедупликацией. `CONS-001` формулирует это так:
при повторной доставке видимое сообщение остаётся одно.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from messenger.adapters import event_cache
from messenger.telemetry import metrics

log = logging.getLogger(__name__)

FACT_TOPIC = "messenger.events.v1"
CONTENT_TOPIC = "messenger.content.v1"


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    delivered: int = 0
    # Половина легла ждать свою пару — это не отказ, а обычный ход дела.
    buffered: int = 0
    duplicates: int = 0
    failed: int = 0


def channel_for(conversation_id: str) -> str:
    """Имя канала по контракту каналов, а не по договорённости."""
    return f"conversation:{conversation_id}"


def _client_event(fact: dict[str, Any], content: dict[str, Any]) -> dict[str, Any]:
    """Событие в форме, которую ждёт клиент.

    Из факта берётся порядок и отправитель, из содержимого — payload.
    Ничего лишнего: `recipient_ids` наружу не уходит, иначе каждый
    участник узнаёт полный список получателей чужого сообщения.
    """
    return {
        "type": "message.created",
        "message_id": fact.get("message_id"),
        "seq": fact.get("conversation_seq"),
        "sender_id": fact.get("sender_id"),
        "payload": content.get("payload", {}),
    }


async def handle_event(
    *,
    topic: str,
    body: dict[str, Any],
    cache: event_cache.EventCache,
    centrifugo,
) -> DeliveryOutcome:
    """Обрабатывает одну запись из потока.

    Публикация идёт только когда собраны обе половины. Отдать факт
    без содержимого значило бы показать в беседе пустое сообщение,
    а потом заменить его текстом — клиент увидел бы мигание.
    """
    message_id = body.get("message_id")
    conversation_id = body.get("conversation_id")
    if not message_id or not conversation_id:
        metrics.realtime_delivery("malformed")
        log.error(
            "событие без обязательных полей",
            extra={"event": "realtime_delivery", "result": "failed",
                   "error_code": "malformed", "topic": topic},
        )
        return DeliveryOutcome(failed=1)

    half = "fact" if topic == FACT_TOPIC else "content"
    other = await cache.remember_half(message_id=message_id, half=half, body=body)
    if other is None:
        metrics.realtime_delivery("buffered")
        return DeliveryOutcome(buffered=1)

    fact, content = (body, other) if half == "fact" else (other, body)

    # Отметка до публикации, а не после: повтор, пришедший в этот же
    # миг в соседнюю реплику, обязан увидеть занятый ключ. Цена ошибки
    # в другую сторону — сообщение, отданное дважды, то есть ровно то,
    # что запрещает CONS-001.
    if not await cache.mark_delivered(message_id=message_id):
        metrics.realtime_delivery("duplicate")
        log.info(
            "повторная доставка отсеяна",
            extra={"event": "realtime_duplicate", "result": "success",
                   "message_id": str(message_id)},
        )
        return DeliveryOutcome(duplicates=1)

    published = await centrifugo.publish(
        channel_for(str(conversation_id)), _client_event(fact, content)
    )
    if not published:
        metrics.realtime_delivery("failed")
        log.warning(
            "Centrifugo не принял публикацию",
            extra={"event": "realtime_delivery", "result": "failed",
                   "error_code": "publish_rejected", "message_id": str(message_id),
                   "dependency": "centrifugo"},
        )
        return DeliveryOutcome(failed=1)

    await cache.forget(message_id=message_id)
    metrics.realtime_delivery("delivered")
    return DeliveryOutcome(delivered=1)
