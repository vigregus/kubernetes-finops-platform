"""Подписчик Kafka: отказ цикла не имеет права уносить хвост пачки.

Проверяется контракт адаптера, а не решение воркера, — свойство «полученную
пачку можно вернуть в поток, если её не дообработали». Свойство общее для
обоих потребителей: `realtime` и `unread` читают одним и тем же `Subscriber`,
поэтому тест живёт отдельно от тестов воркеров. Тест внутри воркера доказал
бы его для одного из двух, а дефект был бы у обоих.

Появилось это свойство не из головы. Интеграционный прогон `unread_check.py`
на живом кластере показал: цикл потребителя упал с `TimeoutError` на одной
записи пачки, и события, лежавшие за ней в той же пачке, не применились
никогда — ни в этом процессе, ни в последующих. Причина в семантике
aiokafka 0.12.0, а не в нашем коде, и подделка ниже повторяет ровно её —
по трём местам установленного пакета:

* `getmany` сдвигает `consumed position` в момент выдачи записей
  (`fetcher.py`: `FetchResult.getall` → `_update_position` →
  `SubscriptionState.consumed_to`). Значит «смещение не зафиксировано» не
  означает «записи придут снова»: внутренняя позиция уже стоит за ними.
* `commit()` без аргументов фиксирует ту же сдвинутую позицию
  (`consumer.py`: `assignment.all_consumed_offsets`). Поэтому отсутствие
  фиксации защищает только от перезапуска — и только до тех пор, пока
  позиция не уехала вперёд.
* `seek(partition, offset)` — синхронный, и кроме позиции выбрасывает буфер
  партиции (`fetcher.py`: `seek_to` + `del self._records[tp]`). Это
  единственный способ вернуть живому процессу то, что он уже получил.

Отсюда и то, что здесь проверяется: необработанные записи возвращаются
в следующий опрос, а смещение в брокере не перескакивает их.
"""
from __future__ import annotations

import asyncio
import json

from aiokafka.errors import IllegalStateError
from aiokafka.structs import TopicPartition

from messenger.adapters import kafka as kafka_adapter

FACT = "messenger.events.v1"
НАСТРОЙКИ = kafka_adapter.ConsumerSettings(
    bootstrap="broker:9092",
    username="messenger-unread",
    password="secret",
    group_id="messenger-unread",
    topics=(FACT,),
)
P0 = TopicPartition(FACT, 0)
P1 = TopicPartition(FACT, 1)


class ПодделкаЗаписи:
    """Запись потока в той части, которую читает адаптер."""

    def __init__(self, *, topic: str, partition: int, offset: int) -> None:
        self.topic = topic
        self.partition = partition
        self.offset = offset
        self.key = b"conversation"
        # Кортежи `(имя, байты)` — как их отдаёт aiokafka, а не словарь:
        # адаптер и разбирает их сам, и подмена словарём проверяла бы
        # другую ветку кода.
        self.headers = [("traceparent", b"00-abc-def-01")]
        self.value = json.dumps(
            {"event_type": "message.created", "conversation_seq": offset}
        ).encode("utf-8")


class ПодделкаБрокера:
    """`AIOKafkaConsumer` в трёх местах, которые решают исход.

    Ровно в трёх, и ни в одном больше: `getmany` двигает позицию,
    `commit` её фиксирует, `seek` возвращает. Всё остальное — буфер,
    сердцебиения, ребалансы — здесь не смоделировано намеренно: тест
    проверяет, что адаптер умеет с этими тремя сделать, а не что
    aiokafka работает.
    """

    def __init__(self, *, partitions: dict[TopicPartition, list[int]]) -> None:
        self._log = {tp: list(offsets) for tp, offsets in partitions.items()}
        # Порядок партиций в пачке — как их вернул бы fetch. Проверке
        # с двумя партициями он нужен воспроизводимым: иначе «ошибка на
        # последней записи первой партиции» перестала бы быть тем, чем
        # названа.
        self._order = list(partitions)
        self._position = {tp: offsets[0] for tp, offsets in self._log.items()}
        self._committed = dict(self._position)
        self._assigned = set(self._log)
        self.seeks: list[tuple[TopicPartition, int]] = []
        # Фиксация может не дойти до брокера — это отдельное состояние,
        # и моделируется оно отдельно от «отказа обработки».
        self.отказ_фиксации: Exception | None = None

    # --- то, что зовёт адаптер --------------------------------------------

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def assignment(self) -> set[TopicPartition]:
        return set(self._assigned)

    async def getmany(self, *, timeout_ms: int, max_records: int) -> dict:
        пачка: dict[TopicPartition, list[ПодделкаЗаписи]] = {}
        for tp in self._order:
            if tp not in self._assigned:
                continue
            окно = [o for o in self._log[tp] if o >= self._position[tp]][:max_records]
            if not окно:
                continue
            пачка[tp] = [
                ПодделкаЗаписи(topic=tp.topic, partition=tp.partition, offset=o)
                for o in окно
            ]
            # Позиция встаёт за последней выданной записью. Это и есть
            # `consumed_to` — то, из-за чего хвост исчезал, и то, ради
            # чего обычной фиксации «после обработки» мало.
            self._position[tp] = окно[-1] + 1
        return пачка

    def seek(self, partition: TopicPartition, offset: int) -> None:
        if partition not in self._assigned:
            raise IllegalStateError(f"партиция не назначена: {partition}")
        if offset < 0:
            raise ValueError(f"отрицательное смещение: {offset}")
        self.seeks.append((partition, offset))
        self._position[partition] = offset
        # Буфер здесь не отдельная сущность: окно считается от позиции,
        # поэтому вернуть позицию — то же самое, что выбросить буфер
        # (`del self._records[tp]`), и записи выдаются заново.

    async def commit(self, offsets: dict | None = None) -> None:
        if self.отказ_фиксации is not None:
            raise self.отказ_фиксации
        self._committed.update(offsets or self._position)

    # --- наблюдение за подделкой ------------------------------------------

    def committed(self, partition: TopicPartition) -> int:
        """Что лежит в брокере. Не «что мы хотели зафиксировать»."""
        return self._committed[partition]

    def отозвать(self, partition: TopicPartition) -> None:
        """Ребаланс между выдачей пачки и отказом обработки."""
        self._assigned.discard(partition)


async def _обработать(пачка, *, подписчик, падение_на: int | None = None) -> bool:
    """Обработка пачки в миниатюре — тот же порядок, что в воркере.

    Цикл повторяет боевой намеренно: контракт «пачку можно вернуть»
    ничего не стоит, пока им не пользуются так, как это делает воркер.
    Проверяй мы метод в отрыве от этого порядка — зелёный тест не
    отвечал бы на вопрос, переживает ли пачка отказ.
    """
    try:
        for запись in пачка:
            if запись.offset == падение_на:
                raise TimeoutError("база не ответила")
        # Фиксация — внутри той же ветки `try`, и это не придирка к
        # форме: за неё отвечает брокер, отказать она умеет так же,
        # а обработанной пачка становится ровно в момент подтверждения.
        await подписчик.commit()
    except Exception:  # noqa: BLE001 - цикл обязан пережить любой отказ
        # Порядок именно такой: сначала возврат, потом (в бою) пауза.
        # Фиксации в этой ветке нет — и это не упущение, а условие.
        await подписчик.rewind()
        return False
    return True


def _смещения(пачка) -> list[int]:
    return [запись.offset for запись in пачка]


def test_отказ_на_записи_возвращает_её_следующему_опросу(monkeypatch):
    """Ронит позицию, уехавшую за необработанный хвост пачки.

    Пачка 10, 11, 12: две записи применены, на третьей — отказ. Возврат
    обязан вернуть ту же тройку живому процессу: без него записи 10 и 11
    повторно не придут (это допустимо), а 12 не придёт больше никогда —
    а это уже потеря, и заметна она только сверкой с источником истины.
    """
    брокер = ПодделкаБрокера(partitions={P0: [10, 11, 12]})
    monkeypatch.setattr(kafka_adapter, "AIOKafkaConsumer", lambda *a, **k: брокер)

    async def прогон():
        подписчик = kafka_adapter.Subscriber(settings=НАСТРОЙКИ)
        assert await подписчик.start()
        первая = await подписчик.poll()
        assert _смещения(первая) == [10, 11, 12]
        assert not await _обработать(первая, подписчик=подписчик, падение_на=12)
        return await подписчик.poll()

    вторая = asyncio.run(прогон())

    assert _смещения(вторая) == [10, 11, 12]
    # И фиксации не было: незавершённая пачка не объявлена обработанной.
    assert брокер.committed(P0) == 10


def test_возврат_не_теряет_хвост_соседней_партиции(monkeypatch):
    """Ронит возврат, знающий только про текущую партицию.

    Пачка собрана из двух партиций: P0 — 10, 11; P1 — 20, 21. Отказ
    на 11 приходит раньше, чем обработана хоть одна запись P1, поэтому
    «вернуть партицию отказавшего события» оставило бы 20 и 21
    прочитанными и неприменёнными — то же исчезновение, только тише.
    """
    брокер = ПодделкаБрокера(partitions={P0: [10, 11], P1: [20, 21]})
    monkeypatch.setattr(kafka_adapter, "AIOKafkaConsumer", lambda *a, **k: брокер)

    async def прогон():
        подписчик = kafka_adapter.Subscriber(settings=НАСТРОЙКИ)
        assert await подписчик.start()
        первая = await подписчик.poll()
        assert _смещения(первая) == [10, 11, 20, 21]
        assert not await _обработать(первая, подписчик=подписчик, падение_на=11)
        return await подписчик.poll()

    вторая = asyncio.run(прогон())

    # Вернулось всё, включая уже применённую 10: возврат идёт к началу
    # пачки, а не к отказавшей записи, и повтор разобранного — цена
    # at-least-once, названная вслух. Пропасть не имеет права ни 11, ни
    # нетронутый хвост P1, и он дороже: его не заметит ни одна проверка
    # состояния, только сверка с источником истины.
    assert _смещения(вторая) == [10, 11, 20, 21]
    assert брокер.committed(P0) == 10
    assert брокер.committed(P1) == 20


def test_успешная_фиксация_забывает_возврат(monkeypatch):
    """Ронит начало пачки, остающееся в памяти после фиксации.

    Фиксация — это точка, до которой возвращаться незачем: событие
    обработано и объявлено обработанным. Оставленное начало пачки
    оживает при первом же отказе **после** фиксации, а такой отказ есть:
    замер длительности и запись в журнал стоят за `commit()` в той же
    ветке `try`, — и тогда разобранная пачка приезжает заново.
    """
    брокер = ПодделкаБрокера(partitions={P0: [10, 11, 12]})
    monkeypatch.setattr(kafka_adapter, "AIOKafkaConsumer", lambda *a, **k: брокер)

    async def прогон():
        подписчик = kafka_adapter.Subscriber(settings=НАСТРОЙКИ)
        assert await подписчик.start()
        assert await _обработать(await подписчик.poll(), подписчик=подписчик)
        await подписчик.rewind()
        return await подписчик.poll()

    assert _смещения(asyncio.run(прогон())) == []
    assert брокер.committed(P0) == 13


def test_упавшая_фиксация_оставляет_возврат_возможным(monkeypatch):
    """Ронит забвение до подтверждения брокера.

    Фиксация может не дойти: брокер недоступен, ответ потерян, аренда
    истекла. Забудь мы начало пачки до её подтверждения — вернуть было
    бы нечего, и пачка, обработка которой никому не заявлена, исчезла бы
    ровно так же, как исчезал её хвост. Порядок внутри `commit` — часть
    контракта, а не деталь реализации.
    """
    брокер = ПодделкаБрокера(partitions={P0: [10, 11, 12]})
    брокер.отказ_фиксации = ConnectionError("брокер недоступен")
    monkeypatch.setattr(kafka_adapter, "AIOKafkaConsumer", lambda *a, **k: брокер)

    async def прогон():
        подписчик = kafka_adapter.Subscriber(settings=НАСТРОЙКИ)
        assert await подписчик.start()
        первая = await подписчик.poll()
        assert not await _обработать(первая, подписчик=подписчик)
        return await подписчик.poll()

    вторая = asyncio.run(прогон())

    assert _смещения(вторая) == [10, 11, 12]
    assert брокер.committed(P0) == 10


def test_возврат_переживает_отзыв_партиции(monkeypatch):
    """Ронит `seek` без проверки назначения.

    Ребаланс мог случиться между выдачей пачки и отказом обработки —
    и тогда `seek` по отозванной партиции бросает `IllegalStateError`
    (`_assigned_state`). Из `except` воркера это исключение уходит наверх,
    то есть роняет процесс, и под уходит в `CrashLoopBackOff` из-за
    события, которое всего лишь надо было перечитать. Партиция, которой
    у нас больше нет, — не потеря: её хвост вернёт новый владелец,
    начиная с зафиксированного смещения.
    """
    брокер = ПодделкаБрокера(partitions={P0: [10, 11], P1: [20, 21]})
    monkeypatch.setattr(kafka_adapter, "AIOKafkaConsumer", lambda *a, **k: брокер)

    async def прогон():
        подписчик = kafka_adapter.Subscriber(settings=НАСТРОЙКИ)
        assert await подписчик.start()
        первая = await подписчик.poll()
        assert _смещения(первая) == [10, 11, 20, 21]
        брокер.отозвать(P1)
        await подписчик.rewind()
        return await подписчик.poll()

    вторая = asyncio.run(прогон())

    assert _смещения(вторая) == [10, 11]
    assert брокер.seeks == [(P0, 10)]
