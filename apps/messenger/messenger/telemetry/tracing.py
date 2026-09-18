"""Спаны: единственный модуль, знающий про OpenTelemetry.

Наружу отсюда не протекает ни один тип SDK, и это не аккуратность ради
аккуратности. Сервисный слой не должен уметь создать спан в обход
`span()`: тогда конверт журнала разошёлся бы с настоящим текущим спаном,
и `span_id` в записи перестал бы указывать на что-либо существующее -
поломка, заметная только на разборе инцидента.

Отсюда три следствия, каждое сделано намеренно.

Первое: идентификаторы рождает `span()`, и только он. Он берёт контекст
созданного спана и кладёт пару в те же contextvars, из которых её читает
`JsonFormatter`. Форматтер журнала при этом не меняется вовсе, а
расхождение между записью и спаном становится невозможным по построению.

Второе: `span()` работает одинаково при включённой и выключенной
трассировке. Без коллектора он отдаёт спан-пустышку с настоящими
синтетическими идентификаторами, а не `None`, поэтому ни один вызывающий
не обрастает `if span is not None`, и тесты идут по тому же коду, что и
прод.

Третье: автоинструментация не подключается сознательно.
`opentelemetry-instrumentation-fastapi` завела бы спаны, о которых
`trace.bind()` не знает, и конверт журнала начал бы отставать от
настоящего текущего спана - то есть инвариант из первого следствия
пришлось бы пересматривать.

Выборка здесь всегда головная и всегда полная: решение по ошибке
принимается, когда трасса уже завершена, а это умеет только коллектор.
Заголовок уходит с флагом `01` всегда (см. `trace.header_for`).
"""
from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Iterator, Mapping, Sequence
from contextvars import ContextVar
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace as otel
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON, Sampler
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    SpanKind,
    Status,
    StatusCode,
    TraceFlags,
    format_span_id,
    format_trace_id,
    set_span_in_context,
)

from messenger.telemetry import metrics, trace

log = logging.getLogger(__name__)

# Размеры очереди экспортёра - константами, а не переменными окружения:
# `OTEL_BSP_*` Python-SDK сам не читает, и полагаться на них значило бы
# оставить в файле развёртывания настройку, которая ничего не делает.
#
# Очередь ограничена намеренно. Переполнение отбрасывает спаны вместо
# того, чтобы копить их: потеря телеметрии допустима, потеря сообщения
# из-за телеметрии - нет.
MAX_QUEUE_SIZE = 2048
SCHEDULE_DELAY_MILLIS = 5000
MAX_EXPORT_BATCH_SIZE = 512

# Таймаут экспорта. Умолчание SDK - 30 секунд: при мёртвом коллекторе
# поток экспортёра занят полминуты на попытку, и очередь заполняется
# быстрее, чем опорожняется. Пять секунд - компромисс, при котором
# отказ виден в журнале, а не копится молча.
EXPORT_TIMEOUT_MILLIS = 5000

# Таймаут досылки при выключении пода. Ограничен по той же причине:
# остановка не должна ждать мёртвый коллектор.
FLUSH_TIMEOUT_MILLIS = 1000

INSTRUMENTATION = "messenger"

# Виды спанов, названные так, как их называет вызывающий. Значения - из
# спецификации OpenTelemetry: `send` -> PRODUCER, `receive` -> CLIENT,
# `process` -> CONSUMER. Неочевидное здесь - `receive` именно CLIENT,
# а не CONSUMER.
#
# От вида зависит не косметика: по нему инструменты разбора рисуют связь
# производителя с потребителем, и спан, помеченный INTERNAL, этой связи
# не даст, даже если ссылка на месте.
INTERNAL = SpanKind.INTERNAL
SERVER = SpanKind.SERVER
CLIENT = SpanKind.CLIENT
PRODUCER = SpanKind.PRODUCER
CONSUMER = SpanKind.CONSUMER

# Пара идентификаторов: (трасса, участок). Ею же принимаются `parent`
# и `links`, чтобы сервисам не приходилось видеть `SpanContext`.
Pair = tuple[str, str]

# Спан, на который ставится ссылка из outbox, - корневой спан запроса.
# Именно он, а не "текущий": добавление любого спана внутри обработчика
# сдвинуло бы цель ссылки, и заметить это можно было бы только по
# неверной ссылке в Tempo.
_ANCHOR: ContextVar[Span | None] = ContextVar("trace_anchor", default=None)

_provider: TracerProvider | None = None
_tracer: otel.Tracer | None = None


class Span:
    """Спан, каким его видит остальной код.

    Обёртка, а не сам спан SDK, по двум причинам. Первая: при выключенной
    трассировке нужен объект с тем же набором методов, но без SDK за
    спиной. Вторая: наружу не должен протекать тип, который можно
    передать в чужие руки и завершить не там.
    """

    __slots__ = ("_raw", "_trace_id", "_span_id")

    def __init__(
        self, raw: otel.Span | None, trace_id: str, span_id: str
    ) -> None:
        self._raw = raw
        self._trace_id = trace_id
        self._span_id = span_id

    @property
    def trace_id(self) -> str:
        return self._trace_id

    @property
    def span_id(self) -> str:
        return self._span_id

    def set_attribute(self, key: str, value: Any) -> None:
        if self._raw is not None:
            self._raw.set_attribute(key, value)

    def update_name(self, name: str) -> None:
        """Переименовать спан. Нужно ровно там, где имя известно после.

        Шаблон маршрута в FastAPI появляется только внутри обработчика,
        поэтому спан запроса открывается с одного метода, а полное имя
        получает после ответа.
        """
        if self._raw is not None:
            self._raw.update_name(name)

    def add_event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        if self._raw is not None:
            self._raw.add_event(name, attributes=attributes)

    def _mark_error(self, error_code: str) -> None:
        if self._raw is None:
            return
        self._raw.set_attribute("error.type", error_code)
        self._raw.set_status(Status(StatusCode.ERROR, error_code))


def configure(*, span_exporter: SpanExporter | None = None) -> None:
    """Ставит провайдер экспорта. Идемпотентна, как `logging.configure`.

    Всё тело в `try`: негодный адрес, недоступный пакет, что угодно -
    процесс обязан подняться, а трассировка выключиться. Телеметрия не
    имеет права уронить сервис на старте.

    Провайдер хранится в модульной переменной, а не в глобальном реестре
    OpenTelemetry: `set_tracer_provider` допускает одну установку на
    процесс, поэтому второй вызов проигнорировался бы молча, а тест,
    подменивший провайдера, отравил бы соседний тест.
    """
    global _provider, _tracer
    if _tracer is not None:
        return

    try:
        exporter = span_exporter or _exporter_from_env()
        if exporter is None:
            # Вопрос "почему в Tempo пусто" получает ответ в первой же
            # строке пода, а не после чтения чарта.
            log.info(
                "экспорт трасс выключен",
                extra={"event": "tracing_config", "result": "skipped",
                       "error_code": "endpoint_not_set"},
            )
            return

        provider = TracerProvider(resource=_resource(), sampler=_sampler_from_env())
        provider.add_span_processor(
            BatchSpanProcessor(
                exporter,
                max_queue_size=MAX_QUEUE_SIZE,
                schedule_delay_millis=SCHEDULE_DELAY_MILLIS,
                max_export_batch_size=MAX_EXPORT_BATCH_SIZE,
                export_timeout_millis=EXPORT_TIMEOUT_MILLIS,
            )
        )
        _provider = provider
        _tracer = provider.get_tracer(INSTRUMENTATION, _version())
        log.info(
            "экспорт трасс включён",
            extra={"event": "tracing_config", "result": "success",
                   "endpoint": os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")},
        )
    except Exception as exc:  # noqa: BLE001 - причина уходит в журнал
        _provider = None
        _tracer = None
        log.warning(
            "экспорт трасс не настроен",
            extra={"event": "tracing_config", "result": "failed",
                   "error_code": type(exc).__name__},
        )


def shutdown() -> None:
    """Досылает остаток и останавливает экспортёр.

    Досылка ограничена по времени: выключение пода не должно ждать
    мёртвый коллектор.
    """
    global _provider, _tracer
    provider, _provider, _tracer = _provider, None, None
    if provider is None:
        return
    try:
        provider.force_flush(FLUSH_TIMEOUT_MILLIS)
        provider.shutdown()
    except Exception as exc:  # noqa: BLE001 - остановка не повод падать
        log.warning(
            "остановка экспорта трасс не удалась",
            extra={"event": "tracing_config", "result": "failed",
                   "error_code": type(exc).__name__},
        )


def reset() -> None:
    """Снимает провайдер и якорь. Шов для тестов: без него фикстура течёт."""
    shutdown()
    _ANCHOR.set(None)


@contextlib.contextmanager
def span(
    name: str,
    *,
    kind: SpanKind = INTERNAL,
    parent: Pair | None = None,
    links: Sequence[Pair | None] = (),
    attributes: Mapping[str, Any] | None = None,
    anchor: bool = False,
) -> Iterator[Span]:
    """Открывает спан и держит его, пока идёт блок.

    `parent` - продолжить чужую трассу (так приходит `traceparent` извне).
    `links` - связать с работой, которая шла в другое время и в другом
    процессе; родителем она быть не может, и это не ограничение, а
    предписание спецификации для обмена сообщениями.

    `anchor=True` помечает спан целью ссылки из outbox. Помечается явно,
    а не "тот, что снаружи": так ссылка не сдвинется от того, что внутри
    обработчика однажды появится ещё один спан.
    """
    raw: otel.Span | None = None
    if _tracer is not None:
        raw = _tracer.start_span(
            name,
            context=_parent_context(parent),
            kind=kind,
            links=_span_links(links),
            attributes=dict(attributes or {}),
        )
        context = raw.get_span_context()
        current = Span(raw, format_trace_id(context.trace_id),
                       format_span_id(context.span_id))
    else:
        # Трассировка выключена: идентификаторы синтетические, но
        # настоящие - по ним связываются записи журнала, и они же дают
        # понять, что спанов нет, а не что связь потеряна.
        current = Span(None, parent[0] if parent else trace.new_trace_id(),
                       trace.new_span_id())

    anchor_token = _ANCHOR.set(current) if anchor else None
    try:
        with trace.bind(trace_id=current.trace_id, span_id=current.span_id):
            if raw is None:
                yield current
            else:
                # `end_on_exit` явно: умолчание этой версии SDK его
                # выключает, и спан остался бы незавершённым - то есть
                # неотправленным.
                with otel.use_span(raw, end_on_exit=True):
                    yield current
    finally:
        if anchor_token is not None:
            _ANCHOR.reset(anchor_token)


def pair(current: Span) -> Pair:
    """Пара идентификаторов спана - для ссылки, родителя или журнала."""
    return current.trace_id, current.span_id


def traceparent(current: Span) -> str:
    """Готовое значение заголовка `traceparent` для этого спана.

    Именно по контексту спана, а не по идентификатору из contextvar:
    заголовок записи в Kafka обязан называть спан, который её создал,
    иначе ссылка потребителя укажет в пустоту.
    """
    return trace.header_for(current.trace_id, current.span_id)


def current_link() -> Pair | None:
    """Пара якорного спана, если он сейчас открыт."""
    current = _ANCHOR.get()
    return pair(current) if current is not None else None


def anchor_span() -> Span | None:
    """Сам якорный спан. Нужен, чтобы отметить его после ответа."""
    return _ANCHOR.get()


def mark_failed(current: Span, error_code: str) -> None:
    """Помечает спан отказом.

    Без этого политика хвостовой выборки "ошибки хранить" не увидит
    ровно тот случай, ради которого заведена.
    """
    current._mark_error(error_code)


def set_anchor_attribute(key: str, value: Any) -> None:
    """Ставит атрибут на якорный спан, если он открыт.

    Сквозной поиск после перехода на три трассы идёт по `message_id`,
    а не по `trace_id`, поэтому сообщение должно находиться в Tempo
    напрямую - по атрибуту спана.
    """
    current = _ANCHOR.get()
    if current is not None:
        current.set_attribute(key, value)


def _version() -> str:
    return os.getenv("SERVICE_VERSION", "unversioned")


def _resource() -> Resource:
    """Кто произвёл спаны.

    Имя сервиса берётся из `metrics.SERVICE`, а не из окружения заново:
    два чтения одной переменной с разными умолчаниями дают
    `service="unknown"` в трассах рядом с `service="api"` в журналах,
    и связь "журнал - трасса" рвётся на пустом месте.
    """
    return Resource.create(
        {
            "service.name": metrics.SERVICE,
            "service.version": _version(),
            "deployment.environment.name": os.getenv("ENVIRONMENT", "local"),
        }
    )


def _sampler_from_env() -> Sampler:
    """Головная выборка: полная, пока не сказано обратное.

    `ParentBased` здесь сознательно не ставится. Он перевёл бы решение
    в ветку "родитель не выбран" при чужом флаге `00`, и наши спаны
    исчезли бы из Tempo молча. Флаг `00` в чужом заголовке мы
    игнорируем намеренно: решение принадлежит нашему коллектору.

    Пустое значение - не ошибка, а состояние выкатки: чарт с этим
    ключом может уехать раньше кода. Тогда трассировка обязана
    остаться включённой, иначе порядок выкатки что-нибудь сломает.
    """
    raw = (os.getenv("OTEL_TRACES_SAMPLER") or "").strip().lower()
    if not raw or raw == "always_on":
        return ALWAYS_ON
    if raw == "always_off":
        return ALWAYS_OFF
    log.warning(
        "незнакомое значение OTEL_TRACES_SAMPLER",
        extra={"event": "tracing_config", "result": "fallback",
               "error_code": "unknown_sampler",
               "dependency": "opentelemetry"},
    )
    return ALWAYS_ON


def _exporter_from_env() -> SpanExporter | None:
    """Экспортёр по адресу из окружения, иначе ничего.

    Импорт внутри функции: пакет экспортёра тянет protobuf и grpcio,
    а модульные тесты, которым нужен только `InMemorySpanExporter`,
    платить за это не должны.
    """
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        return None
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter(
        endpoint=endpoint,
        timeout=EXPORT_TIMEOUT_MILLIS / 1000,
    )


def _span_context(value: Pair) -> SpanContext | None:
    """Контекст чужого спана из пары идентификаторов.

    Негодная пара - это не ошибка обращения, а внешние данные: тело
    события приходит из очереди, и запись прежней версии отправителя
    или испорченная строка не должны ронять обработку. Такая пара
    просто не даёт связи.
    """
    trace_id, span_id = value
    try:
        return SpanContext(
            trace_id=int(trace_id, 16),
            span_id=int(span_id, 16),
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
    except Exception:  # noqa: BLE001 - данные извне, ронять обработку нечем
        return None


def _parent_context(parent: Pair | None) -> otel_context.Context | None:
    """Контекст родителя, а для вложенного спана - `None`.

    `None` здесь означает не "без родителя", а "родитель текущий": так
    его понимает `start_span`. Вернуть пустой контекст значило бы
    потерять вложенность - спаны одной трассы оказались бы соседями,
    а не родителем с детьми, и дерево в Tempo рассыпалось бы.
    """
    if parent is None:
        return None
    context = _span_context(parent)
    if context is None:
        # Пара из внешних данных оказалась негодной. Продолжать чужую
        # трассу нечем, но и терять свою незачем: начинаем её здесь.
        return None
    return set_span_in_context(NonRecordingSpan(context))


def _span_links(links: Sequence[Pair | None]) -> list[otel.Link]:
    """Ссылки на контекст создания записи.

    Ссылка - основной способ связать производителя с потребителем:
    родителем чужой спан быть не может, если сообщений в пачке много,
    а родитель у спана только один. Спецификация разрешает родительство
    лишь для одиночных сообщений и не рекомендует его по умолчанию.
    """
    result: list[otel.Link] = []
    for value in links:
        if value is None:
            continue
        context = _span_context(value)
        if context is not None:
            result.append(otel.Link(context))
    return result
