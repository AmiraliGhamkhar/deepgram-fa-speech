"""Session-level behaviour added by the Deepgram concurrency review.

* A microphone that stops delivering frames is reported as such -- not as a
  network failure -- and the stream is held open with provider keep-alives
  until frames resume (Deepgram closes an audio-less stream after ~10 s).
* A rate-limited connection (Deepgram's per-project stream cap, or the
  host's limiter) shows a distinct "service at capacity" message instead of
  a generic "reconnecting", and withdraws it once a session is recording.
* Rate-limit backoff never exceeds the configured ceiling.
* Keyterms are budgeted by characters as well as by count.
"""
from __future__ import annotations

import threading
import time

import pytest

from medical_stt.injection.backend import DryRunBackend
from medical_stt.injection.text_injector import TextInjector
from medical_stt.stt.base import ErrorCategory, OnError, OnTranscript, ProviderError, STTProvider


class _FakeInputStream:
    def __init__(self, **kwargs) -> None:
        self.callback = kwargs.get("callback")

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _FakeSoundDevice:
    RawInputStream = _FakeInputStream


class KeepAliveProvider(STTProvider):
    """Blocks in `start()` until stopped; counts keep-alives."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self._release = threading.Event()
        self.keep_alives = 0
        self.chunks = 0

    def validate_config(self) -> None:
        pass

    def prepare(self) -> None:
        # One instance serves every reconnect attempt (the app reuses an
        # injected provider), so each run gets a fresh release gate.
        self._release = threading.Event()

    def start(self, on_transcript: OnTranscript, on_error: OnError) -> None:
        self.started.set()
        self._release.wait(timeout=10.0)

    def send_audio(self, chunk: bytes) -> None:
        self.chunks += 1

    def keep_alive(self) -> None:
        self.keep_alives += 1

    def stop(self) -> None:
        self._release.set()


class RateLimitedThenFineProvider(KeepAliveProvider):
    """Fails `failures` times as RATE_LIMIT (a 429 at the handshake), then
    behaves like a healthy provider."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self._failures = failures
        self.attempts = 0

    def start(self, on_transcript: OnTranscript, on_error: OnError) -> None:
        self.attempts += 1
        if self.attempts <= self._failures:
            raise ProviderError(ErrorCategory.RATE_LIMIT, "too many concurrent streams")
        super().start(on_transcript, on_error)


@pytest.fixture(autouse=True)
def _host_credentials(monkeypatch):
    monkeypatch.setenv("MEDICALSTT_HOST_URL", "https://stt.example.com")
    monkeypatch.setenv("MEDICALSTT_HOST_SECRET", "test-shared-secret-not-real")
    yield


def _build_session(provider: STTProvider, monkeypatch):
    from medical_stt import app as app_module

    monkeypatch.setattr(app_module, "load_sounddevice", lambda: _FakeSoundDevice)
    # Fast thresholds: the behaviour is the same, the test is not 3 s slower.
    monkeypatch.setattr(app_module, "AUDIO_SILENCE_SECONDS", 0.3)
    monkeypatch.setattr(app_module, "KEEP_ALIVE_INTERVAL_SECONDS", 0.1)
    stt = app_module.LiveMedicalSTT(provider=provider)
    stt.injector = TextInjector(backend=DryRunBackend(), paste_settle_seconds=0.0)
    stt.overlay.enabled = False
    return stt


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _run_on_thread(target):
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


# -- silent microphone -------------------------------------------------------------


def test_silent_microphone_is_reported_and_the_stream_kept_alive(monkeypatch):
    from medical_stt import app as app_module

    provider = KeepAliveProvider()
    stt = _build_session(provider, monkeypatch)
    session = _run_on_thread(stt._run_session)
    try:
        assert provider.started.wait(timeout=5.0)
        assert _wait_for(lambda: stt.phase == "recording")

        # No frames arrive: the cause is named and the provider kept alive.
        assert _wait_for(lambda: stt.warning == app_module.MIC_SILENT_WARNING, timeout=3.0), (
            "a silent microphone must be reported as such, not left to time out upstream"
        )
        assert _wait_for(lambda: provider.keep_alives >= 2, timeout=3.0)
        assert stt.session_health["warning"] == app_module.MIC_SILENT_WARNING

        # Frames resume: the warning is withdrawn.
        stt._on_audio(b"\x00\x01" * 8, 8, None, None)
        assert _wait_for(lambda: stt.warning == "", timeout=3.0)
        sent_keep_alives = provider.keep_alives
        time.sleep(0.25)
        assert provider.keep_alives == sent_keep_alives, "keep-alives stop once audio flows again"
    finally:
        stt.request_stop()
        session.join(timeout=10.0)
    assert not session.is_alive()


def test_an_active_microphone_never_triggers_the_watchdog(monkeypatch):
    provider = KeepAliveProvider()
    stt = _build_session(provider, monkeypatch)
    session = _run_on_thread(stt._run_session)
    try:
        assert provider.started.wait(timeout=5.0)
        assert _wait_for(lambda: stt.phase == "recording")
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            stt._on_audio(b"\x00\x01" * 8, 8, None, None)
            time.sleep(0.05)
        assert stt.warning == ""
        assert provider.keep_alives == 0
        assert provider.chunks > 0
    finally:
        stt.request_stop()
        session.join(timeout=10.0)


def test_a_dropped_frame_still_counts_as_captured_audio(monkeypatch):
    """A full queue means the sender is slow, not that the microphone is
    silent; the watchdog must not misreport it."""
    from medical_stt import app as app_module

    provider = KeepAliveProvider()
    stt = _build_session(provider, monkeypatch)
    stt._reset_capture_watch()
    # Fill the queue so every further capture is a drop...
    while stt._audio_q.put_nowait(b"\x00"):
        pass
    time.sleep(0.35)
    # ...and a drop arriving now must be seen as capture activity.
    stt._on_audio(b"\x00", 1, None, None)
    stt._watch_capture(provider)
    assert stt.warning != app_module.MIC_SILENT_WARNING
    assert provider.keep_alives == 0


# -- capacity message ---------------------------------------------------------------


def test_rate_limit_shows_a_capacity_message_until_a_session_records(monkeypatch):
    from medical_stt import app as app_module
    from medical_stt.stt.reconnect import ReconnectDecision

    # The backoff itself is tested elsewhere; here it must not slow the test.
    monkeypatch.setattr(app_module, "decide", lambda *_a, **_k: ReconnectDecision(True, 0.0, "test"))
    provider = RateLimitedThenFineProvider(failures=2)
    stt = _build_session(provider, monkeypatch)
    seen_capacity = threading.Event()
    original_warn = stt._warn

    def spy(message: str) -> None:
        if message == app_module.CAPACITY_WARNING:
            seen_capacity.set()
        original_warn(message)

    monkeypatch.setattr(stt, "_warn", spy)
    runner = _run_on_thread(stt.run)
    try:
        assert seen_capacity.wait(timeout=5.0), "a 429 must be shown as 'service at capacity'"
        assert provider.started.wait(timeout=5.0)
        assert _wait_for(lambda: stt.phase == "recording")
        assert stt.warning != app_module.CAPACITY_WARNING, "the message is withdrawn once recording"
        assert provider.attempts == 3
    finally:
        stt.request_stop()
        runner.join(timeout=10.0)
    assert not runner.is_alive()


def test_a_rate_limit_that_never_clears_keeps_the_message_and_keeps_retrying(monkeypatch):
    from medical_stt import app as app_module
    from medical_stt.stt.reconnect import ReconnectDecision

    monkeypatch.setattr(app_module, "decide", lambda *_a, **_k: ReconnectDecision(True, 0.01, "test"))
    provider = RateLimitedThenFineProvider(failures=10_000)
    stt = _build_session(provider, monkeypatch)
    runner = _run_on_thread(stt.run)
    try:
        assert _wait_for(lambda: stt.warning == app_module.CAPACITY_WARNING)
        assert _wait_for(lambda: provider.attempts >= 5)
        # Still cycling (connecting -> stopping -> reconnecting), never recording.
        assert stt.phase != "recording"
        # Re-awaited rather than sampled once: `warning` is rewritten by the
        # session thread on every cycle, and a slow teardown on a loaded
        # runner can land a single sample on a transient teardown warning.
        # The claim under test is "the message is (back) up while retrying",
        # which is an eventually-true predicate -- the same shape as the
        # two waits above. (Flaked once on the Windows build job.)
        assert _wait_for(lambda: stt.warning == app_module.CAPACITY_WARNING)
    finally:
        stt.request_stop()
        runner.join(timeout=10.0)
    assert not runner.is_alive()


class RateLimitThenNetworkProvider(KeepAliveProvider):
    """One 429, then a network failure forever: the capacity message must
    not outlive the capacity problem."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    def start(self, on_transcript: OnTranscript, on_error: OnError) -> None:
        self.attempts += 1
        if self.attempts == 1:
            raise ProviderError(ErrorCategory.RATE_LIMIT, "too many concurrent streams")
        raise ProviderError(ErrorCategory.NETWORK, "connection reset")


def test_a_later_failure_of_another_kind_withdraws_the_capacity_message(monkeypatch):
    from medical_stt import app as app_module
    from medical_stt.stt.reconnect import ReconnectDecision

    monkeypatch.setattr(app_module, "decide", lambda *_a, **_k: ReconnectDecision(True, 0.01, "test"))
    provider = RateLimitThenNetworkProvider()
    stt = _build_session(provider, monkeypatch)
    runner = _run_on_thread(stt.run)
    try:
        assert _wait_for(lambda: provider.attempts >= 3)
        assert _wait_for(lambda: stt.warning != app_module.CAPACITY_WARNING), (
            "a network error after a 429 must not keep saying 'service at capacity'"
        )
    finally:
        stt.request_stop()
        runner.join(timeout=10.0)
    assert not runner.is_alive()


def test_a_new_session_starts_without_the_previous_microphone_verdict(monkeypatch):
    """The warning belongs to the session that observed the silence; the
    next session re-raises it only if the microphone is still silent."""
    from medical_stt import app as app_module

    provider = KeepAliveProvider()
    stt = _build_session(provider, monkeypatch)
    stt._warn(app_module.MIC_SILENT_WARNING)
    stt._reset_capture_watch()
    assert stt.warning == ""
    # A different warning is not clobbered by the reset.
    stt._warn(app_module.CAPACITY_WARNING)
    stt._reset_capture_watch()
    assert stt.warning == app_module.CAPACITY_WARNING


# -- backoff ceiling ------------------------------------------------------------------


def test_rate_limit_backoff_never_exceeds_the_configured_ceiling():
    from medical_stt.stt.reconnect import ReconnectPolicy, decide

    policy = ReconnectPolicy(base_delay=2.0, max_delay=30.0, jitter=0.5, max_attempts=0)
    error = ProviderError(ErrorCategory.RATE_LIMIT, "429")
    delays = [decide(policy, error, attempt).delay_seconds for attempt in range(1, 40) for _ in range(50)]
    assert max(delays) <= policy.max_delay
    assert min(delays) >= policy.rate_limit_delay

    # A ceiling below the rate-limit floor still honours the floor.
    tight = ReconnectPolicy(base_delay=1.0, max_delay=3.0, jitter=0.5)
    assert decide(tight, error, 1).delay_seconds == tight.rate_limit_delay


# -- keyterm budget -------------------------------------------------------------------


def test_keyterms_are_budgeted_by_characters_in_priority_order():
    from medical_stt.config import MAX_KEYTERM_CHARS, load_keyterms

    everything = load_keyterms("ultrasound")
    assert sum(len(term) for term in everything) <= MAX_KEYTERM_CHARS

    budgeted = load_keyterms("ultrasound", max_chars=60)
    assert budgeted, "the budget must admit the highest-priority terms"
    assert sum(len(term) for term in budgeted) <= 60
    assert budgeted == everything[: len(budgeted)], "truncation keeps priority order"
    assert len(budgeted) < len(everything)
