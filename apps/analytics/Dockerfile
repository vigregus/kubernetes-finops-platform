# Build context is the repository root, not this directory, so that
# apps/common/ can be copied in - see scripts/build-workload-images.sh.
FROM python:3.12-slim

WORKDIR /app

COPY apps/analytics/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY apps/common/ ./common/
COPY apps/analytics/app.py .
COPY apps/analytics/worker.py .

RUN mkdir -p /tmp/prometheus && chown 65534:65534 /tmp/prometheus

USER 65534:65534
EXPOSE 8080

# One image, two entrypoints: this default serves the HTTP API, and the
# worker Deployment overrides the command to run worker.py instead (see
# charts/app2/templates/deployment.yaml).
CMD ["gunicorn", "--config", "common/gunicorn_conf.py", "app:app"]
