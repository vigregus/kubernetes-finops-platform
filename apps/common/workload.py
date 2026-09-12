"""Synthetic per-request work, expressed in milliseconds rather than iterations.

The previous version of this burned a fixed `WORK_ITERATIONS` count of sha256
updates. That silently costs a different amount of CPU on every different CPU
- which is fine while everything runs on one node, and actively misleading the
moment the same workload is scheduled across node classes (see the node
strategy phase: comparing an "on-demand" node against a "spot" one is
meaningless if the same config does more work on one of them).

So: measure this CPU's iterations-per-millisecond once at startup, then spin
for the number of *milliseconds* the profile asks for. The calibration result
is exported as a metric precisely so that "is this node slower?" is a question
the dashboards can answer instead of a hidden variable.
"""
from __future__ import annotations

import hashlib
import random
import time

# Set by calibrate(). Module-level rather than passed around because every
# call site wants the same machine-wide constant.
_ITERS_PER_MS: float = 0.0

_CHUNK = 1000
_PAGE = 4096


def calibrate(sample_ms: float = 50.0) -> float:
    """Measure hash iterations per millisecond on this CPU.

    Runs once at process start. Cheap (50ms by default) but not free, which
    is why it is not re-run per request.
    """
    global _ITERS_PER_MS

    digest = hashlib.sha256()
    iterations = 0
    start = time.perf_counter()
    deadline = start + (sample_ms / 1000.0)
    while time.perf_counter() < deadline:
        for _ in range(_CHUNK):
            digest.update(b"calibrate")
        iterations += _CHUNK
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    _ITERS_PER_MS = iterations / elapsed_ms if elapsed_ms > 0 else 0.0
    return _ITERS_PER_MS


def iters_per_ms() -> float:
    return _ITERS_PER_MS


def burn_cpu(cpu_ms: float) -> None:
    """Occupy a CPU for roughly `cpu_ms` milliseconds.

    Note for the gevent-based tiers: this genuinely blocks the worker's hub
    for its duration, exactly as any real CPU-bound section of a request
    handler would. That is the intended behaviour - it is what makes a
    CPU-heavy endpoint actually compete for the core instead of yielding.
    """
    if cpu_ms <= 0 or _ITERS_PER_MS <= 0:
        return
    digest = hashlib.sha256()
    for _ in range(int(_ITERS_PER_MS * cpu_ms)):
        digest.update(b"work")


def churn_memory(mem_mb: float) -> None:
    """Allocate, touch and release `mem_mb` to produce real RSS and GC pressure.

    Touching one byte per page matters: without it the allocation can stay
    virtual, so RSS - the number every memory rightsizing decision is made
    from - would never move.
    """
    if mem_mb <= 0:
        return
    size = int(mem_mb * 1024 * 1024)
    block = bytearray(size)
    for offset in range(0, size, _PAGE):
        block[offset] = 1
    del block


def simulate_io(io_ms: float, jitter_ms: float = 0.0) -> None:
    """Wait as if on a downstream dependency.

    Under gevent this yields the worker to other requests, which is the whole
    reason an I/O-bound tier can hold high concurrency on few cores.
    """
    if io_ms <= 0:
        return
    delay_ms = random.gauss(io_ms, jitter_ms) if jitter_ms > 0 else io_ms
    time.sleep(max(0.0, delay_ms) / 1000.0)


class Profile:
    """The per-endpoint resource shape, supplied from Helm values.

    Three services with three different profiles is how this stand gets
    heterogeneous resource behaviour - CPU-bound, memory-bound, I/O-bound -
    without introducing a second language runtime.
    """

    def __init__(self, cpu_ms: float = 0.0, mem_mb: float = 0.0, io_ms: float = 0.0, io_jitter_ms: float = 0.0):
        self.cpu_ms = cpu_ms
        self.mem_mb = mem_mb
        self.io_ms = io_ms
        self.io_jitter_ms = io_jitter_ms

    @classmethod
    def from_env(cls, env: dict, prefix: str) -> "Profile":
        def num(suffix: str, default: float) -> float:
            return float(env.get(f"{prefix}_{suffix}", default))

        return cls(
            cpu_ms=num("CPU_MS", 0.0),
            mem_mb=num("MEM_MB", 0.0),
            io_ms=num("IO_MS", 0.0),
            io_jitter_ms=num("IO_JITTER_MS", 0.0),
        )

    def run(self) -> None:
        burn_cpu(self.cpu_ms)
        churn_memory(self.mem_mb)
        simulate_io(self.io_ms, self.io_jitter_ms)

    def __repr__(self) -> str:
        return (
            f"Profile(cpu_ms={self.cpu_ms}, mem_mb={self.mem_mb}, "
            f"io_ms={self.io_ms}, io_jitter_ms={self.io_jitter_ms})"
        )
