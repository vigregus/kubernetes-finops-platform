"""Доставка в канал беседы: сборка пары и защита от повтора.

Ни Kafka, ни Redis, ни Centrifugo: проверяется решение потребителя —
когда он публикует, когда ждёт вторую половину и когда молчит.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from messenger.services import realtime_delivery as delivery

MESSAGE_ID = str(uuid.uuid4())
CONVERSATION_ID = str(uuid.uuid4())
SENDER_ID = str(uuid.uuid4())

ФАКТ = {
    "event_type": "message.created",
    "message_id": MESSAGE_ID,
    "conversation_id": CONVERSATION_ID,
    "conversation_seq": 7,
    "sender_id": SENDER_ID,
    "recipient_ids": [str(uuid.uuid4())],
}
СОДЕРЖИМОЕ = {
    "event_type": "message.content",
    "message_id": MESSAGE_ID,
    "conversation_id": CONVERSATION_ID,
    "payload": {"text": "привет"},
}


class FakeCache:
    """Кеш в памяти с той же семантикой, что у Redis."""

    def __init__(self) -> None:
        self.halves: dict[str, dict] = {}
        self.delivered: set[str] = set()
        self.forgotten: list[str] = []

    async def remember_half(self, *, message_id, half, body):
        other = "content" if half == "fact" else "fact"
        self.halves[f"{message_id}:{half}"] = body
        return self.halves.get(f"{message_id}:{other}")

    async def mark_delivered(self, *, message_id):
        if message_id in self.delivered:
            return False
        self.delivered.add(message_id)
        return True

    async def forget(self, *, message_id):
        self.forgotten.append(message_id)


class FakeCentrifugo:
    def __init__(self, *, accepts: bool = True) -> None:
        self.published: list[tuple[str, dict]] = []
        self.accepts = accepts

    async def publish(self, channel, data):
        self.published.append((channel, data))
        return self.accepts


def обработать(topic, body, cache, centrifugo):
    return asyncio.run(
        delivery.handle_event(topic=topic, body=body, cache=cache, centrifugo=centrifugo)
    )


@pytest.fixture
def кеш():
    return FakeCache()


def test_одна_половина_только_ждёт(кеш):
    """Отдать факт без содержимого значило бы показать пустое сообщение,
    а потом заменить его текстом — клиент увидел бы мигание."""
    centrifugo = FakeCentrifugo()
    исход = обработать(delivery.FACT_TOPIC, ФАКТ, кеш, centrifugo)
    assert исход.buffered == 1 and исход.delivered == 0
    assert centrifugo.published == []


def test_пара_собирается_в_любом_порядке(кеш):
    """Порядок гарантирован внутри партиции, а не между топиками."""
    centrifugo = FakeCentrifugo()
    обработать(delivery.CONTENT_TOPIC, СОДЕРЖИМОЕ, кеш, centrifugo)
    исход = обработать(delivery.FACT_TOPIC, ФАКТ, кеш, centrifugo)

    assert исход.delivered == 1
    канал, событие = centrifugo.published[0]
    assert канал == f"conversation:{CONVERSATION_ID}"
    assert событие["type"] == "message.created"
    assert событие["seq"] == 7
    assert событие["payload"] == {"text": "привет"}


def test_список_получателей_наружу_не_уходит(кеш):
    """Иначе каждый участник узнаёт полный список получателей чужого
    сообщения."""
    centrifugo = FakeCentrifugo()
    обработать(delivery.FACT_TOPIC, ФАКТ, кеш, centrifugo)
    обработать(delivery.CONTENT_TOPIC, СОДЕРЖИМОЕ, кеш, centrifugo)
    _, событие = centrifugo.published[0]
    assert "recipient_ids" not in событие


def test_повтор_не_доходит_до_клиента_дважды(кеш):
    """CONS-001: при повторной доставке видимое сообщение остаётся одно.

    Транспорт выбран at-least-once сознательно, значит повтор обязан
    появиться — и обязан быть отсеян здесь, а не замечен пользователем.
    """
    centrifugo = FakeCentrifugo()
    обработать(delivery.FACT_TOPIC, ФАКТ, кеш, centrifugo)
    первый = обработать(delivery.CONTENT_TOPIC, СОДЕРЖИМОЕ, кеш, centrifugo)

    # Обе половины приходят ещё раз — так выглядит перезапуск потребителя
    # до фиксации смещения.
    обработать(delivery.FACT_TOPIC, ФАКТ, кеш, centrifugo)
    второй = обработать(delivery.CONTENT_TOPIC, СОДЕРЖИМОЕ, кеш, centrifugo)

    assert первый.delivered == 1
    assert второй.duplicates == 1
    assert len(centrifugo.published) == 1


def test_событие_без_обязательных_полей_не_публикуется(кеш):
    centrifugo = FakeCentrifugo()
    исход = обработать(delivery.FACT_TOPIC, {"event_type": "message.created"},
                       кеш, centrifugo)
    assert исход.failed == 1 and centrifugo.published == []


def test_отказ_centrifugo_виден_в_исходе(кеш):
    centrifugo = FakeCentrifugo(accepts=False)
    обработать(delivery.FACT_TOPIC, ФАКТ, кеш, centrifugo)
    исход = обработать(delivery.CONTENT_TOPIC, СОДЕРЖИМОЕ, кеш, centrifugo)
    assert исход.failed == 1 and исход.delivered == 0


def test_собранная_пара_убирается_из_кеша(кеш):
    """Половины больше не нужны: держать их значит платить памятью
    за каждое доставленное сообщение."""
    centrifugo = FakeCentrifugo()
    обработать(delivery.FACT_TOPIC, ФАКТ, кеш, centrifugo)
    обработать(delivery.CONTENT_TOPIC, СОДЕРЖИМОЕ, кеш, centrifugo)
    assert кеш.forgotten == [MESSAGE_ID]
