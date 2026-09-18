"""Спаны конвейера: дерево, ссылки и совпадение записи журнала со спаном.

Проверяется ровно то, ради чего спаны заводятся:

* три трассы на одно сообщение связаны **ссылками**, а не родительством:
  продолжить чужую трассу через outbox нельзя, потому что к моменту
  отправки процесс API уже ответил клиенту;
* вид спана на каждой границе соответствует таблице спецификации обмена
  сообщениями — от него зависит, нарисует ли Tempo связь производителя
  с потребителем или покажет два несвязанных спана;
* `span_id` в записи журнала равен идентификатору **существующего** спана.
  Расхождение здесь не видно ни в журнале, ни в трассе по отдельности:
  оно обнаруживается только на разборе инцидента, когда искать уже поздно.

Коллектора нет и не нужно: экспорт подменяется записью в память, а
`span()` идёт по тому же коду, что в проде.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace as otel
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode, format_span_id, format_trace_id
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from messenger.adapters import centrifugo as centrifugo_adapter
from messenger.adapters import event_cache as cache_adapter
from messenger.adapters import kafka as kafka_adapter
from messenger.api import main
from messenger.api.main import app
from messenger.domain import outbox as outbox_domain
from messenger.domain.errors import Reason
from messenger.domain.ids import (
    ClientMessageId,
    ConversationId,
    ConversationSeq,
    EventId,
    MessageId,
    UserId,
)
from messenger.domain.message import Message, MessageKind, MessagePayload
from messenger.domain.user import User
from messenger.services import identity
from messenger.services import messages as message_service
from messenger.services import outbox_relay as relay_service
from messenger.services import realtime_delivery as delivery
from messenger.telemetry import trace, tracing
from messenger.telemetry.logging import SPAN_ID, TRACE_ID, JsonFormatter
from messenger.workers import consumer_realtime

# Пара из примеров спецификации W3C Trace Context: чужая трасса, пришедшая
# снаружи. Значения узнаваемы намеренно — по ним видно, что продолжили
# именно чужое, а не завели своё.
ТРАССА = "4bf92f3577b34da6a3ce929d0e0e4736"
УЧАСТОК = "00f067aa0ba902b7"

CONVERSATION_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
CLIENT_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
MESSAGE_ID = MessageId(uuid.UUID("44444444-4444-4444-4444-444444444444"))
ACTOR_ID = UserId(uuid.UUID("11111111-1111-1111-1111-111111111111"))
NOW = datetime(2026, 9, 18, tzinfo=UTC)

URL = f"/conversations/{CONVERSATION_ID}/messages"
ТЕЛО = {"client_message_id": str(CLIENT_ID), "type": "text", "payload": {"text": "привет"}}

# Имя корневого спана запроса — шаблон маршрута, а не путь: иначе каждый
# новый разговор заводил бы своё имя спана.
ИМЯ_КОРНЯ = "POST /conversations/{conversation_id}/messages"

# Предъявленное удостоверение. Значение латиницей: заголовок HTTP
# кириллицу не переносит, и падение было бы в клиенте, а не в проверке.
ТОКЕН = "test-token-canary"

# Стадия конвейера у ролей различима намеренно: политика хвостовой выборки
# держит два порога задержки по ней, и общий порог означал бы, что холостой
# опрос Kafka (около секунды) удерживает почти каждую пачку потребителя.
СТАДИИ = {"api": "api", "relay": "relay", "realtime": "realtime"}


# --- обвязка ------------------------------------------------------------------


@pytest.fixture
def экспортёр(monkeypatch) -> Iterator[InMemorySpanExporter]:
    """Провайдер, записывающий спаны в память.

    Подменяется не `span()`, а единственная точка выхода спанов наружу:
    тест идёт по тому же коду, что и прод, и проверяет в том числе то,
    что идентификаторы берутся из настоящего спана.
    """
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_TRACES_SAMPLER", raising=False)
    exporter = InMemorySpanExporter()
    tracing.reset()
    tracing.configure(span_exporter=exporter)
    yield exporter
    tracing.shutdown()
    tracing.reset()


def записанные(экспортёр: InMemorySpanExporter) -> list[Any]:
    """Отдаёт завершённые спаны.

    Досылка обязательна: экспорт идёт фоновым потоком, и без неё тест
    гонялся бы с ним наперегонки — то есть падал бы через раз.
    """
    tracing.shutdown()
    return list(экспортёр.get_finished_spans())


def по_именам(спаны: list[Any]) -> dict[str, Any]:
    """Спаны по именам. Для проверок, где имя одно на весь тест."""
    return {span.name: span for span in спаны}


def след(спан) -> str:
    """Идентификатор трассы спана в том же виде, в каком он в журнале."""
    return format_trace_id(спан.context.trace_id)


def участок(спан) -> str:
    return format_span_id(спан.context.span_id)


def родитель(спан) -> str:
    return format_span_id(спан.parent.span_id)


@contextmanager
def с_провайдером(exporter: SpanExporter) -> Iterator[None]:
    """Провайдер с произвольным экспортёром — для проверок его поведения."""
    tracing.reset()
    tracing.configure(span_exporter=exporter)
    try:
        yield
    finally:
        tracing.shutdown()
        tracing.reset()


@pytest.fixture
def журнал() -> Iterator[Any]:
    """Запись журнала тем же форматтером, что и в проде.

    Не `caplog`: проверяется конверт, а не факт записи, и подмена
    форматтера значила бы проверку не того, что уезжает в VictoriaLogs.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("api", "local", "sha256:abc"))
    log = logging.getLogger("test.tracing")
    log.handlers = [handler]
    log.setLevel(logging.INFO)
    log.propagate = False

    def emit() -> dict[str, Any]:
        stream.seek(0)
        stream.truncate()
        log.info("шаг конвейера", extra={"event": "trace_step"})
        return json.loads(stream.getvalue())

    yield emit


# --- дерево запроса API -------------------------------------------------------


class Runtime:
    """Заглушка среды выполнения: до базы дело не доходит."""

    keys = None
    oidc_settings = None

    @asynccontextmanager
    async def connection(self):
        yield None


@pytest.fixture
def клиент() -> Iterator[TestClient]:
    original = app.state.runtime
    app.state.runtime = Runtime()
    yield TestClient(app)
    app.state.runtime = original


def _user() -> User:
    return User(
        user_id=ACTOR_ID,
        external_id="kc-1",
        display_name="Аня",
        email="anya@example.org",
        email_verified=True,
        created_at=NOW,
        updated_at=NOW,
    )


def _message(seq: int = 1) -> Message:
    return Message(
        message_id=MESSAGE_ID,
        conversation_id=ConversationId(CONVERSATION_ID),
        conversation_seq=ConversationSeq(seq),
        sender_id=ACTOR_ID,
        client_message_id=ClientMessageId(CLIENT_ID),
        kind=MessageKind.TEXT,
        payload=MessagePayload(text="привет"),
        created_at=NOW,
    )


def вход_разрешён(monkeypatch) -> None:
    """Подменяет разбор токена, но не `_current`.

    Спан `auth.check` живёт внутри `_current`, и, подменив его целиком,
    тест перестал бы проверять ровно то, ради чего написан.
    """
    async def _authenticate(*args, **kwargs):
        return identity.AuthResult(user=_user())

    monkeypatch.setattr(main.identity_service, "authenticate", _authenticate)


def отправка_проходит(monkeypatch) -> None:
    async def _send(conn, **kwargs):
        return message_service.SendMessageResult(message=_message(), created=True)

    monkeypatch.setattr(message_service, "send_message", _send)


def отправить(клиент: TestClient, **headers) -> Any:
    return клиент.post(
        URL,
        json=ТЕЛО,
        headers={"authorization": f"Bearer {ТОКЕН}", **headers},
    )


def test_дерево_запроса_и_шаблон_в_имени_спана(клиент, экспортёр, monkeypatch):
    вход_разрешён(monkeypatch)
    отправка_проходит(monkeypatch)

    assert отправить(клиент).status_code == 201

    спаны = записанные(экспортёр)
    корень = по_именам(спаны)[ИМЯ_КОРНЯ]

    # Имя — шаблон, а не путь с идентификатором беседы: сырой путь завёл бы
    # имя спана на каждый разговор, то есть ту же кардинальность, от
    # которой этот же обработчик ушёл в метках. Проверяется именно
    # переименование: на входе в посредник шаблона ещё нет, и спан
    # открывается с одного метода.
    assert f"POST /conversations/{CONVERSATION_ID}/messages" not in по_именам(спаны)
    assert корень.parent is None
    assert корень.kind is tracing.SERVER
    assert корень.attributes["http.route"] == "/conversations/{conversation_id}/messages"
    # Стадия конвейера — на корне: политика хвостовой выборки читает
    # признак у корня трассы, у детей он ничего не решает.
    assert корень.attributes["messaging.pipeline.stage"] == СТАДИИ["api"]
    # Сквозной ключ на корневом спане: после перехода на три трассы поиск
    # по `trace_id` находит одну из трёх, и сообщение ищется по нему.
    assert корень.attributes["messaging.message.id"] == str(MESSAGE_ID)

    for имя in ("auth.check", "response"):
        assert имя in по_именам(спаны), f"нет спана {имя}"
        assert родитель(по_именам(спаны)[имя]) == участок(корень)
        assert след(по_именам(спаны)[имя]) == след(корень)


def test_чужая_трасса_продолжается_а_не_начинается_заново(клиент, экспортёр, monkeypatch):
    вход_разрешён(monkeypatch)
    отправка_проходит(monkeypatch)

    ответ = отправить(клиент, **{trace.HEADER: f"00-{ТРАССА}-{УЧАСТОК}-01"})
    assert ответ.status_code == 201
    # Идентификатор уходит клиенту и совпадает с чужим: разойдись они,
    # поддержка искала бы запрос по чужой трассе.
    assert ответ.headers["X-Trace-Id"] == ТРАССА

    спаны = записанные(экспортёр)
    assert {след(span) for span in спаны} == {ТРАССА}
    assert родитель(по_именам(спаны)[ИМЯ_КОРНЯ]) == УЧАСТОК


def test_отказ_входа_не_помечается_ошибкой(клиент, экспортёр, monkeypatch):
    """401 — законный исход, а не сбой.

    Пометив его статусом `ERROR`, мы отдали бы сканеру портов право
    наполнять хранилище трасс: политика «ошибки хранить» удерживает всё
    помеченное, и настоящие ошибки в этом шуме перестали бы отличаться
    от попыток подобрать токен.
    """
    async def _отказ(*args, **kwargs):
        return identity.AuthResult(rejection=identity.TokenRejection.BAD_SIGNATURE)

    monkeypatch.setattr(main.identity_service, "authenticate", _отказ)

    assert отправить(клиент).status_code == 401

    спаны = по_именам(записанные(экспортёр))
    assert спаны["auth.check"].status.status_code is StatusCode.UNSET
    # Токен в атрибуты не попадает: спаны уезжают в чужое хранилище,
    # и предъявленное удостоверение в них означало бы утечку.
    assert ТОКЕН not in json.dumps(
        dict(спаны["auth.check"].attributes), ensure_ascii=False
    )


def test_исключение_из_обработчика_помечает_корень_ошибкой(клиент, экспортёр, monkeypatch):
    """Сломанный обработчик — то, ради чего политика «ошибки хранить» заведена.

    Хвостовая выборка решает по статусу спана. Без пометки этот случай
    удержала бы только вероятностная политика, то есть в пяти случаях
    из сотни, — а разбирать падение в проде по пяти процентам отказов
    нельзя.
    """
    вход_разрешён(monkeypatch)

    async def _падает(*args, **kwargs):
        raise RuntimeError("база недоступна")

    monkeypatch.setattr(message_service, "send_message", _падает)

    with pytest.raises(RuntimeError, match="база недоступна"):
        отправить(клиент)

    # Имя — шаблон маршрута и на отказе: отказавший запрос ищут в Tempo
    # именно по нему, а односложное `POST` не отвечает, куда он шёл.
    корень = по_именам(записанные(экспортёр))[ИМЯ_КОРНЯ]
    assert корень.status.status_code is StatusCode.ERROR
    assert корень.attributes["error.type"] == "RuntimeError"


def test_отказ_сервера_ответом_помечается_по_коду(клиент, экспортёр, monkeypatch):
    """Второй путь к 5xx — ответ, а не исключение.

    У отказа, отданного обработчиком, нет исключения, по которому его
    можно опознать: единственный признак — код ответа.
    """
    вход_разрешён(monkeypatch)

    async def _внутренний(conn, **kwargs):
        return message_service.SendMessageResult(rejection=Reason.INTERNAL)

    monkeypatch.setattr(message_service, "send_message", _внутренний)

    assert отправить(клиент).status_code == 500

    корень = по_именам(записанные(экспортёр))[ИМЯ_КОРНЯ]
    assert корень.status.status_code is StatusCode.ERROR
    # Значение — код, а не слово: для HTTP семантические конвенции
    # предписывают именно его.
    assert корень.attributes["error.type"] == "500"


# --- дерево транзакции приёма сообщения ---------------------------------------


class Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None


class Connection:
    def transaction(self) -> Transaction:
        return Transaction()


def _члены():
    return []


def test_дерево_транзакции_и_вложенность_проверки_членства(экспортёр, monkeypatch):
    """Проверка членства вложена в транзакцию, а не стоит рядом с ней.

    Она *и есть* блокировка строки беседы (`SELECT ... FOR UPDATE`):
    вне транзакции между проверкой и вставкой помещается гонка, и
    в беседу, из которой только что вышли, уедет сообщение.
    """
    async def _lock(*args, **kwargs):
        return ConversationSeq(6)

    async def _missing(*args, **kwargs):
        return None

    async def _sequence(*args, **kwargs):
        return ConversationSeq(7)

    async def _insert(*args, **kwargs):
        return _message()

    async def _members(*args, **kwargs):
        return _члены()

    события: list[dict] = []

    async def _event(*args, **kwargs):
        события.append(kwargs)

    monkeypatch.setattr(message_service.messages, "lock_active_conversation", _lock)
    monkeypatch.setattr(message_service.messages, "fetch_by_client_id", _missing)
    monkeypatch.setattr(message_service.messages, "allocate_sequence", _sequence)
    monkeypatch.setattr(message_service.messages, "insert_message", _insert)
    monkeypatch.setattr(message_service.conversations, "list_active_members", _members)
    monkeypatch.setattr(message_service.outbox, "insert_event", _event)

    asyncio.run(
        message_service.send_message(
            Connection(),
            sender_id=ACTOR_ID,
            conversation_id=ConversationId(CONVERSATION_ID),
            client_message_id=ClientMessageId(CLIENT_ID),
            kind=MessageKind.TEXT,
            payload=MessagePayload(text="привет"),
            trace_id=ТРАССА,
            span_id=УЧАСТОК,
        )
    )

    спаны = по_именам(записанные(экспортёр))
    транзакция = спаны["postgres.transaction"]
    assert транзакция.parent is None
    assert транзакция.kind is tracing.INTERNAL
    for имя in ("membership.check", "message.insert", "outbox.insert"):
        assert имя in спаны, f"нет спана {имя}"
        assert родитель(спаны[имя]) == участок(транзакция)
    # Один спан на обе записи: в контракте узел один, и две записи —
    # одна логическая операция.
    assert спаны["outbox.insert"].attributes["messenger.outbox.records"] == 2

    # Пара создателя уезжает в тела обоих событий: отправитель поставит
    # на неё ссылку, а без участка ссылку строить не на что.
    for событие in события:
        assert событие["payload"]["trace_id"] == ТРАССА
        assert событие["payload"]["span_id"] == УЧАСТОК


# --- дерево отправителя -------------------------------------------------------


НАСТРОЙКИ = relay_service.RelaySettings(owner="relay-1", batch_size=10, lease_seconds=60)


def _запись(record_id: int, *, trace_id=None, span_id=None):
    payload: dict[str, Any] = {
        "event_type": "message.created",
        "message_id": str(MESSAGE_ID),
    }
    if trace_id:
        payload["trace_id"] = trace_id
    if span_id:
        payload["span_id"] = span_id
    return outbox_domain.OutboxRecord(
        id=record_id,
        event_id=EventId(uuid.uuid4()),
        event_type="message.created",
        event_version=1,
        partition_key=str(CONVERSATION_ID),
        payload=payload,
        attempts=0,
        created_at=datetime.now(UTC),
    )


class FakePublisher:
    def __init__(self, *, fails_from: int | None = None) -> None:
        self.headers: list[dict[str, str]] = []
        self.fails_from = fails_from

    async def publish(self, *, topic, key, value, headers):
        if self.fails_from is not None and len(self.headers) >= self.fails_from:
            raise ConnectionError("брокер недоступен")
        self.headers.append(headers)


@pytest.fixture
def репозиторий(monkeypatch):
    """Подменяет репозиторий outbox: проверяется отправитель, не SQL."""
    state: dict[str, Any] = {"claimed": [], "marked": []}

    async def claim_batch(conn, **kwargs):
        return state["claimed"]

    async def mark_published(conn, *, ids, owner):
        state["marked"] = list(ids)
        return len(ids)

    async def record_failure(conn, **kwargs):
        pass

    monkeypatch.setattr(relay_service.outbox, "claim_batch", claim_batch)
    monkeypatch.setattr(relay_service.outbox, "mark_published", mark_published)
    monkeypatch.setattr(relay_service.outbox, "record_failure", record_failure)
    return state


def test_дерево_отправителя_и_ссылка_на_запрос(экспортёр, репозиторий):
    """Отправитель начинает свою трассу и ссылается на чужую.

    Родителем спан запроса быть не может: к моменту отправки процесс API
    уже ответил клиенту, и трасса отправителя приклеилась бы к давно
    завершённой чужой, потеряв собственный смысл длительности.
    """
    репозиторий["claimed"] = [_запись(1, trace_id=ТРАССА, span_id=УЧАСТОК)]
    publisher = FakePublisher()

    asyncio.run(
        relay_service.publish_batch(None, publisher=publisher, settings=НАСТРОЙКИ)
    )

    спаны = записанные(экспортёр)
    корень = по_именам(спаны)["outbox-relay"]
    assert корень.parent is None
    # INTERNAL, а не CONSUMER: отправитель разбирает свою очередь,
    # а не выполняет операцию `process` над принятым сообщением.
    assert корень.kind is tracing.INTERNAL
    assert корень.attributes["messaging.pipeline.stage"] == СТАДИИ["relay"]
    assert корень.attributes["messenger.outbox.claimed"] == 1
    assert родитель(по_именам(спаны)["outbox.claim"]) == участок(корень)

    производство = [span for span in спаны if span.name == "kafka.produce"]
    assert len(производство) == 1
    спан = производство[0]
    # Вид PRODUCER здесь по спецификации, а не по вкусу: его контекст и
    # есть контекст создания записи. Помеченный INTERNAL, он не даст
    # инструментам разбора связи производителя с потребителем.
    assert спан.kind is tracing.PRODUCER
    assert родитель(спан) == участок(корень)
    assert след(спан) != ТРАССА
    assert спан.attributes["messaging.system"] == "kafka"
    assert спан.attributes["messaging.destination.name"] == "messenger.events.v1"
    # Единственный способ найти сообщение прямо в Tempo: по `trace_id`
    # путь больше не находится целиком.
    assert спан.attributes["messaging.message.id"] == str(MESSAGE_ID)

    assert len(спан.links) == 1
    assert format_trace_id(спан.links[0].context.trace_id) == ТРАССА
    assert format_span_id(спан.links[0].context.span_id) == УЧАСТОК


def test_заголовок_kafka_называет_созданный_спан(экспортёр, репозиторий):
    """Заголовок несёт контекст спана `kafka.produce`, а не случайные байты.

    Сегодняшняя трещина: подставлялся свежесгенерированный участок,
    которому не соответствует ни один спан, — и ссылка потребителя
    указывала в пустоту.
    """
    репозиторий["claimed"] = [_запись(1, trace_id=ТРАССА, span_id=УЧАСТОК)]
    publisher = FakePublisher()

    asyncio.run(
        relay_service.publish_batch(None, publisher=publisher, settings=НАСТРОЙКИ)
    )

    спан = [span for span in записанные(экспортёр) if span.name == "kafka.produce"][0]
    assert trace.parse(publisher.headers[0][trace.HEADER]) == (след(спан), участок(спан))


def test_запись_прежнего_отправителя_переживается_штатно(экспортёр, репозиторий):
    """У записи, созданной до этой работы, `span_id` нет.

    Это штатное состояние при выкатке, а не ошибка: подставить случайный
    участок значило бы соврать. Ссылки не будет, а трасса создателя
    останется атрибутом — её видно, хотя связать с конкретным спаном
    уже нечем.
    """
    репозиторий["claimed"] = [_запись(1, trace_id=ТРАССА)]
    asyncio.run(relay_service.publish_batch(None, publisher=FakePublisher(), settings=НАСТРОЙКИ))

    спан = [span for span in записанные(экспортёр) if span.name == "kafka.produce"][0]
    assert len(спан.links) == 0
    assert спан.attributes["messenger.source_trace_id"] == ТРАССА


# --- дерево потребителя -------------------------------------------------------


class FakeSubscriber:
    """Отдаёт пачку и просит остановиться.

    Остановка выставляется сразу после выдачи пачки — так выглядит
    выключение пода посреди обработки. Это же оставляет тест с одним
    кругом цикла: корень создаётся на каждый опрос, и холостой опрос
    завёл бы второй корень, к которому ни одно из утверждений не
    относится.
    """

    def __init__(self, batch: list[Any], stop_event: asyncio.Event) -> None:
        self.batch = batch
        # Событие под своим именем: имя `stop` занято методом остановки,
        # ровно как у настоящего подписчика.
        self.stop_event = stop_event
        self.committed = 0

    async def start(self) -> bool:
        return True

    async def poll(self) -> list[Any]:
        пачка, self.batch = self.batch, []
        self.stop_event.set()
        return пачка

    async def commit(self) -> None:
        self.committed += 1

    async def stop(self) -> None:
        pass


class FakeCache:
    """Кеш половин с семантикой Redis: запомнил, отсеял повтор, забыл."""

    def __init__(self) -> None:
        self.halves: dict[str, dict] = {}
        self.delivered: set[str] = set()

    async def healthy(self) -> bool:
        return True

    async def remember_half(self, *, message_id, half, body):
        other = "content" if half == "fact" else "fact"
        self.halves[f"{message_id}:{half}"] = body
        return self.halves.get(f"{message_id}:{other}")

    async def mark_delivered(self, *, message_id) -> bool:
        if message_id in self.delivered:
            return False
        self.delivered.add(message_id)
        return True

    async def forget(self, *, message_id) -> None:
        pass

    async def close(self) -> None:
        pass


class FakeCentrifugo:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, channel, data) -> bool:
        self.published.append((channel, data))
        return True


def test_дерево_потребителя_и_ссылка_на_отправителя(экспортёр, monkeypatch):
    """Потребитель начинает свою трассу со ссылкой на спан отправителя.

    Обе половины одним потребителем: право на них есть только у него,
    и собрать их обратно больше некому.
    """
    # Заголовок записи — контекст спана `kafka.produce` отправителя.
    ТРАССА_ОТПРАВИТЕЛЯ = "b" * 32
    УЧАСТОК_ОТПРАВИТЕЛЯ = "c" * 16
    headers = {
        trace.HEADER: f"00-{ТРАССА_ОТПРАВИТЕЛЯ}-{УЧАСТОК_ОТПРАВИТЕЛЯ}-01",
    }
    факт = {
        "message_id": str(MESSAGE_ID),
        "conversation_id": str(CONVERSATION_ID),
        "conversation_seq": 7,
        "sender_id": str(ACTOR_ID),
    }
    содержимое = {"message_id": str(MESSAGE_ID), "conversation_id": str(CONVERSATION_ID),
                  "payload": {"text": "привет"}}

    stop = asyncio.Event()
    подписчик = FakeSubscriber(
        [
            (delivery.FACT_TOPIC, headers, факт),
            (delivery.CONTENT_TOPIC, headers, содержимое),
        ],
        stop,
    )
    centrifugo = FakeCentrifugo()

    monkeypatch.setattr(kafka_adapter, "Subscriber", lambda **kwargs: подписчик)
    monkeypatch.setattr(cache_adapter, "EventCache", lambda **kwargs: FakeCache())
    monkeypatch.setattr(centrifugo_adapter, "CentrifugoClient", lambda **kwargs: centrifugo)

    asyncio.run(consumer_realtime.run(stop))
    assert подписчик.committed == 1
    assert len(centrifugo.published) == 1

    спаны = записанные(экспортёр)
    # Корень на пачку, а не на запись: так решений хвостовой выборки
    # меньше, и `decision_wait` коллектора можно держать коротким.
    assert len([span for span in спаны if span.name == "consumer-realtime"]) == 1
    корень = по_именам(спаны)["consumer-realtime"]
    assert корень.parent is None
    # CONSUMER — это операция `process` по таблице спецификации: принятая
    # пачка обработана целиком.
    assert корень.kind is tracing.CONSUMER
    assert корень.attributes["messaging.pipeline.stage"] == СТАДИИ["realtime"]
    assert корень.attributes["messaging.batch.message_count"] == 2

    приём = по_именам(спаны)["kafka.consume"]
    # А это операция `receive`, и спецификация назначает ей CLIENT, а не
    # CONSUMER. Неочевидно настолько, что «поправивший» здесь на CONSUMER
    # сломает разбор связи между производителем и потребителем.
    assert приём.kind is tracing.CLIENT
    assert родитель(приём) == участок(корень)

    публикация = по_именам(спаны)["centrifugo.publish"]
    # CLIENT, а не PRODUCER: свой контекст мы в Centrifugo не кладём,
    # значит его контекст не становится контекстом создания записи.
    assert публикация.kind is tracing.CLIENT
    assert родитель(публикация) == участок(корень)
    assert публикация.attributes["messaging.destination.name"] == (
        f"conversation:{CONVERSATION_ID}"
    )
    assert публикация.attributes["messaging.message.id"] == str(MESSAGE_ID)
    assert len(публикация.links) == 1
    assert format_trace_id(публикация.links[0].context.trace_id) == ТРАССА_ОТПРАВИТЕЛЯ
    assert format_span_id(публикация.links[0].context.span_id) == УЧАСТОК_ОТПРАВИТЕЛЯ


# --- инварианты ----------------------------------------------------------------


def test_запись_журнала_указывает_на_настоящий_спан(экспортёр, журнал):
    """Главный инвариант: `span_id` записи — идентификатор существующего спана.

    Сегодня идентификаторы синтезировались в `bind()`, и их не носил
    ни один спан. Расхождение не видно ни в журнале, ни в трассе по
    отдельности — только на разборе инцидента, когда искать уже поздно.
    """
    with tracing.span("шаг") as span:
        запись = журнал()
        # Журнал читает contextvars, а их заполняет спан: расхождение
        # невозможно по построению, а не по договорённости.
        assert (запись["trace_id"], запись["span_id"]) == (span.trace_id, span.span_id)

    спаны = записанные(экспортёр)
    assert [след(span) for span in спаны] == [span.trace_id]
    assert [участок(span) for span in спаны] == [span.span_id]


def test_якорь_не_сдвигается_вложенным_спаном(экспортёр):
    """Цель ссылки из outbox — корневой спан запроса, а не текущий.

    Иначе добавление любого спана внутри обработчика молча уехало бы
    на него, и заметить это можно было бы только по неверной ссылке
    в Tempo.
    """
    with tracing.span("корень", anchor=True) as корень:
        assert tracing.current_link() == (корень.trace_id, корень.span_id)
        with tracing.span("вложенный") as вложенный:
            assert вложенный.trace_id == корень.trace_id
            assert tracing.current_link() == (корень.trace_id, корень.span_id)
        assert tracing.current_link() == (корень.trace_id, корень.span_id)
    # Снятие обязательно: оставшийся якорь приписал бы следующему запросу
    # чужой корень, и выглядело бы это как настоящая связь.
    assert tracing.current_link() is None


# --- выборка и выключенный экспорт ---------------------------------------------


def test_без_адреса_коллектора_спаны_выключены_но_живы(monkeypatch):
    """Модульные тесты и локальный запуск идут без коллектора.

    Трассировка при этом не «ломается»: `span()` отдаёт спан с настоящими
    синтетическими идентификаторами, поэтому ни один вызывающий не
    обрастает `if span is not None`, а записи журнала остаются связанными
    между собой и после выключения экспорта.
    """
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    tracing.reset()
    tracing.configure()
    try:
        with tracing.span("шаг") as span:
            assert len(span.trace_id) == 32
            assert len(span.span_id) == 16
            assert (TRACE_ID.get(), SPAN_ID.get()) == (span.trace_id, span.span_id)
            span.set_attribute("ключ", "значение")
            span.update_name("переименован")
        assert tracing.current_link() is None
    finally:
        tracing.reset()


def _спаны_с_выборкой(monkeypatch, значение: str) -> list[Any]:
    """Ставит провайдер с заданным значением переменной и отдаёт спаны."""
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", значение)
    exporter = InMemorySpanExporter()
    with с_провайдером(exporter):
        with tracing.span("шаг"):
            pass
    return list(exporter.get_finished_spans())


@pytest.mark.parametrize("значение", ["", "   ", "always_on", "выдуманное"])
def test_пустая_или_незнакомая_выборка_остаётся_полной(monkeypatch, значение):
    """Порядок выкатки безопасен по построению, и это надо сохранить.

    Чарт с ключом выборки может уехать раньше кода: тогда переменная
    окажется пустой строкой, и это обязано означать «полная выборка»,
    а не «трассировка выключена» и не падение на старте.
    """
    assert len(_спаны_с_выборкой(monkeypatch, значение)) == 1


def test_явное_выключение_выборки_убирает_спаны(monkeypatch):
    # Головная доля оставлена как аварийный выключатель трассировки:
    # решение по ошибке принимает коллектор, но выключить экспорт целиком
    # должно быть возможно — и видно, что выключили именно это.
    assert _спаны_с_выборкой(monkeypatch, "always_off") == []


def test_разбор_совпадает_с_эталонной_библиотекой():
    """Два разбора одного формата не должны расходиться.

    Свой разбор нужен там, где SDK не позвать, но разойтись с ним он
    не имеет права: расхождение проявится тем, что чужая трасса
    однажды «не подхватится» — то есть молчанием, а не ошибкой.
    """
    заголовок = trace.header_for(ТРАССА, УЧАСТОК)
    assert trace.parse(заголовок) == (ТРАССА, УЧАСТОК)

    контекст = TraceContextTextMapPropagator().extract({"traceparent": заголовок})
    чужой = otel.get_current_span(контекст).get_span_context()
    assert format_trace_id(чужой.trace_id) == ТРАССА
    assert format_span_id(чужой.span_id) == УЧАСТОК


# --- телеметрия не на критическом пути -----------------------------------------


class МедленныйЭкспортёр(SpanExporter):
    """Отвечает через две секунды — как коллектор под нагрузкой."""

    def __init__(self) -> None:
        self.exported = 0

    def export(self, spans) -> SpanExportResult:
        self.exported += len(spans)
        time.sleep(2.0)
        return SpanExportResult.SUCCESS


class ПадающийЭкспортёр(SpanExporter):
    def export(self, spans) -> SpanExportResult:
        raise ConnectionError("коллектор недоступен")


def test_медленный_экспорт_не_задерживает_работу():
    """Потеря телеметрии допустима; потеря сообщения из-за неё — нет.

    Проверяется прямо, а не гашением коллектора в кластере: блок внутри
    `span()` обязан завершиться, не дожидаясь ответа экспортёра.
    """
    экспортёр = МедленныйЭкспортёр()
    with с_провайдером(экспортёр):
        started = time.perf_counter()
        with tracing.span("шаг"):
            pass
        elapsed = time.perf_counter() - started

    assert elapsed < 0.5, f"спан ждал экспортёр {elapsed:.2f} с"
    # Спан при этом дошёл до экспортёра: развязка настоящая, а не
    # декларированная — работа просто ушла в другой поток.
    assert экспортёр.exported == 1


def test_падение_экспорта_наружу_не_выходит(caplog):
    """Отказ коллектора не повод уронить обработку запроса или остановку пода."""
    with с_провайдером(ПадающийЭкспортёр()):
        with tracing.span("шаг") as span:
            assert len(span.span_id) == 16

    # Дошли сюда — значит `shutdown()` вернулся, а не бросил.
    assert tracing.current_link() is None
    # Потеря телеметрии при этом видна: SDK пишет о ней сам, и глушить его
    # уровнем значило бы спрятать единственный признак того, что спаны
    # не доезжают, — тот самый, ради которого «трассировка выключена»
    # отличается от «трассировка сломана».
    assert any("export" in record.getMessage().lower() for record in caplog.records)


# --- инструментация библиотек ---------------------------------------------------
#
# Спаны внутрь спана дают библиотеки: драйвер базы, кеш и HTTP-клиент.
# Проверяется здесь не то, что библиотеки работают, — а два наших решения.
# Первое: инструментация смотрит на **наш** провайдер. Трейсер она берёт из
# глобального реестра OpenTelemetry, которого мы не занимаем, поэтому
# забытый `tracer_provider=` не сломал бы ничего видимым образом: спаны
# просто исчезли бы, а база выглядела бы быстрой. Второе: содержимое в
# спаны не уезжает — ни текст сообщения в параметрах запроса, ни ключ
# доступа в заголовке.
#
# Драйверы подменяются до установки инструментации: настоящим нужен
# сервер, а подменяется ровно внешняя граница.

СОДЕРЖИМОЕ = "канарейка-в-параметрах-запроса"


def test_запрос_к_базе_даёт_спан_драйвера_без_параметров(monkeypatch):
    """Время внутри спана разложимо, а содержимое — нет."""
    import asyncpg

    async def _фальшивый_fetchrow(self, query, *args, **kwargs):
        return None

    monkeypatch.setattr(asyncpg.Connection, "fetchrow", _фальшивый_fetchrow)

    exporter = InMemorySpanExporter()
    with с_провайдером(exporter):
        # Подделка вместо настоящего соединения: оно требует сервера,
        # а его `__del__` ругается на недостроенный объект. Метод
        # привязывается к подделке явно — ровно так же, как это делает
        # обращение `conn.fetchrow(...)`; иначе инструментация не увидела
        # бы соединения и не положила бы атрибуты, которые проверяются.
        соединение = SimpleNamespace(
            # Поля, из которых инструментация берёт атрибуты соединения.
            _params=SimpleNamespace(database="messenger", user="messenger"),
            _addr=("10.0.0.1", 5432),
        )
        fetchrow = asyncpg.Connection.fetchrow.__get__(соединение, asyncpg.Connection)
        asyncio.run(fetchrow("SELECT $1::text", СОДЕРЖИМОЕ))

    спаны = записанные(exporter)
    # Имя — первый токен запроса: у нас оно низкой кардинальности,
    # а не «postgres.query» на каждый вызов.
    assert [span.name for span in спаны] == ["SELECT"]

    спан = спаны[0]
    assert спан.kind is otel.SpanKind.CLIENT
    assert спан.attributes["db.statement"] == "SELECT $1::text"
    assert спан.attributes["db.name"] == "messenger"
    # Параметры не уехали: `capture_parameters` выключен, и выключен явно.
    # С ним в атрибут уехал бы кортеж параметров запроса, то есть текст
    # сообщения и адрес получателя.
    assert "db.statement.parameters" not in спан.attributes
    assert СОДЕРЖИМОЕ not in str(dict(спан.attributes))


def test_аргументы_команды_кеша_в_спан_не_уезжают(monkeypatch):
    """У Redis в спан попадает `SET ? ?`, а не ключ и не значение.

    Это делает сама библиотека, и проверить это надо именно здесь: на
    другой стороне — `json` тела события, то есть содержимое сообщения,
    а хранится трасса в Tempo неделю.
    """
    import redis.asyncio as aioredis

    async def _фальшивый_execute(self, *args, **kwargs):
        return None

    monkeypatch.setattr(aioredis.Redis, "execute_command", _фальшивый_execute)

    exporter = InMemorySpanExporter()
    with с_провайдером(exporter):
        клиент = aioredis.Redis.from_url("redis://127.0.0.1:6379/0")
        asyncio.run(клиент.execute_command("SET", "pair:abc:content", СОДЕРЖИМОЕ))

    спаны = записанные(exporter)
    assert [span.name for span in спаны] == ["SET"]

    спан = спаны[0]
    assert спан.kind is otel.SpanKind.CLIENT
    assert спан.attributes["db.statement"] == "SET ? ?"
    assert СОДЕРЖИМОЕ not in str(dict(спан.attributes))


def test_ключ_доступа_не_попадает_в_спан_http():
    """Захват заголовков HTTP выключен, и это наше решение, а не умолчание.

    В запросе к Keycloak едет `Authorization: Bearer`, в запрос
    к Centrifugo — `X-API-Key`. Включается захват переменной окружения,
    то есть ставит его файл развёртывания, а он на `tracing.py` не
    смотрит, — поэтому здесь проверка, а не комментарий.

    Поднимается настоящее соединение с петлёй, а не `MockTransport`:
    инструментация оборачивает транспорт, и подменённый транспорт
    остаётся без спана — то есть проверка на нём прошла бы, ничего
    не проверив.
    """
    exporter = InMemorySpanExporter()
    with с_провайдером(exporter):
        asyncio.run(_сходить_в_петлю(ТОКЕН))

    спаны = записанные(exporter)
    assert [span.name for span in спаны] == ["GET"]

    спан = спаны[0]
    assert спан.kind is otel.SpanKind.CLIENT
    assert спан.attributes["http.url"].startswith("http://127.0.0.1:")
    # Ни одним атрибутом: ни заголовком, ни значением в строке запроса.
    assert ТОКЕН not in str(dict(спан.attributes))


async def _сходить_в_петлю(токен: str) -> None:
    """Запрос к серверу на петле, который отвечает и закрывается.

    Свой сервер, а не заглушка транспорта: спан создаётся на настоящем
    транспорте, и без настоящего соединения проверять нечего.
    """
    import httpx

    async def _обработчик(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while await reader.readline() not in (b"\r\n", b"\n", b""):
            pass
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"
        )
        await writer.drain()
        writer.close()

    сервер = await asyncio.start_server(_обработчик, "127.0.0.1", 0)
    порт = сервер.sockets[0].getsockname()[1]
    try:
        async with httpx.AsyncClient() as клиент:
            await клиент.get(
                f"http://127.0.0.1:{порт}/realms/messenger/protocol/openid-connect/token",
                headers={"Authorization": f"Bearer {токен}"},
            )
    finally:
        сервер.close()
        await сервер.wait_closed()


def test_инструментация_живёт_ровно_столько_же_сколько_провайдер():
    """Снятие обязательно, и вот почему это проверяется.

    Инструментация держит трейсер, взятый у провайдера в момент
    установки. Оставленная висеть после снятия провайдера, она писала бы
    спаны в выключенный — то есть соседний тест не увидел бы ни одного
    спана драйвера и не понял бы, почему.
    """
    import asyncpg
    import redis.asyncio as aioredis

    было_у_драйвера = asyncpg.Connection.fetchrow
    было_у_кеша = aioredis.Redis.execute_command

    with с_провайдером(InMemorySpanExporter()):
        assert asyncpg.Connection.fetchrow is not было_у_драйвера
        assert aioredis.Redis.execute_command is not было_у_кеша

    assert asyncpg.Connection.fetchrow is было_у_драйвера
    assert aioredis.Redis.execute_command is было_у_кеша
