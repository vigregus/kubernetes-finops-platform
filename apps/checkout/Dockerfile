# Build context is the repository root, not this directory, so that
# apps/common/ can be copied in - see scripts/build-workload-images.sh.
FROM python:3.12-slim

WORKDIR /app

COPY apps/checkout/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY apps/common/ ./common/
COPY apps/checkout/app.py .

# Written to by prometheus_client only when WEB_CONCURRENCY > 1 (see
# apps/common/gunicorn_conf.py). Created here so the unprivileged user owns
# it; the chart mounts an emptyDir over it.
RUN mkdir -p /tmp/prometheus && chown 65534:65534 /tmp/prometheus

USER 65534:65534
EXPOSE 8080

# gunicorn, not `python app.py`: the Werkzeug development server that used to
# run here is single-process and not built to carry load, so any capacity
# measured against it describes the dev server rather than the service.
CMD ["gunicorn", "--config", "common/gunicorn_conf.py", "app:app"]
