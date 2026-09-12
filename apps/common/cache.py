"""A read-through response cache with the parts that make one production-grade.

Redis was previously used here only as an idempotency-key store and a counter -
never to cache a response. That left hit ratio a fiction and sent every read to
Postgres, which in turn made the database tier's size look like a fixed cost
rather than a consequence of how well the cache works.

Four properties separate this from a dictionary with a TTL, and each exists
because of a specific production failure:

* **Single-flight.** On a miss, one caller loads and the rest wait for its
  result. Without it, N concurrent requests for the same cold key become N
  identical database queries - the thundering herd that turns a deploy or an
  eviction into an outage.
* **Negative caching.** A miss is cached too, with a shorter TTL. Otherwise a
  hot key that does not exist costs nothing to request and a full query to
  answer, every time.
* **TTL jitter.** Identical TTLs expire together, which converts steady load
  into a sawtooth against the database. A spread avoids re-synchronising.
* **Versioned keys.** Invalidating everything is a version bump in the key
  prefix, not a SCAN and DELETE across a live Redis.

Warming is the other half (see `warm`): a pod that starts with a cold cache
briefly *increases* database load, so scaling out under pressure can make
things worse before it makes them better. Warming before readiness turns that
into a measurable choice rather than a surprise.
"""
from __future__ import annotations

import json
import random
import time
import uuid

MISS_SENTINEL = "\x00__absent__"


class CacheResult:
    """What happened, so the caller can label metrics honestly."""

    HIT = "hit"
    NEGATIVE = "negative"  # cached absence - still a hit, but worth counting apart
    MISS = "miss"  # this caller loaded it
    WAITED = "waited"  # someone else was loading; we used their result


class ResponseCache:
    def __init__(
        self,
        redis_client,
        namespace: str,
        ttl_s: float = 60.0,
        negative_ttl_s: float = 10.0,
        jitter_ratio: float = 0.2,
        lock_ttl_s: float = 5.0,
        wait_timeout_s: float = 0.5,
        wait_poll_s: float = 0.02,
    ):
        self.redis = redis_client
        self.namespace = namespace
        self.ttl_s = ttl_s
        self.negative_ttl_s = negative_ttl_s
        self.jitter_ratio = jitter_ratio
        self.lock_ttl_s = lock_ttl_s
        self.wait_timeout_s = wait_timeout_s
        self.wait_poll_s = wait_poll_s
        self._version_key = f"{namespace}:version"

    # ---- keys ----------------------------------------------------------

    def version(self) -> int:
        """Current key-space version. One round trip, and it is cacheable
        client-side if this ever becomes hot."""
        if self.redis is None:
            return 0
        raw = self.redis.get(self._version_key)
        return int(raw) if raw else 1

    def _key(self, key: str, version: int) -> str:
        return f"{self.namespace}:v{version}:{key}"

    def _lock_key(self, key: str, version: int) -> str:
        return f"{self.namespace}:v{version}:lock:{key}"

    def _ttl_with_jitter(self, base: float) -> float:
        if self.jitter_ratio <= 0:
            return base
        return base * (1.0 + random.uniform(-self.jitter_ratio, self.jitter_ratio))

    # ---- read path -----------------------------------------------------

    def get_or_load(self, key: str, loader):
        """Return (value, CacheResult). `loader()` returns the value or None.

        None is cached as an absence rather than treated as "nothing to
        cache" - a missing row is an answer, and answering it from Redis is
        the whole point of negative caching.
        """
        if self.redis is None:
            return loader(), CacheResult.MISS

        version = self.version()
        full = self._key(key, version)

        cached = self.redis.get(full)
        if cached is not None:
            return self._decode(cached)

        # Miss. Try to become the single loader for this key.
        token = uuid.uuid4().hex
        lock = self._lock_key(key, version)
        got_lock = self.redis.set(lock, token, nx=True, px=int(self.lock_ttl_s * 1000))

        if not got_lock:
            # Someone else is loading it. Wait briefly for their result rather
            # than issuing a duplicate query.
            waited = self._wait_for(full)
            if waited is not None:
                value, _ = self._decode(waited)
                return value, CacheResult.WAITED
            # The holder died or is slower than our patience. Load anyway -
            # a duplicate query is better than failing the request.
            return loader(), CacheResult.MISS

        try:
            value = loader()
            self._store(full, value)
            return value, CacheResult.MISS
        finally:
            # Release only our own lock: a lock that expired and was retaken
            # belongs to someone else now.
            try:
                if self.redis.get(lock) == token.encode():
                    self.redis.delete(lock)
            except Exception:
                pass

    def _wait_for(self, full_key: str):
        deadline = time.monotonic() + self.wait_timeout_s
        while time.monotonic() < deadline:
            time.sleep(self.wait_poll_s)
            cached = self.redis.get(full_key)
            if cached is not None:
                return cached
        return None

    def _store(self, full_key: str, value) -> None:
        if value is None:
            payload = MISS_SENTINEL
            ttl = self._ttl_with_jitter(self.negative_ttl_s)
        else:
            payload = json.dumps(value, default=str)
            ttl = self._ttl_with_jitter(self.ttl_s)
        try:
            self.redis.set(full_key, payload, ex=max(1, int(ttl)))
        except Exception:
            # A cache that cannot write must not fail the request it was
            # meant to accelerate.
            pass

    @staticmethod
    def _decode(raw):
        text = raw.decode() if isinstance(raw, bytes) else raw
        if text == MISS_SENTINEL:
            return None, CacheResult.NEGATIVE
        try:
            return json.loads(text), CacheResult.HIT
        except json.JSONDecodeError:
            return None, CacheResult.MISS

    # ---- invalidation --------------------------------------------------

    def invalidate(self, key: str) -> None:
        """Drop one key. Used on write, where the new value is known to differ."""
        if self.redis is None:
            return
        try:
            self.redis.delete(self._key(key, self.version()))
        except Exception:
            pass

    def invalidate_all(self) -> int:
        """Invalidate the whole namespace by bumping its version.

        O(1) and safe on a live instance. The orphaned keys are never read
        again and expire on their own TTL, which costs some memory for a TTL's
        worth of time - the trade against SCAN+DELETE, which is O(keyspace)
        and blocks a single-threaded Redis while it runs.
        """
        if self.redis is None:
            return 0
        return int(self.redis.incr(self._version_key))

    # ---- warming -------------------------------------------------------

    def warm(self, items) -> int:
        """Populate from (key, value) pairs. Returns how many were written.

        Called before readiness so a new pod does not serve its first requests
        straight into the database. Scaling out with cold caches raises
        database load exactly when the system is already under pressure.
        """
        if self.redis is None:
            return 0
        version = self.version()
        count = 0
        for key, value in items:
            self._store(self._key(key, version), value)
            count += 1
        return count
