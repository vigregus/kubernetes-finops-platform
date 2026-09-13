"""API мессенджера. Пока — проводка: пробы, метрики, журналы.

Бизнес-логики здесь нет и не должно быть до G2. Задача этого модуля —
доказать, что каркас работает: сервис поднимается, отвечает на пробы,
отдаёт метрики и пишет журнал в общем конверте.
"""
from __future__ import annotations

import os
import time

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.requests import Request

from messenger.telemetry.logging import configure

log = configure()
SERVICE = os.getenv("SERVICE_NAME", "api")
VERSION = os.getenv("SERVICE_VERSION", "unversioned")

app = FastAPI(title="Messenger API", docs_url=None, redoc_url=None)

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
async def readyz(response: Response) -> dict[str, object]:
    """Можно ли давать трафик. Вот здесь зависимости и проверяются.

    Пока проверять нечего: клиентов Postgres, Kafka и Redis ещё нет.
    Заглушка возвращает пустой список, а не `true` — чтобы отсутствие
    проверок было видно в ответе, а не выглядело как успешная проверка.
    """
    checks: dict[str, str] = {}
    ready = all(v == "ok" for v in checks.values())
    READY.labels(service=SERVICE).set(1 if ready else 0)
    if not ready:
        response.status_code = 503
    return {"ready": ready, "checks": checks}


@app.get("/metrics")
async def metrics() -> Response:
    """Метрики снимаются опросом, а не отправляются.

    Отправка сделала бы хранилище метрик участником критического пути:
    его недоступность замедляла бы обработку запроса.
    """
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
