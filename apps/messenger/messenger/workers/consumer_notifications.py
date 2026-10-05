"""Процесс уведомлений: из Kafka в Web Push (`consumer-notifications`).

Отдельная нагрузка и **отдельная учётная запись Kafka** `messenger-notifications`:
у неё право читать только поток фактов и свою группу, а топика содержимого
она не видит (`SEC-010`). Уведомление несёт сигнал без текста
(`11-threat-model.md`, Р-5), и право на текст ему не нужно — значит, его и
не выдано: это обеспечено правами, а не дисциплиной кода.

**Уведомление — best-effort, а не второй путь доставки.** Сообщение человек
видит в приложении; push лишь зовёт его открыть. Поэтому отказ провайдера
(`retry`) пачку **не** возвращает в поток: повторять уведомление через минуту
после того, как человек уже открыл приложение, хуже, чем потерять его. Но
недоступная **база** — исключение наверх: смещение не фиксируется, пачка
возвращается (`Subscriber.rewind`), как у соседей.

Нет ключа VAPID — нечего отправлять: процесс живёт и ждёт настройки, а не
уходит в `CrashLoopBackOff` (тот же довод, что у обработчика вложений).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time

import httpx
from prometheus_client import start_http_server

from messenger.adapters import kafka
from messenger.adapters.webpush import VapidSettings, WebPushSender
from messenger.repositories import postgres
from messenger.services import push as push_service
from messenger.services import realtime_delivery, runtime
from messenger.telemetry import metrics, tracing
from messenger.telemetry.logging import configure

configure()
log = logging.getLogger(__name__)

# Имя группы совпадает с именем группы в ACL и не совпадать не может.
GROUP = "messenger-notifications"
STAGE = "push"


def consumer_settings_from_env() -> kafka.ConsumerSettings:
    return kafka.ConsumerSettings(
        bootstrap=os.getenv("KAFKA_BOOTSTRAP", ""),
        username=os.getenv("KAFKA_USERNAME", GROUP),
        password=os.getenv("KAFKA_PASSWORD", ""),
        group_id=os.getenv("KAFKA_GROUP", GROUP),
        # Один поток: топика содержимого эта учётная запись не видит.
        topics=(realtime_delivery.FACT_TOPIC,),
    )


def vapid_from_env() -> VapidSettings | None:
    private = os.getenv("VAPID_PRIVATE_KEY", "")
    if not private:
        return None
    return VapidSettings(
        private_key_pem=private,
        subject=os.getenv("VAPID_SUBJECT", "mailto:admin@finops.local"),
    )


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def run(stop: asyncio.Event) -> None:
    vapid = vapid_from_env()
    if vapid is None:
        log.warning(
            "ключ VAPID не настроен, уведомления ждут",
            extra={"event": "push_cycle", "result": "failed", "error_code": "vapid_missing"},
        )
        while not stop.is_set():
            await _sleep(stop, 30.0)
        return

    # Без перехода по перенаправлениям: ответ провайдера не должен вести наш
    # сервер на другой адрес (SSRF через редирект).
    http = httpx.AsyncClient(timeout=5.0, follow_redirects=False)
    sender = WebPushSender(vapid, http)
    subscriber = kafka.Subscriber(settings=consumer_settings_from_env())
    pool = None

    log.info("потребитель запущен", extra={"event": "service_start", "result": "success"})

    while not stop.is_set():
        with tracing.span("consumer-notifications-cycle"):
            if pool is None:
                try:
                    pool = await postgres.create_pool(
                        runtime.pool_settings_from_env(),
                        application_name="messenger-notifications-consumer",
                    )
                except Exception as exc:  # noqa: BLE001 - причина уходит в журнал
                    log.warning("пул не открылся",
                                extra={"event": "db_pool_unavailable", "result": "failed",
                                       "error_code": type(exc).__name__})
                    await _sleep(stop, 2.0)
                    continue

            if not await subscriber.start():
                metrics.dependency_up("kafka", up=False)
                await _sleep(stop, 2.0)
                continue
            metrics.dependency_up("kafka", up=True)

            batch_size = 0
            current: kafka.KafkaRecord | None = None
            try:
                with tracing.span(
                    "consumer-notifications",
                    kind=tracing.CONSUMER,
                    attributes={"messaging.pipeline.stage": STAGE},
                ) as batch:
                    events = await subscriber.poll()
                    batch_size = len(events)
                    if events:
                        batch.set_attribute("messaging.batch.message_count", len(events))
                        started = time.perf_counter()
                        for current in events:
                            async with postgres.connection(pool) as conn:
                                outcome = await push_service.notify_message(
                                    conn, sender=sender, body=current.body
                                )
                            if outcome.sent or outcome.gone or outcome.rejected or outcome.retry:
                                log.info(
                                    "уведомления отправлены",
                                    extra={
                                        "event": "push_event", "result": "ok",
                                        "topic": current.topic, "sent": outcome.sent,
                                        "gone": outcome.gone, "retry": outcome.retry,
                                        "rejected": outcome.rejected,
                                        "skipped_online": outcome.skipped_online,
                                    },
                                )
                        await subscriber.commit()
                        metrics.consumer_processing_duration(
                            time.perf_counter() - started, consumer="notifications"
                        )
            except Exception as exc:  # noqa: BLE001 - цикл обязан пережить любой отказ
                await subscriber.rewind()
                log.warning("цикл потребителя прерван",
                            extra={"event": "push_cycle", "result": "failed",
                                   "error_code": type(exc).__name__, "batch_size": batch_size,
                                   "failed_topic": current.topic if current else None,
                                   "failed_partition": current.partition if current else None,
                                   "failed_offset": current.offset if current else None})
                await _sleep(stop, 1.0)

    await subscriber.stop()
    await http.aclose()
    if pool is not None:
        await pool.close()
    log.info("потребитель остановлен", extra={"event": "service_stop", "result": "success"})


def main() -> int:
    start_http_server(int(os.getenv("METRICS_PORT", "9100")))
    tracing.configure()

    async def _main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await run(stop)

    asyncio.run(_main())
    tracing.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
