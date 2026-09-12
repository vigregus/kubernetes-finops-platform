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


class PooledPostgres:
    def __init__(
        self,
        dsn: str,
        minconn: int = 1,
        maxconn: int = 8,
        statement_timeout_ms: int = 5000,
        connect_timeout_s: int = 3,
        application_name: str = "app",
    ):
        self.dsn = dsn
        self.maxconn = maxconn
        self._lock = threading.Lock()
        # statement_timeout is set as a connection option rather than per
        # query: a hung statement should be bounded even on code paths that
        # forget to think about it.
        self._pool = pg_pool.ThreadedConnectionPool(
            minconn,
            maxconn,
            dsn=dsn,
            connect_timeout=connect_timeout_s,
            application_name=application_name,
            options=f"-c statement_timeout={statement_timeout_ms}",
        )

    @contextmanager
    def cursor(self, commit: bool = False):
        """Borrow a connection, yield a cursor, always return the connection."""
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
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

    def healthy(self) -> bool:
        """Used by the readiness probe: can we actually get a usable connection?"""
        try:
            with self.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            return True
        except Exception:
            return False

    def close(self) -> None:
        with self._lock:
            if self._pool is not None:
                self._pool.closeall()


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
