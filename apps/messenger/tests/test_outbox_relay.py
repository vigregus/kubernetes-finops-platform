"""Отправитель outbox: порядок шагов и поведение при отказе.

Ни базы, ни Kafka: репозиторий и продюсер подменяются, потому что
проверяется не они, а решения отправителя — что он публикует, в каком
порядке останавливается и что отмечает.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from messenger.domain import outbox as domain
from messenger.domain.ids import EventId
from messenger.repositories import outbox as repo
from messenger.services import outbox_relay

НАСТРОЙКИ = outbox_relay.RelaySettings(owner="relay-1", batch_size=10, lease_seconds=60)


def запись(
    record_id: int,
    event_type: str = "message.created",
    attempts: int = 0,
    trace_id: str | None = None,
):
    return domain.OutboxRecord(
        id=record_id,
        event_id=EventId(uuid.uuid4()),
        event_type=event_type,
        event_version=1,
        partition_key="беседа-1",
        payload={"event_type": event_type,
                 **({"trace_id": trace_id} if trace_id else {})},
        attempts=attempts,
        created_at=datetime.now(UTC),
    )


class FakePublisher:
    def __init__(self, *, fails_from: int | None = None) -> None:
        self.published: list[str] = []
        self.headers: list[dict[str, str]] = []
        self.fails_from = fails_from

    async def publish(self, *, topic, key, value, headers):
        if self.fails_from is not None and len(self.published) >= self.fails_from:
            raise ConnectionError("брокер недоступен")
        self.published.append(topic)
        self.headers.append(headers)


@pytest.fixture
def repo_stub(monkeypatch):
    """Подменяет репозиторий и запоминает, что отправитель в него сказал."""
    state = {"claimed": [], "marked": [], "failures": [], "mark_result": None}

    async def claim_batch(conn, **kwargs):
        return state["claimed"]

    async def mark_published(conn, *, ids, owner):
        state["marked"] = list(ids)
        return state["mark_result"] if state["mark_result"] is not None else len(ids)

    async def record_failure(conn, *, record_id, owner, error, retry_after):
        state["failures"].append((record_id, error, retry_after))

    monkeypatch.setattr(repo, "claim_batch", claim_batch)
    monkeypatch.setattr(repo, "mark_published", mark_published)
    monkeypatch.setattr(repo, "record_failure", record_failure)
    return state


def прогон(publisher, **kwargs):
    return asyncio.run(
        outbox_relay.publish_batch(None, publisher=publisher, settings=НАСТРОЙКИ, **kwargs)
    )


def test_факт_и_содержимое_уходят_в_разные_потоки():
    """SEC-010 держится на этом соответствии.

    Событие содержимого, ушедшее в поток фактов, — это утечка текста
    тому потребителю, которому положен только факт появления сообщения.
    """
    assert запись(1, "message.created").topic == "messenger.events.v1"
    assert запись(2, "message.content").topic == "messenger.content.v1"


def test_неизвестный_тип_никуда_не_уходит():
    """`None`, а не поток по умолчанию: умолчание однажды окажется
    не тем потоком, и заметят это по утёкшему содержимому."""
    assert запись(3, "message.выдуманное").topic is None


def test_отсрочка_удваивается_но_не_бесконечно():
    assert domain.backoff_for(0) == domain.MIN_BACKOFF
    assert domain.backoff_for(2) > domain.backoff_for(1)
    assert domain.backoff_for(50) == domain.MAX_BACKOFF


def test_пустая_очередь_не_трогает_kafka(repo_stub):
    publisher = FakePublisher()
    исход = прогон(publisher)
    assert исход.total == 0 and publisher.published == []


def test_вся_пачка_публикуется_и_отмечается(repo_stub):
    repo_stub["claimed"] = [запись(1), запись(2, "message.content")]
    publisher = FakePublisher()
    исход = прогон(publisher)
    assert publisher.published == ["messenger.events.v1", "messenger.content.v1"]
    assert repo_stub["marked"] == [1, 2]
    assert исход.published == 2 and исход.failed == 0


def test_отказ_останавливает_пачку(repo_stub):
    """Порядок внутри беседы важнее пропускной способности.

    Следующая запись может быть содержимым того же сообщения, и,
    опубликовав её после неудачи с фактом, отправитель показал бы
    потребителю содержимое сообщения, о появлении которого тот
    ещё не знает.
    """
    repo_stub["claimed"] = [запись(1), запись(2, "message.content"), запись(3)]
    publisher = FakePublisher(fails_from=1)
    исход = прогон(publisher)

    assert len(publisher.published) == 1
    assert исход.published == 1 and исход.failed == 1
    # Отмечена только первая; неудачная и та, до которой не дошли, - нет.
    assert repo_stub["marked"] == [1]
    assert [f[0] for f in repo_stub["failures"]] == [2]


def test_неудача_отодвигает_следующую_попытку(repo_stub):
    repo_stub["claimed"] = [запись(1, attempts=3)]
    прогон(FakePublisher(fails_from=0))
    _, ошибка, отсрочка = repo_stub["failures"][0]
    assert "ConnectionError" in ошибка
    assert отсрочка == domain.backoff_for(4)


def test_неизвестный_тип_не_публикуется_но_не_рушит_пачку(repo_stub):
    repo_stub["claimed"] = [запись(1, "message.выдуманное"), запись(2)]
    publisher = FakePublisher()
    исход = прогон(publisher)

    assert publisher.published == ["messenger.events.v1"]
    assert исход.unroutable == 1 and исход.published == 1
    # Отсрочка большая: повтор такую запись не исправит.
    assert repo_stub["failures"][0][2] >= timedelta(minutes=30)


def test_потерянная_аренда_видна_в_исходе(repo_stub):
    """Отметились не все — значит, чью-то запись успел забрать другой
    отправитель, и она уйдёт в Kafka второй раз."""
    repo_stub["claimed"] = [запись(1), запись(2)]
    repo_stub["mark_result"] = 1
    исход = прогон(FakePublisher())
    assert исход.published == 1


def test_заголовок_записи_называет_спан_отправителя_а_не_трассу_запроса(repo_stub):
    """Асинхронная граница проходится телом события и заголовком.

    Заголовок нужен потребителю, который тело не разбирает: повтор
    отсеивается по `event_id`, но написать в журнал, к какой трассе
    относится отброшенное, всё равно надо.

    Трасса в заголовке при этом своя у отправителя. Подставлять сюда
    трассу запроса значило бы назвать родителем спан, которого к моменту
    отправки уже нет: процесс API ответил клиенту до того, как запись
    взята из очереди. Связь с запросом ставится ссылкой, и её видно
    только по спанам - здесь же проверяется обратное: заголовок не
    притворяется продолжением чужой трассы.
    """
    трасса = "4bf92f3577b34da6a3ce929d0e0e4736"
    repo_stub["claimed"] = [запись(1, trace_id=трасса)]
    publisher = FakePublisher()
    прогон(publisher)

    разобрано = outbox_relay.trace.parse(publisher.headers[0]["traceparent"])
    assert разобрано is not None
    assert разобрано[0] != трасса
    # Трасса запроса не потеряна: она остаётся в теле события и служит
    # запасным путём для потребителя.
    assert repo_stub["claimed"][0].payload["trace_id"] == трасса


def test_запись_без_трассы_получает_свою(repo_stub):
    """Событие, записанное до появления трассировки, не должно уезжать
    в Kafka без контекста: тогда строки отправителя не связать даже
    между собой."""
    repo_stub["claimed"] = [запись(1)]
    publisher = FakePublisher()
    прогон(publisher)

    разобрано = outbox_relay.trace.parse(publisher.headers[0]["traceparent"])
    assert разобрано is not None and len(разобрано[0]) == 32


def test_трасса_снимается_после_записи(repo_stub):
    """Оставшийся contextvar приписал бы чужую трассу следующей записи —
    худший вид ошибки в расследовании: выглядит как настоящая связь."""
    repo_stub["claimed"] = [запись(1, trace_id="4bf92f3577b34da6a3ce929d0e0e4736")]
    прогон(FakePublisher())
    assert outbox_relay.trace.current_trace_id() is None
