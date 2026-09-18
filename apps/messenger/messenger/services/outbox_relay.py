"""Отправитель outbox: аренда пачки, публикация, отметка.

Три шага в строго этом порядке, и порядок — единственное, что отделяет
«как минимум один раз» от «может быть, ни разу».

    аренда (транзакция закрыта)  →  публикация (сеть)  →  отметка

Публиковать до аренды нельзя: тогда две реплики отправят одно и то же
одновременно. Отмечать до публикации нельзя: процесс, умерший в этот
момент, потеряет событие навсегда — а потерянное событие означает
сообщение, которое отправитель видит у себя, а получатель не увидит
никогда.

Умереть между публикацией и отметкой можно, и это допущено сознательно:
аренда истечёт, запись вернётся в очередь и уйдёт в Kafka второй раз.
Дубль отсеивает потребитель по `event_id` — он для того и лежит
в заголовке записи, чтобы не разбирать ради этого тело.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum

import asyncpg

from messenger.adapters import kafka
from messenger.domain.outbox import OutboxRecord, PublishOutcome, backoff_for
from messenger.repositories import outbox
from messenger.telemetry import metrics, trace, tracing

log = logging.getLogger(__name__)

# Стадия конвейера. По ней политика хвостовой выборки различает порог
# задержки: у отправителя и у веб-слоя разная норма, и общий порог
# удерживал бы всё, что медленно по меркам одного из них.
STAGE = "relay"

# Отсрочка для записи, которую повтор не исправит: тип события неизвестен
# коду. Час, а не пять минут: она не мешает остальным (очередь берётся
# по id, а не по этой записи), а частые попытки только шумят в журнале.
UNROUTABLE_BACKOFF_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class RelaySettings:
    """Кто арендует, сколько и на какой срок."""

    # Имя владельца аренды. Уникальное на реплику - иначе две реплики
    # отмечают чужие записи как свои.
    owner: str
    batch_size: int = 100
    # Аренда заметно длиннее таймаута публикации: иначе запись вернётся
    # в очередь, пока первый отправитель ещё ждёт ответа брокера,
    # и дубль будет создан не отказом, а настройкой.
    lease_seconds: int = 60
    # Пауза, когда очередь пуста. Не ноль: пустой цикл без паузы - это
    # запрос к базе в каждый такт процессора.
    idle_sleep_seconds: float = 0.5


async def publish_batch(
    conn: asyncpg.Connection, *, publisher: kafka.Publisher, settings: RelaySettings
) -> PublishOutcome:
    """Один цикл: взять пачку, опубликовать, отметить.

    Соединение передаётся, а не берётся изнутри, по общему правилу:
    так цикл можно вызвать из теста в чужой транзакции и откатить всё,
    что он сделал.
    """
    # Корень на пачку, а не на запись. Так решений хвостовой выборки
    # меньше, а `decision_wait` коллектора можно держать коротким: трасса
    # живёт секунды, а не минуты. У отправителя вид INTERNAL: он не
    # принимает и не отправляет сообщение в смысле спецификации, а
    # разбирает свою очередь.
    with tracing.span(
        "outbox-relay", attributes={"messaging.pipeline.stage": STAGE}
    ) as batch_span:
        with tracing.span("outbox.claim"):
            records = await outbox.claim_batch(
                conn,
                owner=settings.owner,
                limit=settings.batch_size,
                lease_seconds=settings.lease_seconds,
            )
        if not records:
            return PublishOutcome()
        batch_span.set_attribute("messenger.outbox.claimed", len(records))

        published: list[int] = []
        failed = 0
        unroutable = 0

        for record in records:
            # Контекст создателя берётся из тела события: contextvar не
            # переживает ни коммит, ни Kafka. На него ставится ссылка -
            # родителем он быть не может, отправитель работает после
            # того, как запрос API уже ответил клиенту.
            outcome = await _publish_one(
                conn,
                record=record,
                origin=trace.origin_from_body(record.payload),
                publisher=publisher,
                settings=settings,
            )

            if outcome is _Outcome.UNROUTABLE:
                unroutable += 1
                continue
            if outcome is _Outcome.FAILED:
                failed += 1
                # Дальше по пачке не идём: порядок внутри беседы важнее
                # пропускной способности, а следующая запись может быть
                # содержимым того же сообщения.
                break
            published.append(record.id)

        marked = await outbox.mark_published(conn, ids=published, owner=settings.owner)
        if marked != len(published):
            # Отметились не все: чью-то аренду успел перехватить другой
            # отправитель. Это не поломка, но именно так выглядит дубль
            # в Kafka, и знать об этом надо.
            metrics.outbox_event("lease_lost", "")
            log.warning(
                "отметить удалось не все опубликованные записи",
                extra={"event": "outbox_lease_lost", "result": "failed",
                       "published": len(published), "marked": marked},
            )
        metrics.outbox_published(marked)
        return PublishOutcome(published=marked, failed=failed, unroutable=unroutable)


class _Outcome(Enum):
    """Исход одной записи. Ради него `_publish_one` и вынесена отдельно."""

    PUBLISHED = "published"
    FAILED = "failed"
    UNROUTABLE = "unroutable"


async def _publish_one(
    conn: asyncpg.Connection,
    *,
    record: OutboxRecord,
    origin: trace.Origin | None,
    publisher: kafka.Publisher,
    settings: RelaySettings,
) -> _Outcome:
    """Маршрут, публикация, разметка неудачи — для одной записи.

    Вынесено из цикла не ради длины: спан — менеджер контекста,
    а `break` и `continue` из-под него читаются как выход из спана,
    хотя выходят из цикла. Возвращённый исход такой двусмысленности
    не оставляет.
    """
    topic = record.topic
    message_id = record.payload.get("message_id")
    if topic is None:
        metrics.outbox_event("unroutable", record.event_type)
        log.error(
            "неизвестный тип события в outbox",
            extra={"event": "outbox_unroutable", "result": "failed",
                   "error_code": "unknown_event_type",
                   "event_id": str(record.event_id),
                   "message_id": message_id},
        )
        await outbox.record_failure(
            conn,
            record_id=record.id,
            owner=settings.owner,
            error=f"неизвестный тип события: {record.event_type}",
            retry_after=timedelta(seconds=UNROUTABLE_BACKOFF_SECONDS),
        )
        return _Outcome.UNROUTABLE

    # Ссылка на контекст создания записи, а не родительство. Родителем
    # спан запроса быть не может: к этому моменту процесс API уже
    # ответил клиенту, и трасса отправителя приклеилась бы к давно
    # завершённой чужой, потеряв собственный смысл длительности.
    #
    # Записи от отправителя прежней версии участка не несут вовсе -
    # поле появилось вместе с этой работой. Подставить случайный
    # значило бы соврать; вместо ссылки остаётся то, что есть.
    links = [(origin.trace_id, origin.span_id)] if origin and origin.span_id else []
    attributes = {
        "messaging.system": "kafka",
        "messaging.destination.name": topic,
    }
    if message_id:
        attributes["messaging.message.id"] = str(message_id)

    with tracing.span(
        "kafka.produce", kind=tracing.PRODUCER, links=links, attributes=attributes
    ) as span:
        if origin is not None and not origin.span_id:
            span.set_attribute("messenger.source_trace_id", origin.trace_id)
        try:
            await publisher.publish(
                topic=topic,
                key=record.partition_key,
                value=record.payload,
                # Заголовки, чтобы потребитель отсеивал повтор, не
                # разбирая тело: дедупликация обязана быть дешевле
                # обработки, иначе шторм повторов стоит как шторм работы.
                #
                # `traceparent` здесь по той же причине: потребителю,
                # который отбросил повтор, тело разбирать незачем, а
                # написать в журнал, к какой трассе относится отброшенное,
                # всё равно надо.
                #
                # Контекст берётся у самого спана, а не собирается из
                # идентификаторов заново: потребитель поставит ссылку
                # на него, и она обязана указывать на существующий спан,
                # а не в пустоту.
                headers={
                    "event_id": str(record.event_id),
                    "event_type": record.event_type,
                    "event_version": str(record.event_version),
                    trace.HEADER: tracing.traceparent(span),
                },
            )
        except Exception as exc:  # noqa: BLE001 - причина уходит в базу и метку
            metrics.outbox_event("failed", record.event_type)
            tracing.mark_failed(span, type(exc).__name__)
            log.warning(
                "событие не опубликовано",
                extra={"event": "outbox_publish", "result": "failed",
                       "error_code": type(exc).__name__,
                       "event_id": str(record.event_id),
                       "message_id": message_id, "dependency": "kafka"},
            )
            await outbox.record_failure(
                conn,
                record_id=record.id,
                owner=settings.owner,
                error=f"{type(exc).__name__}: {exc}",
                retry_after=backoff_for(record.attempts + 1),
            )
            return _Outcome.FAILED

    return _Outcome.PUBLISHED


async def report_queue(conn: asyncpg.Connection) -> None:
    """Снимает показатели очереди. Возраст важнее длины.

    Очередь из тысячи событий возрастом в секунду — норма; одна запись
    возрастом в час — отказ, и оповещение строится именно на возрасте.
    """
    metrics.outbox_queue(
        pending=await outbox.pending_count(conn),
        oldest_age_seconds=await outbox.oldest_pending_age_seconds(conn),
    )
