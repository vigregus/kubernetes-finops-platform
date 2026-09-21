"""Процесс уборки протухших realtime-соединений и снятия присутствия.

Отдельная нагрузка, а не поток внутри API, по той же причине, что
у отправителя outbox: уборка идёт своим тактом, а API отвечает на
запросы, и падение одного не должно уносить другое. Плюс такт этот
трогает строки, которые читает каждый connect-proxy, — держать его
в процессе, отвечающем пользователю, значило бы ставить уборку в очередь
за обработчиком HTTP.

**Уборщик не влияет на корректность, и это свойство, а не недосмотр.**
Соединения он только удаляет, а оба ответа системы считаются по времени:
«онлайн» — предикат по `refreshed_at` (мёртвый уборщик оставит лишние
строки, но протухшие в предикат не попадут), «был в сети» — отметка,
записанная при подтверждении, а не при уборке. Поэтому смерть процесса
стоит лишних строк в реестре, а не неверного статуса.

Смерть эта видна не тем, что публикует сам процесс, — и это важно, потому
что мёртвый процесс не публикует ничего. Признаков три, и они про разное:
`up` цели (`VMPodScrape`, получасовое окно скрейпа) отвечает на «жив ли
процесс»; `messenger_dependency_up` — на «доступна ли база», то есть на
случай «процесс жив, такт не идёт»; запись `presence_sweep` с `result` —
на «что вышло из конкретного такта». Метрики присутствия
(`messenger_presence_online_users` и доля) для этого не годятся: они
считаются по времени и при мёртвом уборщике остаются верными.

Входящих запросов нет, поэтому нет и пробы готовности: ради неё нечего
ответить. HTTP-слушатель поднимается единственный — отдать `/metrics`.

Kafka процессу не нужна: он смотрит на собственную таблицу, а не читает
поток. Это единственная нагрузка мессенджера без прав в шине — и `SEC-010`
на неё не распространяется по той же причине, по которой не распространяется
на уборку мусора.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal

from prometheus_client import start_http_server

from messenger.repositories import postgres
from messenger.services import presence, runtime
from messenger.telemetry import metrics, tracing
from messenger.telemetry.logging import configure

# Настройка — один раз на процесс; журнал — свой у модуля.
configure()
log = logging.getLogger(__name__)

# Как часто убирать. Заметно чаще окна живости (180 с, `domain/presence.py`):
# уборка — это то, что ограничивает размер реестра, и реже окна она
# означала бы, что протухшие строки живут в несколько раз дольше, чем
# протухли. Запас в двенадцать раз — не точность, а устойчивость к
# пропущенным тактам: `_sleep` прерывается сигналом, а не копится.
DEFAULT_SWEEP_SECONDS = 15.0


def sweep_seconds_from_env() -> float:
    return float(os.getenv("PRESENCE_SWEEP_SECONDS", str(DEFAULT_SWEEP_SECONDS)))


async def run(stop: asyncio.Event) -> None:
    sweep_seconds = sweep_seconds_from_env()
    pool = None

    log.info("уборщик присутствия запущен",
             extra={"event": "service_start", "result": "success"})

    while not stop.is_set():
        if pool is None:
            # Пул поднимается лениво и переподключается сам: недоступная
            # база — это пропущенный такт уборки, а не повод уходить
            # в CrashLoopBackOff.
            try:
                pool = await postgres.create_pool(
                    runtime.pool_settings_from_env(),
                    application_name="messenger-presence-sweeper",
                )
            except Exception as exc:  # noqa: BLE001 - причина уходит в журнал
                metrics.dependency_up("postgres", up=False)
                log.warning("пул не открылся",
                            extra={"event": "db_pool_unavailable", "result": "failed",
                                   "error_code": type(exc).__name__})
                await _sleep(stop, 2.0)
                continue

        # Спан вокруг всей итерации, а не вокруг одного запроса: такт —
        # это три запроса под одной границей окна, и разложить его на
        # трассы значило бы показать три несвязанных события там, где
        # происходит одно.
        with tracing.span("presence-sweep-cycle"):
            try:
                async with postgres.connection(pool) as conn:
                    result = await presence.sweep(conn)
            except Exception as exc:  # noqa: BLE001 - цикл обязан пережить любой отказ
                metrics.dependency_up("postgres", up=False)
                log.warning("такт уборки присутствия прерван",
                            extra={"event": "presence_sweep", "result": "failed",
                                   "error_code": type(exc).__name__})
                await _sleep(stop, 1.0)
                continue

        metrics.dependency_up("postgres", up=True)
        metrics.presence_online(online=result.online_users, share=result.share)
        # Запись на каждый такт, а не только когда убрали что-то: молчащий
        # уборщик и уборщик, которому нечего убирать, различимы только по
        # этому. Число удалённых — в записи, а не вместо неё.
        log.info("такт уборки присутствия завершён",
                 extra={"event": "presence_sweep", "result": "ok",
                        "deleted": result.deleted,
                        "online_users": result.online_users})
        await _sleep(stop, sweep_seconds)

    if pool is not None:
        await pool.close()
    log.info("уборщик присутствия остановлен",
             extra={"event": "service_stop", "result": "success"})


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    """Пауза, которую прерывает сигнал остановки.

    Обычный `sleep` задержал бы выключение пода на всю свою длину,
    и kubelet добивал бы процесс по истечении срока — посреди такта.
    """
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
    # Досылка остатка спанов. Ограничена по времени: выключение пода
    # не должно ждать мёртвый коллектор.
    tracing.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
