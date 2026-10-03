"""Shutdown / restart lifecycle tests for `LiveMedicalSTT._run_session`.

The bug these cover: Stop used to set the stop flag and immediately
finalize the Deepgram stream while the sender thread still held captured
audio in the queue, so the last words of a dictation never reached the
provider. These tests use a fake audio device and a fake provider, so the
ordering is proven deterministically without a microphone or a network.
"""
from __future__ import annotations

import threading
import time

import pytest

from medical_stt.injection.backend import DryRunBackend
from medical_stt.injection.text_injector import TextInjector
from medical_stt.stt.base import (
    ErrorCategory,
    OnError,
    OnTranscript,
    ProviderError,
    STTProvider,
    TranscriptEvent,
)


class _FakeInputStream:
    """Stands in for sounddevice.RawInputStream."""

    def __init__(self, **kwargs) -> None:
        self.callback = kwargs.get("callback")

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _FakeSoundDevice:
    RawInputStream = _FakeInputStream


class BlockingProvider(STTProvider):
    """Records send/finalize ordering; blocks the sender on demand."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.chunks: list[bytes] = []
        self.started = threading.Event()
        self.first_send_entered = threading.Event()
        self.first_send_gate = threading.Event()
        self._release_start = threading.Event()
        self._lock = threading.Lock()
        self._first_send = True
        self.finalized = False

    def validate_config(self) -> None:
        pass

    def start(self, on_transcript: OnTranscript, on_error: OnError) -> None:
        self.started.set()
        self._release_start.wait(timeout=10.0)

    def send_audio(self, chunk: bytes) -> None:
        if self._first_send:
            self._first_send = False
            self.first_send_entered.set()
            self.first_send_gate.wait(timeout=5.0)
        with self._lock:
            self.chunks.append(chunk)
            self.events.append(f"send:{chunk[0]}")

    def stop(self) -> None:
        with self._lock:
            self.finalized = True
            self.events.append("stop")
        self._release_start.set()

    def release_sender(self) -> None:
        self.first_send_gate.set()


class EchoProvider(STTProvider):
    """Emits one final-but-not-endpointed segment, then blocks."""

    def __init__(self, events) -> None:
        self._events = events
        self.started = threading.Event()
        self._release = threading.Event()

    def validate_config(self) -> None:
        pass

    def start(self, on_transcript: OnTranscript, on_error: OnError) -> None:
        self.started.set()
        for event in self._events:
            on_transcript(event)
        self._release.wait(timeout=10.0)

    def send_audio(self, chunk: bytes) -> None:
        pass

    def stop(self) -> None:
        self._release.set()


class FailingSendProvider(STTProvider):
    """Fails on the first send, like a websocket that has gone away."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self._release = threading.Event()

    def validate_config(self) -> None:
        pass

    def start(self, on_transcript: OnTranscript, on_error: OnError) -> None:
        self.started.set()
        self._release.wait(timeout=10.0)

    def send_audio(self, chunk: bytes) -> None:
        raise ProviderError(ErrorCategory.NETWORK, "connection reset")

    def stop(self) -> None:
        self._release.set()


@pytest.fixture(autouse=True)
def _host_credentials(monkeypatch):
    monkeypatch.setenv("MEDICALSTT_HOST_URL", "https://stt.example.com")
    monkeypatch.setenv("MEDICALSTT_HOST_SECRET", "test-shared-secret-not-real")
    yield


def _build_session(provider: STTProvider, monkeypatch):
    from medical_stt import app as app_module

    monkeypatch.setattr(app_module, "load_sounddevice", lambda: _FakeSoundDevice)
    stt = app_module.LiveMedicalSTT(provider=provider)
    backend = DryRunBackend()
    stt.injector = TextInjector(backend=backend, paste_settle_seconds=0.0)
    stt.overlay.enabled = False
    return stt, backend


def _start_session(stt):
    """Run one session on a thread, surfacing the expected ProviderError.

    `LiveMedicalSTT.run()` is the real caller and catches this; the tests
    drive `_run_session()` directly, so they collect it instead.
    """
    raised: list = []

    def target() -> None:
        try:
            stt._run_session()
        except ProviderError as exc:  # the documented contract for a failed session
            raised.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, raised


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_queued_audio_is_sent_before_the_stream_is_finalized(monkeypatch):
    provider = BlockingProvider()
    stt, _backend = _build_session(provider, monkeypatch)

    session = threading.Thread(target=stt._run_session, daemon=True)
    session.start()
    try:
        assert provider.started.wait(timeout=5.0), "provider never started"

        for index in range(3):
            assert stt._audio_q.put_nowait(bytes([index])) is True

        # The sender is now blocked inside the first send, so the other two
        # chunks are still queued -- exactly the state that used to lose audio.
        assert provider.first_send_entered.wait(timeout=5.0)
        stt.request_stop()
        assert stt.audio_queue_stats.depth >= 1, "test requires audio queued at Stop"

        provider.release_sender()
        session.join(timeout=10.0)
        assert not session.is_alive()
    finally:
        provider.release_sender()
        stt.request_stop()

    assert provider.chunks == [bytes([0]), bytes([1]), bytes([2])]
    assert provider.events[-1] == "stop", (
        "the stream must be finalized only after every queued chunk was sent"
    )
    assert stt.audio_queue_stats.depth == 0


def test_stop_with_an_empty_queue_still_finalizes(monkeypatch):
    provider = BlockingProvider()
    provider.release_sender()
    stt, _backend = _build_session(provider, monkeypatch)

    session = threading.Thread(target=stt._run_session, daemon=True)
    session.start()
    assert provider.started.wait(timeout=5.0)
    stt.request_stop()
    session.join(timeout=10.0)

    assert not session.is_alive()
    assert provider.finalized is True
    assert provider.events in ([], ["stop"]) or provider.events[-1] == "stop"


def test_final_but_not_endpointed_utterance_is_injected_on_shutdown(monkeypatch):
    """A segment Deepgram finalizes without `speech_final` must not vanish."""
    provider = EchoProvider([TranscriptEvent("فشار خون 120/80", is_final=True, speech_final=False)])
    stt, backend = _build_session(provider, monkeypatch)

    session = threading.Thread(target=stt._run_session, daemon=True)
    session.start()
    assert provider.started.wait(timeout=5.0)
    assert _wait_for(lambda: len(stt._utterance._segments) == 1)  # noqa: SLF001 - test hook
    stt.request_stop()
    session.join(timeout=10.0)

    assert not session.is_alive()
    assert backend.pasted, "the buffered final utterance was never injected"
    assert "فشار خون 120/80" in backend.pasted[-1]


def test_send_failure_stops_the_sender_without_losing_the_session(monkeypatch):
    provider = FailingSendProvider()
    stt, _backend = _build_session(provider, monkeypatch)

    session, raised = _start_session(stt)
    assert provider.started.wait(timeout=5.0)
    # Queued after the session reset, so the sender is the one that sees it.
    assert stt._audio_q.put_nowait(b"\x00") is True  # noqa: SLF001 - test hook
    session.join(timeout=10.0)
    assert not session.is_alive()

    assert len(stt._errors) == 1, "one failure must be reported exactly once"
    assert stt._errors[0].category is ErrorCategory.NETWORK
    assert [exc.category for exc in raised] == [ErrorCategory.NETWORK]


def test_sessions_can_be_started_and_stopped_repeatedly(monkeypatch):
    provider = BlockingProvider()
    provider.release_sender()
    stt, backend = _build_session(provider, monkeypatch)

    for _ in range(3):
        session = threading.Thread(target=stt._run_session, daemon=True)
        session.start()
        assert provider.started.wait(timeout=5.0)
        provider.started.clear()
        stt.request_stop()
        session.join(timeout=10.0)
        assert not session.is_alive()

    # Per-session state is reset, so nothing from session N leaks into N+1.
    assert stt._audio_q.qsize() == 0  # noqa: SLF001 - test hook
    assert stt._utterance._segments == []  # noqa: SLF001 - test hook
    assert backend.pasted == []


def test_restart_after_a_provider_error_is_clean(monkeypatch):
    failing = FailingSendProvider()
    stt, _backend = _build_session(failing, monkeypatch)
    session, raised = _start_session(stt)
    assert failing.started.wait(timeout=5.0)
    assert stt._audio_q.put_nowait(b"\x00") is True  # noqa: SLF001 - test hook
    session.join(timeout=10.0)
    assert stt._errors, "the failed session reported an error"

    # A second session must start with clean events (the stop flag set by
    # the failure must not leak into it).
    recovering = BlockingProvider()
    recovering.release_sender()
    stt.provider = recovering
    session = threading.Thread(target=stt._run_session, daemon=True)
    session.start()
    assert recovering.started.wait(timeout=5.0), "a restart after an error did not run"
    stt.request_stop()
    session.join(timeout=10.0)
    assert not session.is_alive()
    assert stt._errors == []
