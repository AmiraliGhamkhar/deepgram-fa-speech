"""What happens *between* sessions: retry, back off, or stop for good.

A session either ends cleanly (the user pressed Stop) or raises a
`ProviderError`. Turning that error into "retry in 4.2 s" / "stop, this is a
configuration fault" is policy, and policy is kept out of the session
lifecycle itself so the whole retry table can be read -- and reasoned about --
in one place.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from ..stt.base import ErrorCategory, ProviderError
from ..stt.reconnect import ReconnectDecision, ReconnectPolicy, decide
from .messages import CAPACITY_WARNING, MICROPHONE_UNAVAILABLE_WARNING
from .runtime import SessionHost

log = logging.getLogger("medical_stt.app")

#: A missing/unusable microphone is not a network condition. Retrying it
#: forever burns session tokens and shows "reconnecting" instead of the real
#: cause. Two consecutive MICROPHONE failures end the session with an error.
MAX_MICROPHONE_FAILURES = 2

#: A connection that lasted at least this long resets the failure streak: the
#: stream worked, so the next failure is a new incident rather than a
#: continuation of a broken start-up.
HEALTHY_SESSION_SECONDS = 30.0


class ReconnectSupervisor:
    """Runs sessions until one succeeds, the user stops, or retrying is wrong."""

    def __init__(
        self,
        host: SessionHost,
        policy: ReconnectPolicy,
        run_session: Callable[[], None],
        decider: Optional[Callable[..., ReconnectDecision]] = None,
    ) -> None:
        self._host = host
        self._policy = policy
        self._run_session = run_session
        # Resolved here, not as a default argument: the default would bind the
        # function object at class-definition time and ignore a patched
        # `supervisor.decide`.
        self._decide = decider or decide

    def run(self) -> int:
        """Return the process exit code for this dictation."""
        host = self._host
        exit_code = 0
        consecutive_failures = 0
        microphone_failures = 0

        while not host.shutdown_event.is_set():
            try:
                self._run_session()
                break
            except KeyboardInterrupt:
                break
            except ProviderError as exc:
                if host.shutdown_event.is_set():
                    # The user pressed Stop while the failure was being
                    # classified: that is a normal stop, not an error.
                    break
                host.note_reconnect()
                if host.connected_at is not None and \
                        time.monotonic() - host.connected_at >= HEALTHY_SESSION_SECONDS:
                    consecutive_failures = 0
                    microphone_failures = 0
                if exc.category is ErrorCategory.MICROPHONE:
                    microphone_failures += 1
                else:
                    microphone_failures = 0
                consecutive_failures += 1
                host.set_phase("reconnecting")

                if exc.category is ErrorCategory.RATE_LIMIT:
                    # Deepgram's concurrent-stream cap (or the host's limiter)
                    # refused this device: say so instead of showing a generic
                    # "reconnecting" indefinitely.
                    host.warn(CAPACITY_WARNING)
                else:
                    host.clear_warning(CAPACITY_WARNING)

                if exc.category is ErrorCategory.MICROPHONE and \
                        microphone_failures >= MAX_MICROPHONE_FAILURES:
                    log.error(
                        "microphone unavailable after %d consecutive failures; not reconnecting",
                        microphone_failures,
                    )
                    host.set_phase("idle")
                    host.warn(MICROPHONE_UNAVAILABLE_WARNING)
                    return 1

                decision: Optional[ReconnectDecision] = self._decide(
                    self._policy, exc, consecutive_failures, retry_after=exc.retry_after
                )
                if decision is None or not decision.should_retry:
                    log.error("not retrying: %s", "" if decision is None else decision.reason)
                    return 1 if exc.category is not ErrorCategory.SHUTDOWN else 0
                log.warning(
                    "reconnecting: %s — retry in %.1fs (attempt %d)",
                    decision.reason, decision.delay_seconds, host.reconnect_count,
                )
                # Interruptible sleep: Stop must not wait out the backoff.
                if host.shutdown_event.wait(decision.delay_seconds):
                    break
        return exit_code
