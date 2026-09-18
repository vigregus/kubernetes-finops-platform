"""Процесс отправителя outbox.

Отдельная нагрузка, а не поток внутри API. Причина не в аккуратности:
отправитель и API масштабируются по-разному и падают по-разному. Поток
внутри API означал бы, что выкатка веб-слоя останавливает доставку,
а всплеск HTTP-нагрузки замедляет её.

Входящих запросов у процесса нет, поэтому нет и пробы готовности.
Живость видна иначе — по возрасту самой старой неотправленной записи,
и это метрика, а не проба. Ради неё процесс поднимает единственный
HTTP-слушатель: отдать `/metrics` и ничего больше.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import uuid

from prometheus_client import start_http_server

from messenger.adapters import kafka
from messenger.repositories import postgres
from messenger.services import outbox_relay, runtime
from messenger.telemetry import metrics
from messenger.telemetry.logging import configure

log = configure()

# Как часто снимать показатели очереди. Реже, чем идёт цикл: возраст
# самой старой записи не меняется скачками, а запрос к базе на каждый
# оборот пустого цикла - это нагрузка ради нуля.
QUEUE_REPORT_SECONDS = 15.0


def publisher_settings_from_env() -> kafka.ProducerSettings:
    return kafka.ProducerSettings(
        bootstrap=os.getenv("KAFKA_BOOTSTRAP", ""),
        username=os.getenv("KAFKA_USERNAME", "messenger-outbox"),
        password=os.getenv("KAFKA_PASSWORD", ""),
    )


def relay_settings_from_env() -> outbox_relay.RelaySettings:
    # Имя владельца аренды - имя пода: оно уникально на реплику и видно
    # в кластере. Случайный идентификатор пришлось бы искать по журналам,
    # чтобы понять, чью аренду он держит.
    owner = os.getenv("HOSTNAME") or f"relay-{uuid.uuid4().hex[:8]}"
    return outbox_relay.RelaySettings(
        owner=owner,
        batch_size=int(os.getenv("OUTBOX_BATCH_SIZE", "100")),
        lease_seconds=int(os.getenv("OUTBOX_LEASE_SECONDS", "60")),
        idle_sleep_seconds=float(os.getenv("OUTBOX_IDLE_SLEEP_SECONDS", "0.5")),
    )


async def run(stop: asyncio.Event) -> None:
    settings = relay_settings_from_env()
    publisher = kafka.Publisher(settings=publisher_settings_from_env())
    pool = None
    reported_at = 0.0

    log.info("отправитель запущен", extra={"event": "service_start",
                                           "result": "success", "owner": settings.owner})

    while not stop.is_set():
        # Пул и продюсер поднимаются лениво и переподключаются сами:
        # недоступные база или брокер - это пауза в доставке,
        # а не повод падать и уходить в CrashLoopBackOff.
        if pool is None:
            try:
                pool = await postgres.create_pool(
                    runtime.pool_settings_from_env(), application_name="messenger-outbox-relay"
                )
            except Exception as exc:  # noqa: BLE001 - причина уходит в журнал
                log.warning("пул не открылся", extra={"event": "db_pool_unavailable",
                                                      "result": "failed",
                                                      "error_code": type(exc).__name__})
                await _sleep(stop, 2.0)
                continue

        if not await publisher.start():
            metrics.dependency_up("kafka", up=False)
            await _sleep(stop, 2.0)
            continue
        metrics.dependency_up("kafka", up=True)

        try:
            async with postgres.connection(pool) as conn:
                outcome = await outbox_relay.publish_batch(
                    conn, publisher=publisher, settings=settings
                )
                now = asyncio.get_running_loop().time()
                if now - reported_at >= QUEUE_REPORT_SECONDS:
                    await outbox_relay.report_queue(conn)
                    reported_at = now
        except Exception as exc:  # noqa: BLE001 - цикл обязан пережить любой отказ
            log.warning("цикл отправителя прерван", extra={"event": "outbox_cycle",
                                                           "result": "failed",
                                                           "error_code": type(exc).__name__})
            await _sleep(stop, 1.0)
            continue

        # Пусто - подождать; была работа - сразу следующий круг,
        # иначе очередь разгребается со скоростью паузы.
        if outcome.total == 0:
            await _sleep(stop, settings.idle_sleep_seconds)

    await publisher.stop()
    if pool is not None:
        await pool.close()
    log.info("отправитель остановлен", extra={"event": "service_stop", "result": "success"})


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    """Пауза, которую прерывает сигнал остановки.

    Обычный `sleep` задержал бы выключение пода на всю свою длину,
    и kubelet добивал бы процесс по истечении срока - посреди цикла.
    """
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


def main() -> int:
    start_http_server(int(os.getenv("METRICS_PORT", "9100")))

    async def _main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await run(stop)

    asyncio.run(_main())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
