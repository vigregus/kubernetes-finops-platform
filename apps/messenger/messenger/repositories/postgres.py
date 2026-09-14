"""Пул соединений с Postgres. Всё, что знает про драйвер, — здесь.

Соединение идёт через PgBouncer (`messenger-db-pool`), а не в базу напрямую.
Это не деталь развёртывания, а условие корректности клиента: пул работает
в режиме `transaction`, то есть серверное соединение закрепляется за клиентом
только на время транзакции и между запросами достаётся другому.

Из этого следует то, из-за чего asyncpg с PgBouncer обычно и ломается:
подготовленное выражение живёт в серверном соединении, а клиент к тому
моменту уже на другом. Поэтому кеш подготовленных выражений выключен —
иначе первые запросы проходят, а под нагрузкой начинается
`prepared statement "__asyncpg_stmt_1__" does not exist`, и выглядит это
как случайный отказ базы.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import asyncpg


@dataclass(frozen=True, slots=True)
class PoolSettings:
    """Куда и как подключаться. Читает окружение вызывающий, не репозиторий.

    Слой, сам берущий конфигурацию из `os.environ`, невозможно поднять в тесте
    с другими значениями, не подменяя окружение процесса целиком.
    """

    host: str
    port: int
    database: str
    user: str
    password: str
    # Ёмкость пода, а не базы. Реплик несколько, и `max_size`, умноженный
    # на число реплик, обязан оставаться ниже `default_pool_size` PgBouncer,
    # иначе очередь переедет из приложения в пул и станет невидимой.
    min_size: int = 2
    max_size: int = 10
    # Ожидание свободного соединения. Без предела запрос копит их
    # бесконечно, и отказ базы превращается в отказ по памяти.
    acquire_timeout_seconds: float = 5.0
    # Ожидание установки соединения. Отдельно от предыдущего: недоступный
    # PgBouncer и занятый пул — разные отказы с разными действиями.
    connect_timeout_seconds: float = 5.0


@dataclass(frozen=True, slots=True)
class PoolStats:
    """Снимок состояния пула для метрик."""

    size: int
    idle: int
    max_size: int

    @property
    def in_use(self) -> int:
        return self.size - self.idle


async def create_pool(settings: PoolSettings, *, application_name: str) -> asyncpg.Pool:
    """Открывает пул. Падает, если база недоступна в момент старта.

    `min_size` соединений устанавливаются сразу: отложенная установка
    перенесла бы отказ подключения в первый же пользовательский запрос,
    то есть в момент, когда о нём узнает пользователь, а не проба.
    """
    return await asyncpg.create_pool(
        host=settings.host,
        port=settings.port,
        database=settings.database,
        user=settings.user,
        password=settings.password,
        min_size=settings.min_size,
        max_size=settings.max_size,
        timeout=settings.connect_timeout_seconds,
        command_timeout=settings.acquire_timeout_seconds,
        # Ноль — обязательное значение при PgBouncer в режиме transaction.
        statement_cache_size=0,
        # `application_name` виден в pg_stat_activity и в журнале медленных
        # запросов. Без него все соединения от всех сервисов выглядят
        # одинаково, и «кто держит блокировку» становится гаданием.
        server_settings={"application_name": application_name},
    )


@asynccontextmanager
async def connection(
    pool: asyncpg.Pool, *, timeout: float = 5.0
) -> AsyncIterator[asyncpg.Connection]:
    """Соединение из пула на время блока.

    Существует, чтобы сервисный слой не знал имени драйвера: вызывающий
    получает `conn`, которое обязан передать репозиторию, и владеет
    транзакцией сам.
    """
    async with pool.acquire(timeout=timeout) as conn:
        yield conn


def stats(pool: asyncpg.Pool) -> PoolStats:
    """Состояние пула. Снимается опросом метрик, не пишется по событию."""
    return PoolStats(size=pool.get_size(), idle=pool.get_idle_size(), max_size=pool.get_max_size())


async def ping(conn: asyncpg.Connection) -> None:
    """Проверка живости соединения.

    `SELECT 1`, а не `SELECT count(*)` по таблице: проба не должна зависеть
    от объёма данных и не должна брать блокировок. Ошибка не гасится —
    вызывающий обязан отличить «база отвечает» от «упало молча».
    """
    await conn.fetchval("SELECT 1")
