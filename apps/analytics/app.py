"""analytics - HTTP API.

The Kafka consumer used to run as a daemon thread inside this same process.
That arrangement cannot survive gunicorn: the module is imported once per
worker, so `WEB_CONCURRENCY=4` would silently start four consumers in the
same group inside one pod. It also meant the queue could never be scaled
independently of HTTP traffic, and the worker's cost could not be separated
from the API's.

The consumer now lives in worker.py and runs as its own Deployment from this
same image. Everything below the HTTP handlers is shared setup that both
entrypoints import.
"""
import json
import logging
import os
import random
import sys
import time
import uuid

import redis as redis_lib
from common import metrics as metrics_mod
from common import workload
from common.db import PoolBusy, PooledPostgres, connect_with_retry
from common.resilience import LoadShedder, Shed
from flask import Flask, Response, request
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry import propagate
from prometheus_client import Counter, Gauge, Histogram

SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "analytics")
SERVICE_VERSION = os.environ.get("SERVICE_VERSION", "0.1.0")
# Downward API fields (see charts/app2/templates/deployment.yaml) - without
# these, a trace/log can't be told apart from its stage/prod twin, or
# pinned to the pod that produced it, without a separate trip to kubectl.
DEPLOYMENT_ENVIRONMENT = os.environ.get("DEPLOYMENT_ENVIRONMENT", "unknown")
K8S_POD_NAME = os.environ.get("K8S_POD_NAME", "")
K8S_NAMESPACE_NAME = os.environ.get("K8S_NAMESPACE_NAME", "")
OTLP_ENDPOINT = os.environ.get(
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "http://tempo-local.observability.svc.cluster.local:4318",
)
PORT = int(os.environ.get("PORT", "8080"))
ERROR_RATE = float(os.environ.get("ERROR_RATE", "0.01"))
MAX_IN_FLIGHT = int(os.environ.get("MAX_IN_FLIGHT", "64"))

# Per-request resource shape. analytics is deliberately given a heavier CPU
# profile than checkout - see apps/common/workload.py; the point of the stand
# is that the tiers do not all look alike to a rightsizing decision.
QUERY_PROFILE = workload.Profile.from_env(os.environ, "PROFILE_ANALYTICS")
BATCH_PROFILE = workload.Profile.from_env(os.environ, "PROFILE_BATCH")

REDIS_URL = os.environ.get("REDIS_URL")
REDIS_POOL_MAX = int(os.environ.get("REDIS_POOL_MAX", "32"))
REDIS_TIMEOUT_S = float(os.environ.get("REDIS_TIMEOUT_S", "2"))

KAFKA_BOOTSTRAP_SERVERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS")
KAFKA_TOPIC = os.environ.get("KAFKA_TOPIC", "orders")
KAFKA_CONSUMER_GROUP = os.environ.get("KAFKA_CONSUMER_GROUP", "analytics")
KAFKA_BATCH_SIZE = int(os.environ.get("KAFKA_CONSUME_BATCH", "100"))

DATABASE_URL = os.environ.get("DATABASE_URL")
DATABASE_NAME = os.environ.get("DATABASE_NAME", SERVICE_NAME)
DB_POOL_MIN = int(os.environ.get("DB_POOL_MIN", "1"))
DB_POOL_MAX = int(os.environ.get("DB_POOL_MAX", "8"))
DB_STATEMENT_TIMEOUT_MS = int(os.environ.get("DB_STATEMENT_TIMEOUT_MS", "5000"))
# How long a request waits for a free connection before being shed.
# Long enough to absorb a contention spike, short enough that a
# saturated pod answers "too busy" instead of holding the caller.
DB_ACQUIRE_TIMEOUT_MS = int(os.environ.get("DB_ACQUIRE_TIMEOUT_MS", "250"))

# Every order on this topic belongs to the same product - checkout is its only
# producer. Hardcoding that is more honest than deriving it from analytics'
# own finops.internal/product label, which describes who processes the event,
# not what the order was for.
EVENT_PRODUCT = "commerce"

REDIS_KEY_PROCESSED = "analytics:orders_processed"

provider = TracerProvider(
    resource=Resource.create(
        {
            "service.name": SERVICE_NAME,
            "service.version": SERVICE_VERSION,
            "deployment.environment": DEPLOYMENT_ENVIRONMENT,
            "k8s.pod.name": K8S_POD_NAME,
            "k8s.namespace.name": K8S_NAMESPACE_NAME,
        }
    )
)
provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{OTLP_ENDPOINT}/v1/traces"))
)
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(SERVICE_NAME)

app = Flask(__name__)

REQUEST_COUNT = Counter(
    "http_requests_total", "Total HTTP requests", ["method", "path", "status"]
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds", "HTTP request latency in seconds", ["method", "path"]
)
CACHE_LATENCY = Histogram("cache_call_duration_seconds", "Redis call latency in seconds")
DB_LATENCY = Histogram("db_query_duration_seconds", "Postgres query latency in seconds")
ORDERS_CONSUMED = Counter("orders_consumed_total", "Order events consumed from Kafka")
EVENTS_PERSISTED = Counter("order_events_persisted_total", "Rows written to order_events")
REQUESTS_SHED = Counter("http_requests_shed_total", "Requests rejected because the pod was at capacity")
IN_FLIGHT = Gauge("http_requests_in_flight", "Requests currently being served by this process")
CPU_CALIBRATION = Gauge(
    "workload_cpu_iters_per_ms",
    "Calibrated synthetic-work iterations per millisecond on this node",
)

# Routed through logging (not bare print) so ERROR lines are a real,
# filterable severity instead of just another line of JSON text - level
# is its own field precisely so "show me only errors" is a VictoriaLogs
# field match, not a string search on message content.
logger = logging.getLogger(SERVICE_NAME)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(_handler)
logger.propagate = False

_LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


class QueryError(Exception):
    pass


def log_json(span, level="info", **fields):
    # See apps/checkout/app.py: Grafana's trace-to-logs correlation looks
    # up VictoriaLogs by the trace_id *field*, and every step needs its
    # own log line with its own span's IDs so a specific span in Tempo
    # (not just the request as a whole) can jump to what it actually did.
    span_ctx = span.get_span_context()
    if span_ctx.is_valid:
        fields["trace_id"] = format(span_ctx.trace_id, "032x")
        fields["span_id"] = format(span_ctx.span_id, "016x")
    # VictoriaLogs requires the primary text field to be named "_msg"
    # specifically - "msg" is silently dropped ("missing _msg field")
    # instead of falling back to the raw line.
    if "msg" in fields:
        fields["_msg"] = fields.pop("msg")
    fields["level"] = level
    logger.log(_LOG_LEVELS.get(level, logging.INFO), json.dumps(fields))


SHEDDER = LoadShedder(MAX_IN_FLIGHT)

redis_client = (
    redis_lib.Redis(
        connection_pool=redis_lib.ConnectionPool.from_url(
            REDIS_URL, max_connections=REDIS_POOL_MAX, socket_timeout=REDIS_TIMEOUT_S
        )
    )
    if REDIS_URL
    else None
)

db: PooledPostgres | None = None


def init_db():
    """Own the order_events schema here.

    analytics is what writes this table on its real consume path, so the DDL
    belongs with the service that owns it rather than in a migration tool the
    rest of this repository does not have.
    """
    if not db:
        return
    with db.cursor(commit=True) as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS order_events (
                event_id UUID PRIMARY KEY,
                order_id UUID NOT NULL,
                event_type TEXT NOT NULL,
                product TEXT NOT NULL,
                amount_cents BIGINT NOT NULL,
                occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                region TEXT NOT NULL
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS order_events_occurred_at_idx ON order_events (occurred_at)")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS order_events_product_occurred_at_idx "
            "ON order_events (product, occurred_at)"
        )


def persist_event(payload, span_ctx_for_sql):
    """Write one consumed order event as a row.

    This is what turns the fact table into a real part of the system rather
    than a prop: the volume it accumulates comes from traffic that actually
    flowed through checkout -> Kafka -> here.
    """
    if not db:
        return
    start = time.perf_counter()
    with tracer.start_as_current_span("analytics.persist_event") as span:
        sql = (
            f"/* trace_id={span_ctx_for_sql} */ "
            "INSERT INTO order_events "
            "(event_id, order_id, event_type, product, amount_cents, region) "
            "VALUES (%s, %s, %s, %s, %s, %s)"
        )
        span.set_attribute("db.system", "postgresql")
        span.set_attribute("db.name", DATABASE_NAME)
        span.set_attribute("db.operation", "INSERT")
        span.set_attribute("db.sql.table", "order_events")
        with db.cursor(commit=True) as cur:
            cur.execute(
                sql,
                (
                    str(uuid.uuid4()),
                    payload.get("order_id"),
                    "order_created",
                    EVENT_PRODUCT,
                    int(payload.get("amount_cents", 0)),
                    payload.get("region", "unknown"),
                ),
            )
        EVENTS_PERSISTED.inc()
        log_json(span, level="debug", _msg="order_events INSERT", order_id=payload.get("order_id"))
    DB_LATENCY.observe(time.perf_counter() - start)


@app.route("/", methods=["GET"])
@app.route("/analytics", methods=["GET"])
def analytics():
    try:
        with SHEDDER.admit():
            IN_FLIGHT.set(SHEDDER.in_flight)
            return _analytics()
    except Shed:
        REQUESTS_SHED.inc()
        REQUEST_COUNT.labels(method="GET", path="/analytics", status="429").inc()
        return Response("overloaded\n", status=429, headers={"Retry-After": "1"})
    finally:
        IN_FLIGHT.set(SHEDDER.in_flight)


def _analytics():
    start = time.perf_counter()
    # See apps/checkout/app.py: Werkzeug title-cases header names, which
    # breaks the propagator's case-sensitive "traceparent" lookup unless
    # the keys are lowered first.
    ctx = propagate.extract({k.lower(): v for k, v in request.headers.items()})
    with tracer.start_as_current_span("analytics.query", context=ctx, kind=SpanKind.SERVER) as span:
        span.set_attribute("http.method", "GET")
        span.set_attribute("http.route", "/analytics")
        span.set_attribute("http.target", request.path)
        span.set_attribute("http.scheme", request.scheme)
        trace_id_hex = format(span.get_span_context().trace_id, "032x")

        processed = None
        try:
            if redis_client:
                cache_start = time.perf_counter()
                with tracer.start_as_current_span("analytics.cache_lookup") as cache_span:
                    cache_span.set_attribute("db.system", "redis")
                    cache_span.set_attribute("db.operation", "GET")
                    cache_span.set_attribute("db.statement", f"GET {REDIS_KEY_PROCESSED}")
                    processed = redis_client.get(REDIS_KEY_PROCESSED)
                    log_json(cache_span, level="debug", _msg="redis GET", key=REDIS_KEY_PROCESSED, hit=processed is not None)
                CACHE_LATENCY.observe(time.perf_counter() - cache_start)

            QUERY_PROFILE.run()

            if random.random() < ERROR_RATE:
                raise QueryError("analytics backend query timed out")
            status = 200
        except PoolBusy as e:
            status = 429
            log_json(span, level="warning", _msg=f"analytics shed at db pool: {e}", path="/analytics")
        except Exception as e:
            status = 500
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            log_json(
                span,
                level="error",
                _msg="analytics request failed",
                path="/analytics",
                error_type=type(e).__name__,
                error=str(e),
            )

        span.set_attribute("http.status_code", status)

    # exemplar: see apps/checkout/app.py for why this is what lets Grafana
    # show a different, representative trace per percentile point (p50 vs
    # p99). Attached only when the exposition format can carry it - see
    # apps/common/metrics.py.
    metrics_mod.observe(
        REQUEST_LATENCY.labels(method="GET", path="/analytics"),
        time.perf_counter() - start,
        trace_id_hex,
    )
    REQUEST_COUNT.labels(method="GET", path="/analytics", status=str(status)).inc()

    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    log_json(
        span,
        level="error" if status == 500 else ("warning" if status == 429 else "info"),
        msg=f"analytics {status} processed={(processed or b'0').decode()} dur={duration_ms}ms",
        path="/analytics",
        status=status,
        duration_ms=duration_ms,
    )

    if status == 429:
        return Response("overloaded\n", status=429, headers={"Retry-After": "1"})
    if status == 500:
        return Response("analytics query failed\n", status=500)
    count = processed.decode() if processed else "0"
    return Response(f"analytics query ok orders_processed={count}\n", status=200)


@app.route("/metrics")
def metrics():
    payload, content_type = metrics_mod.render()
    return Response(payload, mimetype=content_type)


@app.route("/healthz")
def healthz():
    """Liveness: this process only. Deliberately does not touch dependencies -
    a liveness probe that fails on a slow database restarts every healthy pod
    exactly when the database can least afford a reconnect storm."""
    return Response("ok\n", status=200)


@app.route("/readyz")
def readyz():
    """Readiness: can this process serve right now? Dependency health belongs
    here, where failing means "stop sending traffic" rather than "restart"."""
    problems = []
    if db is not None and not db.healthy():
        problems.append("postgres")
    if redis_client is not None:
        try:
            redis_client.ping()
        except Exception:
            problems.append("redis")
    if problems:
        return Response(json.dumps({"ready": False, "failing": problems}) + "\n", status=503)
    return Response(json.dumps({"ready": True}) + "\n", status=200)


def init_clients(run_init_db: bool = True):
    """Shared startup for both entrypoints (this module and worker.py)."""
    global db

    iters = workload.calibrate()
    CPU_CALIBRATION.set(iters)

    if DATABASE_URL:
        connect_with_retry(DATABASE_URL)
        db = PooledPostgres(
            DATABASE_URL,
            minconn=DB_POOL_MIN,
            maxconn=DB_POOL_MAX,
            statement_timeout_ms=DB_STATEMENT_TIMEOUT_MS,
            acquire_timeout_s=DB_ACQUIRE_TIMEOUT_MS / 1000.0,
            application_name=f"{SERVICE_NAME}@{K8S_POD_NAME or 'local'}",
        )
        if run_init_db:
            init_db()


init_clients()


if __name__ == "__main__":
    # Local debugging only - in the cluster this is served by gunicorn.
    app.run(host="0.0.0.0", port=PORT, threaded=True)
