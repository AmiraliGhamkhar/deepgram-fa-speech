"""Capacity limiting for the host: per-identity, global, and in-flight.

Three different questions, deliberately answered by three different objects:

* **How fast may one clinician start sessions?** -- `TokenBucketRateLimiter`
  keyed on the authenticated `client_id`. Bursty on purpose, so 50 colleagues
  pressing Start at 09:00 all succeed.
* **How fast may the site as a whole?** -- a second token bucket with a single
  `"global"` key, sized far above the 50-session target.
* **How much upstream work may be in flight?** -- `InflightLimiter`, a bounded
  counter. Past the cap the host answers 503 immediately: an explicit,
  controlled refusal instead of a queue that grows without limit or a socket
  pile-up against Deepgram.

Failed authentication is limited separately, in `ratelimit.RateLimiter`, keyed
on the peer address: it is abuse rather than legitimate traffic, so a shared
hospital NAT must not exempt it.

`Limiters` bundles the five so the app has one dependency to wire and the
concurrency tests have one place to inspect.
"""
from __future__ import annotations

from dataclasses import dataclass

try:  # package import from a full checkout
    from .config import HostSettings
    from .ratelimit import RateLimiter, TokenBucketRateLimiter
except ImportError:  # pragma: no cover - cPanel copies only the host/ dir
    from config import HostSettings  # type: ignore[no-index]
    from ratelimit import RateLimiter, TokenBucketRateLimiter  # type: ignore[no-index]


class InflightLimiter:
    """Bounded counter for concurrent upstream grants or requests.

    50 simultaneous token requests must all be servable, but an unbounded
    number of them must not be allowed to pile up if Deepgram slows down:
    each one holds a response object and a socket.
    """

    def __init__(self, capacity: int) -> None:
        self._capacity = max(1, capacity)
        self._in_flight = 0

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def capacity(self) -> int:
        return self._capacity

    def acquire(self) -> bool:
        """Take a slot, or return False when the caller must be refused."""
        if self._in_flight >= self._capacity:
            return False
        self._in_flight += 1
        return True

    def release(self) -> None:
        if self._in_flight > 0:
            self._in_flight -= 1


@dataclass
class Limiters:
    """Every admission-control object the request path consults."""

    client: TokenBucketRateLimiter
    global_: TokenBucketRateLimiter
    auth_failure: RateLimiter
    inflight: InflightLimiter
    active: InflightLimiter

    @classmethod
    def from_settings(cls, settings: HostSettings) -> "Limiters":
        """Size every limiter from the operator's configuration.

        The per-client bucket is generous and burst tolerant; the global
        ceiling protects the host from the aggregate while staying far above
        the 50-concurrent-session target.
        """
        window = settings.rate_limit_window_seconds
        return cls(
            client=TokenBucketRateLimiter(
                capacity=settings.client_burst,
                refill_per_second=settings.client_rate_limit_requests / window,
            ),
            global_=TokenBucketRateLimiter(
                capacity=settings.global_burst,
                refill_per_second=settings.global_rate_limit_requests / window,
            ),
            auth_failure=RateLimiter(
                limit=settings.auth_failure_limit, window_seconds=window
            ),
            inflight=InflightLimiter(
                min(settings.max_inflight_grants, settings.grant_max_connections)
            ),
            active=InflightLimiter(settings.max_active_requests),
        )
