"""Контекст трассировки: формат W3C и хранилище текущего контекста.

Модуль остаётся без OpenTelemetry намеренно, хотя экспортёр с гейта G2
уже есть. Здесь две вещи, которым SDK не нужен: разбор и сборка
`traceparent` (W3C Trace Context - это те же 16 байт трассы и 8 байт
участка, что кладёт в заголовок OpenTelemetry) и contextvar, из которого
конверт журнала берёт `trace_id` и `span_id`.

Формат был взят чужим до появления экспортёра, и это окупилось: ни один
идентификатор, уже лежащий в журналах и в outbox, менять не пришлось.
Идентификаторы теперь рождает спан (`telemetry.tracing`), а не `bind`;
`bind` остаётся низкоуровневым способом положить пару в конверт и
синтезирует её, только когда спанов нет вовсе - запуск без коллектора.

Асинхронная граница проходится через тело события, а не через процесс:
между API и потребителем лежит коммит в Postgres и запись в Kafka, и
никакой contextvar это не переживает. Поэтому пара кладётся
в полезную нагрузку outbox (контракты `message.created.v1`
и `message.content.v1`), а потребитель поднимает её обратно.
"""
from __future__ import annotations

import contextlib
import secrets
from collections.abc import Iterator, Mapping
from typing import Any, NamedTuple

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


class Origin(NamedTuple):
    """Откуда пришла запись: трасса и участок её создателя.

    Именно пара, а не один `trace_id`. После перехода на три трассы
    (см. документацию, часть 4) трасса отправителя - своя, и связь
    с трассой запроса, который эту запись породил, держится ссылкой
    на конкретный спан. Без участка ссылку построить нечем.

    `span_id` необязателен не для удобства: событие, записанное
    отправителем прежней версии, участка не несёт - поле появилось
    вместе с этой работой. Это штатное состояние при выкатке, а не
    ошибка, и обрабатывается оно отказом от ссылки, а не подстановкой
    случайного идентификатора.
    """

    trace_id: str
    span_id: str | None = None


def origin_from(
    headers: Mapping[str, str], body: Mapping[str, Any]
) -> Origin | None:
    """Контекст создателя записи: из заголовка, иначе из тела.

    Два источника, потому что их два и на самом деле: заголовок нужен
    тому, кто тело не разбирает (дедупликация), а тело - тому, чья
    запись пришла от отправителя прежней версии, когда заголовка
    ещё не было. Порядок именно такой: заголовок ближе к транспорту
    и не зависит от схемы события.
    """
    parsed = parse(headers.get(HEADER))
    if parsed is not None:
        return Origin(trace_id=parsed[0], span_id=parsed[1])
    from_body = body.get("trace_id")
    if not isinstance(from_body, str) or not from_body:
        return None
    span_id = body.get("span_id")
    return Origin(
        trace_id=from_body,
        span_id=span_id if isinstance(span_id, str) and span_id else None,
    )


def origin_from_body(body: Mapping[str, Any]) -> Origin | None:
    """Контекст создателя, когда заголовков нет вовсе.

    Так приходит запись из outbox: заголовки Kafka ставит отправитель,
    а до него их нет ни у кого - только тело события.
    """
    return origin_from({}, body)


def current_trace_id() -> str | None:
    """Идентификатор трассы текущей задачи, если он есть."""
    return TRACE_ID.get()


def current_span_id() -> str | None:
    """Идентификатор участка текущей задачи. Пара к `current_trace_id`."""
    return SPAN_ID.get()
