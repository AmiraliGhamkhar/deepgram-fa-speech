"""Reconnection policy: exponential backoff with bounded jitter, and a hard
stop for non-retryable errors (auth/config/shutdown).

**Storm resistance.** 50 clients that lose the same upstream at the same
instant must not retry in lockstep, or the recovering provider is hit by a
synchronized 50-request spike. Two properties prevent that:

* *Full jitter.* The delay is drawn uniformly from ``[capped*(1-jitter),
  capped]`` rather than symmetrically around ``capped``. Symmetric jitter
  leaves clients that failed together clustered around the same instant;
  truncating the negative half instead spreads them across a whole window.
* *Per-session randomness.* Each client draws its own sample, and the
  decision path is otherwise deterministic, so nothing coordinates clients.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional, Protocol

from .base import ErrorCategory, ProviderError


class _UniformRandom(Protocol):
    def uniform(self, a: float, b: float) -> float: ...


_default_rng: _UniformRandom = random


@dataclass(frozen=True)
class ReconnectPolicy:
    base_delay: float = 2.0
    max_delay: float = 30.0
    jitter: float = 0.5  # fraction of the computed delay, e.g. 0.5 = up to -50%
    max_attempts: int = 0  # 0 = unlimited
    #: Extra floor applied to RATE_LIMIT retries. A provider that is asking
    #: us to slow down must not be retried at the normal cadence, however
    #: many clients are affected.
    rate_limit_delay: float = 5.0
    #: Upper bound honored from a `Retry-After` hint. A hostile or confused
    #: upstream cannot pin a clinician's session open for hours.
    max_retry_after_seconds: float = 60.0


@dataclass(frozen=True)
class ReconnectDecision:
    should_retry: bool
    delay_seconds: float
    reason: str
    #: True when the delay came from an upstream `Retry-After` hint.
    honored_retry_after: bool = False


def decide(
    policy: ReconnectPolicy,
    error: ProviderError,
    attempt: int,
    retry_after: Optional[float] = None,
) -> ReconnectDecision:
    """Decide whether to retry `error` on the `attempt`-th reconnect
    (1-indexed), and how long to wait first.

    Auth/config/shutdown errors are never retried, regardless of attempt
    count -- reconnecting on a revoked API key or invalid parameters would
    spin forever without ever succeeding.

    `retry_after` is an upstream hint in seconds (the host's HTTP
    `Retry-After`). When present it wins over the computed backoff, but is
    still clamped by `policy.max_retry_after_seconds` so a client recovers
    promptly instead of waiting out a long penalty.
    """
    if not error.category.is_retryable:
        return ReconnectDecision(False, 0.0, f"non-retryable error category: {error.category.value}")

    if policy.max_attempts and attempt > policy.max_attempts:
        return ReconnectDecision(False, 0.0, f"max reconnect attempts ({policy.max_attempts}) reached")

    if retry_after is None:
        # Default to whatever hint the error itself carries, so a caller
        # cannot accidentally drop it by forgetting the argument.
        retry_after = getattr(error, "retry_after", None)

    if retry_after is not None and retry_after > 0:
        # Honor the server, but not unconditionally: an upstream (or a
        # misconfigured proxy) claiming "retry in 6 hours" must not leave a
        # clinician unable to dictate.
        honored = min(float(retry_after), policy.max_retry_after_seconds)
        return ReconnectDecision(
            True,
            honored,
            f"honoring Retry-After of {honored:.0f}s",
            honored_retry_after=True,
        )

    delay = exponential_backoff_delay(policy, attempt)
    if error.category is ErrorCategory.RATE_LIMIT:
        delay = max(delay, policy.rate_limit_delay)
    return ReconnectDecision(True, delay, f"retryable error category: {error.category.value}")


def exponential_backoff_delay(
    policy: ReconnectPolicy, attempt: int, rng: _UniformRandom = _default_rng
) -> float:
    """Full-jitter-bounded exponential backoff.

    ``delay = min(max_delay, base * 2^(attempt-1))`` is the ceiling; the
    actual wait is drawn uniformly from ``[ceiling*(1-jitter), ceiling]``.

    Truncating the negative half rather than jittering symmetrically is what
    decorrelates many clients that failed at the same moment: their retries
    spread across a full window instead of clustering either side of it.
    """
    attempt = max(1, attempt)
    raw = policy.base_delay * (2 ** (attempt - 1))
    capped = min(policy.max_delay, raw)
    jitter = min(max(policy.jitter, 0.0), 1.0)
    if jitter <= 0:
        return capped
    floor = capped * (1.0 - jitter)
    return max(0.0, rng.uniform(floor, capped))
