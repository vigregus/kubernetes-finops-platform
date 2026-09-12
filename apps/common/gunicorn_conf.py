"""gunicorn configuration shared by the HTTP services.

Replaces `app.run(threaded=True)` (the Werkzeug development server), which is
single-process, unbounded in its thread use, and explicitly not meant to carry
load - measuring capacity against it produces a number that describes the dev
server, not the service.

## Why the default is one worker process

`prometheus_client` cannot export exemplars in multiprocess mode - the mmap
files it uses to aggregate across gunicorn workers have nowhere to carry them
(the same mode also drops custom collectors, `Gauge.set_function`, and
Info/Enum metrics). Exemplars are what let a p99 point on a latency panel jump
to a representative trace, which is wired through this whole platform.

So the default is `WEB_CONCURRENCY=1` with the gevent worker class: one
process per pod keeps the ordinary registry (exemplars intact), while
greenlets still give thousands of concurrent connections on very little CPU.
Horizontal scaling is done with pods, which is what the autoscaling
experiments vary anyway.

Setting `WEB_CONCURRENCY>1` is still supported - it turns on multiprocess mode
automatically and logs that exemplars are now disabled. That trade-off is
itself worth measuring: "N pods x 1 worker" against "1 pod x N workers" at
equal total CPU is a real strategy comparison, and this is the knob for it.
"""
from __future__ import annotations

import os
import shutil

_PORT = os.environ.get("PORT", "8080")

bind = f"0.0.0.0:{_PORT}"
workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
worker_class = os.environ.get("GUNICORN_WORKER_CLASS", "gevent")
worker_connections = int(os.environ.get("WORKER_CONNECTIONS", "1000"))

# `timeout` is the hard kill; `graceful_timeout` is how long in-flight
# requests get to finish after SIGTERM. The gap between them matters during
# scale-down: too short and every scale-in event shows up as a burst of 5xx
# that contaminates the SLO measurement of whichever autoscaling strategy
# happens to be under test.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "60"))
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", "30"))
keepalive = int(os.environ.get("GUNICORN_KEEPALIVE", "5"))

# Recycle workers periodically so a slow leak cannot masquerade as a genuine
# memory-rightsizing signal. Jitter prevents every worker recycling at once.
max_requests = int(os.environ.get("GUNICORN_MAX_REQUESTS", "50000"))
max_requests_jitter = int(os.environ.get("GUNICORN_MAX_REQUESTS_JITTER", "5000"))

# Must stay False: each worker builds its own connection pools and runs its
# own CPU calibration. Pre-forking would share a pool's sockets across
# processes, which psycopg2 does not support.
preload_app = False

accesslog = None  # the app emits structured JSON logs itself
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")

_MULTIPROC_DIR = os.environ.get("PROMETHEUS_MULTIPROC_DIR", "")


def on_starting(server):
    """Prepare (and wipe) the multiprocess metrics directory when needed."""
    if workers <= 1:
        return
    if not _MULTIPROC_DIR:
        server.log.error(
            "WEB_CONCURRENCY=%s but PROMETHEUS_MULTIPROC_DIR is unset - "
            "/metrics would report only one worker's numbers",
            workers,
        )
        return
    # The directory must be empty at start: stale files from a previous run
    # are counted as live series and inflate every counter.
    shutil.rmtree(_MULTIPROC_DIR, ignore_errors=True)
    os.makedirs(_MULTIPROC_DIR, exist_ok=True)
    server.log.warning(
        "multiprocess metrics mode enabled (%s workers) - exemplars are "
        "disabled in this mode, so latency panels cannot link to traces",
        workers,
    )


def post_fork(server, worker):
    """Make psycopg2 cooperative under gevent.

    Without this the C driver blocks the whole hub on every query, so one
    slow statement stalls every other greenlet in the worker and the service
    behaves like it has a concurrency of 1.
    """
    if not worker_class.startswith("gevent"):
        return
    try:
        from psycogreen.gevent import patch_psycopg

        patch_psycopg()
        server.log.info("psycopg2 patched for gevent")
    except ImportError:
        server.log.info("psycogreen not installed - skipping psycopg2 gevent patch")


def child_exit(server, worker):
    """Required in multiprocess mode so a dead worker's series stop counting."""
    if workers <= 1 or not _MULTIPROC_DIR:
        return
    try:
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(worker.pid)
    except ImportError:
        pass
