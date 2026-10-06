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


# -- abandoned threads from a superseded session ---------------------------
#
# One `LiveMedicalSTT` is reused across reconnects, so `_stop`, `_errors` and
# the audio queue outlive a single session while the provider and sender
# threads do not. A thread that outlives its join timeout (a wedged provider
# socket, a blocked send) used to keep writing into that shared state -- most
# damagingly by setting `_stop`, which ended the *next* session immediately
# and made `run()` report a clean exit, so dictation stopped with no error.
# Every callback now carries the session generation it was created with.


def _threads_named(name: str) -> list:
    return [thread for thread in threading.enumerate()
            if thread.name == name and thread.is_alive()]


def _fast_teardown(monkeypatch) -> None:
    """Shorten the join timeouts so a wedged thread is abandoned quickly."""
    from medical_stt import app as app_module

    monkeypatch.setattr(app_module, "SENDER_JOIN_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(app_module, "PROVIDER_JOIN_TIMEOUT_SECONDS", 0.2)


class WedgedStartProvider(STTProvider):
    """Session 1's `start()` ignores `stop()` and outlives the join."""

    def __init__(self) -> None:
        self.starts = 0
        self.release_wedge = threading.Event()
        self.wedge_returned = threading.Event()
        self.second_started = threading.Event()
        self.second_release = threading.Event()
        self.second_stopped = False

    def validate_config(self) -> None:
        pass

    def start(self, on_transcript: OnTranscript, on_error: OnError) -> None:
        self.starts += 1
        if self.starts == 1:
            self.release_wedge.wait(timeout=15.0)
            self.wedge_returned.set()
            return
        self.second_started.set()
        self.second_release.wait(timeout=15.0)

    def send_audio(self, chunk: bytes) -> None:
        pass

    def stop(self) -> None:
        # Session 1: a wedged socket ignores the close request entirely.
        if self.starts >= 2:
            self.second_stopped = True
            self.second_release.set()
            self.release_wedge.set()


class CapturingProvider(STTProvider):
    """Blocks in `start()` and records the callbacks it was handed."""

    def __init__(self) -> None:
        self.callbacks: list = []
        self._gates: list = []

    def validate_config(self) -> None:
        pass

    def start(self, on_transcript: OnTranscript, on_error: OnError) -> None:
        gate = threading.Event()
        self._gates.append(gate)
        self.callbacks.append((on_transcript, on_error))
        gate.wait(timeout=15.0)

    def send_audio(self, chunk: bytes) -> None:
        pass

    def stop(self) -> None:
        # Only the second (and later) sessions are stoppable: session 1 stays
        # wedged so its thread is genuinely abandoned and reports late.
        if len(self._gates) >= 2:
            self._gates[-1].set()

    def release_all(self) -> None:
        for gate in self._gates:
            gate.set()


class WedgedSendProvider(STTProvider):
    """Session 1's `send_audio()` blocks past the sender join timeout."""

    def __init__(self) -> None:
        self.starts = 0
        self.second_started = threading.Event()
        self.send_entered = threading.Event()
        self.release_send = threading.Event()
        self.sent: list = []
        self._gates: list = []

    def validate_config(self) -> None:
        pass

    def start(self, on_transcript: OnTranscript, on_error: OnError) -> None:
        self.starts += 1
        gate = threading.Event()
        self._gates.append(gate)
        if self.starts >= 2:
            self.second_started.set()
        gate.wait(timeout=15.0)

    def send_audio(self, chunk: bytes) -> None:
        self.send_entered.set()
        self.release_send.wait(timeout=15.0)
        self.sent.append(chunk)

    def stop(self) -> None:
        # Releases the current session's start() -- so the provider thread
        # exits cleanly and only the *sender* is abandoned -- but not the
        # blocked send, which is what the sender join gives up on.
        if self._gates:
            self._gates[-1].set()

    def release_all(self) -> None:
        self.release_send.set()
        for gate in self._gates:
            gate.set()


def test_an_abandoned_provider_thread_cannot_stop_the_next_session(monkeypatch):
    provider = WedgedStartProvider()
    stt, _backend = _build_session(provider, monkeypatch)
    _fast_teardown(monkeypatch)

    first = threading.Thread(target=stt._run_session, daemon=True)
    first.start()
    try:
        assert _wait_for(lambda: provider.starts == 1), "session 1 never started"

        # Tear session 1 down while its provider thread is still wedged.
        stt._stop.set()
        first.join(timeout=10.0)
        assert not first.is_alive(), "teardown blocked on a wedged provider thread"

        second = threading.Thread(target=stt._run_session, daemon=True)
        second.start()
        assert provider.second_started.wait(timeout=5.0), "the next session never started"

        # The abandoned thread now returns. Before the generation guard this
        # set `_stop`, ending session 2 within one poll interval.
        provider.release_wedge.set()
        assert provider.wedge_returned.wait(timeout=5.0)
        time.sleep(0.4)  # several times the 0.1s the session loop polls `_stop`

        assert not stt._stop.is_set(), "an abandoned thread stopped the current session"
        assert provider.second_stopped is False, "the current session was torn down"
        assert second.is_alive(), "the current session ended"

        stt.request_stop()
        second.join(timeout=10.0)
        assert not second.is_alive()
    finally:
        provider.release_wedge.set()
        provider.second_release.set()
        stt.request_stop()


def test_transcript_and_error_from_a_superseded_session_are_ignored(monkeypatch):
    provider = CapturingProvider()
    stt, backend = _build_session(provider, monkeypatch)
    _fast_teardown(monkeypatch)

    first = threading.Thread(target=stt._run_session, daemon=True)
    first.start()
    try:
        assert _wait_for(lambda: len(provider.callbacks) == 1)
        stale_transcript, stale_error = provider.callbacks[0]

        stt._stop.set()
        first.join(timeout=10.0)
        assert not first.is_alive()

        second = threading.Thread(target=stt._run_session, daemon=True)
        second.start()
        assert _wait_for(lambda: len(provider.callbacks) == 2)

        # Session 1's abandoned thread reports late: its transcript belongs to
        # audio the user has already moved on from, and its error describes a
        # socket the current session is not using.
        stale_transcript(TranscriptEvent("دوز قبلی 500 mg", is_final=True, speech_final=True))
        stale_error(ProviderError(ErrorCategory.NETWORK, "socket from the previous session"))
        time.sleep(0.3)

        assert backend.pasted == [], "a superseded session injected text into the current one"
        assert stt._errors == [], "a superseded session's error was attributed to the current one"
        assert not stt._stop.is_set(), "a superseded session stopped the current one"
        assert second.is_alive()

        stt.request_stop()
        second.join(timeout=10.0)
        assert not second.is_alive()
    finally:
        provider.release_all()
        stt.request_stop()


def test_an_abandoned_sender_stops_consuming_the_shared_queue(monkeypatch):
    """No terminated session may leave a thread draining the live queue."""
    provider = WedgedSendProvider()
    stt, _backend = _build_session(provider, monkeypatch)
    _fast_teardown(monkeypatch)

    first = threading.Thread(target=stt._run_session, daemon=True)
    first.start()
    try:
        assert _wait_for(lambda: provider.starts == 1), "session 1 never started"
        assert stt._audio_q.put_nowait(b"\x01") is True  # noqa: SLF001 - test hook
        assert provider.send_entered.wait(timeout=5.0), "the sender never reached send_audio"

        stt._stop.set()
        first.join(timeout=10.0)
        assert not first.is_alive(), "teardown blocked on a wedged sender"
        assert len(_threads_named("audio-sender")) == 1, "the wedged sender is still alive"

        # Session 2 clears `_stop`, so without the generation guard the
        # abandoned sender would wake up and start draining session 2's queue.
        second = threading.Thread(target=stt._run_session, daemon=True)
        second.start()
        assert provider.second_started.wait(timeout=5.0)
        assert _wait_for(lambda: len(_threads_named("audio-sender")) == 2)

        assert second.is_alive(), "session 2 must still be running"
        assert not stt._stop.is_set(), "test precondition: session 2 cleared the stop flag"

        provider.release_send.set()
        assert _wait_for(lambda: len(_threads_named("audio-sender")) == 1), (
            "a sender left over from session 1 kept consuming the shared audio queue"
        )
        assert provider.sent == [b"\x01"]

        stt.request_stop()
        second.join(timeout=10.0)
        assert not second.is_alive()
    finally:
        provider.release_all()
        stt.request_stop()
