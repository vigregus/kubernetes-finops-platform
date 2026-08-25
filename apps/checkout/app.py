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
    with tracer.start_as_current_span("checkout.process") as span:
        with tracer.start_as_current_span("checkout.validate_payment"):
            validate_payment()

        delay_seconds = max(0.0, random.gauss(LATENCY_MS_MEAN, LATENCY_MS_JITTER)) / 1000.0
        time.sleep(delay_seconds)

        failed = random.random() < ERROR_RATE
        status = 500 if failed else 200
        span.set_attribute("http.status_code", status)

    REQUEST_LATENCY.labels(method="GET", path="/checkout").observe(time.perf_counter() - start)
    REQUEST_COUNT.labels(method="GET", path="/checkout", status=str(status)).inc()

    if failed:
        return Response("checkout failed\n", status=500)
    return Response("checkout ok\n", status=200)


@app.route("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


@app.route("/healthz")
def healthz():
    return Response("ok\n", status=200)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
