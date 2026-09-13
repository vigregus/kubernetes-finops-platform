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

import json
import logging
import os
import re
import sys
import time
from typing import Any

# Поля, которые обязаны быть в каждой записи. Отсутствующие заполняются
# из окружения при настройке, а не проставляются вызывающим кодом:
# забыть про service в одном модуле из сорока — вопрос времени.
ENVELOPE = (
    "timestamp", "level", "service", "environment", "version",
    "event", "result", "error_code",
    "trace_id", "span_id", "request_id",
    "message_id", "event_id",
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
    if isinstance(value, (list, tuple)):
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
            "event": getattr(record, "event", record.name),
            "_msg": record.getMessage(),
        }

        for field in ENVELOPE:
            if field in out:
                continue
            out[field] = getattr(record, field, None)

        # Всё, что вызывающий положил в extra, проходит вычистку.
        reserved = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)
        reserved |= {"event", "message", "asctime", "taskName"}
        for key, value in record.__dict__.items():
            if key in reserved or key in out:
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
    return root
