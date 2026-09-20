"""Публикация в Kafka. Единственное место, знающее про брокер.

Продюсер идемпотентный и с `acks=all`. Это не перестраховка: без `acks=all`
брокер подтверждает запись до того, как её получили реплики, и «отправлено»
начинает означать «возможно, отправлено». Без идемпотентности повтор внутри
самого клиента — а он повторяет сам, при любой сетевой икоте — кладёт вторую
копию записи в лог, и дубль появляется там, где его никто не создавал.

Дубль всё равно возможен — отправитель может умереть между публикацией
и отметкой в базе, — но тогда он хотя бы виден как повтор `event_id`,
а не как две записи с разными смещениями и одинаковым содержимым.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.errors import KafkaError
from aiokafka.structs import TopicPartition

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProducerSettings:
    """Куда и от чьего имени публиковать."""

    bootstrap: str
    username: str
    password: str
    # SASL поверх открытого соединения: внутри кластера трафик и так
    # проходит через mesh, а второй TLS означал бы вторую цепочку доверия
    # рядом с той, которой уже владеет платформа.
    security_protocol: str = "SASL_PLAINTEXT"
    sasl_mechanism: str = "SCRAM-SHA-512"
    # Сколько ждать подтверждения. Короче, чем аренда записи в outbox:
    # иначе аренда истечёт раньше, чем придёт ответ, и ту же запись
    # заберёт второй отправитель.
    request_timeout_ms: int = 10_000


@dataclass(slots=True)
class Publisher:
    """Продюсер с отложенным подключением.

    Подключение не в конструкторе: процесс обязан подниматься при
    недоступном брокере — иначе перезапуск Kafka превращается
    в CrashLoopBackOff отправителя, и когда брокер вернётся,
    отправитель будет сидеть в экспоненциальной паузе.
    """

    settings: ProducerSettings
    _producer: AIOKafkaProducer | None = field(default=None)

    async def start(self) -> bool:
        if self._producer is not None:
            return True
        producer = AIOKafkaProducer(
            bootstrap_servers=self.settings.bootstrap,
            security_protocol=self.settings.security_protocol,
            sasl_mechanism=self.settings.sasl_mechanism,
            sasl_plain_username=self.settings.username,
            sasl_plain_password=self.settings.password,
            enable_idempotence=True,
            acks="all",
            request_timeout_ms=self.settings.request_timeout_ms,
        )
        # Порядка ради здесь нет `max_in_flight_requests_per_connection`:
        # aiokafka такого параметра не принимает - проверено, процесс
        # падал на TypeError при старте. Порядок внутри партиции держит
        # сам идемпотентный режим: он нумерует записи и брокер отвергает
        # пришедшие не по порядку. Дополнительно его держит отправитель -
        # он останавливает пачку на первой же неудаче.
        try:
            await producer.start()
        except (KafkaError, OSError) as exc:
            log.warning(
                "продюсер не подключился",
                extra={"event": "kafka_connect", "result": "failed",
                       "error_code": type(exc).__name__, "dependency": "kafka"},
            )
            await producer.stop()
            return False
        self._producer = producer
        return True

    async def stop(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

    async def publish(
        self, *, topic: str, key: str, value: dict[str, Any], headers: dict[str, str]
    ) -> None:
        """Публикует запись и ждёт подтверждения от брокера.

        Ожидание обязательно: `send` без `await` на результат возвращает
        управление до подтверждения, и отметка «опубликовано» встала бы
        в базу раньше, чем брокер вообще увидел запись.
        """
        if self._producer is None:
            raise ConnectionError("продюсер не подключён")
        await self._producer.send_and_wait(
            topic,
            value=json.dumps(value, ensure_ascii=False).encode("utf-8"),
            # Ключ — идентификатор беседы: так все её события попадают
            # в одну партицию, и порядок внутри беседы сохраняется.
            key=key.encode("utf-8"),
            headers=[(name, val.encode("utf-8")) for name, val in headers.items()],
        )


@dataclass(frozen=True, slots=True)
class ConsumerSettings:
    """Откуда читать и от чьего имени."""

    bootstrap: str
    username: str
    password: str
    group_id: str
    topics: tuple[str, ...]
    security_protocol: str = "SASL_PLAINTEXT"
    sasl_mechanism: str = "SCRAM-SHA-512"
    # Сколько ждать пачку. Короткий таймаут - не нетерпеливость: цикл
    # обязан регулярно возвращать управление, иначе остановка пода
    # ждёт следующего сообщения, которого может не быть часами.
    poll_timeout_ms: int = 1000
    max_records: int = 100


@dataclass(frozen=True, slots=True)
class KafkaRecord:
    """Запись потока: что пришло, откуда и на каком месте.

    Координаты - не украшение. По ним воркер называет в журнале ту
    запись, на которой прервался цикл, а `rewind` возвращает партицию
    к началу выданной пачки. Кортеж «топик, заголовки, тело» этого не
    позволяет: смещения и партиции в нём нет, а восстановить их потом
    неоткуда - транспортный слой о ней знает ровно в этот момент.
    """

    topic: str
    partition: int
    offset: int
    headers: dict[str, str]
    body: dict[str, Any]


@dataclass(slots=True)
class Subscriber:
    """Потребитель с ручной фиксацией смещения.

    Ручной, а не автоматический: автоматическая фиксация отмечает
    обработанным то, что только прочитано. Процесс, умерший между
    чтением и обработкой, потерял бы событие молча - и это была бы
    ровно та потеря, ради невозможности которой существует outbox.

    Фиксация после обработки означает, что при падении событие придёт
    второй раз. Это осознанная цена at-least-once, и переживает её
    дедупликация, а не надежда.

    Одной фиксации, однако, мало, и это выяснилось живым прогоном, а не
    рассуждением: `getmany` сдвигает внутреннюю позицию в момент выдачи
    записей, поэтому отказ на середине пачки оставлял её хвост прочитанным
    и неприменённым - навсегда, без всякого перезапуска. Возврат делает
    `rewind`.
    """

    settings: ConsumerSettings
    _consumer: AIOKafkaConsumer | None = field(default=None)
    # Начало последней выданной пачки по партициям - то, к чему возвращает
    # `rewind`. Именно начало, а не первая необработанная запись: прогресс
    # по каждой записи пришлось бы вести в воркере, который о партициях
    # не знает, а повтор разобранного префикса бесплатен (применение
    # идемпотентно) там, где цена ошибки - потерянный хвост.
    _fetched_from: dict[TopicPartition, int] = field(default_factory=dict)

    async def start(self) -> bool:
        if self._consumer is not None:
            return True
        consumer = AIOKafkaConsumer(
            *self.settings.topics,
            bootstrap_servers=self.settings.bootstrap,
            group_id=self.settings.group_id,
            security_protocol=self.settings.security_protocol,
            sasl_mechanism=self.settings.sasl_mechanism,
            sasl_plain_username=self.settings.username,
            sasl_plain_password=self.settings.password,
            enable_auto_commit=False,
            # С начала: потребитель, поднятый впервые, обязан увидеть
            # уже накопленное. `latest` означал бы, что события,
            # пришедшие до его первого запуска, не увидит никто.
            auto_offset_reset="earliest",
        )
        try:
            await consumer.start()
        except (KafkaError, OSError) as exc:
            log.warning(
                "потребитель не подключился",
                extra={"event": "kafka_connect", "result": "failed",
                       "error_code": type(exc).__name__, "dependency": "kafka"},
            )
            await consumer.stop()
            return False
        self._consumer = consumer
        return True

    async def stop(self) -> None:
        if self._consumer is not None:
            await self._consumer.stop()
            self._consumer = None

    async def poll(self) -> list[KafkaRecord]:
        """Пачка записей: что пришло, откуда и на каком месте.

        Тело разбирается здесь, потому что негодный JSON - это отказ
        транспорта, а не предметной области: сервис не должен уметь
        отличать сообщение от мусора в логе.

        Здесь же запоминается начало пачки по партициям, и это не
        бухгалтерия ради журнала: после `getmany` внутренняя позиция
        стоит за выданными записями, поэтому решение о возврате
        принимается по тому, что выдали мы, а брокер об этом уже
        ничего не помнит.
        """
        if self._consumer is None:
            raise ConnectionError("потребитель не подключён")
        batches = await self._consumer.getmany(
            timeout_ms=self.settings.poll_timeout_ms,
            max_records=self.settings.max_records,
        )
        # Состояние возврата заменяется целиком, а не дополняется: пачка
        # без записей по партиции и означает, что возвращать её некуда.
        self._fetched_from = {
            partition: records[0].offset
            for partition, records in batches.items()
            if records
        }
        events: list[KafkaRecord] = []
        for partition, records in batches.items():
            for record in records:
                headers = {
                    name: value.decode("utf-8") for name, value in (record.headers or ())
                }
                try:
                    body = json.loads(record.value.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    log.error(
                        "нечитаемая запись в потоке",
                        extra={"event": "kafka_record", "result": "failed",
                               "error_code": "unparsable",
                               "topic": partition.topic, "offset": record.offset},
                    )
                    continue
                events.append(KafkaRecord(
                    topic=partition.topic,
                    partition=partition.partition,
                    offset=record.offset,
                    headers=headers,
                    body=body,
                ))
        return events

    async def rewind(self) -> None:
        """Возвращает позицию к началу последней выданной пачки.

        Не фиксация и не «мягкий» пропуск: `getmany` сдвигает внутреннюю
        позицию в момент выдачи, поэтому необработанный хвост пачки
        живому процессу вернёт только `seek`. Без него «смещение не
        зафиксировано» охраняет лишь от перезапуска - и только до тех
        пор, пока позиция не уехала вперёд, - а отказ на середине пачки
        терял бы её хвост молча. Цена названа вслух: разобранный префикс
        пачки приедет второй раз. Это допустимо, потому что применение
        идемпотентно, и дешевле потерянного события.

        Отозванные партиции пропускаются: ребаланс мог случиться между
        выдачей пачки и отказом обработки, а `seek` по чужой партиции
        бросает `IllegalStateError` - из `except` воркера это уронило бы
        процесс. Терять при этом нечего: хвост отозванной партиции
        вернёт новый владелец, начиная с зафиксированного смещения.
        """
        if self._consumer is None:
            return
        assigned = self._consumer.assignment()
        for partition, offset in self._fetched_from.items():
            if partition in assigned:
                # `seek` синхронный и вдобавок выбрасывает буфер партиции -
                # иначе возврат позиции ничего не значил бы: записи
                # вернулись бы из уже прочитанного буфера.
                self._consumer.seek(partition, offset)

    async def commit(self) -> None:
        """Фиксирует смещение. Только после обработки всей пачки.

        Вместе со смещением забывается начало пачки. Возврат к тому, что
        уже зафиксировано, был бы не подстраховкой, а повтором давно
        разобранного - и с каждой неудачей его становилось бы больше.
        Забывается после подтверждения брокера: упавшая фиксация обязана
        оставить возврат возможным.
        """
        if self._consumer is not None:
            await self._consumer.commit()
            self._fetched_from.clear()
