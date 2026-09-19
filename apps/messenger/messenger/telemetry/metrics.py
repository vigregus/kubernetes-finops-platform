"""Метрики внешних зависимостей. Общие для всех сервисов, не только для API.

Потребитель Kafka, отправитель outbox и API одинаково зависят от базы, и если
каждый заведёт своё имя метрики, один дашборд на них не построить. Поэтому
имена живут здесь, а не рядом с вызовом.
"""
from __future__ import annotations

import os

from prometheus_client import Counter, Gauge, Histogram

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
#
# Метка называется `error_code`, а не `error_class`: под тем же значением
# это поле лежит в логах и на спанах (telemetry/logging.py, tracing.py) -
# разное имя одного и того же понятия в разных сигналах значит, что
# значение из легенды графика нельзя скопировать прямо в поиск по
# VictoriaLogs, приходится помнить о переименовании.
TOKEN_REJECTED = Counter(
    "messenger_token_rejected_total",
    "Токены, не прошедшие проверку",
    ["service", "error_code"],
)

# Регистрации. Считаются там же, где заводится запись, а не в обработчике
# входа: профиль заводится и при первом обращении потребителя, и из
# административной команды.
USERS_CREATED = Counter(
    "messenger_users_created_total",
    "Заведённые учётные записи",
    ["service"],
)


def token_rejected(error_code: str) -> None:
    TOKEN_REJECTED.labels(service=SERVICE, error_code=error_code).inc()


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


# Отозванные сессии по причине. «Вышел сам» и «отключён администратором»
# разбираются по-разному, и в одном ряду они бесполезны.
SESSIONS_REVOKED = Counter(
    "messenger_sessions_revoked_total",
    "Отозванные сессии",
    ["service", "reason"],
)


def sessions_revoked(reason: str, count: int = 1) -> None:
    if count:
        SESSIONS_REVOKED.labels(service=SERVICE, reason=reason).inc(count)


# Входы и отказы входа. Операция разделена: первый вход и обновление
# токена ломаются по разным причинам, и в одном ряду всплеск одного
# прячет провал другого.
LOGINS = Counter(
    "messenger_logins_total",
    "Успешные обмены у Keycloak",
    ["service", "operation"],
)

LOGIN_FAILURES = Counter(
    "messenger_login_failures_total",
    "Неудачные обмены у Keycloak",
    ["service", "operation", "error_code"],
)


def login_succeeded(operation: str) -> None:
    LOGINS.labels(service=SERVICE, operation=operation).inc()


def login_failed(operation: str, error_code: str) -> None:
    LOGIN_FAILURES.labels(service=SERVICE, operation=operation, error_code=error_code).inc()


# Письма о подтверждении адреса по исходу. Всплеск `limited` означает либо
# сломанную доставку почты — люди жмут «отправить ещё раз», — либо попытку
# рассылки по чужому адресу, и различить их можно только по тому, один это
# профиль или многие.
VERIFICATION_EMAIL = Counter(
    "messenger_verification_email_total",
    "Повторные отправки письма о подтверждении",
    ["service", "outcome"],
)


def verification_email(outcome: str) -> None:
    VERIFICATION_EMAIL.labels(service=SERVICE, outcome=outcome).inc()


# Разрыв realtime-соединений при отзыве. Считается отдельно от событий:
# «соединение не порвалось» и «сигнал не дошёл» — разные неисправности,
# и прятать одну за другой значит ловить отказ Centrifugo по одной метрике,
# которой всё равно, что именно сломалось.
REALTIME_DISCONNECTS = Counter(
    "messenger_realtime_disconnects_total",
    "Разорванные realtime-соединения по исходу",
    ["service", "outcome"],
)

REALTIME_EVENTS = Counter(
    "messenger_realtime_events_total",
    "Опубликованные realtime-события по исходу",
    ["service", "outcome"],
)


def realtime_disconnected(outcome: str, count: int = 1) -> None:
    if count:
        REALTIME_DISCONNECTS.labels(service=SERVICE, outcome=outcome).inc(count)


def realtime_published(outcome: str, count: int = 1) -> None:
    if count:
        REALTIME_EVENTS.labels(service=SERVICE, outcome=outcome).inc(count)


# --- отправитель outbox -----------------------------------------------------

# Исходы публикации по типу события. Тип в метке, а не идентификатор:
# идентификатор события убил бы хранилище кардинальностью.
OUTBOX_EVENTS = Counter(
    "messenger_outbox_events_total",
    "События outbox по исходу",
    ["service", "outcome", "event_type"],
)

OUTBOX_PUBLISHED = Counter(
    "messenger_outbox_published_total",
    "Записи, отмеченные опубликованными",
    ["service"],
)

# Длина очереди и возраст самой старой записи. Оповещение строится
# на возрасте: тысяча событий возрастом в секунду - норма, одна запись
# возрастом в час - отказ доставки.
OUTBOX_PENDING = Gauge(
    "messenger_outbox_pending",
    "Неопубликованные записи outbox",
    ["service"],
)

OUTBOX_OLDEST_AGE = Gauge(
    "messenger_outbox_oldest_age_seconds",
    "Возраст самой старой неопубликованной записи",
    ["service"],
)


def outbox_event(outcome: str, event_type: str) -> None:
    OUTBOX_EVENTS.labels(service=SERVICE, outcome=outcome, event_type=event_type).inc()


def outbox_published(count: int) -> None:
    if count:
        OUTBOX_PUBLISHED.labels(service=SERVICE).inc(count)


def outbox_queue(*, pending: int, oldest_age_seconds: float) -> None:
    OUTBOX_PENDING.labels(service=SERVICE).set(pending)
    OUTBOX_OLDEST_AGE.labels(service=SERVICE).set(oldest_age_seconds)


# Исходы доставки в реальном времени. `duplicate` здесь не ошибка,
# а доказательство того, что дедупликация работает: транспорт выбран
# at-least-once, и повторы обязаны появляться.
REALTIME_DELIVERY = Counter(
    "messenger_realtime_delivery_total",
    "События, обработанные потребителем realtime",
    ["service", "outcome"],
)


def realtime_delivery(outcome: str) -> None:
    REALTIME_DELIVERY.labels(service=SERVICE, outcome=outcome).inc()


# --- сквозной путь сообщения: T_commit / T_outbox / T_kafka / T_consumer /
# T_realtime / T_delivery (docs/messenger/06-observability.md, часть 1) ------
#
# Без этих гистограмм сквозной путь нельзя разложить на стадии: было видно
# только суммарное «p99 доставки 8с» без ответа, где именно эти секунды
# потеряны.

# Коммит сообщения. Числитель и знаменатель SLO «Запись сообщения»:
# результат — success/failed, а не проценты, сам процент считается запросом
# к этому ряду при разборе бюджета ошибок.
MESSAGE_SEND_OPERATIONS = Counter(
    "messenger_message_send_operations_total",
    "Исходы приёма сообщения (коммит в Postgres)",
    ["service", "result"],
)
MESSAGE_COMMIT_DURATION = Histogram(
    "messenger_message_commit_duration_seconds",
    "T_commit: длительность транзакции, вставившей сообщение и обе записи outbox",
    ["service"],
)


def message_commit(duration_seconds: float, *, result: str) -> None:
    MESSAGE_SEND_OPERATIONS.labels(service=SERVICE, result=result).inc()
    MESSAGE_COMMIT_DURATION.labels(service=SERVICE).observe(duration_seconds)


# T_outbox: от вставки записи (уже случилась, коммит выше) до её аренды
# отправителем. Возраст самой старой записи (`outbox_oldest_age_seconds`)
# уже есть и ловит затор; эта гистограмма показывает нормальное время
# аренды одной пачки, а не аварийное значение одной записи.
OUTBOX_CLAIM_DURATION = Histogram(
    "messenger_outbox_claim_duration_seconds",
    "Длительность аренды пачки outbox",
    ["service"],
)

# T_kafka: от аренды записи до подтверждения публикации брокером.
OUTBOX_KAFKA_PUBLISH_DURATION = Histogram(
    "messenger_outbox_kafka_publish_duration_seconds",
    "T_kafka: длительность публикации одной записи в Kafka",
    ["service"],
)


def outbox_claim_duration(seconds: float) -> None:
    OUTBOX_CLAIM_DURATION.labels(service=SERVICE).observe(seconds)


def outbox_kafka_publish_duration(seconds: float) -> None:
    OUTBOX_KAFKA_PUBLISH_DURATION.labels(service=SERVICE).observe(seconds)


# T_consumer: от получения пачки из Kafka до фиксации смещения. Метка
# `consumer` — потому что каталог метрик документа предполагает несколько
# потребителей (`realtime`, позже `notifications`, `unread`), а не один.
CONSUMER_PROCESSING_DURATION = Histogram(
    "messenger_consumer_processing_duration_seconds",
    "T_consumer: длительность обработки пачки потребителем",
    ["service", "consumer"],
)


def consumer_processing_duration(seconds: float, *, consumer: str) -> None:
    CONSUMER_PROCESSING_DURATION.labels(service=SERVICE, consumer=consumer).observe(
        seconds
    )


# T_realtime: от сборки пары факт+содержимое до подтверждённой публикации
# в Centrifugo.
REALTIME_PUBLISH_DURATION = Histogram(
    "messenger_realtime_publish_duration_seconds",
    "T_realtime: длительность публикации события в Centrifugo",
    ["service"],
)


def realtime_publish_duration(seconds: float) -> None:
    REALTIME_PUBLISH_DURATION.labels(service=SERVICE).observe(seconds)


# T_delivery, приближённо: от `occurred_at` (момент коммита, записанный
# в тело события) до публикации в Centrifugo. Это не то же самое, что t7
# в документе — подтверждение браузера Б, — потому что телеметрии браузера
# ещё нет (G3). Приближение честно называет себя приближением через имя
# метрики и здесь же, а не выдаёт себя за полный SLI.
MESSAGE_DELIVERY_DURATION = Histogram(
    "messenger_message_delivery_duration_seconds",
    "T_delivery (приближение до готовности телеметрии браузера): "
    "occurred_at сообщения -> подтверждённая публикация в Centrifugo",
    ["service"],
)


def message_delivery_duration(seconds: float) -> None:
    MESSAGE_DELIVERY_DURATION.labels(service=SERVICE).observe(seconds)
