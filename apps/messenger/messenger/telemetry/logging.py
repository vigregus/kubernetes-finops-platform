"""Структурированные журналы: обязательный конверт и вычистка секретов.

Две вещи, которые нельзя оставлять на дисциплину пишущего.

Первая — конверт. Если у каждого сервиса свой набор полей, связать их в одно
расследование нечем: поиск по `trace_id` находит два сервиса из пяти, а
остальные писали `traceId` или не писали вовсе.

Вторая — содержимое. Запрет «не логировать текст сообщения» выполняется ровно
до первого `log.info("payload=%s", body)`, написанного в три часа ночи при
отладке. Поэтому вычистка живёт в самом обработчике, а не в договорённости.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
import time
from typing import Any

# Контекст записи: то, что относится к обращению целиком, а не к одной
# строке. Класть это аргументом в каждый вызов журнала невозможно -
# сервисный слой не знает про HTTP, - а без этого связать строки одного
# запроса нечем.
REQUEST_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)
TRACE_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "trace_id", default=None
)
SPAN_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "span_id", default=None
)
# OWASP Logging Cheat Sheet называет source IP обязательным полем для
# security-событий ("Where"), рядом с "Who" (user_id) и "When"
# (timestamp) - тем же контуром, что и trace_id/span_id/request_id:
# сервисный слой не знает про HTTP, значит заполняет не он, а обработчик
# журнала из контекста задачи.
CLIENT_IP: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "client_ip", default=None
)

# Потоки журналов из контракта телеметрии. У каждого свой срок хранения,
# поэтому запись без потока хранить правильно нельзя: один срок на всё -
# это либо дорогой отладочный мусор, либо потерянный аудит.
#
# Поле называется `log_stream`, а не `stream`, и это не вкусовщина.
# `stream` в Kubernetes уже занят: сборщик журналов ставит такую метку
# каждой строке со значением `stdout` или `stderr`. Два разных смысла
# под одним именем хранилище сводит в одно поле, и чьё-то значение
# теряется. Измерено на живом хранилище до переименования: из 168 617
# записей `http_request` за сутки у 226 поток приложения был затёрт
# меткой контейнера - то есть запись о доступе молча оказывалась
# неотличимой от обычного вывода.
STREAM_FIELD = "log_stream"

STREAM_APPLICATION = "application"
STREAM_ACCESS = "access"
STREAM_AUDIT = "audit"
STREAM_SECURITY = "security"
STREAM_DEPLOYMENT = "deployment"
STREAM_SYNTHETIC = "synthetic"

# Журналы библиотек. Они пишут на INFO то, что интересно им, а не нам:
# httpx печатает строку на каждый исходящий запрос, aiokafka - на каждое
# переподключение. В общем потоке это шум, поэтому они говорят тише.
NOISY_LIBRARIES = ("httpx", "httpcore", "aiokafka", "kafka", "asyncio", "urllib3")

# Библиотеки, которые заводят собственные обработчики и потому пишут мимо
# конверта. uvicorn настраивает журналы в `Config.__init__`, то есть до
# импорта приложения, - значит, снять его обработчики можно здесь, после.
#
# Без этого восемь строк на каждый жизненный цикл пода («Started server
# process», «Application startup complete», «Shutting down») уходят
# в хранилище обычным текстом, без уровня и без события: измерено
# в кластере - ровно восемь на каждый перезапуск api.
ADOPTED_LIBRARIES = ("uvicorn", "uvicorn.error", "uvicorn.access")

# Событие для записи, которую сделала библиотека. Своего имени из каталога
# у неё нет и быть не может, а подставлять вместо события имя журнала -
# значит смешать два пространства имён в одном поле: рядом с
# `login_rejected` окажется `aiokafka.consumer.group_coordinator`, и
# запрос `event:...` перестанет означать «наше событие». Имя журнала
# при этом не теряется - оно уходит в поле `logger`.
EVENT_LIBRARY = "library"

# Поля, которые обязаны быть в каждой записи. Отсутствующие заполняются
# из окружения при настройке, а не проставляются вызывающим кодом:
# забыть про service в одном модуле из сорока — вопрос времени.
ENVELOPE = (
    "timestamp", "level", "service", "environment", "version",
    "event", "result", "error_code",
    "trace_id", "span_id", "request_id", "client_ip",
    "message_id", "event_id",
    STREAM_FIELD, "logger",
)

# Ключи, значения которых не попадают в журнал ни при каком уровне
# подробности. Подписанная ссылка здесь не случайно: она даёт доступ
# к объекту тому, кто её прочитал, то есть журнал превращается
# в средство доступа к чужим вложениям.
FORBIDDEN_KEYS = frozenset({
    "text", "body", "payload", "content", "message_text",
    "password", "passwd", "secret", "token", "access_token", "refresh_token",
    "authorization", "cookie", "set-cookie", "api_key", "apikey",
    "dsn", "database_url", "connection_string",
    "presigned_url", "upload_url", "download_url", "signed_url",
})

REDACTED = "[вычищено]"

# Ключи, которые выбрасываются целиком. `color_message` кладёт uvicorn:
# это та же строка, но с кодами цвета терминала. В хранилище она лежит
# как `Started server process [\u001b[36m%d\u001b[0m]` - данные, которых
# никто не искал, и второй экземпляр текста, который уже есть в `_msg`.
DROPPED_KEYS = frozenset({"color_message"})

# Значения, которые сами по себе выглядят как секрет, в каком бы поле
# ни оказались. Ключ может называться как угодно — `ctx`, `extra`, `note`.
_PATTERNS = (
    # JWT: три сегмента base64url через точку
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    # Предподписанная ссылка S3: узнаётся по параметру подписи
    re.compile(r"https?://\S*[?&]X-Amz-Signature=\S+", re.IGNORECASE),
    # Строка подключения с паролем
    re.compile(r"\b[a-z0-9+.-]+://[^\s:/@]+:[^\s@]+@\S+", re.IGNORECASE),
)


def scrub(value: Any, key: str | None = None) -> Any:
    """Рекурсивно вычищает запрещённое. Возвращает копию, вход не меняет."""
    if key is not None and key.lower() in FORBIDDEN_KEYS:
        return REDACTED
    if isinstance(value, dict):
        return {k: scrub(v, k) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        for pattern in _PATTERNS:
            value = pattern.sub(REDACTED, value)
    return value


class JsonFormatter(logging.Formatter):
    """Одна строка JSON на запись. Конверт заполнен всегда."""

    def __init__(self, service: str, environment: str, version: str) -> None:
        super().__init__()
        self._base = {
            "service": service,
            "environment": environment,
            "version": version,
        }

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)
            ) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            **self._base,
            # `event` — машинное имя происшествия, а не текст. По нему
            # строятся запросы; свободный текст уходит в `_msg`.
            "event": getattr(record, "event", EVENT_LIBRARY),
            # Кто написал строку. Нужен ровно тогда, когда событие -
            # `library`: без него непонятно, чья это жалоба.
            "logger": record.name,
            "_msg": record.getMessage(),
        }

        # Поток обязателен: по нему различаются сроки хранения.
        # Умолчание - `application`, потому что запись, не отнесённая
        # ни к чему, хранилась бы дольше или меньше, чем нужно.
        out[STREAM_FIELD] = getattr(record, STREAM_FIELD, STREAM_APPLICATION)

        for field in ENVELOPE:
            if field in out:
                continue
            value = getattr(record, field, None)
            # Контекст обращения подставляется сам: сервисный слой
            # не знает ни про HTTP, ни про трассу, а связать строки
            # одного запроса без этих полей нечем.
            if value is None:
                if field == "request_id":
                    value = REQUEST_ID.get()
                elif field == "trace_id":
                    value = TRACE_ID.get()
                elif field == "span_id":
                    value = SPAN_ID.get()
                elif field == "client_ip":
                    value = CLIENT_IP.get()
            out[field] = value

        # Всё, что вызывающий положил в extra, проходит вычистку.
        reserved = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)
        reserved |= {"event", "message", "asctime", "taskName"}
        for key, value in record.__dict__.items():
            if key in reserved or key in out or key in DROPPED_KEYS:
                continue
            out[key] = scrub(value, key)

        out["_msg"] = scrub(out["_msg"])

        if record.exc_info:
            # Трассировка стека — да, но тоже через вычистку: в аргументах
            # упавшего вызова бывает ровно то, что запрещено.
            out["exception"] = scrub(self.formatException(record.exc_info))

        return json.dumps(out, ensure_ascii=False, default=str)


def configure(
    service: str | None = None,
    environment: str | None = None,
    version: str | None = None,
    level: str | None = None,
) -> logging.Logger:
    """Настраивает корневой журнал. Идемпотентна."""
    service = service or os.getenv("SERVICE_NAME", "unknown")
    environment = environment or os.getenv("ENVIRONMENT", "local")
    version = version or os.getenv("SERVICE_VERSION", "unversioned")
    level = level or os.getenv("LOG_LEVEL", "INFO")

    root = logging.getLogger()
    root.setLevel(level)
    for existing in list(root.handlers):
        root.removeHandler(existing)

    # stdout, а не отправка по сети. Наблюдаемость не на критическом пути:
    # недоступное хранилище журналов не должно задерживать отправку
    # сообщения даже на миллисекунду.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service, environment, version))
    root.addHandler(handler)

    # Библиотеки говорят тише приложения. Формат у них тот же - они пишут
    # через корневой журнал, - но содержание к предметной области
    # отношения не имеет, и на INFO они заглушают собой настоящие события.
    for name in NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)

    # А эти пишут в обход корневого: свой обработчик, свой формат, свой
    # поток вывода. Обработчик снимается, распространение включается -
    # и строки уходят в том же конверте, что и всё остальное.
    for name in ADOPTED_LIBRARIES:
        library = logging.getLogger(name)
        if not library.handlers:
            # Журнал без своего обработчика уже молчит намеренно: именно
            # так uvicorn выполняет `--no-access-log` - снимает обработчик
            # и выключает распространение. Включив его обратно, мы вернули
            # бы журнал обращений вторым экземпляром той записи, которую
            # посредник уже написал сам, и в чужом потоке.
            continue
        library.handlers.clear()
        library.propagate = True

    return root
