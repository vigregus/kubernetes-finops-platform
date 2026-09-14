"""Метрики внешних зависимостей. Общие для всех сервисов, не только для API.

Потребитель Kafka, отправитель outbox и API одинаково зависят от базы, и если
каждый заведёт своё имя метрики, один дашборд на них не построить. Поэтому
имена живут здесь, а не рядом с вызовом.
"""
from __future__ import annotations

import os

from prometheus_client import Counter, Gauge

# Имя берётся из окружения один раз. Передавать его аргументом в каждый вызов
# значит однажды передать не то — и ряд разъедется с остальными.
SERVICE = os.getenv("SERVICE_NAME", "unknown")

# Доступна ли зависимость по последней проверке. 0 или 1, без градаций:
# «наполовину доступна» — это отдельные метрики задержки и ошибок, а
# оповещение строится на простом признаке.
DEPENDENCY_UP = Gauge(
    "messenger_dependency_up",
    "Зависимость ответила на последнюю проверку готовности",
    ["service", "dependency"],
)

# Состояние пула соединений. Очередь на соединение — первое, что упирается
# при росте нагрузки, и без этой метрики она выглядит как «база стала
# медленной», хотя база при этом не при делах.
DB_POOL_CONNECTIONS = Gauge(
    "messenger_db_pool_connections",
    "Соединения в пуле по состоянию",
    ["service", "state"],
)

DB_POOL_MAX = Gauge(
    "messenger_db_pool_max_connections",
    "Верхний предел пула в этом процессе",
    ["service"],
)


def dependency_up(dependency: str, *, up: bool) -> None:
    DEPENDENCY_UP.labels(service=SERVICE, dependency=dependency).set(1 if up else 0)


def db_pool(*, in_use: int, idle: int, max_size: int) -> None:
    DB_POOL_CONNECTIONS.labels(service=SERVICE, state="in_use").set(in_use)
    DB_POOL_CONNECTIONS.labels(service=SERVICE, state="idle").set(idle)
    DB_POOL_MAX.labels(service=SERVICE).set(max_size)


# Отказы входа. Метка — внутренняя причина, а не код ответа: наружу уходит
# один и тот же 401, и по нему не отличить ротацию ключей от сломанного
# обновления токена у клиентов.
TOKEN_REJECTED = Counter(
    "messenger_token_rejected_total",
    "Токены, не прошедшие проверку",
    ["service", "error_class"],
)

# Регистрации. Считаются там же, где заводится запись, а не в обработчике
# входа: профиль заводится и при первом обращении потребителя, и из
# административной команды.
USERS_CREATED = Counter(
    "messenger_users_created_total",
    "Заведённые учётные записи",
    ["service"],
)


def token_rejected(error_class: str) -> None:
    TOKEN_REJECTED.labels(service=SERVICE, error_class=error_class).inc()


def user_created() -> None:
    USERS_CREATED.labels(service=SERVICE).inc()


# Сколько ключей подписи сейчас в памяти. Ноль означает, что проверить
# токен нечем, и узнать об этом надо до того, как это заметит первый
# вошедший: прогрев при старте намеренно молчалив - недоступный Keycloak
# не повод не подниматься, - и без этой метрики он остаётся невидимым.
OIDC_KEYS = Gauge(
    "messenger_oidc_signing_keys",
    "Ключи подписи реалма, доступные процессу",
    ["service"],
)


def oidc_keys(count: int) -> None:
    OIDC_KEYS.labels(service=SERVICE).set(count)
