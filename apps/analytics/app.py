import hashlib
import json
import logging
import os
import random
import sys
import threading
import time

import redis as redis_lib
from confluent_kafka import Consumer
from flask import Flask, Response, request
from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode
from prometheus_client import REGISTRY, Counter, Histogram
from prometheus_client.openmetrics.exposition import CONTENT_TYPE_LATEST, generate_latest

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
LATENCY_MS_MEAN = float(os.environ.get("LATENCY_MS_MEAN", "20"))
LATENCY_MS_JITTER = float(os.environ.get("LATENCY_MS_JITTER", "10"))
ERROR_RATE = float(os.environ.get("ERROR_RATE", "0.01"))
WORK_ITERATIONS = int(os.environ.get("WORK_ITERATIONS", "300000"))
BATCH_ROWS = int(os.environ.get("BATCH_ROWS", "5000"))

REDIS_URL = os.environ.get("REDIS_URL")
KAFKA_BOOTSTRAP_SERVERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS")
KAFKA_TOPIC = os.environ.get("KAFKA_TOPIC", "orders")
KAFKA_CONSUMER_GROUP = os.environ.get("KAFKA_CONSUMER_GROUP", "analytics")

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
ORDERS_CONSUMED = Counter("orders_consumed_total", "Order events consumed from Kafka")

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


redis_client = redis_lib.from_url(REDIS_URL, socket_timeout=2) if REDIS_URL else None


def aggregate_batch():
    # Heavier than checkout's per-request work on purpose - this now runs
    # once per consumed order event rather than once per HTTP request, so
    # analytics' CPU usage tracks checkout's traffic through Kafka instead
    # of direct hits on /analytics. That's the point: a derived/shared
    # cost signal, not just a per-endpoint one.
    digest = hashlib.sha256()
    rows = []
    for i in range(BATCH_ROWS):
        digest.update(str(i).encode())
        rows.append(digest.hexdigest())
    for _ in range(WORK_ITERATIONS):
        digest.update(b"analytics")
    return len(rows)


def consume_loop():
    if not KAFKA_BOOTSTRAP_SERVERS:
        return
    consumer = Consumer(
        {
            "bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS,
            "group.id": KAFKA_CONSUMER_GROUP,
            "auto.offset.reset": "earliest",
        }
    )
    consumer.subscribe([KAFKA_TOPIC])
    try:
        while True:
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                logger.error(json.dumps({"level": "error", "_msg": "kafka poll error", "error": str(msg.error())}))
                continue

            headers = {k: v.decode("utf-8") for k, v in (msg.headers() or [])}
            ctx = propagate.extract(headers)
            with tracer.start_as_current_span(
                "analytics.consume_order", context=ctx, kind=SpanKind.CONSUMER
            ) as span:
                span.set_attribute("messaging.system", "kafka")
                span.set_attribute("messaging.destination", msg.topic())
                span.set_attribute("messaging.destination_kind", "topic")
                span.set_attribute("messaging.operation", "receive")
                span.set_attribute("messaging.kafka.partition", msg.partition())
                span.set_attribute("messaging.kafka.consumer_group", KAFKA_CONSUMER_GROUP)
                payload = {}
                try:
                    payload = json.loads(msg.value())
                    span.set_attribute("order.id", payload.get("order_id", ""))
                except (json.JSONDecodeError, TypeError):
                    pass
                order_id = payload.get("order_id") if isinstance(payload, dict) else None

                log_json(
                    span,
                    _msg="kafka consume",
                    topic=msg.topic(),
                    partition=msg.partition(),
                    offset=msg.offset(),
                    order_id=order_id,
                )

                try:
                    with tracer.start_as_current_span("analytics.aggregate_batch") as agg_span:
                        row_count = aggregate_batch()
                        span.set_attribute("analytics.batch_rows", row_count)
                        log_json(agg_span, _msg="aggregate batch computed", rows=row_count, order_id=order_id)

                    if redis_client:
                        start = time.perf_counter()
                        with tracer.start_as_current_span("analytics.cache_update") as cache_span:
                            cache_span.set_attribute("db.system", "redis")
                            cache_span.set_attribute("db.operation", "INCR")
                            cache_span.set_attribute("db.statement", f"INCR {REDIS_KEY_PROCESSED}")
                            new_total = redis_client.incr(REDIS_KEY_PROCESSED)
                            log_json(cache_span, _msg="redis INCR", key=REDIS_KEY_PROCESSED, new_value=new_total)
                        CACHE_LATENCY.observe(time.perf_counter() - start)

                    log_json(span, msg="order consumed", order_id=order_id)
                except Exception as e:
                    span.record_exception(e)
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    log_json(span, level="error", _msg="order consume failed", order_id=order_id, error=str(e))

            ORDERS_CONSUMED.inc()
    finally:
        consumer.close()


@app.route("/", methods=["GET"])
@app.route("/analytics", methods=["GET"])
def analytics():
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
                    log_json(cache_span, _msg="redis GET", key=REDIS_KEY_PROCESSED, hit=processed is not None)
                CACHE_LATENCY.observe(time.perf_counter() - cache_start)

            delay_seconds = max(0.0, random.gauss(LATENCY_MS_MEAN, LATENCY_MS_JITTER)) / 1000.0
            time.sleep(delay_seconds)

            if random.random() < ERROR_RATE:
                raise QueryError("analytics backend query timed out")
            status = 200
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
    # p99) instead of only "some trace from this time range."
    REQUEST_LATENCY.labels(method="GET", path="/analytics").observe(
        time.perf_counter() - start, exemplar={"trace_id": trace_id_hex}
    )
    REQUEST_COUNT.labels(method="GET", path="/analytics", status=str(status)).inc()

    log_json(
        span,
        level="error" if status == 500 else "info",
        msg="analytics request handled",
        path="/analytics",
        status=status,
        duration_ms=round((time.perf_counter() - start) * 1000, 2),
    )

    if status == 500:
        return Response("analytics query failed\n", status=500)
    count = processed.decode() if processed else "0"
    return Response(f"analytics query ok orders_processed={count}\n", status=200)


@app.route("/metrics")
def metrics():
    return Response(generate_latest(REGISTRY), mimetype=CONTENT_TYPE_LATEST)


@app.route("/healthz")
def healthz():
    return Response("ok\n", status=200)


if __name__ == "__main__":
    consumer_thread = threading.Thread(target=consume_loop, daemon=True)
    consumer_thread.start()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
