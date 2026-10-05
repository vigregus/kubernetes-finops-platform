"""Подметальщик звонков: цикл переживает отказы и различимо о них говорит."""
from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest

from messenger.services import calls as service
from messenger.workers import call_sweeper as worker


class Pool:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class Stand:
    def __init__(self, monkeypatch, *, pauses=1, sweep_fails=frozenset(), pool_fails=0,
                 result=None):
        self.pool = Pool()
        self.result = result or service.SweepResult(examined=0, ended=0)
        self.sweeps = 0
        self.pools_opened = 0
        self.pauses: list[float] = []
        self.dependencies: list[bool] = []

        async def create_pool(settings, *, application_name):
            self.pools_opened += 1
            if self.pools_opened <= pool_fails:
                raise OSError("база недоступна")
            return self.pool

        @contextlib.asynccontextmanager
        async def connection(pool):
            yield None

        async def sweep(conn, *, realtime, **kwargs):
            self.sweeps += 1
            if self.sweeps in sweep_fails:
                raise RuntimeError("такт не удался")
            return self.result

        async def sleep(stop_event, seconds):
            self.pauses.append(seconds)
            if len(self.pauses) >= pauses:
                stop_event.set()

        monkeypatch.setattr(worker.postgres, "create_pool", create_pool)
        monkeypatch.setattr(worker.postgres, "connection", connection)
        monkeypatch.setattr(worker.calls, "sweep", sweep)
        monkeypatch.setattr(worker, "_sleep", sleep)
        monkeypatch.setattr(worker.runtime, "centrifugo_client_from_env", lambda: None)
        monkeypatch.setattr(
            worker.metrics, "dependency_up",
            lambda dependency, *, up: self.dependencies.append(up),
        )

    def run(self) -> None:
        async def _main() -> None:
            await worker.run(asyncio.Event())

        asyncio.run(_main())


def records(caplog, event):
    return [r for r in caplog.records if getattr(r, "event", None) == event]


@pytest.fixture
def caplog_info(caplog):
    caplog.set_level(logging.INFO, logger=worker.__name__)
    return caplog


def test_такт_пишет_запись_даже_когда_завершать_нечего(monkeypatch, caplog_info):
    stand = Stand(monkeypatch)
    stand.run()
    record = records(caplog_info, "call_sweep")[-1]
    assert record.result == "ok" and record.ended == 0
    assert stand.dependencies == [True]
    assert stand.pool.closed


def test_число_живых_звонков_идёт_в_метрику(monkeypatch):
    seen: list[int] = []
    Stand(monkeypatch, result=service.SweepResult(examined=5, ended=2))
    monkeypatch.setattr(worker.metrics, "calls_live", seen.append)
    asyncio.run(worker.run(asyncio.Event()))
    assert seen == [3]


def test_число_завершённых_попадает_в_запись(monkeypatch, caplog_info):
    Stand(monkeypatch, result=service.SweepResult(examined=4, ended=2)).run()
    record = records(caplog_info, "call_sweep")[-1]
    assert (record.examined, record.ended) == (4, 2)


def test_упавший_такт_не_роняет_цикл_и_отличим_от_успешного(monkeypatch, caplog_info):
    stand = Stand(monkeypatch, pauses=1, sweep_fails=frozenset({1}))

    # Первый такт падает, пауза после него короткая (1 с), второй проходит.
    monkeypatch.setattr(
        worker, "_sleep",
        _sleep_until(stand, stops_after=2),
    )
    stand.run()
    results = [r.result for r in records(caplog_info, "call_sweep")]
    assert results == ["failed", "ok"]
    assert stand.dependencies == [False, True]
    assert stand.pauses[0] == 1.0


def _sleep_until(stand, *, stops_after):
    async def sleep(stop_event, seconds):
        stand.pauses.append(seconds)
        if len(stand.pauses) >= stops_after:
            stop_event.set()

    return sleep


def test_недоступная_база_это_пропущенный_такт_а_не_падение(monkeypatch, caplog_info):
    stand = Stand(monkeypatch, pool_fails=1)
    monkeypatch.setattr(worker, "_sleep", _sleep_until(stand, stops_after=2))
    stand.run()
    assert stand.pools_opened == 2
    assert stand.dependencies[0] is False
    assert records(caplog_info, "call_sweep")[-1].result == "ok"


def test_период_берётся_из_окружения(monkeypatch):
    monkeypatch.setenv("CALL_SWEEP_SECONDS", "2.5")
    assert worker.sweep_seconds_from_env() == 2.5
    monkeypatch.delenv("CALL_SWEEP_SECONDS")
    assert worker.sweep_seconds_from_env() == worker.DEFAULT_SWEEP_SECONDS
