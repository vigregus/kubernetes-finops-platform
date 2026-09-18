"""Процесс потребителя realtime: из Kafka в канал беседы.

Отдельная нагрузка по той же причине, что и отправитель: доставка
не должна останавливаться от выкатки веб-слоя, а всплеск HTTP —
замедлять её.

Смещение фиксируется после обработки пачки, а не до. Процесс, умерший
между обработкой и фиксацией, получит те же события ещё раз — и это
нормально: повтор отсеет дедупликация. Обратный порядок терял бы
события молча.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal

from prometheus_client import start_http_server

from messenger.adapters import centrifugo, event_cache, kafka
from messenger.services import realtime_delivery
from messenger.telemetry import metrics, trace, tracing
from messenger.telemetry.logging import configure

# Настройка - один раз на процесс; журнал - свой у модуля.
# Корневой журнал в поле `logger` назвался бы `root`, и по нему
# нельзя понять, чей это модуль.
configure()
log = logging.getLogger(__name__)

GROUP = "messenger-realtime"

# Стадия конвейера. По ней политика хвостовой выборки различает порог
# задержки: у доставки норма другая, чем у веб-слоя.
STAGE = "realtime"


def consumer_settings_from_env() -> kafka.ConsumerSettings:
    return kafka.ConsumerSettings(
        bootstrap=os.getenv("KAFKA_BOOTSTRAP", ""),
        username=os.getenv("KAFKA_USERNAME", "messenger-realtime"),
        password=os.getenv("KAFKA_PASSWORD", ""),
        group_id=os.getenv("KAFKA_GROUP", GROUP),
        # Оба потока одним потребителем: право на обе половины есть
        # только у него, и собрать их обратно больше некому.
        topics=(realtime_delivery.FACT_TOPIC, realtime_delivery.CONTENT_TOPIC),
    )


def cache_settings_from_env() -> event_cache.CacheSettings:
    return event_cache.CacheSettings(url=os.getenv("REDIS_URL", ""))


def centrifugo_settings_from_env() -> centrifugo.CentrifugoSettings:
    return centrifugo.CentrifugoSettings(
        api_url=os.getenv("CENTRIFUGO_API_URL", ""),
        api_key=os.getenv("CENTRIFUGO_HTTP_API_KEY", ""),
        token_hmac_secret_key=os.getenv("CENTRIFUGO_CLIENT_TOKEN_HMAC_SECRET_KEY", ""),
    )


async def run(stop: asyncio.Event) -> None:
    subscriber = kafka.Subscriber(settings=consumer_settings_from_env())
    cache = event_cache.EventCache(settings=cache_settings_from_env())
    client = centrifugo.CentrifugoClient(settings=centrifugo_settings_from_env())

    log.info("потребитель запущен", extra={"event": "service_start", "result": "success"})

    while not stop.is_set():
        if not await subscriber.start():
            metrics.dependency_up("kafka", up=False)
            await _sleep(stop, 2.0)
            continue
        metrics.dependency_up("kafka", up=True)

        # Кеш проверяется до чтения: без него нечем ни собрать пару,
        # ни отсеять повтор, и обработка превратилась бы в раздачу
        # дублей. Лучше не читать вовсе - события подождут в Kafka.
        if not await cache.healthy():
            metrics.dependency_up("redis", up=False)
            await _sleep(stop, 2.0)
            continue
        metrics.dependency_up("redis", up=True)

        try:
            # Корень на пачку, а не на запись: так решений хвостовой
            # выборки меньше, а `decision_wait` коллектора можно держать
            # коротким. Вид CONSUMER - это операция `process` по таблице
            # спецификации: потребитель обрабатывает принятую пачку.
            with tracing.span(
                "consumer-realtime",
                kind=tracing.CONSUMER,
                attributes={"messaging.pipeline.stage": STAGE},
            ) as batch:
                # А вот это `receive`, и спецификация назначает ей вид
                # CLIENT, а не CONSUMER. Неочевидно настолько, что
                # читатель, «поправивший» здесь на CONSUMER, сломает
                # разбор связи между производителем и потребителем.
                with tracing.span("kafka.consume", kind=tracing.CLIENT) as consume:
                    events = await subscriber.poll()
                    consume.set_attribute("messaging.batch.message_count", len(events))
                if events:
                    batch.set_attribute("messaging.batch.message_count", len(events))
                    for topic, headers, body in events:
                        # Контекст создателя берётся из заголовка, а тело -
                        # запасной путь: заголовок ставит отправитель, и он
                        # есть даже у записи, которую потребитель отбросит
                        # не разбирая. На него ставится ссылка, а не
                        # родительство: пачка приходит от многих отправителей,
                        # а родитель у спана только один.
                        #
                        # Своя трасса начинается здесь, а не продолжается
                        # чужая: трасса отправителя завершилась вместе
                        # с его работой, и её длительность о работе
                        # потребителя ничего не говорит.
                        await realtime_delivery.handle_event(
                            topic=topic, body=body, cache=cache, centrifugo=client,
                            link=trace.origin_from(headers, body),
                        )
                    await subscriber.commit()
        except Exception as exc:  # noqa: BLE001 - цикл обязан пережить любой отказ
            log.warning(
                "цикл потребителя прерван",
                extra={"event": "realtime_cycle", "result": "failed",
                       "error_code": type(exc).__name__},
            )
            await _sleep(stop, 1.0)

    await subscriber.stop()
    await cache.close()
    log.info("потребитель остановлен", extra={"event": "service_stop", "result": "success"})


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
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
