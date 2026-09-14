"""Готовность: что отвечает проба, когда база есть и когда её нет.

Сеть здесь не задействована. Пул подменяется поддельным, потому что
проверяется не asyncpg, а решение: недоступная база обязана снимать под
с трафика, не убивая процесс.

Асинхронные вызовы запускаются через `asyncio.run` вместо плагина: одна
зависимость меньше, а выигрыш плагина на десятке тестов нулевой.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from messenger.api.main import app
from messenger.repositories.postgres import PoolSettings
from messenger.services import runtime as runtime_service


class FakeConn:
    """Соединение, которое либо отвечает, либо падает так же, как настоящее."""

    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails

    async def fetchval(self, query: str, *args):
        if self.fails:
            # Именно OSError: отказ PgBouncer выглядит для клиента как
            # закрытый порт, а не как ошибка SQL.
            raise OSError("соединение закрыто")
        return 1


class FakePool:
    def __init__(self, *, fails: bool = False, size: int = 3, idle: int = 2) -> None:
        self.fails = fails
        self._size = size
        self._idle = idle
        self.closed = False

    @asynccontextmanager
    async def _ctx(self):
        yield FakeConn(fails=self.fails)

    def acquire(self, timeout: float | None = None):
        return self._ctx()

    def get_size(self) -> int:
        return self._size

    def get_idle_size(self) -> int:
        return self._idle

    def get_max_size(self) -> int:
        return 10

    async def close(self) -> None:
        self.closed = True


def make_runtime(pool) -> runtime_service.Runtime:
    """Runtime с готовым пулом. `ensure_pool` тогда ничего не открывает."""
    settings = PoolSettings(
        host="нет-такого", port=5432, database="messenger", user="messenger", password=""
    )
    runtime = runtime_service.Runtime(settings=settings)
    runtime.pool = pool
    return runtime


class UnreachableRuntime(runtime_service.Runtime):
    """База не поднимается. Открытие пула молча не удаётся — как в жизни."""

    async def ensure_pool(self):
        return None


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def with_runtime():
    """Подменяет владельца соединений на время проверки и возвращает обратно."""
    original = app.state.runtime

    def _set(runtime):
        app.state.runtime = runtime
        return runtime

    yield _set
    app.state.runtime = original


def test_пустой_список_проверок_не_считается_готовностью():
    """Состояние «проверять нечем» — не успех.

    Первая редакция возвращала `all([])`, то есть `true` при полном
    отсутствии проверок: под получал трафик, ничего не проверив.
    """
    assert runtime_service.ReadinessReport().ready is False


def test_база_отвечает_значит_готов():
    report = asyncio.run(runtime_service.check_readiness(make_runtime(FakePool())))
    assert report.checks == {"postgres": "ok"}
    assert report.ready is True


def test_база_не_отвечает_значит_не_готов():
    """Тест отказа: соединение есть, запрос падает."""
    report = asyncio.run(runtime_service.check_readiness(make_runtime(FakePool(fails=True))))
    assert report.checks == {"postgres": "failed"}
    assert report.ready is False


def test_пул_не_открылся_названо_отдельно():
    """«Не открылся» и «не ответил» — разные отказы с разными действиями.

    Первое — недоступный PgBouncer или неверный пароль, второе — живое
    соединение и упавший запрос.
    """
    runtime = UnreachableRuntime(settings=PoolSettings("h", 5432, "d", "u", ""))
    report = asyncio.run(runtime_service.check_readiness(runtime))
    assert report.checks == {"postgres": "unavailable"}


def test_проба_готовности_отдаёт_503_и_называет_виновного(client, with_runtime):
    with_runtime(UnreachableRuntime(settings=PoolSettings("h", 5432, "d", "u", "")))
    r = client.get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["ready"] is False
    # Разбивка в ответе: `ready: false` без неё означает поход в журналы
    # в момент, когда как раз некогда.
    assert body["checks"]["postgres"] == "unavailable"


def test_недоступная_база_не_трогает_пробу_живости(client, with_runtime):
    """Главное в этом тесте.

    Проба живости, падающая из-за базы, устраивает каскад: kubelet
    перезапускает исправные поды, те снова не видят базу и падают опять —
    пока база восстанавливается, сервис уничтожает сам себя.
    """
    with_runtime(UnreachableRuntime(settings=PoolSettings("h", 5432, "d", "u", "")))
    assert client.get("/readyz").status_code == 503
    assert client.get("/livez").status_code == 200


def test_готовность_видна_в_метриках(client, with_runtime):
    with_runtime(make_runtime(FakePool(size=3, idle=2)))
    assert client.get("/readyz").status_code == 200
    body = client.get("/metrics").text
    # Метки в выводе идут по алфавиту, а не в порядке объявления.
    assert 'messenger_dependency_up{dependency="postgres"' in body
    assert 'messenger_db_pool_connections{service="' in body
    assert 'state="in_use"} 1.0' in body
    assert 'state="idle"} 2.0' in body


def test_остановка_закрывает_пул():
    """Оборванное на выключении соединение база заметит только по таймауту —
    держа блокировки всё это время."""
    pool = FakePool()
    runtime = make_runtime(pool)
    asyncio.run(runtime.stop())
    assert pool.closed is True
    assert runtime.pool is None
