"""Контекст трассировки: один идентификатор на весь путь сообщения.

Зачем это здесь, а не в OpenTelemetry. Экспортёр трасс - отдельная работа
(гейт G2), а `trace_id` в журнале нужен раньше: без него расследование
«сообщение не пришло» состоит из поиска по времени в трёх сервисах, и
совпадение по времени - не доказательство, а догадка.

Формат взят чужой намеренно. `traceparent` из W3C Trace Context - это те же
16 байт идентификатора трассы и 8 байт идентификатора участка, что кладёт
в заголовок OpenTelemetry. Когда экспортёр появится, идентификаторы, уже
лежащие в журналах и в outbox, окажутся теми же самыми, и связывать
записанное задним числом не придётся.

Асинхронная граница проходится через тело события, а не через процесс:
между API и потребителем лежит коммит в Postgres и запись в Kafka, и
никакой contextvar это не переживает. Поэтому `trace_id` кладётся
в полезную нагрузку outbox (он есть в контрактах `message.created.v1`
и `message.content.v1`), а потребитель поднимает его обратно.
"""
from __future__ import annotations

import contextlib
import secrets
from collections.abc import Iterator, Mapping
from typing import Any

from messenger.telemetry.logging import SPAN_ID, TRACE_ID

HEADER = "traceparent"

# Нулевые идентификаторы спецификация объявляет негодными: заголовок
# с ними означает «контекста нет», а не «контекст такой».
_ZERO_TRACE = "0" * 32
_ZERO_SPAN = "0" * 16


def new_trace_id() -> str:
    """16 байт в шестнадцатеричном виде — как в W3C и OpenTelemetry."""
    return secrets.token_hex(16)


def new_span_id() -> str:
    """8 байт: идентификатор единицы работы, а не всего пути."""
    return secrets.token_hex(8)


def parse(header: str | None) -> tuple[str, str] | None:
    """Разбирает `traceparent`. Возвращает трассу и родительский участок.

    Негодный заголовок не ошибка обращения: чужой сервис с поломанной
    телеметрией не должен получать `400` от нас. Такой заголовок просто
    игнорируется, и трасса начинается здесь.
    """
    if not header:
        return None
    parts = header.strip().split("-")
    if len(parts) < 4 or parts[0] != "00":
        return None
    trace_id, parent_id = parts[1], parts[2]
    if len(trace_id) != 32 or len(parent_id) != 16:
        return None
    if trace_id == _ZERO_TRACE or parent_id == _ZERO_SPAN:
        return None
    if not all(c in "0123456789abcdef" for c in trace_id + parent_id):
        return None
    return trace_id, parent_id


def header_for(trace_id: str, span_id: str) -> str:
    """Заголовок для исходящего вызова или записи в Kafka.

    Флаг выборки всегда `01`. Выборка в первой версии решается на стороне
    коллектора (часть 2 наблюдаемости: хвостовая, а не головная), и
    выставлять здесь `00` значило бы отбросить трассу до того, как
    станет известно, была ли она интересной.
    """
    return f"00-{trace_id}-{span_id}-01"


@contextlib.contextmanager
def bind(*, trace_id: str | None = None, span_id: str | None = None) -> Iterator[str]:
    """Привязывает контекст к текущей задаче и снимает его на выходе.

    Снятие обязательно и потому сделано менеджером контекста: процесс
    потребителя живёт долго и обрабатывает события подряд, а contextvar,
    оставленный от предыдущего, приписал бы чужую трассу следующему
    событию - худший вид ошибки в расследовании, потому что выглядит
    он как настоящая связь.
    """
    trace = trace_id or new_trace_id()
    span = span_id or new_span_id()
    trace_token = TRACE_ID.set(trace)
    span_token = SPAN_ID.set(span)
    try:
        yield trace
    finally:
        TRACE_ID.reset(trace_token)
        SPAN_ID.reset(span_token)


def from_carrier(headers: Mapping[str, str], body: Mapping[str, Any]) -> str | None:
    """Трасса из заголовков записи, иначе из её тела, иначе `None`.

    Два источника, потому что их два и на самом деле: заголовок нужен
    тому, кто тело не разбирает (дедупликация), а тело - тому, чья
    запись пришла от отправителя прежней версии, когда заголовка
    ещё не было. Порядок именно такой: заголовок ближе к транспорту
    и не зависит от схемы события.
    """
    parsed = parse(headers.get(HEADER))
    if parsed is not None:
        return parsed[0]
    from_body = body.get("trace_id")
    return from_body if isinstance(from_body, str) and from_body else None


def current_trace_id() -> str | None:
    """Идентификатор трассы текущей задачи, если он есть."""
    return TRACE_ID.get()
