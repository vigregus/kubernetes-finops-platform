"""Процесс обработки вложений и уборки неприкреплённых (G4).

Отдельная нагрузка по той же причине, что и отправитель outbox: обработка
файла (чтение из хранилища, сканер) и уборка не должны делить процесс с
API, у которого свои задержки и свой масштаб. Приоритет у неё ниже
(`messenger-background`): при нехватке CPU планировщик вытеснит сначала её.

Уборщик живёт здесь же, а не отдельным подом: он раз в несколько минут
делает один запрос, и второй процесс ради этого — лишний подвижный
компонент на одном узле.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import uuid

from prometheus_client import start_http_server

from messenger.adapters import scanner as scanner_adapter
from messenger.adapters.object_store import ObjectStore, ObjectStoreSettings
from messenger.repositories import attachments as attachments_repo
from messenger.repositories import postgres
from messenger.services import attachments as attachment_service
from messenger.services import runtime
from messenger.telemetry import metrics, tracing
from messenger.telemetry.logging import configure

configure()
log = logging.getLogger(__name__)

IDLE_SLEEP_SECONDS = float(os.getenv("ATTACHMENT_IDLE_SLEEP_SECONDS", "1.0"))
CLEANUP_EVERY_SECONDS = float(os.getenv("ATTACHMENT_CLEANUP_EVERY_SECONDS", "300"))
REPORT_EVERY_SECONDS = 15.0


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def run(stop: asyncio.Event) -> None:
    owner = os.getenv("HOSTNAME") or f"attachments-{uuid.uuid4().hex[:8]}"
    store_settings = ObjectStoreSettings.from_env()
    if store_settings is None:
        # Без хранилища воркеру делать нечего. Падать нельзя: CrashLoopBackOff
        # на стенде без MinIO — шум, а не сигнал. Он живёт и ждёт настройки.
        log.warning(
            "объектное хранилище не настроено, обработка вложений ждёт",
            extra={"event": "attachment_process", "result": "failed",
                   "error_code": "store_not_configured"},
        )
        while not stop.is_set():
            await _sleep(stop, 30.0)
        return

    store = ObjectStore(store_settings)
    scanner = scanner_adapter.scanner_from_env()
    pool = None
    loop = asyncio.get_running_loop()
    last_cleanup = 0.0
    last_report = 0.0

    log.info(
        "обработчик вложений запущен",
        extra={"event": "service_start", "result": "success", "owner": owner},
    )

    while not stop.is_set():
        if pool is None:
            try:
                pool = await postgres.create_pool(
                    runtime.pool_settings_from_env(), application_name="messenger-attachments"
                )
            except Exception as exc:  # noqa: BLE001 - причина уходит в журнал
                log.warning(
                    "пул не открылся",
                    extra={"event": "db_pool_unavailable", "result": "failed",
                           "error_code": type(exc).__name__},
                )
                await _sleep(stop, 2.0)
                continue

        try:
            async with postgres.connection(pool) as conn:
                outcome = await attachment_service.process_batch(
                    conn, store=store, scanner=scanner, owner=owner
                )
                now = loop.time()
                if now - last_report >= REPORT_EVERY_SECONDS:
                    metrics.attachment_processing_age(
                        await attachments_repo.oldest_processing_age_seconds(conn)
                    )
                    last_report = now
                if now - last_cleanup >= CLEANUP_EVERY_SECONDS:
                    await attachment_service.cleanup_orphans(conn, store=store)
                    last_cleanup = now
        except Exception as exc:  # noqa: BLE001 - цикл обязан пережить любой отказ
            log.warning(
                "цикл обработчика вложений прерван",
                extra={"event": "attachment_process", "result": "failed",
                       "error_code": type(exc).__name__},
            )
            await _sleep(stop, 2.0)
            continue

        if outcome.ready + outcome.rejected + outcome.failed + outcome.deferred == 0:
            await _sleep(stop, IDLE_SLEEP_SECONDS)

    await store.close()
    if pool is not None:
        await pool.close()


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
