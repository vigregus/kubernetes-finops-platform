# Build context is the repository root, not this directory, so that
# apps/common/ can be copied in - see scripts/build-workload-images.sh.
FROM python:3.12-slim

WORKDIR /app

COPY apps/gateway-sim/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY apps/common/ ./common/
COPY apps/gateway-sim/app.py .

RUN mkdir -p /tmp/prometheus && chown 65534:65534 /tmp/prometheus

USER 65534:65534
EXPOSE 8080

# Deliberately lighter than checkout's dependency set: no database, no Kafka,
# no tracing exporter. A stand-in for someone else's service should not be
# able to fail in ways that service could not, or its failures would be
# indistinguishable from ours during an experiment.
CMD ["gunicorn", "--config", "common/gunicorn_conf.py", "app:app"]
