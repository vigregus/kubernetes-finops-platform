import json
import logging
import os
import random
import sys
import threading
import time
import uuid

import redis as redis_lib
from common import metrics as metrics_mod
from common import workload
from common.cache import CacheResult, ResponseCache
import psycopg2
from common.db import PoolBusy, PooledPostgres, connect_with_retry
from common.resilience import CircuitBreaker, CircuitOpen, LoadShedder, RetryBudget, Shed, call_with_retry
from confluent_kafka import Producer
from flask import Flask, Response, request
from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode
from prometheus_client import Counter, Gauge, Histogram

SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "checkout")
SERVICE_VERSION = os.environ.get("SERVICE_VERSION", "0.1.0")
# Downward API fields (see charts/app1/templates/deployment.yaml) - without
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
ERROR_RATE = float(os.environ.get("ERROR_RATE", "0.02"))

# Per-request resource shape, in milliseconds of CPU / MB of allocation / ms
# of simulated downstream wait. See apps/common/workload.py for why this is
# expressed in time rather than in a fixed iteration count.
CHECKOUT_PROFILE = workload.Profile.from_env(os.environ, "PROFILE_CHECKOUT")
LOOKUP_PROFILE = workload.Profile.from_env(os.environ, "PROFILE_LOOKUP")

# Concurrency cap past which requests are rejected rather than queued.
MAX_IN_FLIGHT = int(os.environ.get("MAX_IN_FLIGHT", "64"))

DATABASE_URL = os.environ.get("DATABASE_URL")
DATABASE_NAME = os.environ.get("DATABASE_NAME", SERVICE_NAME)
DB_POOL_MIN = int(os.environ.get("DB_POOL_MIN", "1"))
DB_POOL_MAX = int(os.environ.get("DB_POOL_MAX", "8"))
DB_STATEMENT_TIMEOUT_MS = int(os.environ.get("DB_STATEMENT_TIMEOUT_MS", "5000"))
# How long a request waits for a free connection before being shed.
# Long enough to absorb a contention spike, short enough that a
# saturated pod answers "too busy" instead of holding the caller.
DB_ACQUIRE_TIMEOUT_MS = int(os.environ.get("DB_ACQUIRE_TIMEOUT_MS", "250"))

REDIS_URL = os.environ.get("REDIS_URL")
REDIS_POOL_MAX = int(os.environ.get("REDIS_POOL_MAX", "32"))
REDIS_TIMEOUT_S = float(os.environ.get("REDIS_TIMEOUT_S", "2"))

# Response cache. TTL jitter is not cosmetic: identical TTLs expire together
# and turn steady traffic into a sawtooth against Postgres.
CACHE_TTL_S = float(os.environ.get("CACHE_TTL_S", "60"))
CACHE_NEGATIVE_TTL_S = float(os.environ.get("CACHE_NEGATIVE_TTL_S", "10"))
CACHE_JITTER_RATIO = float(os.environ.get("CACHE_JITTER_RATIO", "0.2"))
# Warm before accepting traffic. A pod that starts cold sends its first
# requests straight to the database, so scaling out under load briefly makes
# database pressure worse - the opposite of what scaling out is for.
CACHE_WARM_ON_START = os.environ.get("CACHE_WARM_ON_START", "true").lower() == "true"
CACHE_WARM_KEYS = int(os.environ.get("CACHE_WARM_KEYS", "500"))

KAFKA_BOOTSTRAP_SERVERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS")
KAFKA_TOPIC = os.environ.get("KAFKA_TOPIC", "orders")
KAFKA_LINGER_MS = int(os.environ.get("KAFKA_LINGER_MS", "20"))
KAFKA_BATCH_SIZE = int(os.environ.get("KAFKA_BATCH_SIZE", "65536"))
KAFKA_COMPRESSION = os.environ.get("KAFKA_COMPRESSION", "lz4")
KAFKA_QUEUE_MAX_MESSAGES = int(os.environ.get("KAFKA_QUEUE_MAX_MESSAGES", "100000"))

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
# status is a label here on purpose. The latency SLI has to be computed over
# *served* requests: a 429 answered in 2ms is not evidence that the service is
# fast, it is evidence that it refused. Measured on this cluster, 5319 of 9553
# 429s reached this histogram (the rest are shed before the handler runs), and
# without the label they would drag the latency SLI upward at exactly the
# moment the service was failing to serve.
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "path", "status"],
)
DB_LATENCY = Histogram("db_query_duration_seconds", "Postgres query latency in seconds")
CACHE_LATENCY = Histogram("cache_call_duration_seconds", "Redis call latency in seconds")
KAFKA_ENQUEUE_LATENCY = Histogram(
    "kafka_enqueue_duration_seconds", "Time spent handing a message to the producer queue"
)
# Shed requests are the signal that a pod is at capacity. Without this being
# its own counter they would be indistinguishable from application errors,
# and "the service is saturated" would look identical to "the service is
# broken" on every dashboard and in every experiment result.
REQUESTS_SHED = Counter("http_requests_shed_total", "Requests rejected because the pod was at capacity")
IN_FLIGHT = Gauge("http_requests_in_flight", "Requests currently being served by this process")
KAFKA_DELIVERY_ERRORS = Counter("kafka_delivery_errors_total", "Kafka messages the broker never acked")
BREAKER_STATE = Gauge("dependency_circuit_state", "0=closed 1=half-open 2=open", ["dependency"])
CACHE_REQUESTS = Counter(
    "cache_requests_total", "Response cache outcomes", ["cache", "result"]
)
CACHE_WARMED = Gauge("cache_warmed_keys", "Keys loaded into the cache at startup")
CACHE_READY = Gauge("cache_warm_complete", "1 once startup warming has finished")
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


class PaymentValidationError(Exception):
    pass


def log_json(span, level="info", **fields):
    # Grafana's trace-to-logs correlation looks up VictoriaLogs by the
    # trace_id *field* (VictoriaLogs auto-parses JSON lines and promotes
    # top-level keys to real fields), so every step needs its own log line
    # with its own span's trace/span ID - one summary line at the end of
    # the request doesn't let you jump from a specific span (e.g.
    # checkout.persist_order) to what that step actually did.
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
RETRY_BUDGET = RetryBudget(ratio=float(os.environ.get("RETRY_BUDGET_RATIO", "0.1")))
DB_BREAKER = CircuitBreaker(
    "postgres",
    failure_threshold=int(os.environ.get("DB_BREAKER_THRESHOLD", "5")),
    recovery_timeout_s=float(os.environ.get("DB_BREAKER_RECOVERY_S", "10")),
    neutral_exceptions=(PoolBusy,),
)

# Redis gets a bounded pool rather than a connection per call, for the same
# reason Postgres does: otherwise the connection count tracks request rate.
redis_client = (
    redis_lib.Redis(
        connection_pool=redis_lib.ConnectionPool.from_url(
            REDIS_URL, max_connections=REDIS_POOL_MAX, socket_timeout=REDIS_TIMEOUT_S
        )
    )
    if REDIS_URL
    else None
)

order_cache: ResponseCache | None = None
_warm_complete = not CACHE_WARM_ON_START

db: PooledPostgres | None = None
kafka_producer: Producer | None = None
_shutting_down = threading.Event()

REGIONS = ["us-east", "us-west", "eu-central", "ap-south"]

# Always present after init_db() - gives /checkout/lookup a real row to
# find without depending on request ordering or cross-request state.
SEED_ORDER_ID = "00000000-0000-0000-0000-000000000001"


def order_id_for(key: str) -> str:
    """Derive a stable order id from a numeric idempotency key.

    The load generator needs to *read back* the orders it creates, across a
    key space large enough for caching to mean anything. Without a derivable
    id it can only re-read whatever id it happens to be told about, which is
    how the previous profile ended up reading one single row 70% of the time
    against a table of 42k: hit ratio was ~100% by construction, maxmemory
    never bound, and every caching strategy measured identically.

    Formatting the key into the UUID lets both sides compute the same id with
    no discovery round trip and no shared state.
    """
    if key.isdigit():
        return f"00000000-0000-4000-8000-{int(key):012d}"
    return str(uuid.uuid4())


def init_db():
    if not db:
        return
    with db.cursor(commit=True) as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id UUID PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                status TEXT NOT NULL
            )
            """
        )
        cur.execute(
            "INSERT INTO orders (id, status) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
            (SEED_ORDER_ID, "placed"),
        )


def cache_lookup(idempotency_key):
    if not redis_client:
        return None
    start = time.perf_counter()
    with tracer.start_as_current_span("checkout.cache_lookup") as span:
        span.set_attribute("db.system", "redis")
        span.set_attribute("db.operation", "GET")
        span.set_attribute("db.statement", f"GET {idempotency_key}")
        value = redis_client.get(idempotency_key)
        log_json(span, level="debug", _msg="redis GET", key=idempotency_key, hit=value is not None)
    CACHE_LATENCY.observe(time.perf_counter() - start)
    return value


def cache_store(idempotency_key, order_id):
    if not redis_client:
        return
    start = time.perf_counter()
    with tracer.start_as_current_span("checkout.cache_store") as span:
        span.set_attribute("db.system", "redis")
        span.set_attribute("db.operation", "SET")
        span.set_attribute("db.statement", f"SET {idempotency_key}")
        redis_client.set(idempotency_key, order_id, ex=300)
        log_json(span, level="debug", _msg="redis SET", key=idempotency_key, ttl_seconds=300)
    CACHE_LATENCY.observe(time.perf_counter() - start)


def persist_order(order_id, span_ctx_for_sql):
    if not db:
        return
    start = time.perf_counter()
    with tracer.start_as_current_span("checkout.persist_order") as span:
        # A SQL comment carrying the trace ID is what makes Postgres's own
        # log_statement output (see charts/app1/templates/postgres.yaml)
        # greppable by trace_id - without it, the query shows up in
        # Postgres's log with zero link back to the request that ran it.
        # ON CONFLICT, not a plain INSERT: order ids are derived from the
        # idempotency key (order_id_for), so a repeated key is a repeated id.
        # The Redis idempotency lookup skips most of these, but it is a cache -
        # it evicts, especially now that it shares a bounded allkeys-lru Redis
        # with the response cache. Leaning on it for correctness produced
        # duplicate-key violations under load, five of which in a row opened
        # the circuit breaker and turned 24 real errors into 1031 cascading
        # 503s. Idempotency has to be enforced where the data lives.
        sql = (
            f"/* trace_id={span_ctx_for_sql} */ "
            "INSERT INTO orders (id, status) VALUES (%s, %s) "
            "ON CONFLICT (id) DO NOTHING"
        )
        span.set_attribute("db.system", "postgresql")
        span.set_attribute("db.name", DATABASE_NAME)
        span.set_attribute("db.operation", "INSERT")
        span.set_attribute("db.sql.table", "orders")
        span.set_attribute("db.statement", sql)

        def _write():
            with db.cursor(commit=True) as cur:
                cur.execute(sql, (order_id, "placed"))

        try:
            # Retries are bounded by a shared budget so a struggling database
            # cannot be turned into an outage by its own clients retrying.
            call_with_retry(
                _write,
                attempts=2,
                budget=RETRY_BUDGET,
                breaker=DB_BREAKER,
                retry_on=(psycopg2.Error,),
            )
        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            log_json(span, level="error", _msg="postgres INSERT failed", table="orders", order_id=order_id, error=str(e))
            raise
        finally:
            BREAKER_STATE.labels(dependency="postgres").set(DB_BREAKER.state_code)
        log_json(span, level="debug", _msg="postgres INSERT", table="orders", order_id=order_id)
        # The row this key would have cached is now stale. Dropping it is
        # safer than writing the new value through: the write may still be
        # rolled back by an outer failure, and a wrong cached value outlives
        # the request that created it.
        if order_cache is not None:
            order_cache.invalidate(f"order:{order_id}")
    DB_LATENCY.observe(time.perf_counter() - start)


def lookup_order_cached(order_id_str, span_ctx_for_sql, slow=False):
    """Read through the cache, except on the deliberately-slow path.

    slow=1 exists to produce a genuine Postgres slow-query log line; serving
    it from cache would quietly remove the thing it was added to demonstrate.
    """
    if order_cache is None or slow:
        row = lookup_order(order_id_str, span_ctx_for_sql, slow=slow)
        CACHE_REQUESTS.labels(cache="order", result="bypass").inc()
        return row

    def _load():
        row = lookup_order(order_id_str, span_ctx_for_sql, slow=False)
        if row is None:
            return None
        return {"id": str(row[0]), "status": row[1], "created_at": str(row[2])}

    value, result = order_cache.get_or_load(f"order:{order_id_str}", _load)
    CACHE_REQUESTS.labels(cache="order", result=result).inc()
    if value is None:
        return None
    return (value["id"], value["status"], value["created_at"])


def lookup_order(order_id_str, span_ctx_for_sql, slow=False):
    # A real read path, not just the write path persist_order covers -
    # three genuinely distinct outcomes, not simulated with a coin flip:
    # (1) found - a real row comes back, (2) valid UUID but no such row -
    # a real empty result, not an error, (3) not a UUID at all - Postgres
    # itself rejects the value (invalid input syntax for type uuid), a
    # real driver-level exception, not an app-level check we chose to add.
    if not db:
        return None
    start = time.perf_counter()
    with tracer.start_as_current_span("checkout.lookup_order") as span:
        sql = f"/* trace_id={span_ctx_for_sql} */ SELECT id, status, created_at FROM orders WHERE id = %s"
        span.set_attribute("db.system", "postgresql")
        span.set_attribute("db.name", DATABASE_NAME)
        span.set_attribute("db.operation", "SELECT")
        span.set_attribute("db.sql.table", "orders")
        span.set_attribute("db.statement", sql)
        try:
            with db.cursor() as cur:
                if slow:
                    # Deliberately crosses Postgres's own 200ms
                    # log_min_duration_statement threshold (see
                    # charts/app1/templates/postgres.yaml) so a real
                    # slow-query log line - not just a fast SELECT -
                    # shows up on the Postgres side too, distinct
                    # from the always-logged writes.
                    cur.execute(f"/* trace_id={span_ctx_for_sql} */ SELECT pg_sleep(0.25)")
                cur.execute(sql, (order_id_str,))
                row = cur.fetchone()
        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            log_json(
                span,
                level="error",
                _msg="postgres SELECT failed",
                table="orders",
                order_id=order_id_str,
                error_type=type(e).__name__,
                error=str(e),
            )
            DB_LATENCY.observe(time.perf_counter() - start)
            raise

        if row is None:
            span.set_attribute("db.rows_returned", 0)
            log_json(span, level="warning", _msg="postgres SELECT no rows", table="orders", order_id=order_id_str)
        else:
            span.set_attribute("db.rows_returned", 1)
            log_json(span, level="debug", _msg="postgres SELECT", table="orders", order_id=order_id_str, status=row[1])
    DB_LATENCY.observe(time.perf_counter() - start)
    return row


def _on_delivery(err, msg):
    if err is not None:
        KAFKA_DELIVERY_ERRORS.inc()


def publish_order_event(order_id):
    if not kafka_producer:
        return
    start = time.perf_counter()
    with tracer.start_as_current_span("checkout.publish_event", kind=SpanKind.PRODUCER) as span:
        span.set_attribute("messaging.system", "kafka")
        span.set_attribute("messaging.destination", KAFKA_TOPIC)
        span.set_attribute("messaging.destination_kind", "topic")
        span.set_attribute("messaging.operation", "publish")
        headers = {}
        propagate.inject(headers)
        kafka_headers = [(k, v.encode("utf-8")) for k, v in headers.items()]

        # amount_cents/region are the fields analytics writes into its
        # order_events table - without them here that table would have
        # nothing to derive them from and the consumer would be inventing
        # data rather than recording what actually flowed through.
        event = {
            "order_id": order_id,
            "amount_cents": random.randint(500, 50000),
            "region": random.choice(REGIONS),
        }
        # Enqueue only. The previous version called flush(2.0) here, which
        # blocked the request until the broker acked - capping producer
        # throughput at (concurrency / round-trip latency) regardless of what
        # the broker could actually take, and putting a 2s worst case on a
        # request that has already done its real work. Delivery reports are
        # served by the background poll thread instead, and failures surface
        # as kafka_delivery_errors_total rather than as request latency.
        try:
            kafka_producer.produce(
                KAFKA_TOPIC,
                value=json.dumps(event).encode("utf-8"),
                headers=kafka_headers,
                on_delivery=_on_delivery,
            )
        except BufferError:
            # The local queue is full: the broker is slower than we are
            # producing. Dropping here (and counting it) is deliberate -
            # blocking would convert a Kafka problem into a checkout outage.
            KAFKA_DELIVERY_ERRORS.inc()
            log_json(span, level="warning", _msg="kafka produce queue full", topic=KAFKA_TOPIC, order_id=order_id)
        log_json(span, level="debug", _msg="kafka enqueued", topic=KAFKA_TOPIC, order_id=order_id)
    KAFKA_ENQUEUE_LATENCY.observe(time.perf_counter() - start)


@app.route("/", methods=["GET"])
@app.route("/checkout", methods=["GET"])
def checkout():
    try:
        with SHEDDER.admit():
            IN_FLIGHT.set(SHEDDER.in_flight)
            return _checkout()
    except Shed:
        REQUESTS_SHED.inc()
        REQUEST_COUNT.labels(method="GET", path="/checkout", status="429").inc()
        return Response("overloaded\n", status=429, headers={"Retry-After": "1"})
    finally:
        IN_FLIGHT.set(SHEDDER.in_flight)


def _checkout():
    start = time.perf_counter()
    # Werkzeug reconstructs header names title-cased ("Traceparent"), but
    # the W3C propagator's default getter does a case-sensitive lookup for
    # the literal lowercase "traceparent" - without lowering the keys this
    # silently finds nothing and always starts a new root span instead of
    # continuing whatever called us (e.g. ingress-nginx).
    ctx = propagate.extract({k.lower(): v for k, v in request.headers.items()})
    with tracer.start_as_current_span("checkout.process", context=ctx, kind=SpanKind.SERVER) as span:
        span.set_attribute("http.method", "GET")
        span.set_attribute("http.route", "/checkout")
        span.set_attribute("http.target", request.path)
        span.set_attribute("http.scheme", request.scheme)
        trace_id_hex = format(span.get_span_context().trace_id, "032x")
        raw_key = request.args.get("key", uuid.uuid4().hex)
        idempotency_key = f"checkout:idem:{raw_key}"
        cached_order_id = cache_lookup(idempotency_key)

        with tracer.start_as_current_span("checkout.validate_payment"):
            CHECKOUT_PROFILE.run()

        order_id = None
        try:
            if random.random() < ERROR_RATE:
                raise PaymentValidationError("payment gateway declined the transaction")
            order_id = cached_order_id.decode() if cached_order_id else order_id_for(raw_key)
            if not cached_order_id:
                persist_order(order_id, trace_id_hex)
                publish_order_event(order_id)
                cache_store(idempotency_key, order_id)
            status = 200
        except PoolBusy as e:
            # At capacity, not broken: the database is fine, this pod simply
            # has no free connection. Reporting it as 5xx would blame the
            # dependency and, worse, make a saturated service look like a
            # failing one in every experiment result.
            status = 429
            span.set_attribute("checkout.shed_reason", "db_pool")
            log_json(span, level="warning", _msg=f"checkout shed at db pool: {e}", path="/checkout")
        except CircuitOpen as e:
            # The dependency is known-bad; fail fast and say so distinctly
            # rather than waiting for a timeout we already expect.
            status = 503
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            log_json(span, level="error", _msg="checkout dependency circuit open", path="/checkout", error=str(e))
        except Exception as e:
            status = 500
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            log_json(
                span,
                level="error",
                _msg="checkout request failed",
                path="/checkout",
                error_type=type(e).__name__,
                error=str(e),
            )

        span.set_attribute("http.status_code", status)

    # exemplar: OpenMetrics lets a histogram observation carry one sample
    # (trace_id here) alongside the value it fell into that bucket - each
    # bucket keeps whichever exemplar was freshest as of the last scrape.
    # That's what lets Grafana show a *different*, actually representative
    # trace for the p50 point vs the p99 point on the same graph. Attached
    # only when the process is exporting a format that can carry it - see
    # apps/common/metrics.py.
    metrics_mod.observe(
        REQUEST_LATENCY.labels(method="GET", path="/checkout", status=str(status)),
        time.perf_counter() - start,
        trace_id_hex,
    )
    REQUEST_COUNT.labels(method="GET", path="/checkout", status=str(status)).inc()

    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    # Everything worth knowing goes in _msg, because that is the only field
    # a log viewer renders inline - a message of "checkout request handled"
    # forces opening every single line to learn anything. The structured
    # fields stay alongside it so they remain filterable.
    log_json(
        span,
        level="error" if status >= 500 else ("warning" if status == 429 else "info"),
        msg=(
            f"checkout {status} order={order_id or '-'} "
            f"cache={'hit' if cached_order_id else 'miss'} dur={duration_ms}ms"
        ),
        path="/checkout",
        status=status,
        order_id=order_id if status == 200 else None,
        duration_ms=duration_ms,
    )

    if status == 429:
        return Response("overloaded\n", status=429, headers={"Retry-After": "1"})
    if status == 503:
        return Response("checkout dependency unavailable\n", status=503)
    if status == 500:
        return Response("checkout failed\n", status=500)
    return Response(f"checkout ok order_id={order_id}\n", status=200)


@app.route("/checkout/lookup", methods=["GET"])
def checkout_lookup():
    try:
        with SHEDDER.admit():
            IN_FLIGHT.set(SHEDDER.in_flight)
            return _checkout_lookup()
    except Shed:
        REQUESTS_SHED.inc()
        REQUEST_COUNT.labels(method="GET", path="/checkout/lookup", status="429").inc()
        return Response("overloaded\n", status=429, headers={"Retry-After": "1"})
    finally:
        IN_FLIGHT.set(SHEDDER.in_flight)


def _checkout_lookup():
    start = time.perf_counter()
    ctx = propagate.extract({k.lower(): v for k, v in request.headers.items()})
    order_id_str = request.args.get("id", SEED_ORDER_ID)
    slow = request.args.get("slow") == "1"
    with tracer.start_as_current_span(
        "checkout.lookup_order_request", context=ctx, kind=SpanKind.SERVER
    ) as span:
        span.set_attribute("http.method", "GET")
        span.set_attribute("http.route", "/checkout/lookup")
        span.set_attribute("http.target", request.path)
        span.set_attribute("http.scheme", request.scheme)
        trace_id_hex = format(span.get_span_context().trace_id, "032x")

        LOOKUP_PROFILE.run()

        row = None
        try:
            row = lookup_order_cached(order_id_str, trace_id_hex, slow=slow)
            status = 200 if row is not None else 404
        except PoolBusy as e:
            # Previously this landed in the generic handler below and was
            # reported as 400 - blaming the caller's input for this pod
            # being out of connections.
            status = 429
            log_json(span, level="warning", _msg=f"lookup shed at db pool: {e}", path="/checkout/lookup")
        except Exception as e:
            status = 400
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            log_json(
                span,
                level="error",
                _msg="checkout lookup failed",
                path="/checkout/lookup",
                order_id=order_id_str,
                error_type=type(e).__name__,
                error=str(e),
            )

        span.set_attribute("http.status_code", status)

    metrics_mod.observe(
        REQUEST_LATENCY.labels(method="GET", path="/checkout/lookup", status=str(status)),
        time.perf_counter() - start,
        trace_id_hex,
    )
    REQUEST_COUNT.labels(method="GET", path="/checkout/lookup", status=str(status)).inc()

    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    log_json(
        span,
        level="error" if status == 400 else ("warning" if status in (404, 429) else "info"),
        msg=f"lookup {status} order={order_id_str} slow={int(slow)} dur={duration_ms}ms",
        path="/checkout/lookup",
        order_id=order_id_str,
        status=status,
        duration_ms=duration_ms,
    )

    if status == 429:
        return Response("overloaded\n", status=429, headers={"Retry-After": "1"})
    if status == 400:
        return Response(f"invalid order id: {order_id_str}\n", status=400)
    if status == 404:
        return Response(f"order not found: {order_id_str}\n", status=404)
    return Response(f"order {row[0]} status={row[1]} created_at={row[2]}\n", status=200)


@app.route("/admin/cache/invalidate", methods=["POST"])
def invalidate_cache():
    """Bump the cache key-space version.

    Unauthenticated on purpose - this is a load stand, and the experiment
    runner needs to force a cold cache between variants to compare warm and
    cold behaviour. It would need protecting in anything real.
    """
    if order_cache is None:
        return Response(json.dumps({"invalidated": False, "reason": "no cache"}) + "\n", status=503)
    version = order_cache.invalidate_all()
    logger.info(json.dumps({"_msg": f"cache invalidated, now at v{version}", "level": "info", "cache_version": version}))
    return Response(json.dumps({"invalidated": True, "version": version}) + "\n", status=200)


@app.route("/metrics")
def metrics():
    payload, content_type = metrics_mod.render()
    return Response(payload, mimetype=content_type)


@app.route("/healthz")
def healthz():
    """Liveness: is this process itself alive?

    Deliberately does not touch dependencies. A liveness probe that fails
    when Postgres is slow restarts every healthy pod in the deployment at
    exactly the moment the database can least afford a reconnect storm.
    """
    return Response("ok\n", status=200)


@app.route("/readyz")
def readyz():
    """Readiness: can this process actually serve a request right now?

    This is where dependency health belongs - a pod that cannot reach its
    database should stop receiving traffic without being restarted.
    """
    if not _warm_complete:
        # Deliberate: an unwarmed pod would serve its first traffic straight
        # into Postgres. Staying unready until warm is what makes scale-out
        # help immediately instead of adding database load first.
        return Response(json.dumps({"ready": False, "warming": True}) + "\n", status=503)

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


def _kafka_poll_loop():
    """Serve delivery callbacks without blocking any request.

    poll(0) rather than poll(timeout): a blocking poll would hold the gevent
    hub for its whole timeout and stall every other greenlet in the worker.
    """
    while not _shutting_down.is_set():
        try:
            kafka_producer.poll(0)
        except Exception:
            pass
        time.sleep(0.05)


def _shutdown(signum=None, frame=None):
    """Drain on SIGTERM so scale-down does not show up as errors.

    Kubernetes sends SIGTERM, waits terminationGracePeriodSeconds, then
    SIGKILLs. Anything still buffered in the Kafka producer at that point is
    silently lost, and any in-flight request dies mid-response - which would
    make every autoscaling strategy look worse the more often it scales in.
    """
    _shutting_down.set()
    try:
        if kafka_producer is not None:
            kafka_producer.flush(10.0)
    except Exception:
        pass
    try:
        if db is not None:
            db.close()
    except Exception:
        pass


def _warm_cache() -> int:
    """Preload the most recent orders, newest first.

    Recency is the cheapest available proxy for "likely to be read" without
    tracking access frequency; the point is to start with a populated cache,
    not to predict perfectly.
    """
    with db.cursor() as cur:
        cur.execute(
            "SELECT id, status, created_at FROM orders ORDER BY created_at DESC LIMIT %s",
            (CACHE_WARM_KEYS,),
        )
        rows = cur.fetchall()
    return order_cache.warm(
        (
            f"order:{r[0]}",
            {"id": str(r[0]), "status": r[1], "created_at": str(r[2])},
        )
        for r in rows
    )


def _init_process():
    """Per-worker startup. Runs once per gunicorn worker (preload_app=False)."""
    global db, kafka_producer

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
        init_db()

    global order_cache, _warm_complete
    if redis_client is not None:
        order_cache = ResponseCache(
            redis_client,
            namespace="checkout:cache:order",
            ttl_s=CACHE_TTL_S,
            negative_ttl_s=CACHE_NEGATIVE_TTL_S,
            jitter_ratio=CACHE_JITTER_RATIO,
        )
        if CACHE_WARM_ON_START and db is not None:
            _warm_complete = False
            try:
                warmed = _warm_cache()
                CACHE_WARMED.set(warmed)
                logger.info(json.dumps({"_msg": f"cache warmed with {warmed} orders", "level": "info", "warmed": warmed}))
            except Exception as exc:  # noqa: BLE001 - warming must never block startup
                logger.warning(json.dumps({"_msg": f"cache warm failed: {exc}", "level": "warning"}))
            finally:
                _warm_complete = True
                CACHE_READY.set(1)

    # Reflect the real state rather than only the happy path: with no Redis
    # configured there is nothing to warm, and the pod is ready immediately.
    CACHE_READY.set(1 if _warm_complete else 0)

    if KAFKA_BOOTSTRAP_SERVERS:
        kafka_producer = Producer(
            {
                "bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS,
                # Batch rather than send-per-message: linger briefly so the
                # producer can coalesce, which is the difference between a
                # few hundred and a few tens of thousands of messages/sec.
                "linger.ms": KAFKA_LINGER_MS,
                "batch.size": KAFKA_BATCH_SIZE,
                "compression.type": KAFKA_COMPRESSION,
                "queue.buffering.max.messages": KAFKA_QUEUE_MAX_MESSAGES,
                "enable.idempotence": True,
            }
        )
        threading.Thread(target=_kafka_poll_loop, daemon=True).start()

    BREAKER_STATE.labels(dependency="postgres").set(DB_BREAKER.state_code)

    import atexit
    import signal

    atexit.register(_shutdown)
    try:
        signal.signal(signal.SIGTERM, _shutdown)
    except ValueError:
        # Not on the main thread (some worker classes) - atexit still covers it.
        pass


_init_process()


if __name__ == "__main__":
    # Local debugging only. In the cluster this module is served by gunicorn
    # (see apps/common/gunicorn_conf.py); the Werkzeug server below is
    # single-process and explicitly not for load.
    app.run(host="0.0.0.0", port=PORT, threaded=True)
