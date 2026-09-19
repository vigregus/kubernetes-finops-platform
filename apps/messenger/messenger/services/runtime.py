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
from enum import Enum

import asyncpg

from messenger.adapters import centrifugo, keycloak, oidc, ratelimit
from messenger.repositories import postgres
from messenger.services.login import LoginSettings
from messenger.telemetry import metrics

log = logging.getLogger(__name__)

POSTGRES = "postgres"
# Отдельное имя для метрики зависимости, хотя база та же. Реплика —
# необязательная зависимость: её отсутствие не отказ, и сливать её
# состояние с писателем значило бы показывать «postgres down» на исправном
# сервисе ровно тогда, когда реплика не настроена.
POSTGRES_READ = "postgres-read"


class ReadMode(str, Enum):
    """Сколько свежести требует запрос.

    Писатель и реплика — свойство развёртывания, а не домена, и потому это
    живёт здесь, а не в `domain/`: доменное правило не должно меняться от
    того, сколько у нас реплик. Наружу, в API, это тоже не выходит: служба
    называет режим, соединение под него выдаёт `Runtime`.
    """

    # Из писателя: страница, на которой может оказаться только что
    # записанное, и всё, что касается восстановления после обрыва.
    STRONG = "strong"
    # Допустимо отставание: старые страницы листания, где задержка
    # реплики на секунды никого не касается.
    STALE_OK = "stale_ok"


# Обслуживает ли реплика страницы истории, то есть превращается ли
# `STALE_OK` в чтение с реплики. Сегодня — нет, и это не переключатель,
# которым пользуются: он стоит здесь затем, чтобы у будущей правки было
# ровно одно место вместо ветки в пути запроса. Условие включения и цена
# ошибки — в `Runtime._pool_for`.
_READER_SERVES_PAGES = False


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


def read_pool_settings_from_env() -> postgres.PoolSettings | None:
    """Настройки пула чтения — или `None`, если читать неоткуда.

    Отсутствие `DATABASE_READ_HOST` означает «реплики нет», и это не
    ошибка конфигурации, а сегодняшнее состояние стендов: второй `Pooler`
    не поднимается намеренно
    (`gitops/04-messenger/messenger-postgres/manifests/pooler.yaml`), потому
    что указывал бы на тот же под и создавал ложное ощущение разделения
    нагрузки. Поэтому умолчание — `None`, а не адрес писателя: с адресом
    писателя `STALE_OK` молча читал бы с него и никто бы не заметил, что
    развилка не работает.

    Ёмкость чтения меньше ёмкости записи сознательно. `max_size` — размер
    пода, а не базы, и сумма (10 + 5) обязана оставаться ниже
    `default_pool_size: "20"` PgBouncer, иначе очередь переезжает из
    приложения в пул и становится невидимой (предупреждение в
    `repositories/postgres.py`, `PoolSettings`).
    """
    host = os.getenv("DATABASE_READ_HOST", "")
    if not host:
        return None
    return postgres.PoolSettings(
        host=host,
        port=int(os.getenv("DATABASE_READ_PORT", os.getenv("DATABASE_PORT", "5432"))),
        database=os.getenv(
            "DATABASE_READ_NAME", os.getenv("DATABASE_NAME", "messenger")
        ),
        user=os.getenv("DATABASE_READ_USER", os.getenv("DATABASE_USER", "messenger")),
        password=os.getenv(
            "DATABASE_READ_PASSWORD", os.getenv("DATABASE_PASSWORD", "")
        ),
        min_size=int(os.getenv("DATABASE_READ_POOL_MIN", "2")),
        max_size=int(os.getenv("DATABASE_READ_POOL_MAX", "5")),
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


def admin_settings_from_env() -> keycloak.AdminSettings:
    """Доступ к административному API реалма от имени служебной записи.

    Секрет выдал сам Keycloak, сверка реалма положила его в Secret, под
    читает из окружения. В git его нет ни в каком виде.
    """
    return keycloak.AdminSettings(
        base_url=os.getenv("KEYCLOAK_URL", ""),
        realm=os.getenv("KEYCLOAK_REALM", "messenger"),
        client_id=os.getenv("OIDC_AUDIENCE", "messenger-api"),
        client_secret=os.getenv("OIDC_CLIENT_SECRET", ""),
    )


def limit_settings_from_env() -> ratelimit.LimitSettings:
    """Счётчики лимитов. БД 2 — роль security, отдельно от кеша приложения."""
    return ratelimit.LimitSettings(url=os.getenv("REDIS_SECURITY_URL", ""))


def centrifugo_settings_from_env() -> centrifugo.CentrifugoSettings:
    """Адрес и ключи Centrifugo. Оба ключа — из секрета, в git их нет.

    Ключ подписи connect-токена и ключ серверного API держит один Secret
    (`messenger-centrifugo`), но роли у них разные: первый подписывает
    токен, которым клиент входит в Centrifugo, второй разрешает нашему
    поду звать `/publish` и `/disconnect`. Перепутать их нельзя.
    """
    return centrifugo.CentrifugoSettings(
        api_url=os.getenv("CENTRIFUGO_API_URL", ""),
        api_key=os.getenv("CENTRIFUGO_HTTP_API_KEY", ""),
        token_hmac_secret_key=os.getenv("CENTRIFUGO_CLIENT_TOKEN_HMAC_SECRET_KEY", ""),
    )


def centrifugo_client_from_env() -> centrifugo.CentrifugoClient | None:
    """Клиент Centrifugo, либо `None`, если поднимать его не на что.

    Centrifugo — best-effort зависимость: отзыв сессии живёт в Postgres,
    HTTP уже отрезал отозванную, а разрыв соединений — ускорение доставки,
    а не условие отзыва. Нет ни URL, ни ключа подписи — значит, подключить
    и разорвать нечего, и под не должен ни падать, ни выносить это в
    готовность. `None` просит сервисы пропустить разрыв молча.
    """
    settings = centrifugo_settings_from_env()
    if (
        not settings.api_url
        or not settings.api_key
        or not settings.token_hmac_secret_key
    ):
        return None
    return centrifugo.CentrifugoClient(settings=settings)


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
    # Пул чтения необязателен: он появляется, когда появляется реплика,
    # и до тех пор оба режима идут в писателя.
    read_settings: postgres.PoolSettings | None = field(
        default_factory=read_pool_settings_from_env
    )
    read_pool: asyncpg.Pool | None = None
    keys: oidc.JwksCache | None = None
    login: LoginSettings | None = None
    admin: keycloak.AdminClient | None = None
    limiter: ratelimit.RateLimiter | None = None
    centrifugo: centrifugo.CentrifugoClient | None = None
    backchannel_audience: str = field(
        default_factory=lambda: os.getenv("OIDC_BACKCHANNEL_AUDIENCE", "messenger-web")
    )

    def __post_init__(self) -> None:
        if self.keys is None:
            self.keys = oidc.JwksCache(settings=self.oidc_settings)
        if self.login is None:
            self.login = login_settings_from_env(self.oidc_settings)
        if self.admin is None:
            self.admin = keycloak.AdminClient(settings=admin_settings_from_env())
        if self.limiter is None:
            self.limiter = ratelimit.RateLimiter(settings=limit_settings_from_env())
        if self.centrifugo is None:
            self.centrifugo = centrifugo_client_from_env()

    async def start(self) -> None:
        """Пытается открыть пул и прогреть ключи. Неудача — не повод
        не подниматься.

        Ключи читаются заранее, чтобы первый вошедший не ждал обращения
        к Keycloak. Но в готовность это не входит: выданные токены
        проверяются по уже прочитанным ключам, и снимать под с трафика
        из-за недоступного Keycloak значит устроить отказ там, где его
        ещё нет.

        Пул чтения открывается здесь же, хотя страниц пока не обслуживает:
        это единственное место, где он открывается вообще, — в пути запроса
        его нет (`_pool_for`). Так недоступная реплика стоит одной попытки
        при подъёме, а не задержки в каждом запросе.
        """
        await self.ensure_pool()
        await self.ensure_read_pool()
        if self.keys is not None and self.oidc_settings.jwks_url:
            await self.keys.key_for("прогрев")

    async def stop(self) -> None:
        """Закрывает пулы, дожидаясь возврата занятых соединений.

        Без этого выключение пода обрывает соединение посреди транзакции,
        и база узнаёт об этом только по таймауту — держа блокировки всё это
        время.

        Пул чтения закрывается первым и обязательно. Забытый `read_pool`
        держал бы соединения до убийства пода и обнулял бы смысл `close()`
        с ожиданием — то есть выключение начинало бы зависеть от второй
        базы, которой может и не быть.
        """
        if self.read_pool is not None:
            await self.read_pool.close()
            self.read_pool = None
            metrics.dependency_up(POSTGRES_READ, up=False)
        if self.pool is not None:
            await self.pool.close()
            self.pool = None
            metrics.dependency_up(POSTGRES, up=False)
        if self.limiter is not None:
            await self.limiter.close()

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

    async def ensure_read_pool(self) -> asyncpg.Pool | None:
        """Пул чтения, если он настроен и открылся. Иначе `None`.

        `None` здесь означает «читай с писателя», а не отказ: реплика —
        ускорение, а не условие работы. Поэтому недоступная реплика не
        бросает исключения и не выносится в готовность — она молча
        возвращает чтение писателю, и сервис продолжает отвечать.

        Вызывается сегодня только из `start()`, и это осознанно: в пути
        запроса реплики нет, пока она не умеет отвечать за границу
        страницы (`_pool_for`). То есть цикл переподключения у неё —
        только подъём процесса, а не каждая проба готовности.

        `TimeoutError` ловится здесь, а `ensure_pool` его не ловит, и это
        не расхождение. У писателя падение по таймауту означает, что
        работать не на чем, и исключение обязано дойти до обработчика.
        У реплики за тем же таймаутом стоит настроенный, но недоступный
        хост — тот самый случай, ради которого развилка и написана.
        """
        if self.read_pool is not None:
            return self.read_pool
        if self.read_settings is None:
            return None
        try:
            self.read_pool = await postgres.create_pool(
                self.read_settings,
                application_name=f"{self.application_name}-read",
            )
        except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
            log.warning(
                "пул чтения не открылся, чтение пойдёт с писателя",
                extra={
                    "event": "db_read_pool_unavailable",
                    "result": "failed",
                    "error_code": type(exc).__name__,
                    "dependency": POSTGRES_READ,
                },
            )
            self.read_pool = None
        else:
            metrics.dependency_up(POSTGRES_READ, up=True)
        return self.read_pool

    async def _pool_for(self, mode: ReadMode) -> asyncpg.Pool | None:
        """Пул под требуемую свежесть. Выбор ровно в одном месте.

        `STALE_OK` не означает «читать с реплики» — он означает, что
        отставание допустимо. Реплики нет, она не поднялась, или её чтение
        небезопасно — читаем с писателя, и результат от этого только свежее.

        Сегодня безопасным оно не бывает ни при каких условиях, потому что
        ответа на вопрос «догнала ли реплика границу, с которой читается эта
        страница» в сервисе нет. Пока его нет, `_READER_SERVES_PAGES` —
        `False`, и реплика страниц не обслуживает.

        Цена ошибки здесь — не задержка, а молчаливая дыра в истории.
        Писатель на номере 100, реплика на 90, страница `before_seq = 96`:
        реплика вернёт 90…86, и номеров 95…91 не увидит никто и никогда —
        следующий ответ снова выглядит непрерывным, а сравнить клиенту не
        с чем. То есть проверить это на стенде нечем, и заметить нельзя.
        Локально невоспроизводимо вовсе: `DATABASE_READ_HOST` не задан,
        обе ветки идут в один пул — то есть `HIST-001` зелёный именно
        поэтому, а не потому, что развилка проверена.

        Отсюда же и вторая половина решения: пока реплика не обслуживает
        страницы, её нет и в пути запроса. Иначе `ensure_read_pool`
        вызывалась бы на каждом `STALE_OK`, и недоступная реплика добавляла
        бы к запросу свой `connect_timeout`, собирая шторм подключений;
        а поднявшаяся однажды и умершая позже возвращала бы нерабочий пул
        (поле не `None` — повторной попытки не будет), и ошибка `acquire`
        не привела бы никуда, хотя комментарий обещает обратное:
        «реплика — ускорение, а не условие работы».

        Пул всё равно открывается — в `start()`, вместе с процессом.
        Не ради страниц, а чтобы шов был живым, а не описанным: он виден
        метрикой `postgres-read`, а его отказы — событием
        `db_read_pool_unavailable`.
        """
        if mode is ReadMode.STALE_OK and _READER_SERVES_PAGES:
            replica = await self.ensure_read_pool()
            if replica is not None:
                return replica
        return await self.ensure_pool()

    @asynccontextmanager
    async def connection(
        self, mode: ReadMode = ReadMode.STRONG
    ) -> AsyncIterator[asyncpg.Connection]:
        """Соединение на время блока. Транзакцией владеет вызывающий.

        Умолчание — писатель, и это не удобство, а безопасная сторона:
        забыть указать режим значит прочитать свежее, а не устаревшее.
        """
        pool = await self._pool_for(mode)
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

    # Реплика в пробу готовности не входит, и добавление её — правка одной
    # строки, которую здесь и хочется сделать. Делать её нельзя: реплики
    # может не быть вовсе, и под ушёл бы в цикл перезапуска при исправной
    # записи и исправном STRONG-чтении. Отставание реплики наблюдается
    # метрикой, а не снятием пода с трафика.
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
