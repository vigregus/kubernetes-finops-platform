"""A stand-in for a third-party payment gateway.

It exists because checkout had no outbound network dependency at all: the
"payment gateway declined" path was `random.random() < ERROR_RATE` and never
left the process. That made the resilience machinery decorative - the circuit
breaker and retry budget only ever saw the database, whose failure mode is
"no free connection", not "the remote end stopped answering".

Deliberately NOT a real third-party API. This stand ramps to 800 rps; pointing
that at someone else's service is both abuse and unreproducible - it would hit
a rate limit long before it hit anything interesting, and no two runs would be
comparable. Everything a real gateway does badly is reproduced here on purpose
and under a knob.

Deployed outside the mesh (namespace `external`), so the call from checkout is
a real egress hop rather than another in-mesh conversation: no mTLS, subject
to DNS policy, and attributable to its own cost centre.

Failure modes, all values-driven:

    DECLINE_RATE  the charge is refused - a business outcome, answered 402
    ERROR_RATE    the gateway itself is broken - answered 502
    TIMEOUT_RATE  no answer arrives inside the caller's deadline
    LATENCY_MS    base latency, with LATENCY_TAIL_MS applied to TAIL_RATE
                  of requests, because a gateway with one flat latency never
                  produces the tail that timeouts are actually made of
"""
from __future__ import annotations

import json
import os
import random
import time

from flask import Flask, Response

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import metrics as metrics_mod
from prometheus_client import Counter, Histogram

app = Flask(__name__)

DECLINE_RATE = float(os.environ.get("DECLINE_RATE", "0.02"))
ERROR_RATE = float(os.environ.get("ERROR_RATE", "0.01"))
TIMEOUT_RATE = float(os.environ.get("TIMEOUT_RATE", "0.005"))
LATENCY_MS = float(os.environ.get("LATENCY_MS", "15"))
LATENCY_TAIL_MS = float(os.environ.get("LATENCY_TAIL_MS", "250"))
TAIL_RATE = float(os.environ.get("TAIL_RATE", "0.02"))
# Longer than any sane client deadline: the point is that the caller gives up
# first, which is what a timeout *is*. Bounded so the greenlet is eventually
# released rather than held for the lifetime of the process.
TIMEOUT_HOLD_S = float(os.environ.get("TIMEOUT_HOLD_S", "30"))

REQUESTS = Counter(
    "gateway_requests_total",
    "Authorisation attempts by outcome",
    ["outcome"],
)
LATENCY = Histogram(
    "gateway_request_duration_seconds",
    "Time spent in the gateway, as the gateway sees it",
    ["outcome"],
    # Buckets reach past a typical client deadline so the tail that causes
    # timeouts is visible rather than collapsed into +Inf.
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)


def _sleep_like_a_gateway() -> None:
    delay = LATENCY_MS
    if random.random() < TAIL_RATE:
        delay = LATENCY_TAIL_MS
    # Jitter, so a histogram of this does not come out as two vertical lines.
    time.sleep(max(0.0, random.gauss(delay, delay * 0.25)) / 1000.0)


@app.route("/authorize", methods=["GET", "POST"])
def authorize():
    start = time.perf_counter()
    roll = random.random()

    if roll < TIMEOUT_RATE:
        # No response at all. The caller's own timeout is what ends this, and
        # that asymmetry is the point: the gateway has no idea it timed out,
        # so its own metrics will never show one. Only the client sees it.
        REQUESTS.labels(outcome="hung").inc()
        time.sleep(TIMEOUT_HOLD_S)
        return Response("too late\n", status=504)

    _sleep_like_a_gateway()

    if roll < TIMEOUT_RATE + ERROR_RATE:
        outcome, status, body = "error", 502, "gateway error\n"
    elif roll < TIMEOUT_RATE + ERROR_RATE + DECLINE_RATE:
        # A decline is an answer, not a failure - the same distinction
        # checkout draws when it maps this to 402 rather than 500.
        outcome, status, body = "declined", 402, json.dumps({"decision": "declined"}) + "\n"
    else:
        outcome, status, body = "approved", 200, json.dumps({"decision": "approved"}) + "\n"

    REQUESTS.labels(outcome=outcome).inc()
    LATENCY.labels(outcome=outcome).observe(time.perf_counter() - start)
    return Response(body, status=status, mimetype="application/json")


@app.route("/healthz", methods=["GET"])
def healthz():
    return Response("ok\n", status=200)


@app.route("/readyz", methods=["GET"])
def readyz():
    # Nothing downstream to check: this service is the bottom of the stack by
    # construction. Readiness that always says yes is honest here, unlike in
    # checkout where it reports dependency health.
    return Response("ok\n", status=200)


@app.route("/metrics", methods=["GET"])
def metrics():
    body, content_type = metrics_mod.render()
    return Response(body, mimetype=content_type)
