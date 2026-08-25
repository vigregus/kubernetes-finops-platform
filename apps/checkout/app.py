import hashlib
import json
import os
import random
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
from opentelemetry.trace import SpanKind
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "checkout")
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
REDIS_URL = os.environ.get("REDIS_URL")
KAFKA_BOOTSTRAP_SERVERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS")
KAFKA_TOPIC = os.environ.get("KAFKA_TOPIC", "orders")

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
DB_LATENCY = Histogram("db_query_duration_seconds", "Postgres query latency in seconds")
CACHE_LATENCY = Histogram("cache_call_duration_seconds", "Redis call latency in seconds")
KAFKA_LATENCY = Histogram("kafka_publish_duration_seconds", "Kafka publish latency in seconds")

redis_client = redis_lib.from_url(REDIS_URL, socket_timeout=2) if REDIS_URL else None
kafka_producer = (
    Producer({"bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS}) if KAFKA_BOOTSTRAP_SERVERS else None
)


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
        conn.commit()


def cache_lookup(idempotency_key):
    if not redis_client:
        return None
    start = time.perf_counter()
    with tracer.start_as_current_span("checkout.cache_lookup"):
        value = redis_client.get(idempotency_key)
    CACHE_LATENCY.observe(time.perf_counter() - start)
    return value


def cache_store(idempotency_key, order_id):
    if not redis_client:
        return
    start = time.perf_counter()
    with tracer.start_as_current_span("checkout.cache_store"):
        redis_client.set(idempotency_key, order_id, ex=300)
    CACHE_LATENCY.observe(time.perf_counter() - start)


def persist_order(order_id):
    if not DATABASE_URL:
        return
    start = time.perf_counter()
    with tracer.start_as_current_span("checkout.persist_order"):
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO orders (id, status) VALUES (%s, %s)", (order_id, "placed")
                )
            conn.commit()
    DB_LATENCY.observe(time.perf_counter() - start)


def publish_order_event(order_id):
    if not kafka_producer:
        return
    start = time.perf_counter()
    with tracer.start_as_current_span("checkout.publish_event", kind=SpanKind.PRODUCER):
        headers = {}
        propagate.inject(headers)
        kafka_headers = [(k, v.encode("utf-8")) for k, v in headers.items()]
        kafka_producer.produce(
            KAFKA_TOPIC,
            value=json.dumps({"order_id": order_id}).encode("utf-8"),
            headers=kafka_headers,
        )
        kafka_producer.poll(0)
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
    ctx = propagate.extract(dict(request.headers))
    with tracer.start_as_current_span("checkout.process", context=ctx, kind=SpanKind.SERVER) as span:
        idempotency_key = f"checkout:idem:{request.args.get('key', uuid.uuid4().hex)}"
        cached_order_id = cache_lookup(idempotency_key)

        with tracer.start_as_current_span("checkout.validate_payment"):
            validate_payment()

        delay_seconds = max(0.0, random.gauss(LATENCY_MS_MEAN, LATENCY_MS_JITTER)) / 1000.0
        time.sleep(delay_seconds)

        failed = random.random() < ERROR_RATE
        status = 500 if failed else 200
        span.set_attribute("http.status_code", status)

        if not failed:
            order_id = cached_order_id.decode() if cached_order_id else str(uuid.uuid4())
            if not cached_order_id:
                persist_order(order_id)
                publish_order_event(order_id)
                cache_store(idempotency_key, order_id)

    REQUEST_LATENCY.labels(method="GET", path="/checkout").observe(time.perf_counter() - start)
    REQUEST_COUNT.labels(method="GET", path="/checkout", status=str(status)).inc()

    if failed:
        return Response("checkout failed\n", status=500)
    return Response(f"checkout ok order_id={order_id}\n", status=200)


@app.route("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


@app.route("/healthz")
def healthz():
    return Response("ok\n", status=200)


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
