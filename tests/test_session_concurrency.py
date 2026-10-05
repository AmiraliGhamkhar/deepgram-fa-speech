"""50-concurrent-session proof for the desktop client (Phase 13, Tests 3-5).

Each "session" here is one clinician's `LiveMedicalSTT` running a full
session: its own provider thread, its own audio sender thread, its own
bounded audio queue and its own provider connection. 50 of them run at once
in one process, which is a strictly harsher test than 50 clinicians on 50
machines -- if state leaks between sessions it shows up here.

No microphone, no Deepgram credentials, no network, no GUI: the audio
device and the STT provider are the same stand-ins used by
`tests/test_shutdown_flow.py`.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import List

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

SESSION_COUNT = 50

#: Threads that exist before the test runs. Used to prove no session thread
#: outlives the test.
_THREAD_NAMES = ("stt-provider", "audio-sender", "stt-session")


class _FakeInputStream:
    def __init__(self, **kwargs) -> None:
        self.callback = kwargs.get("callback")

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _FakeSoundDevice:
    RawInputStream = _FakeInputStream


class ConcurrentProvider(STTProvider):
    """One independent provider per session.

    Records every chunk it is given, tagged with a marker unique to its own
    session, so any cross-session audio leakage is detectable.
    """

    def __init__(self, marker: int, fail_first: int = 0, fail_always: bool = False) -> None:
        self.marker = marker
        self.fail_first = fail_first
        self.fail_always = fail_always
        self.started = threading.Event()
        self.released = threading.Event()
        self.chunks: List[int] = []
        self.transcripts: List[str] = []
        self.finalized = False
        self.start_count = 0
        self._lock = threading.Lock()
        self._remaining_failures = fail_first
        self._error_sink: OnError = lambda _e: None

    def validate_config(self) -> None:
        pass

    def start(self, on_transcript: OnTranscript, on_error: OnError) -> None:
        self._error_sink = on_error
        with self._lock:
            self.start_count += 1
        self.started.set()
        self.released.wait(timeout=20.0)

    def send_audio(self, chunk: bytes) -> None:
        with self._lock:
            if self._remaining_failures > 0 or self.fail_always:
                self._remaining_failures -= 1
                fail = True
            else:
                fail = False
        if fail:
            raise ProviderError(ErrorCategory.NETWORK, "simulated upstream failure")
        with self._lock:
            self.chunks.append(chunk[0])

    def emit(self, text: str) -> None:
        with self._lock:
            self.transcripts.append(text)

    def stop(self) -> None:
        with self._lock:
            self.finalized = True
        self.released.set()


@pytest.fixture(autouse=True)
def _host_credentials(monkeypatch):
    monkeypatch.setenv("MEDICALSTT_HOST_URL", "https://stt.example.com")
    monkeypatch.setenv("MEDICALSTT_HOST_SECRET", "test-shared-secret-not-real")
    monkeypatch.setenv("MEDICALSTT_APP_DATA_DIR", "/tmp/medicalstt-concurrency")


def _build_session(provider: STTProvider, monkeypatch):
    """A fully wired, GUI-free, audio-free session."""
    from medical_stt import app as app_module

    monkeypatch.setattr(app_module, "load_sounddevice", lambda: _FakeSoundDevice)
    stt = app_module.LiveMedicalSTT(provider=provider)
    stt.injector = TextInjector(backend=DryRunBackend(), paste_settle_seconds=0.0)
    stt.overlay.enabled = False
    return stt


def _session_threads() -> set:
    return {
        thread.name
        for thread in threading.enumerate()
        if thread.name in _THREAD_NAMES
    }


def _wait_for(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _run_sessions(stt_list, monkeypatch):
    """Start every session on its own thread, returning the threads."""
    threads = []
    for stt in stt_list:
        thread = threading.Thread(target=_swallow, args=(stt,), daemon=True, name="stt-session")
        thread.start()
        threads.append(thread)
    assert _wait_for(lambda: all(s.provider.started.is_set() for s in stt_list))
    return threads


def _swallow(stt) -> None:
    """Run one session, absorbing the ProviderError its contract allows."""
    try:
        stt._run_session()
    except ProviderError:
        pass


# ===========================================================================
# Test 3 -- 50 concurrent sessions
# ===========================================================================


def test_50_concurrent_sessions_are_isolated(monkeypatch):
    """Test 3: no cross-session state, no queue cross-talk, no collisions."""
    providers = [ConcurrentProvider(marker=index) for index in range(SESSION_COUNT)]
    sessions = [_build_session(provider, monkeypatch) for provider in providers]

    _run_sessions(sessions, monkeypatch)

    # Feed each session audio tagged with its own marker. A queue that
    # leaked between sessions would deliver a foreign marker.
    for index, stt in enumerate(sessions):
        for _ in range(5):
            assert stt._audio_q.put_nowait(bytes([index])) is True

    assert _wait_for(lambda: all(p.chunks for p in providers))

    for index, provider in enumerate(providers):
        assert set(provider.chunks) == {index}, (
            f"session {index} received audio from another session: "
            f"{sorted(set(provider.chunks))}"
        )
        assert len(provider.chunks) == 5

    # Each session holds its own queue and its own provider: nothing shared.
    queues = {id(stt._audio_q) for stt in sessions}
    assert len(queues) == SESSION_COUNT, "audio queues are shared between sessions"

    # Each provider got exactly one connection: no session reused another's.
    assert all(p.start_count == 1 for p in providers)

    # Session ids are unique (the host mints them; uniqueness was proven in
    # test_host_concurrency) -- here we prove the client never reuses a
    # provider connection between sessions.
    for stt in sessions:
        stt.request_stop()
    for thread in _session_threads():
        for candidate in threading.enumerate():
            if candidate.name == thread and candidate.is_alive():
                candidate.join(timeout=20.0)


def test_50_concurrent_sessions_do_not_leak_credentials(monkeypatch, caplog):
    """Test 3: no secret or token may appear in any session's log output."""
    secret = "test-shared-secret-not-real"
    providers = [ConcurrentProvider(marker=index) for index in range(SESSION_COUNT)]
    sessions = [_build_session(provider, monkeypatch) for provider in providers]

    with caplog.at_level(logging.DEBUG):
        threads = _run_sessions(sessions, monkeypatch)
        for index, stt in enumerate(sessions):
            stt._audio_q.put_nowait(bytes([index]))
        assert _wait_for(lambda: all(p.chunks for p in providers))
        for stt in sessions:
            stt.request_stop()
        for thread in threads:
            thread.join(timeout=20.0)

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert secret not in text, "the host shared secret was logged"
    assert "DEEPGRAM_API_KEY" not in text
    assert "dg_" not in text


# ===========================================================================
# Test 4 -- reconnect storm
# ===========================================================================


def test_50_simultaneous_failures_do_not_retry_in_lockstep(monkeypatch):
    """Test 4: jitter must separate 50 clients that failed at the same instant.

    This is the storm test that matters operationally: if every client drew
    the same delay, a recovering provider would take a synchronized
    50-request spike. The delays must be spread, not identical.
    """
    from medical_stt.stt.reconnect import ReconnectPolicy, exponential_backoff_delay

    policy = ReconnectPolicy(base_delay=2.0, max_delay=30.0, jitter=0.5, max_attempts=0)
    delays = [exponential_backoff_delay(policy, attempt=1) for _ in range(SESSION_COUNT)]

    assert len(set(delays)) > SESSION_COUNT * 0.9, (
        f"retries are not decorrelated: {len(set(delays))} distinct delays "
        f"across {SESSION_COUNT} clients"
    )
    # Full jitter: never above the ceiling, and never below it minus the
    # jitter fraction.
    assert all(1.0 <= delay <= 2.0 for delay in delays)

    # And the spread must actually break synchrony over repeated attempts.
    spreads = [
        len(set(exponential_backoff_delay(policy, attempt=n) for _ in range(SESSION_COUNT)))
        for n in (1, 2, 3, 5, 8)
    ]
    assert all(spread > SESSION_COUNT * 0.8 for spread in spreads), (
        f"retry attempts are synchronizing: {spreads}"
    )


def test_reconnect_retry_limits_are_enforced(monkeypatch):
    """Test 4: retry limits must actually stop a session."""
    from medical_stt.stt.reconnect import ReconnectPolicy, decide
    from medical_stt.stt.base import ErrorCategory as EC
    from medical_stt.stt.base import ProviderError

    policy = ReconnectPolicy(base_delay=1.0, max_delay=10.0, jitter=0.0, max_attempts=3)
    error = ProviderError(EC.NETWORK, "upstream down")

    assert [decide(policy, error, n).should_retry for n in (1, 2, 3)] == [True, True, True]
    assert decide(policy, error, 4).should_retry is False

    # AUTH and CONFIG stay terminal regardless of attempt count.
    terminal = ReconnectPolicy(base_delay=1.0, max_delay=10.0, jitter=0.0, max_attempts=0)
    for category in (EC.AUTH, EC.CONFIG, EC.SHUTDOWN):
        assert decide(terminal, ProviderError(category, "x"), 99).should_retry is False


def test_retry_after_is_honored_and_bounded(monkeypatch):
    """Test 4: an upstream Retry-After wins, but cannot pin a session open."""
    from medical_stt.stt.reconnect import ReconnectPolicy, decide
    from medical_stt.stt.base import ErrorCategory as EC
    from medical_stt.stt.base import ProviderError

    policy = ReconnectPolicy(base_delay=1.0, max_delay=10.0, jitter=0.0)
    error = ProviderError(EC.RATE_LIMIT, "slow down", retry_after=7.0)

    decision = decide(policy, error, attempt=1)
    assert decision.delay_seconds == 7.0
    assert decision.honored_retry_after is True

    # A hostile or confused upstream cannot demand hours.
    capped = decide(policy, ProviderError(EC.RATE_LIMIT, "x", retry_after=86_400.0), attempt=1)
    assert capped.delay_seconds == policy.max_retry_after_seconds

    # No hint -> fall back to the computed backoff.
    plain = decide(policy, ProviderError(EC.RATE_LIMIT, "x"), attempt=1)
    assert plain.honored_retry_after is False
    assert plain.delay_seconds >= policy.rate_limit_delay


def test_one_failing_session_does_not_disturb_the_others(monkeypatch):
    """Test 4: a storm is isolated -- 49 healthy sessions keep working."""
    failing = [ConcurrentProvider(marker=0, fail_always=True)]
    healthy = [ConcurrentProvider(marker=i + 1) for i in range(SESSION_COUNT - 1)]
    providers = failing + healthy
    sessions = [_build_session(provider, monkeypatch) for provider in providers]

    threads = _run_sessions(sessions, monkeypatch)

    for index, stt in enumerate(sessions):
        stt._audio_q.put_nowait(bytes([index]))
        if index == 0:
            assert stt._audio_q.put_nowait(bytes([0])) is True

    # The healthy 49 must all receive their audio despite the broken one.
    assert _wait_for(lambda: all(p.chunks for p in healthy), timeout=15.0)
    assert not providers[0].chunks, "the failing session somehow sent audio"

    for index, provider in enumerate(healthy):
        assert set(provider.chunks) == {index + 1}

    for stt in sessions:
        stt.request_stop()
    for thread in threads:
        thread.join(timeout=20.0)
    assert all(not thread.is_alive() for thread in threads)


def test_a_session_stops_during_backoff_without_waiting_it_out(monkeypatch):
    """Test 4/Phase 6: Stop must cancel a pending reconnect immediately."""
    from medical_stt import app as app_module

    provider = ConcurrentProvider(marker=0, fail_always=True)
    stt = _build_session(provider, monkeypatch)
    # A long base delay: if the backoff were not interruptible, Stop would
    # block for this long.
    stt.settings = app_module.get_settings()
    object.__setattr__(stt.settings, "reconnect_delay", 30.0)
    object.__setattr__(stt.settings, "max_reconnect_attempts", 0)

    _run_sessions([stt], monkeypatch)
    stt._audio_q.put_nowait(b"\x00")
    assert _wait_for(lambda: bool(stt._errors), timeout=15.0)

    started = time.monotonic()
    stt.request_stop()
    assert stt.is_stopping is True
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, f"Stop waited {elapsed:.2f}s for the backoff to expire"


# ===========================================================================
# Test 5 -- simultaneous shutdown
# ===========================================================================


def test_50_simultaneous_shutdowns_release_everything(monkeypatch):
    """Test 5: 50/50 terminate, with no leaked threads, queues or sessions."""
    before = _session_threads()

    providers = [ConcurrentProvider(marker=index) for index in range(SESSION_COUNT)]
    sessions = [_build_session(provider, monkeypatch) for provider in providers]
    threads = _run_sessions(sessions, monkeypatch)

    for index, stt in enumerate(sessions):
        stt._audio_q.put_nowait(bytes([index]))
    assert _wait_for(lambda: all(p.chunks for p in providers))

    # Stop every session at the same moment.
    stop_barrier = threading.Barrier(SESSION_COUNT)

    def stopper(stt):
        stop_barrier.wait(timeout=15.0)
        stt.request_stop()

    stoppers = [
        threading.Thread(target=stopper, args=(stt,), daemon=True, name="stopper")
        for stt in sessions
    ]
    for thread in stoppers:
        thread.start()
    for thread in stoppers:
        thread.join(timeout=15.0)

    for thread in threads:
        thread.join(timeout=20.0)

    # 50/50 terminated.
    assert all(not thread.is_alive() for thread in threads), "a session thread outlived Stop"

    # 0 provider threads, 0 sender threads left.
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        leaked = _session_threads() - before
        if not leaked:
            break
        time.sleep(0.02)
    leaked = _session_threads() - before
    assert not leaked, f"leaked threads after shutdown: {sorted(leaked)}"

    # Every provider finalized its stream; every queue is empty.
    assert all(p.finalized for p in providers), "a provider was never finalized"
    assert all(stt.audio_queue_stats.depth == 0 for stt in sessions), "queued audio left over"
    assert all(stt.audio_queue_stats.dropped_total == 0 for stt in sessions)


def test_repeated_session_cycles_do_not_accumulate_threads(monkeypatch):
    """Phase 6: connect/disconnect churn must not grow the thread count."""
    baseline = threading.active_count()
    provider = ConcurrentProvider(marker=0)
    stt = _build_session(provider, monkeypatch)

    for _cycle in range(10):
        thread = threading.Thread(target=_swallow, args=(stt,), daemon=True, name="stt-session")
        thread.start()
        assert provider.started.wait(timeout=15.0)
        provider.started.clear()
        stt._audio_q.put_nowait(b"\x00")
        stt.request_stop()
        thread.join(timeout=20.0)
        assert not thread.is_alive()

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if threading.active_count() <= baseline:
            break
        time.sleep(0.02)
    assert threading.active_count() <= baseline + 2, (
        f"thread count grew from {baseline} to {threading.active_count()} over 10 cycles"
    )


# ===========================================================================
# Processing isolation (Phase 10)
# ===========================================================================


def test_processing_pipeline_state_is_not_shared_between_sessions(monkeypatch):
    """Phase 10: one session's terminology must not affect another's.

    The FST automaton is built once per `LiveMedicalSTT`. If any of it were
    a module global, rewriting in one session would be observable in another.
    """
    from medical_stt.processing.terminology import TerminologyEngine

    engine_a = TerminologyEngine()
    engine_a.load([{"from": "آلفا", "to": "ALPHA", "category": "safe_lexical"}])
    engine_b = TerminologyEngine()  # never loaded

    assert engine_a.apply("آلفا") == "ALPHA"
    assert engine_b.apply("آلفا") == "آلفا", "terminology leaked between engines"

    # And a second LiveMedicalSTT must not be affected by the first.
    first = _build_session(ConcurrentProvider(marker=0), monkeypatch)
    second = _build_session(ConcurrentProvider(marker=1), monkeypatch)
    assert first.terminology is not second.terminology
    assert first._utterance is not second._utterance
    assert first._audio_q is not second._audio_q
    assert first.injector is not second.injector


def test_number_and_negation_protection_survive_concurrency(monkeypatch):
    """Rule 4: clinical safety is never traded away for throughput."""
    sessions = [_build_session(ConcurrentProvider(marker=i), monkeypatch) for i in range(20)]
    backends = [stt.injector.backend for stt in sessions]

    event = TranscriptEvent(
        text="فشار خون 120/80 mmHg است و علائمی ندارد",
        is_final=True,
        speech_final=True,
    )
    threads = [
        threading.Thread(target=stt._on_transcript, args=(event,), daemon=True)
        for stt in sessions
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15.0)

    for backend in backends:
        assert backend.pasted, "a session injected nothing"
        text = backend.pasted[-1]
        assert "120/80" in text, "clinical numbers were rewritten under concurrency"
        assert "ندارد" in text, "negation was rewritten under concurrency"
