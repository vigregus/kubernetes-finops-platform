"""Отправитель outbox против живых Postgres и Kafka.

Вход отправителя — таблица outbox, поэтому проверка кладёт запись прямо
в неё и смотрит, что произошло с брокером. Это не обход API: HTTP-маршрут
отправки сообщения — отдельная задача, а доставка события обязана работать
независимо от того, кто это событие записал.

Проверяется ровно то, чего не видно в коде: что аренда и отметка сходятся
на живой базе, что запись действительно уходит в поток и что запись
с неизвестным типом события никуда не уходит и не застревает в голове
очереди.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import UTC, datetime

from aiokafka import AIOKafkaConsumer, TopicPartition

from messenger.repositories.postgres import PoolSettings, create_pool

failures: list[str] = []

BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "messenger-kafka-kafka-bootstrap.kafka.svc:9092")
KAFKA_USER = os.getenv("KAFKA_USERNAME", "messenger-outbox")
KAFKA_PASSWORD = os.environ.get("KAFKA_PASSWORD", "")
TOPIC = "messenger.events.v1"
# Столько ждём отправителя. Он опрашивает очередь дважды в секунду,
# поэтому секунды хватает с запасом; десять — на случай, если он
# в этот момент переподключается к брокеру.
DEADLINE_SECONDS = 20


def ok(what: str) -> None:
    print(f"  \033[32m✓\033[0m {what}")


def bad(what: str, detail: str = "") -> None:
    print(f"  \033[31m✗\033[0m {what}")
    if detail:
        print(f"      {detail}")
    failures.append(what)


def check(what: str, condition: bool, detail: str = "") -> None:
    ok(what) if condition else bad(what, detail)


def pool_settings() -> PoolSettings:
    return PoolSettings(
        host=os.getenv("DATABASE_HOST", "messenger-db-pool"),
        port=int(os.getenv("DATABASE_PORT", "5432")),
        database=os.getenv("DATABASE_NAME", "messenger"),
        user=os.getenv("DATABASE_USER", "messenger"),
        password=os.getenv("DATABASE_PASSWORD", ""),
        min_size=1,
        max_size=2,
    )


async def end_offsets_sum() -> int:
    """Конец лога по всем партициям.

    Учётная запись отправителя умеет только писать и смотреть метаданные,
    поэтому здесь именно `end_offsets`, а не чтение: право на чтение
    этого потока у неё отсутствует намеренно.
    """
    consumer = AIOKafkaConsumer(
        bootstrap_servers=BOOTSTRAP,
        security_protocol="SASL_PLAINTEXT",
        sasl_mechanism="SCRAM-SHA-512",
        sasl_plain_username=KAFKA_USER,
        sasl_plain_password=KAFKA_PASSWORD,
        enable_auto_commit=False,
    )
    await consumer.start()
    try:
        parts = consumer.partitions_for_topic(TOPIC) or set()
        offsets = await consumer.end_offsets([TopicPartition(TOPIC, p) for p in parts])
        return sum(offsets.values())
    finally:
        await consumer.stop()


async def insert_event(pool, *, event_type: str) -> tuple[int, uuid.UUID]:
    event_id = uuid.uuid4()
    message_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    payload = {
        "event_version": 1,
        "event_id": str(event_id),
        "event_type": event_type,
        "occurred_at": datetime.now(UTC).isoformat(),
        "message_id": str(message_id),
        "conversation_id": str(conversation_id),
        "conversation_seq": 1,
        "sender_id": str(uuid.uuid4()),
        "type": "text",
    }
    record_id = await pool.fetchval(
        """
        INSERT INTO outbox (aggregate_type, aggregate_id, event_type, event_version,
                            partition_key, event_id, payload)
        VALUES ('message', $1, $2, 1, $3, $4, $5::jsonb)
        RETURNING id
        """,
        message_id, event_type, str(conversation_id), event_id,
        json.dumps(payload, ensure_ascii=False),
    )
    return record_id, event_id


async def wait_published(pool, record_id: int) -> bool:
    for _ in range(DEADLINE_SECONDS * 2):
        published = await pool.fetchval(
            "SELECT published_at FROM outbox WHERE id = $1", record_id
        )
        if published is not None:
            return True
        await asyncio.sleep(0.5)
    return False


async def run() -> None:
    pool = await create_pool(pool_settings(), application_name="messenger-integration")
    created_ids: list[int] = []
    try:
        before = await end_offsets_sum()

        # --- обычное событие ----------------------------------------
        record_id, _ = await insert_event(pool, event_type="message.created")
        created_ids.append(record_id)
        published = await wait_published(pool, record_id)
        check("отправитель забрал запись и отметил её", published,
              f"за {DEADLINE_SECONDS} с отметки не появилось")

        after = await end_offsets_sum()
        check("событие действительно попало в поток", after > before,
              f"конец лога: до={before} после={after}")

        row = await pool.fetchrow(
            "SELECT lease_owner, lease_until, attempts, last_error FROM outbox WHERE id = $1",
            record_id,
        )
        check("аренда снята после отправки",
              row["lease_owner"] is None and row["lease_until"] is None, str(dict(row)))
        check("успешная отправка не считается попыткой отказа",
              row["attempts"] == 0 and row["last_error"] is None, str(dict(row)))

        # --- событие неизвестного типа -------------------------------
        # Такая запись не должна уходить никуда: поток по умолчанию
        # означал бы утечку содержимого туда, где ему не место.
        before_unknown = await end_offsets_sum()
        bad_id, _ = await insert_event(pool, event_type="message.выдуманное")
        created_ids.append(bad_id)

        for _ in range(10):
            attempts = await pool.fetchval(
                "SELECT attempts FROM outbox WHERE id = $1", bad_id
            )
            if attempts and attempts > 0:
                break
            await asyncio.sleep(0.5)

        state = await pool.fetchrow(
            "SELECT published_at, attempts, last_error, lease_until FROM outbox WHERE id = $1",
            bad_id,
        )
        check("неизвестный тип не опубликован", state["published_at"] is None)
        check("неудача записана с причиной",
              state["attempts"] > 0 and "неизвестный тип" in (state["last_error"] or ""),
              str(dict(state)))
        check("следующая попытка отодвинута", state["lease_until"] is not None)
        check("в поток ничего не ушло", await end_offsets_sum() == before_unknown)

        # --- очередь не встала ---------------------------------------
        # Застрявшая запись не должна закрывать собой следующие:
        # очередь берётся по id, а неудачная отодвинута арендой.
        tail_id, _ = await insert_event(pool, event_type="message.created")
        created_ids.append(tail_id)
        check("очередь продолжает двигаться после застрявшей записи",
              await wait_published(pool, tail_id))
    finally:
        if created_ids:
            await pool.execute("DELETE FROM outbox WHERE id = ANY($1::bigint[])", created_ids)
        await pool.close()


def main() -> int:
    asyncio.run(run())
    if failures:
        print(f"\nне выполнено проверок: {len(failures)}")
        return 1
    print("\nотправитель outbox подтверждён живой публикацией в Kafka")
    return 0


if __name__ == "__main__":
    sys.exit(main())
