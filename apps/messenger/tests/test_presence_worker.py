"""Уборщик присутствия: что он публикует за такт, что делает при отказе.

Ни базы, ни прометеевского реестра: подменены все внешние края цикла —
пул, такт, метрики, пауза. Проверяются решения уборщика, а не SQL
(`tests/test_presence.py`) и не правило окна (`domain/presence.py`).

Смысл проверок — в том, что отказы цикла различимы между собой. Уборщик
не влияет на корректность (`workers/presence_sweeper.py`), поэтому
единственное, чем он вообще может быть полезен, — это различимость:
«такта не было», «такт был и упал» и «такт прошёл, убирать было нечего»
не должны выглядеть одинаково.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest

from messenger.domain import presence as domain
from messenger.services import presence as service
from messenger.workers import presence_sweeper as worker

СОБЫТИЕ = "presence_sweep"


class Pool:
    """Пул, о котором известно одно: его можно закрыть."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class Stand:
    """Подмена всех внешних краёв цикла и журнал того, что уборщик сказал.

    Пауза здесь не ждёт, а считается: период и есть предмет проверки,
    а не то, сколько он длится в секундах реального времени. Остановка
    ставится по числу пауз — то есть после стольких тактов, сколько
    попросил вызывающий.
    """

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        pauses: int = 1,
        sweep_fails: frozenset[int] = frozenset(),
        pool_fails: int = 0,
        result: service.SweepResult | None = None,
    ) -> None:
        self.pool = Pool()
        self.result = result or service.SweepResult(
            deleted=0, online_users=0, registered_users=0
        )
        self.cycles = 0
        self.sweeps = 0
        self.pools_opened = 0
        self.pauses: list[float] = []
        self.dependencies: list[bool] = []
        self.online: list[tuple[int, float]] = []
        self._stop_at_pause = pauses
        self._sweep_fails = sweep_fails
        self._pool_fails = pool_fails

        async def create_pool(settings, *, application_name):
            self.pools_opened += 1
            if self.pools_opened <= self._pool_fails:
                raise OSError("база недоступна")
            return self.pool

        @contextlib.asynccontextmanager
        async def connection(pool):
            yield None

        async def sweep(conn):
            self.sweeps += 1
            if self.sweeps in self._sweep_fails:
                raise RuntimeError("такт не удался")
            return self.result

        async def sleep(stop_event, seconds):
            self.pauses.append(seconds)
            self.cycles += 1
            if len(self.pauses) >= self._stop_at_pause:
                stop_event.set()

        monkeypatch.setattr(worker.postgres, "create_pool", create_pool)
        monkeypatch.setattr(worker.postgres, "connection", connection)
        monkeypatch.setattr(worker.presence, "sweep", sweep)
        monkeypatch.setattr(worker, "_sleep", sleep)
        monkeypatch.setattr(
            worker.metrics, "dependency_up",
            lambda dependency, *, up: self.dependencies.append(up),
        )
        monkeypatch.setattr(
            worker.metrics, "presence_online",
            lambda *, online, share: self.online.append((online, share)),
        )

    def запустить(self) -> None:
        async def _main() -> None:
            await worker.run(asyncio.Event())

        asyncio.run(_main())


def записи(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event", None) == event]


@pytest.fixture
def caplog_info(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO, logger=worker.__name__)
    return caplog


def test_такт_снимает_метрики_и_пишет_журнал(monkeypatch, caplog_info):
    """За такт наружу уходит и число, и доля — из одного счёта.

    Доля без знаменателя необъяснима, но здесь важнее другое: обе метрики
    снимаются из **одного** результата такта, поэтому доля не может быть
    собрана из двух состояний системы.
    """
    stand = Stand(monkeypatch, result=service.SweepResult(
        deleted=2, online_users=3, registered_users=12
    ))

    stand.запустить()

    assert stand.online == [(3, 0.25)]
    assert stand.dependencies == [True]
    запись = записи(caplog_info, СОБЫТИЕ)[-1]
    assert запись.result == "ok"
    # Число удалённых — в записи, а не вместо неё: «уборка не нашла ничего»
    # и «уборка не работает» различимы только по этому полю.
    assert (запись.deleted, запись.online_users) == (2, 3)


def test_период_берётся_из_окружения_с_умолчанием(monkeypatch):
    monkeypatch.delenv("PRESENCE_SWEEP_SECONDS", raising=False)
    assert worker.sweep_seconds_from_env() == worker.DEFAULT_SWEEP_SECONDS

    monkeypatch.setenv("PRESENCE_SWEEP_SECONDS", "5")
    assert worker.sweep_seconds_from_env() == 5.0


def test_период_чаще_окна_живости():
    """Тождество между двумя числами, живущими в разных модулях.

    Период — то, что ограничивает размер реестра: протухшая строка живёт
    в нём до ближайшего такта. Период вровень с окном означал бы, что
    строки лежат в реестре вдвое дольше, чем протухли, а период больше
    окна — что реестр растёт быстрее, чем убирается.

    Ронится эта проверка увеличением периода — то есть правкой, которая
    в живом прогоне выглядит не как неверная константа, а как медленно
    растущая таблица.
    """
    assert worker.DEFAULT_SWEEP_SECONDS < domain.ONLINE_WINDOW_SECONDS


def test_отказ_такта_не_роняет_цикл(monkeypatch, caplog_info):
    """Уборщик обязан пережить любой отказ: следующий такт лечит его сам.

    Падение же означало бы CrashLoopBackOff у нагрузки, чья единственная
    работа — подождать и попробовать снова.
    """
    stand = Stand(monkeypatch, pauses=2, sweep_fails=frozenset({1}))

    stand.запустить()

    assert stand.sweeps == 2, "цикл не пережил отказа такта"
    assert stand.dependencies == [False, True]
    запись = записи(caplog_info, СОБЫТИЕ)[0]
    assert запись.result == "failed"
    assert запись.error_code == "RuntimeError"
    # Ошибка — пропущенный такт, а не повод ждать полный период: следующий
    # идёт раньше, поэтому паузы разные.
    assert stand.pauses == [1.0, worker.DEFAULT_SWEEP_SECONDS]


def test_отказ_не_выдаёт_прошлые_метрики_присутствия(monkeypatch):
    """Упавший такт метрик присутствия не снимает вовсе.

    Снять их «последним удачным значением» значило бы показать долю,
    которой в этот момент никто не считал: ряд остаётся свежим и верным
    по форме там, где счёт не состоялся.
    """
    stand = Stand(monkeypatch, pauses=2, sweep_fails=frozenset({1}))

    stand.запустить()

    assert stand.online == [(0, 0.0)], "метрики сняты и за упавший такт"


def test_недоступный_пул_это_пропущенный_такт(monkeypatch, caplog_info):
    """Пул поднимается лениво и переподключается сам.

    Отсутствие базы в момент старта — обычное дело при выкатке, и уходить
    из-за него в CrashLoopBackOff значит ждать её перезапуском пода, а не
    следующей попыткой.
    """
    stand = Stand(monkeypatch, pauses=2, pool_fails=1)

    stand.запустить()

    assert stand.pools_opened == 2, "пул не переподключился"
    assert stand.sweeps == 1, "такт прошёл без пула"
    assert stand.pauses[0] == 2.0
    assert stand.dependencies == [False, True]
    запись = записи(caplog_info, "db_pool_unavailable")[0]
    assert запись.result == "failed"


def test_остановка_закрывает_пул(monkeypatch, caplog_info):
    """Пауза прерывается сигналом, а не досчитывается до конца.

    Обычный `sleep` задержал бы выключение пода на всю свою длину, и
    kubelet добивал бы процесс по истечении срока — посреди такта.
    """
    stand = Stand(monkeypatch)

    stand.запустить()

    assert stand.pool.closed
    assert записи(caplog_info, "service_start")[0].result == "success"
    assert записи(caplog_info, "service_stop")[0].result == "success"
