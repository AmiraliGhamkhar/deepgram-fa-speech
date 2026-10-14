"""Bounded synchronous streaming harness shared by live diagnostics.

No credentials are loaded here. The caller supplies a configured provider and
must explicitly opt in before invoking this module against a real service.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Iterable

from .base import ErrorCategory, OnError, OnTranscript, ProviderError, STTProvider


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
    """Listen concurrently, wait for readiness, pace PCM, then drain finals."""
    if bytes_per_second <= 0:
        raise ValueError("bytes_per_second must be positive")
    errors: list[ProviderError] = []
    finished = threading.Event()
    provider.validate_config()
    provider.prepare()

    def error(exc: ProviderError) -> None:
        errors.append(exc)
        on_error(exc)

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
        while not provider.wait_ready(0.05):
            if errors:
                raise errors[0]
            if finished.is_set():
                raise ProviderError(ErrorCategory.NETWORK, "Provider exited before readiness")
            if time.monotonic() >= deadline:
                raise ProviderError(ErrorCategory.TIMEOUT, "Provider readiness timed out")
        if on_ready is not None:
            on_ready()
        for chunk in chunks:
            if errors:
                raise errors[0]
            if finished.is_set():
                raise ProviderError(ErrorCategory.NETWORK, "Provider exited during audio transmission")
            provider.send_audio(chunk)
            finished.wait(len(chunk) / bytes_per_second)
    finally:
        # Production stop has its own bounded finalize path. Keep a bound for
        # third-party diagnostic providers too.
        closer = threading.Thread(target=provider.stop, daemon=True, name="diagnostic-close")
        closer.start()
        closer.join(1.2)
        worker.join(join_timeout)
        if worker.is_alive() or closer.is_alive():
            provider.abort()
            worker.join(1.0)
            raise ProviderError(ErrorCategory.TIMEOUT, "Provider did not shut down")
    if errors and any(exc.category != ErrorCategory.SHUTDOWN for exc in errors):
        raise next(exc for exc in errors if exc.category != ErrorCategory.SHUTDOWN)
