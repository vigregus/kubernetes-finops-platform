import hashlib
import json
import logging
import os
import random
import sys
import time
import uuid

import psycopg2
import redis as redis_lib
from confluent_kafka import Producer
from flask import Flask, Response, request
from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

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
LATENCY_MS_MEAN = float(os.environ.get("LATENCY_MS_MEAN", "40"))
LATENCY_MS_JITTER = float(os.environ.get("LATENCY_MS_JITTER", "15"))
ERROR_RATE = float(os.environ.get("ERROR_RATE", "0.02"))
WORK_ITERATIONS = int(os.environ.get("WORK_ITERATIONS", "150000"))

DATABASE_URL = os.environ.get("DATABASE_URL")
DATABASE_NAME = os.environ.get("DATABASE_NAME", SERVICE_NAME)
REDIS_URL = os.environ.get("REDIS_URL")
KAFKA_BOOTSTRAP_SERVERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS")
KAFKA_TOPIC = os.environ.get("KAFKA_TOPIC", "orders")

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
DB_LATENCY = Histogram("db_query_duration_seconds", "Postgres query latency in seconds")
CACHE_LATENCY = Histogram("cache_call_duration_seconds", "Redis call latency in seconds")
KAFKA_LATENCY = Histogram("kafka_publish_duration_seconds", "Kafka publish latency in seconds")

# Routed through logging (not bare print) so ERROR lines are a real,
# filterable severity instead of just another line of JSON text - level
# is its own field precisely so "show me only errors" is a VictoriaLogs
# field match, not a string search on message content.
logger = logging.getLogger(SERVICE_NAME)
logger.setLevel(logging.INFO)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(_handler)
logger.propagate = False

_LOG_LEVELS = {"info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}


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


redis_client = redis_lib.from_url(REDIS_URL, socket_timeout=2) if REDIS_URL else None
kafka_producer = (
    Producer({"bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS}) if KAFKA_BOOTSTRAP_SERVERS else None
)



# Always present after init_db() - gives /checkout/lookup a real row to
# find without depending on request ordering or cross-request state.
SEED_ORDER_ID = "00000000-0000-0000-0000-000000000001"


def init_db():
    if not DATABASE_URL:
        return
    with psycopg2.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
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
        conn.commit()


def cache_lookup(idempotency_key):
    if not redis_client:
        return None
    start = time.perf_counter()
    with tracer.start_as_current_span("checkout.cache_lookup") as span:
        span.set_attribute("db.system", "redis")
        span.set_attribute("db.operation", "GET")
        span.set_attribute("db.statement", f"GET {idempotency_key}")
        value = redis_client.get(idempotency_key)
        log_json(span, _msg="redis GET", key=idempotency_key, hit=value is not None)
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
        log_json(span, _msg="redis SET", key=idempotency_key, ttl_seconds=300)
    CACHE_LATENCY.observe(time.perf_counter() - start)


def persist_order(order_id, span_ctx_for_sql):
    if not DATABASE_URL:
        return
    start = time.perf_counter()
    with tracer.start_as_current_span("checkout.persist_order") as span:
        # A SQL comment carrying the trace ID is what makes Postgres's own
        # log_statement output (see charts/app1/templates/postgres.yaml)
        # greppable by trace_id - without it, the query shows up in
        # Postgres's log with zero link back to the request that ran it.
        sql = (
            f"/* trace_id={span_ctx_for_sql} */ "
            "INSERT INTO orders (id, status) VALUES (%s, %s)"
        )
        span.set_attribute("db.system", "postgresql")
        span.set_attribute("db.name", DATABASE_NAME)
        span.set_attribute("db.operation", "INSERT")
        span.set_attribute("db.sql.table", "orders")
        span.set_attribute("db.statement", sql)
        try:
            with psycopg2.connect(DATABASE_URL) as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, (order_id, "placed"))
                conn.commit()
        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            log_json(span, level="error", _msg="postgres INSERT failed", table="orders", order_id=order_id, error=str(e))
            raise
        log_json(span, _msg="postgres INSERT", table="orders", order_id=order_id)
    DB_LATENCY.observe(time.perf_counter() - start)


def lookup_order(order_id_str, span_ctx_for_sql, slow=False):
    # A real read path, not just the write path persist_order covers -
    # three genuinely distinct outcomes, not simulated with a coin flip:
    # (1) found - a real row comes back, (2) valid UUID but no such row -
    # a real empty result, not an error, (3) not a UUID at all - Postgres
    # itself rejects the value (invalid input syntax for type uuid), a
    # real driver-level exception, not an app-level check we chose to add.
    if not DATABASE_URL:
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
            with psycopg2.connect(DATABASE_URL) as conn:
                with conn.cursor() as cur:
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
            log_json(span, _msg="postgres SELECT", table="orders", order_id=order_id_str, status=row[1])
    DB_LATENCY.observe(time.perf_counter() - start)
    return row


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

        delivery = {}

        def on_delivery(err, msg):
            if err is not None:
                delivery["error"] = str(err)
            else:
                delivery["partition"] = msg.partition()
                delivery["offset"] = msg.offset()

        kafka_producer.produce(
            KAFKA_TOPIC,
            value=json.dumps({"order_id": order_id}).encode("utf-8"),
            headers=kafka_headers,
            on_delivery=on_delivery,
        )
        # Blocks until the broker acks (or the 2s timeout) so the delivery
        # report (partition/offset, or the error) is available to log
        # immediately - poll(0) alone doesn't guarantee the callback has
        # fired yet.
        kafka_producer.flush(2.0)
        if "error" in delivery:
            span.set_status(Status(StatusCode.ERROR, delivery["error"]))
            log_json(span, level="error", _msg="kafka produce failed", topic=KAFKA_TOPIC, order_id=order_id, **delivery)
        else:
            span.set_attribute("messaging.kafka.partition", delivery.get("partition", -1))
            log_json(span, _msg="kafka produce", topic=KAFKA_TOPIC, order_id=order_id, **delivery)
    KAFKA_LATENCY.observe(time.perf_counter() - start)


def validate_payment():
    # Burns real CPU so container_cpu_usage_seconds_total carries a signal
    # that scales with traffic, instead of every request costing ~nothing.
    digest = hashlib.sha256()
    for _ in range(WORK_ITERATIONS):
        digest.update(b"checkout")
    return digest.hexdigest()


@app.route("/", methods=["GET"])
@app.route("/checkout", methods=["GET"])
def checkout():
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
        idempotency_key = f"checkout:idem:{request.args.get('key', uuid.uuid4().hex)}"
        cached_order_id = cache_lookup(idempotency_key)

        with tracer.start_as_current_span("checkout.validate_payment"):
            validate_payment()

        delay_seconds = max(0.0, random.gauss(LATENCY_MS_MEAN, LATENCY_MS_JITTER)) / 1000.0
        time.sleep(delay_seconds)

        order_id = None
        try:
            if random.random() < ERROR_RATE:
                raise PaymentValidationError("payment gateway declined the transaction")
            order_id = cached_order_id.decode() if cached_order_id else str(uuid.uuid4())
            if not cached_order_id:
                persist_order(order_id, trace_id_hex)
                publish_order_event(order_id)
                cache_store(idempotency_key, order_id)
            status = 200
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

    REQUEST_LATENCY.labels(method="GET", path="/checkout").observe(time.perf_counter() - start)
    REQUEST_COUNT.labels(method="GET", path="/checkout", status=str(status)).inc()

    log_json(
        span,
        level="error" if status == 500 else "info",
        msg="checkout request handled",
        path="/checkout",
        status=status,
        order_id=order_id if status == 200 else None,
        duration_ms=round((time.perf_counter() - start) * 1000, 2),
    )

    if status == 500:
        return Response("checkout failed\n", status=500)
    return Response(f"checkout ok order_id={order_id}\n", status=200)


@app.route("/checkout/lookup", methods=["GET"])
def checkout_lookup():
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

        row = None
        try:
            row = lookup_order(order_id_str, trace_id_hex, slow=slow)
            status = 200 if row is not None else 404
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

    REQUEST_LATENCY.labels(method="GET", path="/checkout/lookup").observe(time.perf_counter() - start)
    REQUEST_COUNT.labels(method="GET", path="/checkout/lookup", status=str(status)).inc()

    log_json(
        span,
        level="error" if status == 400 else ("warning" if status == 404 else "info"),
        msg="checkout lookup request handled",
        path="/checkout/lookup",
        order_id=order_id_str,
        status=status,
        duration_ms=round((time.perf_counter() - start) * 1000, 2),
    )

    if status == 400:
        return Response(f"invalid order id: {order_id_str}\n", status=400)
    if status == 404:
        return Response(f"order not found: {order_id_str}\n", status=404)
    return Response(f"order {row[0]} status={row[1]} created_at={row[2]}\n", status=200)


@app.route("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


@app.route("/healthz")
def healthz():
    return Response("ok\n", status=200)


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
