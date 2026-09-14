"""API мессенджера: пробы, метрики, журналы и жизненный цикл зависимостей.

Бизнес-логики здесь нет и не должно быть: обработчик разбирает запрос и
отдаёт код ответа, всё остальное — ниже по слоям. Правило проверяется
автоматически (`scripts/check-layers.py`), и потому `api` не видит
репозиториев: обработчик, ходящий в базу напрямую, однажды сделает это
мимо транзакции, которой владеет сервис.
"""
from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.requests import Request

from messenger.services import runtime as runtime_service
from messenger.telemetry import metrics
from messenger.telemetry.logging import configure

log = configure()

# Имя берётся из телеметрии, а не из `os.getenv` заново. Два чтения одной
# переменной с разными значениями по умолчанию дают метрики с `service="api"`
# и `service="unknown"` в одном процессе — ряд разъезжается, а дашборд
# показывает половину.
SERVICE = metrics.SERVICE
VERSION = os.getenv("SERVICE_VERSION", "unversioned")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Открывает пул при старте и закрывает при остановке.

    Неудача при открытии процесс не роняет: падение в момент подъёма даёт
    CrashLoopBackOff, и когда база возвращается, кластер держит под
    в экспоненциальной паузе ещё несколько минут. Вместо этого сервис
    поднимается не готовым — трафика он не получит, а перезапускать
    его не за что.
    """
    await app.state.runtime.start()
    # Событие старта — отдельная запись: по ней видно, когда процесс
    # действительно принял конфигурацию, а не когда kubelet создал под.
    log.info("сервис поднят", extra={"event": "service_start", "result": "success"})
    try:
        yield
    finally:
        await app.state.runtime.stop()


app = FastAPI(title="Messenger API", docs_url=None, redoc_url=None, lifespan=lifespan)

# Владелец соединений создаётся при импорте, а не внутри lifespan: тогда
# проверка может подменить его, не поднимая приложение целиком, и модульный
# тест готовности не идёт в сеть.
app.state.runtime = runtime_service.Runtime(
    settings=runtime_service.pool_settings_from_env(), application_name=SERVICE
)

# «Что сейчас запущено» — непрерывный ряд. Из него нельзя строить отметки
# на графиках: Grafana поставит отметку на каждой точке. Для «когда
# произошло изменение» есть эксплуатационное событие, и это другая вещь.
BUILD_INFO = Gauge(
    "messenger_build_info", "Запущенная версия сервиса", ["service", "version"]
)
BUILD_INFO.labels(service=SERVICE, version=VERSION).set(1)

# RED. Метка route — шаблон маршрута, а не путь: `/conversations/123/messages`
# в метке означал бы новый временной ряд на каждую беседу, то есть гибель
# хранилища метрик в момент роста нагрузки.
REQUESTS = Counter(
    "http_requests_total",
    "Запросы HTTP",
    ["service", "route", "method", "status_class"],
)
DURATION = Histogram(
    "http_request_duration_seconds",
    "Длительность обработки запроса",
    ["service", "route", "method"],
)

READY = Gauge("messenger_ready", "Готовность принимать трафик", ["service"])
READY.labels(service=SERVICE).set(0)


def _route_template(request: Request) -> str:
    """Шаблон маршрута из роутера, иначе — заглушка.

    Возврат сырого пути был бы ровно тем, что запрещено: неизвестный путь
    от сканера портов создал бы временной ряд на каждый его запрос.
    """
    route = request.scope.get("route")
    return getattr(route, "path", "unmatched")


@app.middleware("http")
async def observe(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    route = _route_template(request)
    elapsed = time.perf_counter() - started

    REQUESTS.labels(
        service=SERVICE,
        route=route,
        method=request.method,
        status_class=f"{response.status_code // 100}xx",
    ).inc()
    DURATION.labels(service=SERVICE, route=route, method=request.method).observe(elapsed)

    # Журнал обращений — отдельный поток со своим сроком хранения.
    log.info(
        "%s %s %s",
        request.method,
        route,
        response.status_code,
        extra={
            "event": "http_request",
            "stream": "access",
            "route": route,
            "method": request.method,
            "status": response.status_code,
            "duration_ms": round(elapsed * 1000, 2),
            "result": "success" if response.status_code < 500 else "failed",
        },
    )
    return response


@app.get("/livez")
async def livez() -> dict[str, str]:
    """Жив ли процесс. Зависимости здесь не проверяются намеренно.

    Проба живости, падающая из-за недоступной базы, устраивает каскад:
    kubelet перезапускает исправные поды, те стартуют, снова не видят базу
    и перезапускаются опять — пока база восстанавливается, сервис
    целенаправленно уничтожает сам себя.
    """
    return {"status": "ok"}


@app.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, object]:
    """Можно ли давать трафик. Вот здесь зависимости и проверяются.

    Проверка — настоящий запрос к базе тем же путём, каким пойдёт рабочий:
    через PgBouncer, от роли приложения. «Клиент создан» доказывает только
    то, что объект создан, — пул считается открытым, пока первый запрос
    не упрётся в закрытый порт.

    Каждая зависимость названа в ответе отдельно. `ready: false` без
    разбивки означает поход в журналы в момент, когда как раз некогда.
    """
    runtime = request.app.state.runtime
    report = await runtime_service.check_readiness(runtime)
    READY.labels(service=SERVICE).set(1 if report.ready else 0)
    if not report.ready:
        response.status_code = 503
    return {"ready": report.ready, "checks": report.checks}


@app.get("/metrics")
async def metrics() -> Response:
    """Метрики снимаются опросом, а не отправляются.

    Отправка сделала бы хранилище метрик участником критического пути:
    его недоступность замедляла бы обработку запроса.
    """
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
