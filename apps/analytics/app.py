import hashlib
import json
import os
import random
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
from opentelemetry.trace import SpanKind
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "analytics")
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

provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
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
            if msg is None or msg.error():
                continue

            headers = {k: v.decode("utf-8") for k, v in (msg.headers() or [])}
            ctx = propagate.extract(headers)
            with tracer.start_as_current_span(
                "analytics.consume_order", context=ctx, kind=SpanKind.CONSUMER
            ) as span:
                try:
                    payload = json.loads(msg.value())
                    span.set_attribute("order.id", payload.get("order_id", ""))
                except (json.JSONDecodeError, TypeError):
                    pass

                with tracer.start_as_current_span("analytics.aggregate_batch"):
                    row_count = aggregate_batch()
                    span.set_attribute("analytics.batch_rows", row_count)

                if redis_client:
                    start = time.perf_counter()
                    with tracer.start_as_current_span("analytics.cache_update"):
                        redis_client.incr(REDIS_KEY_PROCESSED)
                    CACHE_LATENCY.observe(time.perf_counter() - start)

            ORDERS_CONSUMED.inc()
    finally:
        consumer.close()


@app.route("/", methods=["GET"])
@app.route("/analytics", methods=["GET"])
def analytics():
    start = time.perf_counter()
    ctx = propagate.extract(dict(request.headers))
    with tracer.start_as_current_span("analytics.query", context=ctx, kind=SpanKind.SERVER) as span:
        processed = None
        if redis_client:
            cache_start = time.perf_counter()
            with tracer.start_as_current_span("analytics.cache_lookup"):
                processed = redis_client.get(REDIS_KEY_PROCESSED)
            CACHE_LATENCY.observe(time.perf_counter() - cache_start)

        delay_seconds = max(0.0, random.gauss(LATENCY_MS_MEAN, LATENCY_MS_JITTER)) / 1000.0
        time.sleep(delay_seconds)

        failed = random.random() < ERROR_RATE
        status = 500 if failed else 200
        span.set_attribute("http.status_code", status)

    REQUEST_LATENCY.labels(method="GET", path="/analytics").observe(time.perf_counter() - start)
    REQUEST_COUNT.labels(method="GET", path="/analytics", status=str(status)).inc()

    if failed:
        return Response("analytics query failed\n", status=500)
    count = processed.decode() if processed else "0"
    return Response(f"analytics query ok orders_processed={count}\n", status=200)


@app.route("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


@app.route("/healthz")
def healthz():
    return Response("ok\n", status=200)


if __name__ == "__main__":
    consumer_thread = threading.Thread(target=consume_loop, daemon=True)
    consumer_thread.start()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
