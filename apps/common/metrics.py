"""Metrics exposition that works in both single- and multi-process modes.

Single process (the default, see gunicorn_conf.py): expose OpenMetrics, which
is the format that can carry exemplars - the trace_id attached to a latency
observation is what makes a p99 point on a Grafana panel clickable through to
the trace that produced it.

Multiple gunicorn workers: each process has its own registry, so `/metrics`
served from a random worker would report roughly 1/N of the traffic. The
multiprocess collector aggregates the workers' mmap files instead - at the
cost of exemplars, which that format cannot represent.
"""
from __future__ import annotations

import os

from prometheus_client import CONTENT_TYPE_LATEST as CLASSIC_CONTENT_TYPE
from prometheus_client import REGISTRY, CollectorRegistry, generate_latest
from prometheus_client.openmetrics.exposition import CONTENT_TYPE_LATEST as OPENMETRICS_CONTENT_TYPE
from prometheus_client.openmetrics.exposition import generate_latest as generate_openmetrics

_MULTIPROC_DIR = os.environ.get("PROMETHEUS_MULTIPROC_DIR", "")
_WEB_CONCURRENCY = int(os.environ.get("WEB_CONCURRENCY", "1"))

MULTIPROCESS = _WEB_CONCURRENCY > 1 and bool(_MULTIPROC_DIR)

# Call sites use this to decide whether to attach an exemplar at all: passing
# one in multiprocess mode raises rather than being ignored.
EXEMPLARS_SUPPORTED = not MULTIPROCESS


def render() -> tuple[bytes, str]:
    """Return (payload, content_type) for the /metrics endpoint."""
    if not MULTIPROCESS:
        return generate_openmetrics(REGISTRY), OPENMETRICS_CONTENT_TYPE

    from prometheus_client import multiprocess

    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)
    return generate_latest(registry), CLASSIC_CONTENT_TYPE


def observe(histogram, value: float, trace_id: str | None = None) -> None:
    """Record a latency observation, with an exemplar when the mode allows it."""
    if trace_id and EXEMPLARS_SUPPORTED:
        histogram.observe(value, exemplar={"trace_id": trace_id})
    else:
        histogram.observe(value)
