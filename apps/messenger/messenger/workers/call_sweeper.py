"""Подметальщик звонков: завершает те, у которых вышел срок.

Три срока (`domain/call.py`): звонит дольше `RING_SECONDS` — «пропущенный»;
принят, но медиа не пошло за `SETUP_SECONDS` — «не соединился»; активен, а
подтверждений нет `STALE_SECONDS` — «тишина». Итог пишется в ленту, линия
участников освобождается, обеим сторонам уходит событие.

**Корректность звонка от него не зависит**, и это свойство, а не недосмотр:
просроченный `ringing` отвергается при любом действии над ним, а уникальное
ограничение на живой звонок снимается завершением. Мёртвый подметальщик стоит
звонков, которые «звонят» дольше, и линий, занятых дольше, — но не неверных
состояний. Смерть процесса видна по `up` цели и по записи `call_sweep` с
`result` в журнале, а не по тому, что публикует сам процесс.

Kafka не нужна: он смотрит на собственные таблицы. Итог звонка ложится в outbox
тем же путём, что сообщение, а отправляет его `outbox-relay`.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal

from prometheus_client import start_http_server

from messenger.repositories import postgres
from messenger.services import calls, runtime
from messenger.telemetry import metrics, tracing
from messenger.telemetry.logging import configure

configure()
log = logging.getLogger(__name__)

# Заметно чаще самого короткого срока (30 с звонка): «пропущенный» не должен
# опаздывать на заметную долю срока.
DEFAULT_SWEEP_SECONDS = 5.0


def sweep_seconds_from_env() -> float:
    return float(os.getenv("CALL_SWEEP_SECONDS", str(DEFAULT_SWEEP_SECONDS)))


async def run(stop: asyncio.Event) -> None:
    sweep_seconds = sweep_seconds_from_env()
    realtime = runtime.centrifugo_client_from_env()
    pool = None

    log.info("подметальщик звонков запущен",
             extra={"event": "service_start", "result": "success"})

    while not stop.is_set():
        if pool is None:
            # Пул поднимается лениво: недоступная база — это пропущенный такт,
            # а не повод уходить в CrashLoopBackOff.
            try:
                pool = await postgres.create_pool(
                    runtime.pool_settings_from_env(),
                    application_name="messenger-call-sweeper",
                )
            except Exception as exc:  # noqa: BLE001 - причина уходит в журнал
                metrics.dependency_up("postgres", up=False)
                log.warning("пул не открылся",
                            extra={"event": "db_pool_unavailable", "result": "failed",
                                   "error_code": type(exc).__name__})
                await _sleep(stop, 2.0)
                continue

        with tracing.span("call-sweep-cycle"):
            try:
                async with postgres.connection(pool) as conn:
                    result = await calls.sweep(conn, realtime=realtime)
            except Exception as exc:  # noqa: BLE001 - цикл обязан пережить любой отказ
                metrics.dependency_up("postgres", up=False)
                log.warning("такт подметания звонков прерван",
                            extra={"event": "call_sweep", "result": "failed",
                                   "error_code": type(exc).__name__})
                await _sleep(stop, 1.0)
                continue

        metrics.dependency_up("postgres", up=True)
        # Запись на каждый такт, а не только когда что-то завершили: молчащий
        # подметальщик и подметальщик, которому нечего завершать, различимы
        # только по этому.
        log.info("такт подметания звонков завершён",
                 extra={"event": "call_sweep", "result": "ok",
                        "examined": result.examined, "ended": result.ended})
        await _sleep(stop, sweep_seconds)

    if pool is not None:
        await pool.close()
    log.info("подметальщик звонков остановлен",
             extra={"event": "service_stop", "result": "success"})


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    """Пауза, которую прерывает сигнал остановки."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


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
