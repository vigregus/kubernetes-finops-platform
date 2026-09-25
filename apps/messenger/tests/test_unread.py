"""Воркер потребителя непрочитанного: пачка, фиксация смещения, отказ.

Проверяется **цикл**, а не проекция: применение события подменено, и это
намеренно. Вопросы, которые здесь ставятся, решаются в воркере и только
в нём — сколько раз он ходит за соединением, когда фиксирует смещение,
что попадает в журнал, переживает ли пачка отказ, — и ни один из них не
решается внутри `apply_event`. Обратное тоже верно: свойства проекции
(повтор, разрыв, пересборка) проверяются соседним `test_unread_service.py`,
и подменой их не доказать.

Цикл кончается остановкой, а остановку ставит подмена — опустошением
очереди опросов. Иначе тест не завершился бы: цикл крутится, пока его
не попросят встать.

Отказ цикла обязан ещё и вернуть незавершённую пачку в поток: без этого
прочитанный хвост пачки не вернётся живому процессу никогда. Само это
свойство принадлежит адаптеру и проверяется отдельно
(`test_kafka_subscriber.py`) — здесь проверяется, что цикл им пользуется
и что запись об отказе называет ту запись, которую он не закончил.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager

import pytest

from messenger.adapters import kafka
from messenger.domain.ids import ConversationId, ConversationSeq, UserId
from messenger.domain.unread import (
    UnreadCount,
    UnreadNotice,
    UnreadOutcome,
    UnreadOutcomeKind,
)
from messenger.workers import consumer_unread as worker

FACT = "messenger.events.v1"
FACT_EVENT = "message.created"
CONVERSATION_ID = ConversationId(uuid.UUID("33333333-3333-3333-3333-333333333333"))
OTHER_CONVERSATION_ID = ConversationId(uuid.UUID("44444444-4444-4444-4444-444444444444"))
SENDER_ID = UserId(uuid.UUID("11111111-1111-1111-1111-111111111111"))
READER_ID = UserId(uuid.UUID("22222222-2222-2222-2222-222222222222"))


class Записи(logging.Handler):
    """Собирает записи журнала как есть — вместе с полями `extra`.

    Не форматирует: проверяются поля, а не текст. Конверт общий и
    проверяется отдельно (`test_logging.py`), и разбор JSON здесь ловил бы
    расхождения не того модуля.
    """

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def по(self, event: str) -> list[logging.LogRecord]:
        return [r for r in self.records if getattr(r, "event", None) == event]


@pytest.fixture
def журнал():
    """Записи журнала воркера. Соседние тесты видят свой логгер чистым."""
    handler = Записи()
    log = logging.getLogger(worker.__name__)
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    # Запись не уходит в корневой логгер: у него свой обработчик, и под
    # тестом он печатал бы строки, к проверкам не относящиеся.
    прежнее = log.propagate
    log.propagate = False
    yield handler
    log.removeHandler(handler)
    log.propagate = прежнее


def _событие(
    seq: int,
    *,
    event_type: str = FACT_EVENT,
    conversation_id: ConversationId = CONVERSATION_ID,
) -> dict:
    """Тело события в том виде, в каком его отдаёт `kafka.poll`: разобранное."""
    return {
        "event_type": event_type,
        # Идентификатор выводится из номера: по нему тест называет то
        # событие, на котором должен отказать.
        "event_id": f"00000000-0000-4000-8000-{seq:012d}",
        "conversation_id": str(conversation_id),
        "conversation_seq": seq,
        "sender_id": str(SENDER_ID),
    }


def _запись(*bodies: dict) -> list[kafka.KafkaRecord]:
    """Пачка из Kafka — ровно то же, что отдаёт адаптер.

    Подмена берёт настоящий `KafkaRecord`, а не свой словарь: контракт
    между адаптером и воркером иначе проверялся бы дважды по-разному,
    и расхождение — как раз то, что здесь и ловится.
    """
    # Смещения начинаются заведомо выше номеров событий беседы: это
    # разные числа, и проверка, в которой они совпали, не отличила бы
    # одно от другого.
    return [
        kafka.KafkaRecord(topic=FACT, partition=0, offset=100 + seq,
                          headers={}, body=body)
        for seq, body in enumerate(bodies, start=1)
    ]


class Пул:
    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


class Стенд:
    """Подмены вокруг воркера: брокер, пул и применение события.

    Заменяется ровно то, что воркер зовёт, и ничего больше: тело цикла,
    порядок шагов и обработка отказа — настоящие. Поэтому проверки здесь
    отвечают на вопрос «что делает цикл», а не «что делает подмена».
    """

    def __init__(
        self,
        monkeypatch,
        *,
        batches=(),
        ok: bool = True,
        failing=(),
        invalid=(),
        pool_ok: bool = True,
        notices: tuple[UserId, ...] = (),
        realtime: bool = True,
    ) -> None:
        # Очередь опросов, а не событий: пустой опрос — такой же ответ
        # брокера, и он обязан быть представим.
        self.batches = [list(batch) for batch in batches]
        self.остановка = asyncio.Event()
        self.ok = ok
        self.pool_ok = pool_ok
        self.failing = set(failing)
        self.invalid = set(invalid)
        # Адресаты, которых исход применения объявит. Пустой кортеж —
        # законный случай (повтор, чужое событие, беседа без собеседников),
        # и он проверяется наравне с непустым.
        self.notices = tuple(notices)
        # `realtime=False` — Centrifugo не сконфигурирован; это не ошибка,
        # и проекция считается в любом случае.
        self.realtime = realtime

        self.polls = 0
        self.commits = 0
        self.rewinds = 0
        self.started = 0
        self.stopped = 0
        self.connections = 0
        self.bodies: list[dict] = []
        self.durations: list[str] = []
        self.dependencies: list[tuple[str, bool]] = []
        self.pool_names: list[str] = []
        self.pool = Пул()
        # Порядок событий вокруг публикации: что было открыто, что закрыто
        # и что ушло. Список общий на подмену соединения и на подмену
        # публикатора — иначе «после коммита» проверялось бы сравнением
        # двух списков, которые нечем выстроить в один ряд.
        self.journal: list[str] = []
        self.published: list[tuple[str, dict]] = []

        monkeypatch.setattr(worker.kafka, "Subscriber", self._subscriber)
        monkeypatch.setattr(worker.postgres, "create_pool", self._create_pool)
        monkeypatch.setattr(worker.postgres, "connection", self._connection)
        monkeypatch.setattr(worker.unread, "apply_event", self._apply)
        monkeypatch.setattr(worker.metrics, "consumer_processing_duration", self._duration)
        monkeypatch.setattr(worker.metrics, "dependency_up", self._dependency)
        monkeypatch.setattr(
            worker.runtime, "centrifugo_client_from_env", self._client
        )

    # --- подмены ----------------------------------------------------------

    def _subscriber(self, *, settings):
        self.settings = settings
        return self

    async def _create_pool(self, settings, *, application_name: str):
        if not self.pool_ok:
            # Недоступная база — единственный отказ, после которого цикл
            # не доходит до опустошения очереди и не останавливается сам.
            # Здесь остановка ставится, иначе тест крутился бы вечно:
            # так устроен и боевой цикл — он повторяет попытку.
            self.остановка.set()
            raise ConnectionError("база недоступна")
        self.pool_names.append(application_name)
        return self.pool

    @asynccontextmanager
    async def _connection(self, pool, *, timeout: float = 5.0):
        self.connections += 1
        # Журнал вокруг блока соединения, а не счётчик: предмет проверки —
        # **порядок** «закрыто, потом публикация», и одно число его не
        # выражает. В боевом коде на выходе из блока соединение
        # возвращается в пул, а транзакция сервиса к этому моменту уже
        # закоммичена, — то есть «после закрытия» и значит «после коммита».
        self.journal.append("открыто")
        try:
            yield object()
        finally:
            self.journal.append("закрыто")

    def _client(self):
        """Клиент Centrifugo: публикатор в общий журнал.

        Подменяется `centrifugo_client_from_env`, а не `publish` у клиента:
        проверяется, что воркер строит клиент **один раз на процесс** и
        зовёт настоящий `announce_changed` — тот самый, что собирает тело
        события. Подмена публикатора оставила бы сборку тела непроверенной.
        """
        if not self.realtime:
            return None
        return self

    async def publish(self, channel: str, body: dict) -> bool:
        self.journal.append(f"публикация:{channel}")
        self.published.append((channel, body))
        return True

    async def _apply(self, conn, *, body):
        self.bodies.append(body)
        if body["event_id"] in self.failing:
            raise ConnectionError("база недоступна")
        if body["event_id"] in self.invalid:
            return UnreadOutcome(
                kind=UnreadOutcomeKind.INVALID, reason="bad_seq"
            )
        if body.get("event_type") != FACT_EVENT:
            return UnreadOutcome(kind=UnreadOutcomeKind.IGNORED)
        conversation_id = ConversationId(uuid.UUID(body["conversation_id"]))
        return UnreadOutcome(
            kind=UnreadOutcomeKind.APPLIED,
            conversation_id=conversation_id,
            conversation_seq=ConversationSeq(body["conversation_seq"]),
            affected=1,
            # Число выводится из номера события: тест читает его в журнале
            # и отличает одно событие пачки от другого.
            notices=tuple(
                UnreadNotice(
                    user_id=user,
                    conversation_id=conversation_id,
                    unread_count=UnreadCount(body["conversation_seq"]),
                )
                for user in self.notices
            ),
        )

    def _duration(self, seconds: float, *, consumer: str) -> None:
        self.durations.append(consumer)

    def _dependency(self, name: str, *, up: bool) -> None:
        self.dependencies.append((name, up))

    # --- поведение подписчика ---------------------------------------------

    async def start(self) -> bool:
        self.started += 1
        if not self.ok:
            # Цикл после отказа засыпает на две секунды; чтобы тест не
            # ждал их вправду, остановка ставится здесь.
            self.остановка.set()
        return self.ok

    async def stop(self) -> None:
        self.stopped += 1

    async def poll(self):
        self.polls += 1
        if not self.batches:
            return []
        batch = self.batches.pop(0)
        if not self.batches:
            # Остановка ставится **до** обработки пачки, и это то, что
            # проверяет `test_остановка_доигрывает_пачку`: `stop` читается
            # в начале итерации, поэтому выданная пачка доигрывается.
            self.остановка.set()
        return batch

    async def commit(self) -> None:
        self.commits += 1

    async def rewind(self) -> None:
        """Возврат пачки в поток. Считается: этим свойством цикл и ценен."""
        self.rewinds += 1


def _исходы(журнал: Записи) -> list[tuple[str, object]]:
    """Исходы применений в порядке записи: `unread_event` без служебного."""
    return [
        (r.result, r.reason)  # type: ignore[attr-defined]
        for r in журнал.по("unread_event")
    ]


def test_пустой_опрос_не_фиксирует_смещение(monkeypatch, журнал):
    """Ронит фиксацию вне ветки «пачка не пуста».

    Пустой опрос повторяется раз в `poll_timeout_ms`, и фиксация на
    каждом обороте — это запись в брокер раз в секунду ни за чем. Замер
    длительности там же отвечал бы на «сколько раз в Kafka ничего не
    было», а не на «долго ли обрабатывали».
    """
    стенд = Стенд(monkeypatch, batches=[[], []])

    asyncio.run(worker.run(стенд.остановка))

    assert стенд.polls == 2
    assert стенд.commits == 0
    assert стенд.durations == []
    assert журнал.по("unread_cycle") == []
    # Состояние зависимости сообщается и на пустом цикле, и на каждом его
    # обороте: без него «потребитель жив, но поток пуст» и «потребитель
    # умер» неотличимы. Две записи — потому что оборотов два.
    assert стенд.dependencies == [("kafka", True)] * стенд.polls


def test_падение_применения_не_фиксирует_смещение(monkeypatch, журнал):
    """Ронит `commit()` в `finally` и отказ без возврата пачки.

    Фиксация в `finally` выглядит заботой о «не потерять прогресс», а на
    деле объявляет обработанным то, что не обработано: упавшее событие
    потерялось бы молча. Повтор применения бесплатен — событие отсекает
    чекпойнт беседы, — и это делает отказ дешевле разбирательства.

    Возврат — вторая половина того же: не отдать фиксацию мало, потому
    что позиция потребителя уехала вперёд ещё в момент выдачи пачки.
    """
    первое, второе = _событие(1), _событие(2)
    стенд = Стенд(monkeypatch, batches=[_запись(первое, второе)],
                  failing=[второе["event_id"]])

    asyncio.run(worker.run(стенд.остановка))

    # Событие до отказа применено, само отказавшее — тоже позвано: цикл
    # останавливается исключением, а не пропуском.
    assert стенд.bodies == [первое, второе]
    assert стенд.commits == 0
    assert стенд.rewinds == 1
    assert [r.result for r in журнал.по("unread_cycle")] == ["failed"]
    assert стенд.pool.closed == 1
    assert стенд.stopped == 1


def test_отказ_называет_незавершённую_запись(monkeypatch, журнал):
    """Ронит журнал отказа без координат записи.

    «Цикл прерван» без «на чём» оставляет разбор с нуля: чтобы понять,
    какую запись цикл не закончил, поток пришлось бы сверять с проекцией
    руками. Следующий такой отказ обязан отвечать на это сразу — и на
    живом прогоне, с которого эта правка и началась, отвечать было
    нечем: в журнале стоял только `TimeoutError`.
    """
    первое, второе, третье = _событие(1), _событие(2), _событие(3)
    стенд = Стенд(monkeypatch, batches=[_запись(первое, второе, третье)],
                  failing=[третье["event_id"]])

    asyncio.run(worker.run(стенд.остановка))

    отказ = журнал.по("unread_cycle")[-1]
    assert отказ.result == "failed"
    assert отказ.batch_size == 3
    assert отказ.failed_topic == FACT
    assert отказ.failed_partition == 0
    # Смещение, а не номер события беседы: это разные числа, и запись
    # обязана называть то, по которому запись ищется в потоке.
    assert отказ.failed_offset == 103


def test_соединение_берётся_на_каждое_событие(monkeypatch):
    """Ронит соединение, вынесенное за цикл по событиям.

    Транзакцию открывает сервис, и её граница — одно событие. Общее
    соединение на пачку связало бы границы в одну сессию и продержало бы
    занятым слот пула всё время обработки пачки; при сотне записей в
    пачке это заметно соседям по пулу.
    """
    тела = [_событие(seq) for seq in range(1, 13)]
    стенд = Стенд(monkeypatch, batches=[_запись(*тела)])

    asyncio.run(worker.run(стенд.остановка))

    assert len(стенд.bodies) == 12
    assert стенд.connections == 12


def test_порядок_событий_сохранён(monkeypatch):
    """Ронит группировку пачки по беседе.

    Группировка (один замок беседы за пачку) — очевидная оптимизация, и
    она сознательно не сделана: она меняет порядок применения, а вместе
    с ним и семантику отказа. Порядок внутри беседы сохраняет партиция
    (`message.created` ключуется `conversation_id`), и воркер обязан его
    не переставлять: перестановка здесь ломает монотонность чекпойнта.
    """
    первое = _событие(1, conversation_id=CONVERSATION_ID)
    чужое = _событие(1, conversation_id=OTHER_CONVERSATION_ID)
    третье = _событие(2, conversation_id=CONVERSATION_ID)
    стенд = Стенд(monkeypatch, batches=[_запись(первое, чужое, третье)])

    asyncio.run(worker.run(стенд.остановка))

    assert стенд.bodies == [первое, чужое, третье]


def test_пачка_даёт_один_замер_и_одну_фиксацию(monkeypatch):
    """Ронит замер внутри цикла по событиям и возврат на успешном пути.

    Замер — «от получения пачки до фиксации смещения». Внутри цикла он
    мерил бы одно событие и отвечал бы на другой вопрос, а гистограмма
    наполнялась бы в двенадцать раз быстрее, оставаясь правдоподобной.

    Возврат на успешном пути ронял бы больше: обработанная и
    зафиксированная пачка приезжала бы заново каждый оборот цикла, то
    есть потребитель не двигался бы по потоку вовсе.
    """
    тела = [_событие(seq) for seq in range(1, 13)]
    стенд = Стенд(monkeypatch, batches=[_запись(*тела)])

    asyncio.run(worker.run(стенд.остановка))

    assert стенд.durations == ["unread"]
    assert стенд.commits == 1
    assert стенд.rewinds == 0


def test_остановка_доигрывает_пачку(monkeypatch, журнал):
    """Ронит проверку остановки внутри цикла по событиям.

    Остановка приходит сигналом посреди обработки, и брошенная на
    половине пачка — это либо потеря событий (если смещение всё же
    зафиксировано), либо их повтор целиком. Читается флаг в начале
    итерации, поэтому пачка доигрывается и фиксируется один раз.
    """
    тела = [_событие(seq) for seq in range(1, 4)]
    стенд = Стенд(monkeypatch, batches=[_запись(*тела)])

    asyncio.run(worker.run(стенд.остановка))

    assert стенд.остановка.is_set()
    assert стенд.bodies == тела
    assert стенд.commits == 1
    assert [r.result for r in журнал.по("unread_cycle")] == ["ok"]
    # Процесс закрывается за собой: отпущенный пул и остановленный
    # подписчик — это не «мелочь на выходе», а то, чем под проверками
    # живёт следующий тест.
    assert стенд.pool.closed == 1
    assert стенд.stopped == 1


def test_постороннее_событие_решает_сервис_а_не_воркер(monkeypatch, журнал):
    """Ронит отбор по типу события внутри воркера.

    Второго отбора здесь быть не должно: «что считается фактом появления
    сообщения» — одно правило, и живёт оно в `domain/unread.py`. Копия
    в воркере разошлась бы с ним молча, а исход `ignored` перестал бы
    попадать в журнал — то есть состояние проекции менялось бы без следа.
    """
    чужое = _событие(1, event_type="message.read")
    стенд = Стенд(monkeypatch, batches=[_запись(чужое)])

    asyncio.run(worker.run(стенд.остановка))

    assert стенд.bodies == [чужое]
    assert _исходы(журнал) == [("ignored", None)]
    assert стенд.commits == 1


def test_негодное_событие_видно_в_журнале(monkeypatch, журнал):
    """Ронит потерю причины и понижение уровня у пропущенного события.

    Пропуск негодного события — единственное место, где состояние
    проекции расходится с потоком навсегда: очереди отклонённых записей
    нет. Заметно это ровно одним — записью в журнале, и запись обязана
    нести код причины и быть предупреждением: `info` в потоке уровня
    `info` не отличается от рутины и не читается.
    """
    годное, битое, следующее = _событие(1), _событие(2), _событие(3)
    стенд = Стенд(monkeypatch, batches=[_запись(годное, битое, следующее)],
                  invalid=[битое["event_id"]])

    asyncio.run(worker.run(стенд.остановка))

    записи = журнал.по("unread_event")
    assert [(r.result, r.reason) for r in записи] == [
        ("applied", None), ("invalid", "bad_seq"), ("applied", None)
    ]
    пропуск = записи[1]
    assert пропуск.levelno == logging.WARNING
    # Топик несёт только воркер: сервис подписан на один поток и второго
    # у него нет по правам, поэтому он о топике и не знает.
    assert пропуск.topic == FACT  # type: ignore[attr-defined]
    # Пачка при этом фиксируется: негодное событие — это ожидаемый отказ,
    # и очередь на нём встать не может.
    assert стенд.commits == 1


def test_пул_поднимается_лениво_и_один_раз(monkeypatch):
    """Ронит пул, поднимаемый на каждой итерации.

    Пул переживает итерации цикла: поднимать его заново значило бы
    каждый раз заводить соединения и терять их, а имя приложения —
    единственное, по чему пул видно в `pg_stat_activity` рядом с
    остальными нагрузками.
    """
    стенд = Стенд(monkeypatch, batches=[_запись(_событие(1)), _запись(_событие(2))])

    asyncio.run(worker.run(стенд.остановка))

    assert стенд.polls == 2
    assert стенд.pool_names == ["messenger-unread-consumer"]


def test_без_пула_поток_не_читается(monkeypatch, журнал):
    """Ронит чтение потока, который некуда применять.

    События в Kafka ждут, а сдвинутое смещение не ждёт: прочитанное и
    неприменённое — это потеря, которую видно только сверкой. Поэтому
    недоступная база останавливает цикл до чтения, а не после.
    """
    стенд = Стенд(monkeypatch, batches=[_запись(_событие(1))], pool_ok=False)

    asyncio.run(worker.run(стенд.остановка))

    assert стенд.polls == 0
    assert стенд.started == 0
    assert стенд.commits == 0
    отказ = журнал.по("db_pool_unavailable")
    assert [r.result for r in отказ] == ["failed"]


def test_отказ_подписчика_не_фиксирует_смещение(monkeypatch):
    """Брокер не подключился — цикл ждёт и сообщает состояние.

    Отдельно от отказа базы: здесь не читается ничего, и зависимость
    обязана быть видна снаружи (`dependency_up`), иначе под с неживым
    брокером ничем не отличается от под с пустым потоком.
    """
    стенд = Стенд(monkeypatch, ok=False)

    asyncio.run(worker.run(стенд.остановка))

    assert стенд.started == 1
    assert стенд.polls == 0
    assert стенд.commits == 0
    assert стенд.dependencies == [("kafka", False)]


# ---------------------------------------------------------------------------
# Публикация нового числа
# ---------------------------------------------------------------------------


def test_публикация_идёт_после_закрытия_соединения(monkeypatch):
    """`D7`: событие уходит **после** коммита, и это проверяется порядком.

    Ронит перенос публикации внутрь блока соединения — туда, где она
    читается как «рядом с записью». Транзакцию открывает сервис, и к
    выходу из блока она закоммичена; событие, ушедшее раньше, пережило бы
    откат, а `publish` не отменяется: канал разошёлся бы с базой в ту
    сторону, из которой восстановления нет, — сверка приведёт клиента
    к REST, а второго события о том же сдвиге не будет (`D11`).

    Порядок, а не счётчик: «сколько раз опубликовали» одинаково у обеих
    редакций, и различие между ними ровно в том, что было раньше.
    """
    стенд = Стенд(
        monkeypatch, batches=[_запись(_событие(1))], notices=(READER_ID,)
    )

    asyncio.run(worker.run(стенд.остановка))

    assert стенд.journal == ["открыто", "закрыто", f"публикация:user:{READER_ID}"]
    канал, тело = стенд.published[0]
    # Тело собирает `domain.unread.changed_event`, а не воркер: имя канала
    # и форма события — по одному контракту, и собирать их в двух местах
    # значило бы заводить вторую редакцию того же.
    assert тело == {
        "type": "unread.changed",
        "conversation_id": str(CONVERSATION_ID),
        "unread_count": 1,
    }
    assert канал == f"user:{READER_ID}"


def test_пустые_адресаты_не_публикуют_ничего(monkeypatch):
    """Повтор, чужое событие и беседа без собеседников молчат.

    Развилки на «есть ли кому» в воркере нет намеренно: пустой кортеж
    адресатов — это цикл по пустому кортежу, а не ветка, которую
    пришлось бы держать в согласии с исходом. Проверка сторожит именно
    это: появление такой ветки с побочным действием (метрика, запись
    в журнал) сделало бы рутину заметной на каждом повторе.
    """
    стенд = Стенд(monkeypatch, batches=[_запись(_событие(1))], notices=())

    asyncio.run(worker.run(стенд.остановка))

    assert стенд.published == []
    assert стенд.journal == ["открыто", "закрыто"]


def test_без_centrifugo_проекция_всё_равно_считается(monkeypatch):
    """`None` вместо клиента — не ошибка, а «Centrifugo не сконфигурирован».

    Ронит развилку «нет клиента — не применяем событие». Числа доезжают
    списком бесед (`D11`), и проекция остаётся источником для него;
    отказ от применения оставил бы чекпойнт на месте и вернул бы ту же
    пачку снова, то есть вечный повтор вместо тишины в канале.
    """
    стенд = Стенд(
        monkeypatch, batches=[_запись(_событие(1))], notices=(READER_ID,),
        realtime=False,
    )

    asyncio.run(worker.run(стенд.остановка))

    assert стенд.published == []
    assert стенд.commits == 1
    assert стенд.connections == 1
