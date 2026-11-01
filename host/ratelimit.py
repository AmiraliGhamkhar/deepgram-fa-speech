"""In-process rate limiters with bounded memory.

Two shapes are needed and they are not interchangeable:

* `TokenBucketRateLimiter` -- burst tolerant, per authenticated client and a
  global ceiling. 50 clinicians pressing Start together is *legitimate*
  traffic that must succeed; one client looping on the same request is not.
* `RateLimiter` -- a fixed window, used only for *failed authentication*,
  where burst tolerance would be exactly wrong.

Both are per process (no Redis, no extra moving parts) and both bound their
own state, so a stream of unique keys cannot grow memory without limit.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict, defaultdict, deque
from typing import Callable, Deque, Dict, Tuple

#: Upper bound on the number of keys a limiter keeps. Without this, a stream
#: of unique keys (a scanner, a botnet, or a proxy that forwards unpredictable
#: addresses) would grow the table without bound.
MAX_TRACKED_CLIENTS = 10_000

#: `allow()` calls between opportunistic purges of expired keys.
PURGE_INTERVAL_CALLS = 256

class RateLimiter:
    """Fixed-window request counter with bounded memory.

    Retained for the *failed-authentication* limiter, where a small fixed
    window over a raw IP is exactly the right shape: brute-force attempts
    are not legitimate traffic and must not be burst-tolerant.

    In-process and therefore per-worker; for a single small service that is
    the right trade-off (no Redis, no extra moving parts). Put a reverse
    proxy in front for multi-worker deployments.

    Client keys are reclaimed opportunistically during `allow()`: entries
    whose most recent hit is older than the window can no longer block
    anything, so they are dropped. The table is also capped, so a stream of
    unique client addresses cannot grow memory without bound.
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        max_tracked_clients: int = MAX_TRACKED_CLIENTS,
        purge_interval_calls: int = PURGE_INTERVAL_CALLS,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._max_tracked_clients = max(1, max_tracked_clients)
        self._purge_interval_calls = max(1, purge_interval_calls)
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._calls = 0

    def is_locked(self, client_id: str) -> Tuple[bool, int]:
        """True if this key is currently over its limit. Consumes nothing.

        Used to gate *failed* authentication: legitimate traffic must not
        spend the brute-force budget, so the budget is only charged when a
        request actually fails to authenticate.
        """
        now = self._clock()
        cutoff = now - self._window
        with self._lock:
            hits = self._hits.get(client_id)
            if not hits:
                return False, 0
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self._limit:
                retry_after = max(1, int(hits[0] + self._window - now) + 1)
                return True, retry_after
            return False, 0

    @property
    def tracked_clients(self) -> int:
        """Number of client keys currently held (test/observability hook)."""
        with self._lock:
            return len(self._hits)

    def allow(self, client_id: str) -> Tuple[bool, int]:
        """Return (allowed, seconds until the window resets)."""
        now = self._clock()
        cutoff = now - self._window
        with self._lock:
            self._calls += 1
            # Periodic sweep first, so an over-limit client cannot prevent
            # reclamation by never getting an allowed request.
            if self._calls % self._purge_interval_calls == 0:
                self._purge(cutoff)
            hits = self._hits[client_id]
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self._limit:
                retry_after = max(1, int(hits[0] + self._window - now) + 1)
                return False, retry_after
            hits.append(now)
            if len(self._hits) > self._max_tracked_clients:
                self._purge(cutoff)
            return True, 0

    def _purge(self, cutoff: float) -> None:
        """Drop keys whose newest hit is already outside the window.

        Called with `self._lock` held.
        """
        stale = [key for key, hits in self._hits.items() if not hits or hits[-1] <= cutoff]
        for key in stale:
            del self._hits[key]
        if len(self._hits) <= self._max_tracked_clients:
            return
        # Still over the cap means more than `max_tracked_clients` *active*
        # clients in one window: evict the least recently active keys. This
        # can only under-count abusive clients, never lock anyone out.
        ordered = sorted(self._hits.items(), key=lambda item: item[1][-1])
        for key, _ in ordered[: len(self._hits) - self._max_tracked_clients]:
            del self._hits[key]

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
            self._calls = 0

class TokenBucketRateLimiter:
    """Burst-tolerant token bucket with bounded, self-expiring state.

    A fixed window is the wrong shape for session startup: 50 clinicians
    pressing Start at the same moment is *legitimate* traffic that must
    succeed, whereas a client looping on one request is not. A token bucket
    separates the two -- `burst` tokens are available immediately (so a
    50-client morning surge passes) and `refill_per_second` limits the
    sustained rate.

    Memory is bounded twice over: buckets idle for a full refill period are
    dropped, and the table is hard-capped with least-recently-used eviction.
    """

    def __init__(
        self,
        capacity: int,
        refill_per_second: float,
        clock: Callable[[], float] = time.monotonic,
        max_tracked_keys: int = MAX_TRACKED_CLIENTS,
        purge_interval_calls: int = PURGE_INTERVAL_CALLS,
    ) -> None:
        self._capacity = max(1, capacity)
        self._refill_per_second = max(0.001, float(refill_per_second))
        self._clock = clock
        self._max_tracked_keys = max(1, max_tracked_keys)
        self._purge_interval_calls = max(1, purge_interval_calls)
        # key -> [tokens, last_refill_monotonic]
        self._buckets: OrderedDict[str, list] = OrderedDict()
        self._lock = threading.Lock()
        self._calls = 0

    @property
    def tracked_keys(self) -> int:
        """Number of buckets currently held (test/observability hook)."""
        with self._lock:
            return len(self._buckets)

    def allow(self, key: str, cost: float = 1.0) -> Tuple[bool, int]:
        """Return (allowed, retry_after_seconds).

        A rejected request does **not** consume a token, so a client that
        waits for `retry_after` and retries will be served.
        """
        now = self._clock()
        with self._lock:
            self._calls += 1
            if self._calls % self._purge_interval_calls == 0:
                self._purge(now)

            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = [float(self._capacity), now]
                self._buckets[key] = bucket
            else:
                self._buckets.move_to_end(key)
                elapsed = max(0.0, now - bucket[1])
                bucket[0] = min(
                    float(self._capacity), bucket[0] + elapsed * self._refill_per_second
                )
                bucket[1] = now

            if bucket[0] >= cost:
                bucket[0] -= cost
                if len(self._buckets) > self._max_tracked_keys:
                    self._evict_locked()
                return True, 0

            missing = cost - bucket[0]
            retry_after = max(1, int(missing / self._refill_per_second) + 1)
            if len(self._buckets) > self._max_tracked_keys:
                self._evict_locked()
            return False, retry_after

    def _purge(self, now: float) -> None:
        """Drop buckets that have refilled to capacity (i.e. idle).

        Called with the lock held.
        """
        full_refill = self._capacity / self._refill_per_second
        stale = [
            key
            for key, bucket in self._buckets.items()
            if now - bucket[1] >= full_refill
        ]
        for key in stale:
            del self._buckets[key]

    def _evict_locked(self) -> None:
        """Enforce the hard cap by dropping the least recently used buckets."""
        while len(self._buckets) > self._max_tracked_keys:
            self._buckets.popitem(last=False)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()
            self._calls = 0
