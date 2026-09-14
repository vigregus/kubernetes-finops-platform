"""Жизненный цикл внешних зависимостей и ответ на вопрос «можно ли трафик».

Слой существует потому, что обработчику HTTP нельзя знать про репозитории:
иначе он однажды сходит в базу мимо транзакции. Пул открывается здесь,
а `api` получает готовый `Runtime` и не знает даже имени драйвера.

Главное решение этого модуля — **недоступная база при старте не роняет
процесс**. Падение в момент подъёма даёт CrashLoopBackOff: под перезапускается,
снова не видит базу, снова падает — и когда база возвращается, кластер держит
его в экспоненциальной паузе и не пускает ещё несколько минут. Поэтому
процесс поднимается, отвечает на пробу живости и остаётся не готов, пока
соединение не установится.
"""
from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import asyncpg

from messenger.adapters import keycloak, oidc
from messenger.repositories import postgres
from messenger.services.login import LoginSettings
from messenger.telemetry import metrics

log = logging.getLogger(__name__)

POSTGRES = "postgres"


def pool_settings_from_env() -> postgres.PoolSettings:
    """Настройки пула из окружения. Единственное место, читающее `os.environ`.

    Пароль приходит из секрета через переменную, а не из строки подключения
    целиком: DSN с паролем внутри попадает в журнал при первой же ошибке
    подключения, и вычистка по шаблону — последняя защита, а не первая.
    """
    return postgres.PoolSettings(
        host=os.getenv("DATABASE_HOST", "localhost"),
        port=int(os.getenv("DATABASE_PORT", "5432")),
        database=os.getenv("DATABASE_NAME", "messenger"),
        user=os.getenv("DATABASE_USER", "messenger"),
        password=os.getenv("DATABASE_PASSWORD", ""),
        min_size=int(os.getenv("DATABASE_POOL_MIN", "2")),
        max_size=int(os.getenv("DATABASE_POOL_MAX", "10")),
    )


def oidc_settings_from_env() -> oidc.OidcSettings:
    """Издатель, ключи и аудитория из окружения.

    Три значения, а не одно: издатель внешний, ключи внутренние,
    аудитория — имя ресурсного сервера. Подробности, почему адреса
    расходятся, — в `adapters/oidc.py`.
    """
    return oidc.OidcSettings(
        issuer=os.getenv("OIDC_ISSUER", ""),
        jwks_url=os.getenv("OIDC_JWKS_URL", ""),
        audience=os.getenv("OIDC_AUDIENCE", "messenger-api"),
    )


def login_settings_from_env(oidc_settings: oidc.OidcSettings) -> LoginSettings:
    """Настройки входа. Адрес обмена внутренний, как и адрес ключей.

    Клиент — браузерный и публичный: обмен делает сервер, но от имени
    того же клиента, которому выдан код. Секрета у него нет и быть
    не может — он живёт в странице.
    """
    return LoginSettings(
        tokens=keycloak.TokenSettings(
            token_url=os.getenv("OIDC_TOKEN_URL", ""),
            client_id=os.getenv("OIDC_CLIENT_ID", "messenger-web"),
        ),
        oidc=oidc_settings,
    )


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """Что ответила каждая зависимость.

    Словарь, а не флаг: `ready=false` без разбивки означает поход в журналы
    в момент, когда как раз некогда. В ответе пробы видно, кто именно лёг.
    """

    checks: dict[str, str] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        # Пустой список проверок готовностью не считается: это состояние
        # «проверять нечем», и выдавать его за успех нельзя.
        return bool(self.checks) and all(v == "ok" for v in self.checks.values())


@dataclass(slots=True)
class Runtime:
    """Владелец соединений процесса. Один экземпляр на приложение."""

    settings: postgres.PoolSettings
    oidc_settings: oidc.OidcSettings = field(default_factory=oidc_settings_from_env)
    application_name: str = "messenger-api"
    pool: asyncpg.Pool | None = None
    keys: oidc.JwksCache | None = None
    login: LoginSettings | None = None

    def __post_init__(self) -> None:
        if self.keys is None:
            self.keys = oidc.JwksCache(settings=self.oidc_settings)
        if self.login is None:
            self.login = login_settings_from_env(self.oidc_settings)

    async def start(self) -> None:
        """Пытается открыть пул и прогреть ключи. Неудача — не повод
        не подниматься.

        Ключи читаются заранее, чтобы первый вошедший не ждал обращения
        к Keycloak. Но в готовность это не входит: выданные токены
        проверяются по уже прочитанным ключам, и снимать под с трафика
        из-за недоступного Keycloak значит устроить отказ там, где его
        ещё нет.
        """
        await self.ensure_pool()
        if self.keys is not None and self.oidc_settings.jwks_url:
            await self.keys.key_for("прогрев")

    async def stop(self) -> None:
        """Закрывает пул, дожидаясь возврата занятых соединений.

        Без этого выключение пода обрывает соединение посреди транзакции,
        и база узнаёт об этом только по таймауту — держа блокировки всё это
        время.
        """
        if self.pool is not None:
            await self.pool.close()
            self.pool = None
            metrics.dependency_up(POSTGRES, up=False)

    async def ensure_pool(self) -> asyncpg.Pool | None:
        """Открывает пул, если его ещё нет. Повторная попытка — на каждой пробе.

        Отдельного цикла переподключения нет намеренно: проба готовности и так
        приходит каждые пять секунд, и второй таймер делал бы то же самое,
        но с собственными ошибками.
        """
        if self.pool is not None:
            return self.pool
        try:
            self.pool = await postgres.create_pool(
                self.settings, application_name=self.application_name
            )
        except (OSError, asyncpg.PostgresError) as exc:
            # Причина, а не текст: в сообщении драйвера бывает строка
            # подключения, и она не должна оказаться в журнале.
            log.warning(
                "пул не открылся",
                extra={
                    "event": "db_pool_unavailable",
                    "result": "failed",
                    "error_code": type(exc).__name__,
                    "dependency": POSTGRES,
                },
            )
            self.pool = None
        return self.pool

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[asyncpg.Connection]:
        """Соединение на время блока. Транзакцией владеет вызывающий."""
        pool = await self.ensure_pool()
        if pool is None:
            raise ConnectionError("Postgres недоступен")
        async with postgres.connection(pool) as conn:
            yield conn


async def check_readiness(runtime: Runtime) -> ReadinessReport:
    """Настоящий запрос к каждой зависимости, а не «клиент создан».

    Созданный клиент ничего не доказывает: пул считается открытым, пока
    первый запрос не упрётся в закрытый порт. Поэтому проверка — `SELECT 1`
    через PgBouncer, тем же путём, каким пойдёт рабочий запрос.
    """
    checks: dict[str, str] = {}

    pool = await runtime.ensure_pool()
    if pool is None:
        checks[POSTGRES] = "unavailable"
        metrics.dependency_up(POSTGRES, up=False)
        metrics.db_pool(in_use=0, idle=0, max_size=runtime.settings.max_size)
        return ReadinessReport(checks=checks)

    try:
        async with postgres.connection(pool) as conn:
            await postgres.ping(conn)
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        checks[POSTGRES] = "failed"
        metrics.dependency_up(POSTGRES, up=False)
        log.warning(
            "база не ответила на проверку",
            extra={
                "event": "dependency_check",
                "result": "failed",
                "error_code": type(exc).__name__,
                "dependency": POSTGRES,
            },
        )
    else:
        checks[POSTGRES] = "ok"
        metrics.dependency_up(POSTGRES, up=True)

    stats = postgres.stats(pool)
    metrics.db_pool(in_use=stats.in_use, idle=stats.idle, max_size=stats.max_size)
    return ReadinessReport(checks=checks)
