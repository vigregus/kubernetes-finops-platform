"""A pooled Postgres client.

Replaces the previous `psycopg2.connect()`-per-request pattern. That pattern
puts a TCP connect plus authentication on the critical path of every single
request, and - far worse for a stand meant to measure scaling - it makes the
connection count a function of *request rate*. Scale the deployment out under
load and Postgres hits `max_connections` long before it hits a CPU limit, so
every autoscaling experiment ends up measuring connection exhaustion rather
than the strategy under test.

Sizing note: each gunicorn worker process holds its own pool, so the real
ceiling is `pods x WEB_CONCURRENCY x maxconn`. Keep maxconn small; a shared
PgBouncer in front of the cluster is the actual fix once several pods are in
play.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager

import psycopg2
from psycopg2 import pool as pg_pool


class PoolBusy(Exception):
    """No connection became available within the wait budget.

    Deliberately distinct from a connection *error*: this means the service is
    at capacity, not that Postgres is unwell. A stress run showed why the
    distinction matters - psycopg2 raises PoolError the instant the pool is
    empty, that was fed to the circuit breaker, and the breaker then opened
    against a database that was working perfectly. Callers should turn this
    into a 429, not a 5xx.
    """


class PooledPostgres:
    def __init__(
        self,
        dsn: str,
        minconn: int = 1,
        maxconn: int = 8,
        statement_timeout_ms: int = 5000,
        connect_timeout_s: int = 3,
        application_name: str = "app",
        acquire_timeout_s: float = 0.25,
    ):
        self.dsn = dsn
        self.maxconn = maxconn
        self.acquire_timeout_s = acquire_timeout_s
        self._lock = threading.Lock()
        # psycopg2's pool has no bounded wait - getconn() either returns
        # immediately or raises. This semaphore turns it into a small queue:
        # a brief contention spike is absorbed, sustained overload sheds.
        # Without it the admission limit and the pool size disagree, and the
        # service cheerfully admits work that is guaranteed to fail.
        self._slots = threading.Semaphore(maxconn)
        self.statement_timeout_ms = statement_timeout_ms
        # No `options=-c statement_timeout=...` here. That is a startup
        # parameter, and PgBouncer in transaction mode rejects startup
        # parameters it does not know about - so a connection option that
        # works fine against Postgres directly fails the moment a pooler is
        # put in front of it. The timeout is applied per transaction instead
        # (see cursor()), which is pooling-safe by construction.
        self._pool = pg_pool.ThreadedConnectionPool(
            minconn,
            maxconn,
            dsn=dsn,
            connect_timeout=connect_timeout_s,
            application_name=application_name,
        )

    @contextmanager
    def cursor(self, commit: bool = False):
        """Borrow a connection, yield a cursor, always return the connection.

        Waits up to `acquire_timeout_s` for a free connection and raises
        PoolBusy if none appears, so that "everyone is using the database"
        reads as backpressure rather than as a database fault.
        """
        if not self._slots.acquire(timeout=self.acquire_timeout_s):
            raise PoolBusy(f"no connection within {self.acquire_timeout_s}s (pool size {self.maxconn})")
        try:
            conn = self._pool.getconn()
        except pg_pool.PoolError as exc:
            self._slots.release()
            raise PoolBusy(str(exc)) from exc
        except Exception:
            self._slots.release()
            raise
        try:
            with conn.cursor() as cur:
                # SET LOCAL is scoped to this transaction, so it travels with
                # the work rather than with the connection - which is what
                # makes it survive a pooler handing that connection to
                # someone else immediately afterwards.
                if self.statement_timeout_ms:
                    cur.execute("SET LOCAL statement_timeout = %s", (self.statement_timeout_ms,))
                yield cur
            if commit:
                conn.commit()
            else:
                # Even read-only work opens a transaction in psycopg2's default
                # mode; leaving it open would pin an idle-in-transaction backend
                # and block vacuum on a table this stand deliberately makes huge.
                conn.rollback()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            self._pool.putconn(conn)
            self._slots.release()

    def healthy(self) -> bool:
        """Used by the readiness probe: is Postgres reachable?

        Busy is not unhealthy. An earlier version answered this by borrowing a
        pooled connection, which meant the probe competed with live traffic
        for the scarcest resource in the service: at saturation the probe
        failed, Kubernetes pulled every pod out of the Service, and an
        overloaded deployment became an unavailable one - visible in the
        stress run as throughput collapsing from 231 rps to 9 rather than
        levelling off. Saturation is what 429 is for; readiness is for "can I
        serve at all".
        """
        try:
            with self.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            return True
        except PoolBusy:
            # Every connection is in use, which means the database is very
            # much alive and this pod is simply working hard.
            return True
        except Exception:
            return False

    def close(self) -> None:
        with self._lock:
            if self._pool is not None:
                self._pool.closeall()


def dsn_from_env(env) -> str | None:
    """Build a DSN, preferring explicit parts over a ready-made URI.

    CNPG publishes a `uri` in the cluster's app secret, but it points at the
    cluster's own read-write service. Routing through PgBouncer means keeping
    the same credentials and changing only the host, which a prebuilt URI does
    not allow - hence assembling it from parts when DB_HOST is set.
    """
    host = env.get("DB_HOST")
    if not host:
        return env.get("DATABASE_URL")
    user = env.get("DB_USER", "")
    password = env.get("DB_PASSWORD", "")
    port = env.get("DB_PORT", "5432")
    name = env.get("DB_NAME", "")
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


def connect_with_retry(dsn: str, attempts: int = 30, delay_s: float = 2.0) -> None:
    """Block until Postgres accepts a connection.

    Called before the pool is built at startup: CNPG can take a while to
    finish bootstrapping, and a pod that gives up immediately just
    CrashLoopBackOffs its way through the wait instead of starting cleanly.
    """
    import time

    last: Exception | None = None
    for _ in range(attempts):
        try:
            conn = psycopg2.connect(dsn, connect_timeout=3)
            conn.close()
            return
        except Exception as exc:  # noqa: BLE001 - any failure here is retryable
            last = exc
            time.sleep(delay_s)
    raise RuntimeError(f"postgres not reachable after {attempts} attempts: {last}")
