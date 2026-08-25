import hashlib
import os
import random
import time

from flask import Flask, Response
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "analytics")
OTLP_ENDPOINT = os.environ.get(
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "http://tempo-local.observability.svc.cluster.local:4318",
)
PORT = int(os.environ.get("PORT", "8080"))
LATENCY_MS_MEAN = float(os.environ.get("LATENCY_MS_MEAN", "90"))
LATENCY_MS_JITTER = float(os.environ.get("LATENCY_MS_JITTER", "30"))
ERROR_RATE = float(os.environ.get("ERROR_RATE", "0.01"))
WORK_ITERATIONS = int(os.environ.get("WORK_ITERATIONS", "300000"))
BATCH_ROWS = int(os.environ.get("BATCH_ROWS", "5000"))

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


def aggregate_batch():
    # Heavier than checkout's work on purpose: analytics is modeled as a
    # batch-ish aggregation endpoint, both CPU (hashing) and memory
    # (holding the batch in a list) bound, so its cost/rightsizing story
    # in Grafana looks different from checkout's.
    digest = hashlib.sha256()
    rows = []
    for i in range(BATCH_ROWS):
        digest.update(str(i).encode())
        rows.append(digest.hexdigest())
    for _ in range(WORK_ITERATIONS):
        digest.update(b"analytics")
    return len(rows)


@app.route("/", methods=["GET"])
@app.route("/analytics", methods=["GET"])
def analytics():
    start = time.perf_counter()
    with tracer.start_as_current_span("analytics.query") as span:
        with tracer.start_as_current_span("analytics.aggregate_batch"):
            row_count = aggregate_batch()
            span.set_attribute("analytics.batch_rows", row_count)

        delay_seconds = max(0.0, random.gauss(LATENCY_MS_MEAN, LATENCY_MS_JITTER)) / 1000.0
        time.sleep(delay_seconds)

        failed = random.random() < ERROR_RATE
        status = 500 if failed else 200
        span.set_attribute("http.status_code", status)

    REQUEST_LATENCY.labels(method="GET", path="/analytics").observe(time.perf_counter() - start)
    REQUEST_COUNT.labels(method="GET", path="/analytics", status=str(status)).inc()

    if failed:
        return Response("analytics query failed\n", status=500)
    return Response("analytics query ok\n", status=200)


@app.route("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


@app.route("/healthz")
def healthz():
    return Response("ok\n", status=200)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
