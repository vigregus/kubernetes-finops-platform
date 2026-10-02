"""Браузерная телеметрия (G3-008): счётчик событий, T_delivery, best-effort.

Ни базы, ни Prometheus-реестра по-настоящему: репозиторий подменяется
(monkeypatch, тем же приёмом, что `test_outbox_relay.py`), счётчики и
гистограмма читаются из их же `prometheus_client`-объектов - дешевле и
честнее, чем парсить `/metrics`. `asyncio.run` в синтетическом прогоне -
тем же приёмом, что у `test_outbox_relay.py:прогон`: pytest-asyncio
в проекте не установлен (`requirements-dev.txt`), а тесты остаются
обычными синхронными функциями.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from messenger.domain.ids import MessageId
from messenger.repositories import messages as messages_repo
from messenger.services import browser_telemetry
from messenger.telemetry import metrics


def событие(event_type: str, *, occurred_at: datetime, message_id: MessageId | None = None):
    return browser_telemetry.TelemetryEvent(
        event_type=event_type, occurred_at=occurred_at, message_id=message_id
    )


def прогон(events):
    return asyncio.run(browser_telemetry.ingest(object(), events=events))


def _browser_events_count(event_type: str) -> float:
    return metrics.BROWSER_EVENTS.labels(
        service=metrics.SERVICE, event_type=event_type
    )._value.get()


def _delivery_sample_sum() -> float:
    return metrics.BROWSER_DELIVERY_DURATION.labels(service=metrics.SERVICE)._sum.get()


def test_каждое_событие_считается_по_типу():
    before = _browser_events_count("ws_reconnected")

    прогон([событие("ws_reconnected", occurred_at=datetime.now(UTC))])

    assert _browser_events_count("ws_reconnected") == before + 1


def test_delivery_ack_считает_t_delivery(monkeypatch):
    created_at = datetime.now(UTC) - timedelta(seconds=2)
    message_id = MessageId(uuid.uuid4())

    async def fetch_created_at(conn, *, message_id):
        return created_at

    monkeypatch.setattr(messages_repo, "fetch_created_at", fetch_created_at)

    before = _delivery_sample_sum()
    ack_at = created_at + timedelta(seconds=2)

    прогон([событие("delivery_ack", occurred_at=ack_at, message_id=message_id)])

    # Сумма гистограммы выросла ровно на измеренные 2 секунды - не только
    # факт наблюдения, но и то, что t1 взят из репозитория, а не угадан.
    assert _delivery_sample_sum() == pytest.approx(before + 2, abs=0.01)


def test_delivery_ack_без_message_id_не_падает_и_не_пишет_метрику(monkeypatch):
    called = False

    async def fetch_created_at(conn, *, message_id):
        nonlocal called
        called = True
        return datetime.now(UTC)

    monkeypatch.setattr(messages_repo, "fetch_created_at", fetch_created_at)
    before = _delivery_sample_sum()

    # Контракт отвергает это 422-м раньше (model_validator в main.py);
    # сервис здесь проверяется отдельно - на случай вызова мимо API.
    прогон([событие("delivery_ack", occurred_at=datetime.now(UTC), message_id=None)])

    assert called is False
    assert _delivery_sample_sum() == before


def test_delivery_ack_на_неизвестное_сообщение_тихо_игнорируется(monkeypatch):
    async def fetch_created_at(conn, *, message_id):
        return None

    monkeypatch.setattr(messages_repo, "fetch_created_at", fetch_created_at)
    before = _delivery_sample_sum()

    прогон(
        [
            событие(
                "delivery_ack",
                occurred_at=datetime.now(UTC),
                message_id=MessageId(uuid.uuid4()),
            )
        ]
    )

    assert _delivery_sample_sum() == before


def test_отрицательная_задержка_не_пишется(monkeypatch):
    """Рассинхронизация часов браузера - не повод соврать гистограмме."""
    created_at = datetime.now(UTC)

    async def fetch_created_at(conn, *, message_id):
        return created_at

    monkeypatch.setattr(messages_repo, "fetch_created_at", fetch_created_at)
    before = _delivery_sample_sum()

    прогон(
        [
            событие(
                "delivery_ack",
                occurred_at=created_at - timedelta(seconds=5),
                message_id=MessageId(uuid.uuid4()),
            )
        ]
    )

    assert _delivery_sample_sum() == before
