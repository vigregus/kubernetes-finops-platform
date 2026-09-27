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

**Публикация — вне блока соединения, и это то же правило.** Тот, кто
записал проекцию, обязан сказать об этом адресату: без события вторая
вкладка не сойдётся с первой, а сходимость и есть выходной критерий
гейта (`RCP-002`). Соединение закрывается вместе с транзакцией сервиса,
поэтому публикация стоит **после** `async with postgres.connection(...)`:
событие, ушедшее внутри, пережило бы откат — `publish` отменить нельзя, —
и канал разошёлся бы с базой в ту сторону, из которой восстановления
нет. Отказ Centrifugo исход события не меняет: запись состоялась, а
потерянное событие клиент добегает сверкой по REST (`D11`).

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

**Незавершённая пачка возвращается в поток.** Одной несостоявшейся
фиксации для этого мало, и выяснилось это живым прогоном, а не
рассуждением: `getmany` сдвигает внутреннюю позицию потребителя в момент
выдачи записей, поэтому отказ на середине пачки оставлял её хвост
прочитанным и неприменённым **навсегда** — не «до перезапуска», а
вообще, потому что перезапуска в этом месте не бывает. Поэтому цикл
в ветке отказа зовёт `subscriber.rewind()`: позиция возвращается к началу
последней выданной пачки, и следующий опрос отдаёт её снова. Цена названа
вслух — разобранный префикс пачки приезжает второй раз, — и она
допустима: применение идемпотентно. Порядок именно такой: возврат,
потом пауза; отказ самого возврата наверх не подавляется, потому что
`rewind` — операция в памяти и падать ей не на чем.

**Отказ асимметричен, и это решение.** Недоступная база — исключение
наверх: смещение не фиксируется, а пачка возвращается в поток, потому
что применение идемпотентно и повтор дешевле любого разбирательства.
Негодное событие — исход `invalid`: оно пропускается, а пачка фиксируется,
иначе очередь встала бы навсегда, а очереди отклонённых записей у нас
нет. Пропуск при этом виден в журнале всегда, и это единственная защита
от него.

**Долг: журнал — не лечение.** Пропуск виден, но не исправлен. Если
негодное событие (или запись, не разобравшаяся ещё в `adapters.kafka.poll`)
оказалось для беседы последним, следующего не будет — значит, не будет
и разрыва номеров, которым проекция пересобирается, — и чекпойнт
останется ниже головы беседы. Восстановление на чтении её тоже не
тронет: оно включается отсутствием строки, а не отставанием
(`services/unread`, `_restore_conversation`). Проекция останется
отставшей **навсегда**, и по журналу это будет видно, а по числам —
нет. Настоящая защита — карантин записей, сверка проекции с источником
истины или ручная пересборка; это отдельная работа, а не `G3-003`:
внутренний продюсер обязан выпускать валидный `message.created`,
а очереди отклонённых записей (DLQ) в первой версии нет. Записано,
чтобы видимость пропуска не читалась как его безвредность.
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
    # Публикация непрочитанного — вторая половина работы процесса, и клиент
    # у неё один на процесс, как у соседа (`consumer_realtime`). Строится
    # он здесь, а не лениво вместе с пулом: `None` от него означает не
    # «подождать», а «Centrifugo не сконфигурирован», и в этом случае
    # проекция всё равно считается, а числа доезжают списком бесед.
    realtime = runtime.centrifugo_client_from_env()
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

            # Координаты записи, на которой цикл прервался, и размер пачки.
            # Заводятся до `try`: отказать может и сам опрос, когда никакой
            # записи ещё не было, — и тогда `null` в журнале правда, а
            # падение на неинициализированном имени нет.
            batch_size = 0
            current: kafka.KafkaRecord | None = None

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
                    batch_size = len(events)
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
                        for current in events:
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
                                outcome = await unread.apply_event(conn, body=current.body)
                            # Публикация — после закрытия соединения, то
                            # есть после коммита. Пустой список адресатов
                            # (повтор, чужое событие, беседа без
                            # собеседников) не стоит ничего: цикл по
                            # пустому кортежу, а не развилка, которую
                            # пришлось бы держать в согласии с исходом.
                            await unread.announce_changed(
                                realtime=realtime, notices=outcome.notices
                            )
                            _report(topic=current.topic, outcome=outcome)
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
                # Возврат, а не только отказ от фиксации: `getmany` уже
                # сдвинул позицию за выданные записи, и без `seek` хвост
                # незавершённой пачки живому процессу не вернётся - ни
                # этим циклом, ни следующим, ни через сутки.
                await subscriber.rewind()
                log.warning("цикл потребителя прерван",
                            extra={"event": "unread_cycle", "result": "failed",
                                   "error_code": type(exc).__name__,
                                   # Координаты незавершённой записи. Следующий
                                   # такой отказ обязан сразу отвечать на «какую
                                   # запись мы не закончили», а не заставлять
                                   # сверять поток с проекцией руками.
                                   "batch_size": batch_size,
                                   "failed_topic": current.topic if current else None,
                                   "failed_partition": (
                                       current.partition if current else None
                                   ),
                                   "failed_offset": current.offset if current else None})
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
