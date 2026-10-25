"""Bounded synchronous streaming harness shared by live diagnostics.

No credentials are loaded here. The caller supplies a configured provider and
must explicitly opt in before invoking this module against a real service.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Iterable, Optional

from .base import ErrorCategory, OnError, OnTranscript, ProviderError, STTProvider

log = logging.getLogger("medical_stt.stt.runner")


def _first_real_error(errors: list[ProviderError]) -> Optional[ProviderError]:
    """First error that is not a deliberate-shutdown notice.

    `SHUTDOWN` is filtered the same way at EVERY decision point in this
    harness: the mid-stream checks used to re-raise it while the final
    check ignored it, so a provider that reported its own orderly shutdown
    aborted an otherwise successful diagnostic run.
    """
    return next((exc for exc in errors if exc.category != ErrorCategory.SHUTDOWN), None)


def stream_audio(
    provider: STTProvider,
    chunks: Iterable[bytes],
    on_transcript: OnTranscript,
    on_error: OnError,
    *,
    bytes_per_second: int,
    ready_timeout: float = 25.0,
    join_timeout: float = 6.0,
    on_ready: Callable[[], None] | None = None,
) -> None:
    """Listen concurrently, wait for readiness, pace PCM, then drain finals.

    Error surfacing is single-channel: an error the provider reports (or
    raises out of `start`) is delivered through `on_error` exactly once and
    is NOT raised again from here -- callers used to see the same failure
    twice, once per channel. This function raises only for failures that
    were never delivered through the callback (readiness timeouts, a
    provider that exits silently, a listener that will not stop).
    """
    if bytes_per_second <= 0:
        raise ValueError("bytes_per_second must be positive")
    errors: list[ProviderError] = []
    delivered: set[int] = set()
    finished = threading.Event()
    provider.validate_config()
    provider.prepare()

    def error(exc: ProviderError) -> None:
        errors.append(exc)
        on_error(exc)
        delivered.add(id(exc))

    def listen() -> None:
        try:
            provider.start(on_transcript, error)
        except ProviderError as exc:
            error(exc)
        except Exception as exc:
            error(ProviderError(ErrorCategory.UNKNOWN, "Provider worker failed", cause=exc))
        finally:
            finished.set()

    worker = threading.Thread(target=listen, daemon=True, name="diagnostic-provider")
    worker.start()
    try:
        deadline = time.monotonic() + ready_timeout
        ready = False
        while not ready:
            if _first_real_error(errors) is not None:
                break  # already surfaced via on_error; just stop streaming
            if provider.wait_ready(0.05):
                ready = True
                break
            if finished.is_set():
                raise ProviderError(ErrorCategory.NETWORK, "Provider exited before readiness")
            if time.monotonic() >= deadline:
                raise ProviderError(ErrorCategory.TIMEOUT, "Provider readiness timed out")
        if ready and _first_real_error(errors) is None:
            if on_ready is not None:
                on_ready()
            for chunk in chunks:
                if _first_real_error(errors) is not None:
                    break  # already surfaced via on_error; just stop streaming
                if finished.is_set():
                    raise ProviderError(
                        ErrorCategory.NETWORK, "Provider exited during audio transmission"
                    )
                provider.send_audio(chunk)
                finished.wait(len(chunk) / bytes_per_second)
    finally:
        # Production stop has its own bounded finalize path. Keep a bound for
        # third-party diagnostic providers too.
        closer = threading.Thread(target=provider.stop, daemon=True, name="diagnostic-close")
        closer.start()
        closer.join(1.2)
        worker.join(join_timeout)
        if worker.is_alive():
            provider.abort()
            worker.join(1.0)
            raise ProviderError(ErrorCategory.TIMEOUT, "Provider did not shut down")
        if closer.is_alive():
            # The listener finished, so every final was already delivered;
            # a close that merely outlives its 1.2 s budget must not turn a
            # successful session into a TIMEOUT. The closer is a daemon
            # thread: log it and let it finish on its own.
            log.warning(
                "provider stop exceeded its budget after all finals were "
                "delivered; continuing without it"
            )
    undelivered = [
        exc
        for exc in errors
        if exc.category != ErrorCategory.SHUTDOWN and id(exc) not in delivered
    ]
    if undelivered:
        raise undelivered[0]
