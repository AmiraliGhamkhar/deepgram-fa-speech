"""Reconnection policy: exponential backoff with bounded jitter, and a hard
stop for non-retryable errors (auth/config/shutdown).
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Protocol

from .base import ProviderError


class _UniformRandom(Protocol):
    def uniform(self, a: float, b: float) -> float: ...


_default_rng: _UniformRandom = random


@dataclass(frozen=True)
class ReconnectPolicy:
    base_delay: float = 2.0
    max_delay: float = 30.0
    jitter: float = 0.5  # fraction of the computed delay, e.g. 0.5 = +/-50%
    max_attempts: int = 0  # 0 = unlimited


@dataclass(frozen=True)
class ReconnectDecision:
    should_retry: bool
    delay_seconds: float
    reason: str


def decide(policy: ReconnectPolicy, error: ProviderError, attempt: int) -> ReconnectDecision:
    """Decide whether to retry `error` on the `attempt`-th reconnect
    (1-indexed), and how long to wait first.

    Auth/config/shutdown errors are never retried, regardless of attempt
    count -- reconnecting on a revoked API key or invalid parameters would
    spin forever without ever succeeding.
    """
    if not error.category.is_retryable:
        return ReconnectDecision(False, 0.0, f"non-retryable error category: {error.category.value}")

    if policy.max_attempts and attempt > policy.max_attempts:
        return ReconnectDecision(False, 0.0, f"max reconnect attempts ({policy.max_attempts}) reached")

    delay = exponential_backoff_delay(policy, attempt)
    return ReconnectDecision(True, delay, f"retryable error category: {error.category.value}")


def exponential_backoff_delay(
    policy: ReconnectPolicy, attempt: int, rng: _UniformRandom = _default_rng
) -> float:
    """Full-jitter-bounded exponential backoff:
    delay = min(max_delay, base * 2^(attempt-1)), then +/- jitter fraction.
    """
    attempt = max(1, attempt)
    raw = policy.base_delay * (2 ** (attempt - 1))
    capped = min(policy.max_delay, raw)
    if policy.jitter <= 0:
        return capped
    spread = capped * policy.jitter
    jittered = capped + rng.uniform(-spread, spread)
    return max(0.0, jittered)
