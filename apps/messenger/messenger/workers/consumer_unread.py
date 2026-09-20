"""Процесс потребителя непрочитанного: из Kafka в проекцию.

Отдельная нагрузка по той же причине, что и у соседей: потребитель
обязан догонять поток при выкатке веб-слоя, а всплеск HTTP — не
замедлять его. Но есть и своя: у процесса **другая учётная запись
Kafka**. `messenger-unread` имеет Read/Describe только на поток факта
и на свою группу, а топика содержимого не видит вовсе, — то есть
`SEC-010` обеспечивается правами, а не дисциплиной кода
(`gitops/04-messenger/messenger-kafka-topics/manifests/users.yaml`).
Слить этого потребителя с realtime значило бы выдать проекции право
на текст сообщений, которого ей не нужно.

**Транзакцию открывает сервис, а не воркер.** Граница согласованности
здесь — «замок беседы плюс обе записи», и лежать она обязана там же,
где берётся замок. Воркер, забывший открыть транзакцию, не получил бы
никакой ошибки: `lock_offsets` отпустил бы строку сразу, а отказ между
записью проекции и сдвигом чекпойнта оставил бы расхождение, которое
нашлось бы только сверкой. Подробности — в `services/unread.py`.

**Транзакция на событие, фиксация смещения на пачку.** Одна транзакция
на пачку дешевле, но связала бы судьбы событий разных бесед: отказ на
одном откатил бы уже применённые чужие. Группировка по `conversation_id`
внутри пачки (один замок на беседу за пачку) — очевидная следующая
оптимизация, и она сознательно **не** делается: она меняет семантику
отказа, а нагрузка, ради которой она нужна, в v1 не заявлена. Записано,
чтобы не изобреталось заново.

**Смещение фиксируется после обработки, а не до.** Процесс, умерший
между обработкой и фиксацией, получит те же события ещё раз — и это
нормально: повтор отсекает монотонный чекпойнт беседы, а не надежда.
Обратный порядок терял бы события молча.

**Отказ асимметричен, и это решение.** Недоступная база — исключение
наверх: смещение не фиксируется, потому что применение идемпотентно и
повтор дешевле любого разбирательства. Негодное событие — исход
`invalid`: оно пропускается, а пачка фиксируется, иначе очередь встала
бы навсегда, а очереди отклонённых записей у нас нет. Пропуск при этом
виден в журнале всегда, и это единственная защита от него.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time

from prometheus_client import start_http_server

from messenger.adapters import kafka
from messenger.domain.unread import UnreadOutcome, UnreadOutcomeKind
from messenger.repositories import postgres
from messenger.services import realtime_delivery, runtime, unread
from messenger.telemetry import metrics, tracing
from messenger.telemetry.logging import configure

# Настройка - один раз на процесс; журнал - свой у модуля.
# Корневой журнал в поле `logger` назвался бы `root`, и по нему
# нельзя понять, чей это модуль.
configure()
log = logging.getLogger(__name__)

# Имя группы совпадает с именем группы в ACL и не совпадать не может:
# список прав выдаёт доступ к группе по имени, и разошедшаяся строка
# обернулась бы `GroupAuthorizationFailed` на первом же чтении.
GROUP = "messenger-unread"

# Стадия конвейера. По ней политика хвостовой выборки различает порог
# задержки: у проекции норма другая, чем у веб-слоя.
STAGE = "unread"


def consumer_settings_from_env() -> kafka.ConsumerSettings:
    return kafka.ConsumerSettings(
        bootstrap=os.getenv("KAFKA_BOOTSTRAP", ""),
        username=os.getenv("KAFKA_USERNAME", GROUP),
        password=os.getenv("KAFKA_PASSWORD", ""),
        group_id=os.getenv("KAFKA_GROUP", GROUP),
        # Один поток, и второй сюда не добавится по правам: топика
        # содержимого эта учётная запись не видит, а проекции оно и не
        # нужно — считать непрочитанное по факту появления достаточно.
        topics=(realtime_delivery.FACT_TOPIC,),
    )


async def run(stop: asyncio.Event) -> None:
    subscriber = kafka.Subscriber(settings=consumer_settings_from_env())
    pool = None

    log.info("потребитель запущен", extra={"event": "service_start", "result": "success"})

    while not stop.is_set():
        # Спан вокруг всей итерации, а не только вокруг обработки пачки:
        # `subscriber.start()` при переподключении выполняется
        # инструментированным клиентом и создаёт собственный спан
        # независимо от того, открыт ли уже какой-то span в контексте.
        # Без внешнего span он завёл бы СВОЮ трассу на каждую итерацию —
        # тот же класс проблемы, что в `workers/consumer_realtime.py`
        # и в `repositories/postgres.py`.
        with tracing.span("consumer-unread-cycle"):
            # Пул поднимается лениво и переподключается сам: недоступная
            # база — это пауза в проекции, а не повод уходить
            # в CrashLoopBackOff.
            if pool is None:
                try:
                    pool = await postgres.create_pool(
                        runtime.pool_settings_from_env(),
                        application_name="messenger-unread-consumer",
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

            try:
                # Корень пачки - дочерний относительно span'а цикла, а не
                # трассы: так решений хвостовой выборки меньше, а
                # `decision_wait` коллектора можно держать коротким. Вид
                # CONSUMER - это операция `process` по таблице
                # спецификации: потребитель обрабатывает принятую пачку.
                with tracing.span(
                    "consumer-unread",
                    kind=tracing.CONSUMER,
                    attributes={"messaging.pipeline.stage": STAGE},
                ) as batch:
                    events = await subscriber.poll()
                    if events:
                        batch.set_attribute("messaging.batch.message_count", len(events))
                        # T_consumer: от получения пачки до фиксации
                        # смещения. Считается только когда есть что
                        # обрабатывать - иначе время между пустыми
                        # опросами (оно же `poll` timeout) смешалось бы
                        # с временем настоящей работы, и гистограмма
                        # отвечала бы не на «долго ли обрабатывали»,
                        # а на «сколько раз в Kafka ничего не было».
                        processing_started = time.perf_counter()
                        for topic, _headers, body in events:
                            # Заголовки не читаются, и это не потеря.
                            # Ссылка на создателя ставится на спан
                            # события, а спан здесь один на пачку — пачка
                            # приходит от многих отправителей, а родитель
                            # у спана только один.
                            #
                            # Соединение берётся на событие, а не на
                            # пачку: транзакция открывается внутри
                            # `apply_event`, и делить её между событиями
                            # разных бесед нельзя.
                            async with postgres.connection(pool) as conn:
                                outcome = await unread.apply_event(conn, body=body)
                            _report(topic=topic, outcome=outcome)
                        await subscriber.commit()
                        metrics.consumer_processing_duration(
                            time.perf_counter() - processing_started, consumer="unread"
                        )
                        # Запись об успешном цикле - только когда было что
                        # обрабатывать. Пустой опрос повторяется раз
                        # в секунду, и «ok» на каждое его срабатывание
                        # утопило бы журнал записями ни о чём: тот же
                        # довод, по которому длительность выше считается
                        # не на пустой пачке.
                        log.info("цикл потребителя завершён",
                                 extra={"event": "unread_cycle", "result": "ok",
                                        # Число событий в пачке, а не строк
                                        # проекции: `affected` в записи об
                                        # исходе события считает строки, и
                                        # одно имя на два разных числа
                                        # читалось бы как одно и то же.
                                        "events": len(events)})
            except Exception as exc:  # noqa: BLE001 - цикл обязан пережить любой отказ
                # Смещение не фиксируется: пачка придёт заново, а повтор
                # применения бесплатен - событие отсеет чекпойнт беседы.
                log.warning("цикл потребителя прерван",
                            extra={"event": "unread_cycle", "result": "failed",
                                   "error_code": type(exc).__name__})
                await _sleep(stop, 1.0)

    await subscriber.stop()
    if pool is not None:
        await pool.close()
    log.info("потребитель остановлен", extra={"event": "service_stop", "result": "success"})


def _report(*, topic: str, outcome: UnreadOutcome) -> None:
    """Запись об исходе одного события.

    Топик берётся здесь, а не в сервисе: сервису он не нужен — тот
    подписан на один поток и второго у него нет по правам, — а журналу
    нужен, потому что запись обязана отвечать на «откуда это пришло»
    без похода в конфигурацию нагрузки.

    Уровень зависит от исхода, и это не украшение. Негодное событие
    пропускается навсегда — очереди отклонённых записей нет, — и
    единственное, что делает пропуск заметным, это запись о нём.
    `warning` тут единственный уровень, который кто-то увидит.

    Отсутствующие значения полей пишутся `null`, а не выбрасываются:
    у постороннего события и у негодного беседы нет вовсе, и запись
    без ключа читалась бы как «поле потерялось при сериализации».

    `extra` собран словарём на месте вызова, а не в переменной, и
    уровень поэтому выбран ветвью, а не значением: контракт журналов
    читается разбором исходника (`scripts/check-log-streams.py`), и
    запись, у которой `extra` лежит в переменной, для него неотличима
    от записи без события вовсе. Список полей от этого продублирован,
    и это осознанная цена: он виден целиком в одном месте диффа.
    """
    conversation_id = (
        str(outcome.conversation_id) if outcome.conversation_id is not None else None
    )
    conversation_seq = (
        int(outcome.conversation_seq) if outcome.conversation_seq is not None else None
    )
    if outcome.kind is UnreadOutcomeKind.INVALID:
        log.warning(
            "событие пропущено",
            extra={
                "event": "unread_event", "result": outcome.kind.value, "topic": topic,
                "reason": outcome.reason, "conversation_id": conversation_id,
                "conversation_seq": conversation_seq, "affected": outcome.affected,
            },
        )
    else:
        log.info(
            "событие применено",
            extra={
                "event": "unread_event", "result": outcome.kind.value, "topic": topic,
                "reason": outcome.reason, "conversation_id": conversation_id,
                "conversation_seq": conversation_seq, "affected": outcome.affected,
            },
        )


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    """Пауза, которую прерывает сигнал остановки.

    Обычный `sleep` задержал бы выключение пода на всю свою длину,
    и kubelet добивал бы процесс по истечении срока - посреди цикла.
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
