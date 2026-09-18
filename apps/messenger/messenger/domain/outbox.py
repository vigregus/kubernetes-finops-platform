"""Запись outbox и правила её жизни.

Outbox существует ради одного обещания: сообщение и событие о нём либо
появляются оба, либо не появляется ни одного. Обещание держится тем, что
их пишет одна транзакция, а отправкой занимается кто-то другой и потом.

Отсюда всё остальное. Отправитель не может публиковать внутри транзакции —
сетевой вызов держал бы блокировки строк ровно столько, сколько длится
таймаут Kafka. Значит, между «взял в работу» и «опубликовал» есть окно,
и в этом окне процесс может умереть. Поэтому доставка здесь **как минимум
один раз**, а не ровно один: дубль в Kafka неизбежен, и отсеивает его
потребитель по `event_id`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from messenger.domain.ids import EventId

# Куда какой поток. Разделение факта и содержимого — не деталь
# развёртывания, а требование SEC-010: потребитель непрочитанных обязан
# получать факт появления сообщения, не получая его текста. Поэтому
# соответствие живёт в домене, а не в настройках отправителя.
TOPICS: dict[str, str] = {
    "message.created": "messenger.events.v1",
    "message.content": "messenger.content.v1",
}

# После скольких неудач запись считается безнадёжной. Не «бесконечно»:
# запись, которую невозможно опубликовать, иначе навсегда остаётся
# в голове очереди и закрывает собой все следующие.
MAX_ATTEMPTS = 10

# Пределы отсрочки. Нижний — чтобы отказ Kafka не превращался в цикл
# без пауз; верхний — чтобы восстановившийся брокер не ждал полчаса.
MIN_BACKOFF = timedelta(seconds=1)
MAX_BACKOFF = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    """Событие, ожидающее отправки. Неизменяемое: правки — через базу."""

    id: int
    event_id: EventId
    event_type: str
    event_version: int
    partition_key: str
    payload: dict[str, Any]
    attempts: int
    created_at: datetime

    @property
    def topic(self) -> str | None:
        """Поток для этого типа события; `None` — тип неизвестен.

        `None`, а не исключение и не «поток по умолчанию»: событие
        неизвестного типа, ушедшее в поток фактов, — это утечка
        содержимого туда, где его не должно быть.
        """
        return TOPICS.get(self.event_type)

    @property
    def exhausted(self) -> bool:
        return self.attempts >= MAX_ATTEMPTS


def backoff_for(attempts: int) -> timedelta:
    """Отсрочка перед следующей попыткой. Удваивается, но не бесконечно.

    Считается от числа уже сделанных попыток, а не от времени последней:
    время последней попытки — это ещё одна колонка, которую придётся
    держать согласованной, а удвоение и так даёт нужную форму.
    """
    if attempts <= 0:
        return MIN_BACKOFF
    doubled = MIN_BACKOFF * (2 ** min(attempts - 1, 20))
    return min(doubled, MAX_BACKOFF)


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    """Что случилось с пачкой. Считается вызывающим, пишется метрикой."""

    published: int = 0
    failed: int = 0
    # Записи, для которых не нашлось потока. Отдельно от `failed`:
    # повтор их не исправит, и всплеск здесь означает рассинхронизацию
    # кода и схем, а не отказ Kafka.
    unroutable: int = 0

    @property
    def total(self) -> int:
        return self.published + self.failed + self.unroutable
