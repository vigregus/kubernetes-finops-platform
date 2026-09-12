"""Overload and failure handling: load shedding, circuit breaking, bounded retries.

Why this exists: without it, an overloaded service just queues. Latency grows
without bound, the load generator's closed/open-model distinction stops
mattering because everything is stuck, and a stress test can never answer
"what is this pod's actual capacity?" - it only ever answers "how long until
something times out". A service that sheds load has a measurable saturation
point, which is the number every rightsizing and autoscaling decision needs.

Stdlib only, and all synchronisation primitives come from `threading`, which
gevent monkey-patches into cooperative equivalents - so the same code is
correct under both the gevent and the sync gunicorn worker classes.
"""
from __future__ import annotations

import random
import threading
import time
from contextlib import contextmanager

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"

# Exported as a gauge; string states are not graphable.
_STATE_CODES = {CLOSED: 0, HALF_OPEN: 1, OPEN: 2}


class Shed(Exception):
    """Raised when a request is rejected to protect the service."""


class CircuitOpen(Exception):
    """Raised when a dependency call is refused because its breaker is open."""


class LoadShedder:
    """Caps concurrent in-flight work; rejects immediately past the cap.

    The cap is deliberately a *count of concurrent requests*, not a rate: a
    rate limit has to be guessed ahead of time and goes stale the moment the
    per-request cost changes, whereas a concurrency cap self-adjusts - slower
    requests occupy slots longer, so the effective rate falls on its own.
    """

    def __init__(self, max_in_flight: int):
        self.max_in_flight = max_in_flight
        self._sem = threading.BoundedSemaphore(max_in_flight) if max_in_flight > 0 else None
        self._in_flight = 0
        self._lock = threading.Lock()

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @contextmanager
    def admit(self):
        """Admit one request or raise Shed. Use as a context manager."""
        if self._sem is None:
            yield
            return
        if not self._sem.acquire(blocking=False):
            raise Shed(f"over capacity: {self.max_in_flight} in flight")
        with self._lock:
            self._in_flight += 1
        try:
            yield
        finally:
            with self._lock:
                self._in_flight -= 1
            self._sem.release()


class RetryBudget:
    """Caps retries as a fraction of overall traffic.

    Retrying without a budget is how a slow dependency becomes an outage: every
    client multiplies its own load at exactly the moment the dependency is
    least able to take it. The budget makes retries a small, bounded fraction
    of normal traffic no matter how bad things get.
    """

    def __init__(self, ratio: float = 0.1, window_s: float = 10.0):
        self.ratio = ratio
        self.window_s = window_s
        self._attempts = 0
        self._retries = 0
        self._window_start = time.monotonic()
        self._lock = threading.Lock()

    def _roll(self) -> None:
        now = time.monotonic()
        if now - self._window_start >= self.window_s:
            self._attempts = 0
            self._retries = 0
            self._window_start = now

    def record_attempt(self) -> None:
        with self._lock:
            self._roll()
            self._attempts += 1

    def allow_retry(self) -> bool:
        with self._lock:
            self._roll()
            if self._retries >= max(1.0, self._attempts * self.ratio):
                return False
            self._retries += 1
            return True


class CircuitBreaker:
    """Stops calling a dependency that is failing, and probes it periodically.

    Closed -> (consecutive failures reach the threshold) -> Open -> (after the
    recovery timeout) -> Half-open -> one probe decides: success closes it,
    failure re-opens it.
    """

    def __init__(self, name: str, failure_threshold: int = 5, recovery_timeout_s: float = 10.0):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout_s = recovery_timeout_s
        self._state = CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        return self._state

    @property
    def state_code(self) -> int:
        return _STATE_CODES[self._state]

    def _on_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._state = CLOSED

    def _on_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                self._state = OPEN
                self._opened_at = time.monotonic()

    def _allow(self) -> bool:
        with self._lock:
            if self._state == CLOSED:
                return True
            if self._state == OPEN:
                if time.monotonic() - self._opened_at >= self.recovery_timeout_s:
                    self._state = HALF_OPEN
                    return True
                return False
            # HALF_OPEN: let the single probe through.
            return True

    @contextmanager
    def call(self):
        if not self._allow():
            raise CircuitOpen(f"{self.name} circuit is open")
        try:
            yield
        except Exception:
            self._on_failure()
            raise
        else:
            self._on_success()


def call_with_retry(
    fn,
    attempts: int = 3,
    base_delay_s: float = 0.05,
    max_delay_s: float = 1.0,
    budget: RetryBudget | None = None,
    breaker: CircuitBreaker | None = None,
    retry_on: tuple = (Exception,),
):
    """Call `fn`, retrying transient failures with full-jitter backoff.

    Full jitter (a uniform draw from [0, delay]) rather than a fixed backoff:
    identical clients retrying on identical schedules re-synchronise into a
    thundering herd, which is the failure mode retries are supposed to avoid.
    """
    last_error: Exception | None = None
    for attempt in range(attempts):
        if budget is not None:
            budget.record_attempt()
        try:
            if breaker is not None:
                with breaker.call():
                    return fn()
            return fn()
        except CircuitOpen:
            raise
        except retry_on as exc:
            last_error = exc
            is_last = attempt == attempts - 1
            if is_last:
                break
            if budget is not None and not budget.allow_retry():
                break
            delay = min(max_delay_s, base_delay_s * (2**attempt))
            time.sleep(random.uniform(0.0, delay))
    raise last_error
