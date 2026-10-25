"""Regression pins for the audit follow-up batch (runner, census, DPAPI,
single-instance, data-dir and host terminal-log fixes).

Every test here is logic-level: no Windows, microphone, network or
credentials are required.
"""
from __future__ import annotations

import logging
import threading
import time


from medical_stt.processing.negation import census_fully_negated_sources
from medical_stt.stt.base import ErrorCategory, ProviderError, STTProvider, TranscriptEvent
from medical_stt.stt.runner import stream_audio


# -- C6: diagnostic runner ---------------------------------------------------


class _RunnerProvider(STTProvider):
    """Provider whose listener blocks until `stop()` releases it."""

    def __init__(self, *, error: ProviderError | None = None, slow_stop: bool = False) -> None:
        self.released = threading.Event()
        self.error_sent = threading.Event()
        self.audio: list[bytes] = []
        self.aborted = False
        self._error = error
        self._slow_stop = slow_stop

    def validate_config(self) -> None:
        return None

    def wait_ready(self, timeout: float) -> bool:
        if self._error is not None:
            return self.error_sent.wait(timeout)
        return True

    def start(self, on_transcript, on_error) -> None:  # noqa: ANN001 - provider protocol
        if self._error is not None:
            on_error(self._error)
            self.error_sent.set()
        else:
            on_transcript(TranscriptEvent("final", True))
        self.released.wait(10)

    def send_audio(self, chunk: bytes) -> None:
        self.audio.append(chunk)

    def stop(self) -> None:
        self.released.set()
        if self._slow_stop:
            # The listener can finish (all finals were delivered in start)
            # while this cleanup keeps running past the stop budget.
            time.sleep(3.0)

    def abort(self) -> None:
        self.aborted = True
        self.released.set()


def test_runner_slow_stop_after_finals_does_not_raise_timeout():
    provider = _RunnerProvider(slow_stop=True)
    transcripts: list[TranscriptEvent] = []
    stream_audio(
        provider, [b"one"], transcripts.append, lambda _e: None,
        bytes_per_second=100000, join_timeout=2.0,
    )
    assert [t.text for t in transcripts] == ["final"]
    assert provider.aborted is False


def test_runner_ignores_shutdown_errors_mid_stream():
    provider = _RunnerProvider(
        error=ProviderError(ErrorCategory.SHUTDOWN, "deliberate shutdown")
    )
    delivered: list[ProviderError] = []
    stream_audio(
        provider, [b"one"], lambda _e: None, delivered.append,
        bytes_per_second=100000, join_timeout=2.0,
    )
    assert provider.audio == [b"one"], "a SHUTDOWN notice must not abort streaming"
    assert len(delivered) == 1


def test_runner_does_not_double_surface_a_callback_delivered_error():
    provider = _RunnerProvider(
        error=ProviderError(ErrorCategory.NETWORK, "connection reset")
    )
    delivered: list[ProviderError] = []
    # No raise: the error already reached the caller through on_error.
    stream_audio(
        provider, [b"one"], lambda _e: None, delivered.append,
        bytes_per_second=100000, join_timeout=2.0,
    )
    assert len(delivered) == 1
    assert provider.audio == [], "streaming stops on a real error, without re-raising it"


# -- C6: negation census -----------------------------------------------------


def test_negation_census_warns_about_sources_that_can_never_fire(caplog):
    with caplog.at_level(logging.WARNING, logger="medical_stt.processing.negation"):
        assert census_fully_negated_sources(["بیوپسی", "ندارد", "کاتتر"]) == 1
    assert "negation" in caplog.text
    assert census_fully_negated_sources(["بیوپسی"]) == 0
