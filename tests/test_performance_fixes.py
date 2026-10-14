"""Regression pins for the performance audit. No live network or native input."""
from __future__ import annotations

import asyncio
import threading
import wave
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from medical_stt.app import LiveMedicalSTT, SessionController
from medical_stt.audio import BoundedAudioQueue
from medical_stt.config import ConfigError, Settings, validate_settings
from medical_stt.injection.backend import DryRunBackend, FailingBackend
from medical_stt.injection.text_injector import TextInjector
from medical_stt.stt.accumulator import UtteranceAccumulator, UtteranceLimitError
from medical_stt.stt.base import ErrorCategory, ProviderError, STTProvider, TranscriptEvent
from medical_stt.stt.deepgram_provider import DeepgramProvider
from medical_stt.stt.reconnect import ReconnectPolicy, decide, exponential_backoff_delay
from medical_stt.stt.runner import stream_audio
from medical_stt.ui.dispatch import Mailbox


class Provider(STTProvider):
    def __init__(self, ready=True):
        self.started = threading.Event()
        self.ready = threading.Event()
        if ready:
            self.ready.set()
        self.released = threading.Event()
        self.audio = []
        self.transcript = lambda event: None
        self.error = lambda exc: None

    def validate_config(self):
        pass

    def wait_ready(self, timeout):
        return self.ready.wait(timeout)

    def start(self, on_transcript, on_error):
        self.transcript, self.error = on_transcript, on_error
        self.started.set()
        self.released.wait(5)

    def send_audio(self, chunk):
        assert self.ready.is_set()
        self.audio.append(chunk)

    def stop(self):
        self.released.set()


@pytest.fixture
def build_session(monkeypatch):
    from medical_stt import app
    from medical_stt.ui import overlay
    monkeypatch.setattr(overlay, "_TK_AVAILABLE", False)
    monkeypatch.setattr(app, "get_settings", lambda: Settings(
        host_url="https://example.test", host_secret="fake-test-secret", overlay_enabled=False))
    captured = threading.Event()

    class Input:
        def __init__(self, **kwargs):
            self.callback = kwargs["callback"]

        def __enter__(self):
            captured.set()
            self.callback(b"\x00" * 2560, 1280, None, None)
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(app, "load_sounddevice", lambda: SimpleNamespace(RawInputStream=Input))

    def build(provider=None):
        stt = LiveMedicalSTT(provider=provider or Provider())
        stt.injector = TextInjector(backend=DryRunBackend(), paste_settle_seconds=0)
        return stt, captured
    return build


def run_session(stt):
    errors = []
    def run():
        try:
            stt._run_session()
        except ProviderError as exc:
            errors.append(exc)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, errors


def test_capture_waits_for_readiness(build_session):
    provider = Provider(ready=False)
    stt, captured = build_session(provider)
    thread, errors = run_session(stt)
    try:
        assert provider.started.wait(1)
        assert not captured.wait(.1)
        assert stt.phase == "connecting"
        provider.ready.set()
        assert captured.wait(1)
    finally:
        stt.request_stop()
        thread.join(2)
    assert not thread.is_alive()
    assert not errors
    assert provider.audio == [b"\x00" * 2560]
    assert stt.audio_queue_stats.sent_bytes == 2560


def test_stop_before_ready_never_opens_microphone(build_session):
    provider = Provider(ready=False)
    stt, captured = build_session(provider)
    thread, _ = run_session(stt)
    assert provider.started.wait(1)
    stt.request_stop()
    thread.join(2)
    assert not thread.is_alive()
    assert not captured.is_set()


def test_old_callbacks_rejected_immediately_after_session(build_session):
    provider = Provider()
    stt, captured = build_session(provider)
    thread, errors = run_session(stt)
    assert captured.wait(1)
    stt.request_stop()
    thread.join(2)
    assert not thread.is_alive()
    provider.transcript(TranscriptEvent("must not be pasted", True))
    provider.error(ProviderError(ErrorCategory.NETWORK, "old error"))
    assert not stt.injector.backend.pasted
    assert not errors
    assert not stt._errors


def test_receive_callback_not_blocked_by_injection(build_session):
    stt, captured = build_session()
    entered, release = threading.Event(), threading.Event()
    class SlowBackend(DryRunBackend):
        def paste_text(self, *args):
            entered.set()
            assert release.wait(3)
            return super().paste_text(*args)
    stt.injector = TextInjector(backend=SlowBackend())
    thread, _ = run_session(stt)
    assert captured.wait(1)
    try:
        stt.provider.transcript(TranscriptEvent("بیمار خوب است", True))
        assert entered.wait(1)  # callback already returned; worker is still blocked
        stt.provider.transcript(TranscriptEvent("جمله دوم", True))
        assert len(stt.injector.backend.pasted) == 0
    finally:
        stt.request_stop()
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert len(stt.injector.backend.pasted) == 2


def test_failed_injection_is_retained_without_success(build_session):
    stt, _ = build_session()
    stt.injector = TextInjector(backend=FailingBackend())
    stt.overlay.set_done = Mock()
    stt._finalize_utterance(TranscriptEvent("بیمار", True))
    stt.overlay.set_done.assert_not_called()
    assert stt.recovery_text()
    assert stt.warning
    assert stt._stop.is_set()
    stt.clear_recovery()
    assert stt.recovery_text() == ""


def test_type_mode_is_honored(build_session):
    stt, _ = build_session()
    stt.settings.inject_mode = "type"
    stt._finalize_utterance(TranscriptEvent("سلام", True))
    assert stt.injector.backend.typed
    assert not stt.injector.backend.pasted


def test_controller_cancel_during_factory():
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    instance = SimpleNamespace(request_stop=stopped.set, run=lambda: 0)
    def factory():
        entered.set()
        assert release.wait(2)
        return instance
    controller = SessionController(factory)
    creator = threading.Thread(target=controller.start)
    creator.start()
    assert entered.wait(1)
    controller.cancel_start()
    release.set()
    creator.join(2)
    assert stopped.is_set()
    assert controller.stop(1)


def test_mailbox_coalesces_partials_without_tk():
    mailbox = Mailbox(capacity=2)
    calls = []
    for i in range(10000):
        mailbox.post("partial", lambda i=i: calls.append(i))
    mailbox.post("close", lambda: calls.append("closed"))
    mailbox.drain()
    assert calls == [9999, "closed"]


def test_mailbox_overload_is_explicit():
    mailbox = Mailbox(capacity=1)
    mailbox.post("one", lambda: None)
    with pytest.raises(RuntimeError, match="full"):
        mailbox.post("two", lambda: None)


def test_audio_age_and_loss_accounting(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr("medical_stt.audio.queue.time.monotonic", lambda: clock[0])
    audio = BoundedAudioQueue(maxsize=1)
    audio.put_nowait(b"abc")
    clock[0] = 12.0
    assert audio.stats().oldest_age_seconds == 2
    assert not audio.put_nowait(b"12345")
    assert audio.stats().dropped_bytes == 5
    chunk = audio.get()
    assert audio.stats().last_queue_age_seconds == 2
    audio.record_failed(chunk)
    audio.task_done()
    assert audio.stats().dropped_bytes == 8


def test_accumulator_has_a_size_budget():
    accumulator = UtteranceAccumulator(max_chars=5)
    accumulator.add(TranscriptEvent("abc", True, speech_final=False))
    with pytest.raises(UtteranceLimitError):
        accumulator.add(TranscriptEvent("def", True, speech_final=False))
    assert accumulator.flush().text == "abc"
    accumulator.add(TranscriptEvent("def", True))  # budget reset after flush


@pytest.mark.parametrize("attempt", [1025, 1000000])
def test_unlimited_backoff_never_overflows(attempt):
    assert 15 <= exponential_backoff_delay(ReconnectPolicy(), attempt) <= 30


def test_retry_after_keeps_positive_jitter():
    error = ProviderError(ErrorCategory.RATE_LIMIT, "test", retry_after=10)
    delays = [decide(ReconnectPolicy(), error, 1).delay_seconds for _ in range(50)]
    assert all(10 <= value <= 15 for value in delays)
    assert len(set(delays)) > 1


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_client_settings_rejected(value):
    settings = Settings(host_url="https://example.test", host_secret="test", host_timeout_seconds=value)
    assert any("finite" in message for message in validate_settings(settings))


class Socket:
    def __init__(self):
        self.listen_entered = threading.Event()
        self.release = threading.Event()
        self.finalized = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def on(self, *args):
        pass

    def start_listening(self):
        self.listen_entered.set()
        self.release.wait(4)

    def send_finalize(self):
        self.finalized = True

    def send_close_stream(self):
        self.release.set()


def install_sdk(monkeypatch, connect):
    import deepgram
    monkeypatch.setattr(deepgram, "DeepgramClient", lambda **kw: SimpleNamespace(
        listen=SimpleNamespace(v1=SimpleNamespace(connect=connect))))


def test_stop_during_token_fetch_prevents_connect(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    connect = Mock()
    install_sdk(monkeypatch, connect)
    def token():
        entered.set()
        assert release.wait(2)
        return "fake"
    provider = DeepgramProvider(Settings(), token_provider=token)
    errors = []
    def run():
        try:
            provider.start(lambda e: None, lambda e: None)
        except ProviderError as exc:
            errors.append(exc)
    thread = threading.Thread(target=run)
    thread.start()
    assert entered.wait(1)
    provider.stop()
    release.set()
    thread.join(2)
    connect.assert_not_called()
    assert errors[0].category == ErrorCategory.SHUTDOWN


def test_old_provider_finally_cannot_clear_new_connection(monkeypatch):
    first, second = Socket(), Socket()
    sockets = iter([first, second])
    install_sdk(monkeypatch, lambda **kw: next(sockets))
    provider = DeepgramProvider(Settings(), token_provider=lambda: "fake")
    old = threading.Thread(target=provider.start, args=(lambda e: None, lambda e: None))
    old.start()
    assert first.listen_entered.wait(1)
    provider.prepare()  # supersede first without waiting for its stuck listener
    new = threading.Thread(target=provider.start, args=(lambda e: None, lambda e: None))
    new.start()
    try:
        assert second.listen_entered.wait(1)
        first.release.set()
        old.join(1)
        assert provider._connection is second
        assert provider.wait_ready(0)
    finally:
        first.release.set()
        provider.stop()
        new.join(2)
        old.join(2)
    assert not new.is_alive()


def test_streaming_harness_listens_concurrently_and_drains():
    provider = Provider()
    stream_audio(provider, [b"one", b"two"], lambda e: None, lambda e: None, bytes_per_second=100000)
    assert provider.audio == [b"one", b"two"]
    assert provider.released.is_set()


def test_wav_format_must_match_stream(tmp_path):
    from benchmarks.run_benchmark import _read_wav
    path = tmp_path / "test.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(b"\0" * 16)
    with pytest.raises(SystemExit, match="sample rate"):
        _read_wav(path, 16000, 1)
    with pytest.raises(SystemExit, match="channels"):
        _read_wav(path, 44100, 1)


def test_invalid_url_does_not_write_credentials(monkeypatch):
    from medical_stt import config
    store = Mock()
    monkeypatch.setattr(config, "SecretStore", store)
    with pytest.raises(ConfigError):
        config.save_host_credentials("http://untrusted.test", "fake")
    store.assert_not_called()


@pytest.fixture
def host_factory(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "fake-audit-key")
    monkeypatch.setenv("HOST_SHARED_SECRET", "s" * 40)
    from host.app import create_app
    from host.core import HostSettings
    def create(**kw):
        return create_app(replace(HostSettings("fake", "s" * 40, allow_http=True), **kw))
    return create


def test_api_body_deadline_and_admission_cap(host_factory):
    import httpx
    async def run():
        app = host_factory(request_body_timeout_seconds=.15, max_active_requests=1)
        gate = asyncio.Event()
        async def body():
            yield b"{"
            gate.set()
            await asyncio.Event().wait()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                headers = {"Authorization": "Bearer " + "s" * 40}
                slow = asyncio.create_task(client.post("/v1/session", headers=headers, content=body()))
                await asyncio.wait_for(gate.wait(), 1)
                overflow = await client.post("/v1/session", headers=headers, json={})
                assert overflow.status_code == 503
                response = await asyncio.wait_for(slow, 1)
                assert response.status_code == 408
                assert app.state.active_requests.in_flight == 0
                assert app.state.inflight.in_flight == 0
                assert app.state.metrics.counter_value("session_http_requests_total") == 2
                assert app.state.metrics.counter_value("session_http_outcomes_total", status="408") == 1
    asyncio.run(run())


@pytest.mark.parametrize("raw", ['{"ttl_seconds":1e309}', '{"ttl_seconds":true}', '{"ttl_seconds":1.5}',
                                 '{"ttl_seconds":-5}', '[]', 'not-json'])
def test_api_rejects_invalid_ttl_without_upstream(host_factory, raw):
    import httpx
    async def run():
        app = host_factory()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post("/v1/session", headers={"Authorization": "Bearer " + "s" * 40}, content=raw)
                assert response.status_code == 422
                assert app.state.metrics.counter_value("token_requests_total") == 0
    asyncio.run(run())


def test_grant_total_deadline_releases_capacity(host_factory):
    import httpx
    async def run():
        app = host_factory(grant_deadline_seconds=.02)
        cancelled = asyncio.Event()
        async def slow(*args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        app.state.grant_client.grant = slow
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post("/v1/session", headers={"Authorization": "Bearer " + "s" * 40}, json={})
                assert response.status_code == 504
                assert cancelled.is_set()
                assert app.state.inflight.in_flight == 0
                assert app.state.active_requests.in_flight == 0
    asyncio.run(run())


def test_upstream_retry_after_is_preserved():
    import httpx
    from host.core import AsyncGrantClient, GrantError
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(429, headers={"Retry-After": "12"}))) as client:
            grant = AsyncGrantClient(client=client)
            with pytest.raises(GrantError) as error:
                await grant.grant("fake", 30)
            assert error.value.retry_after == 12
            assert error.value.status == 503
    asyncio.run(run())


def test_gui_start_is_nonblocking_and_worker_never_calls_tk():
    from medical_stt.ui.control import ControlWindow
    entered, release = threading.Event(), threading.Event()
    class Controller:
        is_running = False
        last_error = None
        def start(self):
            entered.set()
            assert release.wait(2)
        def stop(self, timeout=8):
            return True
    window = ControlWindow(Controller())
    root = Mock()
    root.after.side_effect = AssertionError("worker called Tk")
    window._root = root
    window._start()
    try:
        assert entered.wait(1)
        assert window._starting
        assert not window._start_done.is_set()
        root.after.assert_not_called()
    finally:
        release.set()
        assert window._start_done.wait(2)


def test_gui_initialization_failure_is_not_success(monkeypatch):
    from medical_stt.ui import control
    monkeypatch.setattr(control, "_TK_AVAILABLE", True)
    class TclError(Exception):
        pass
    monkeypatch.setattr(control, "tk", SimpleNamespace(Tk=Mock(side_effect=TclError("no display")),
                                                       TclError=TclError), raising=False)
    window = control.ControlWindow(Mock())
    assert window.start() is False


def test_settings_save_is_atomic_and_preserves_client_id(monkeypatch, tmp_path):
    from medical_stt import config
    monkeypatch.setenv("MEDICALSTT_APP_DATA_DIR", str(tmp_path))
    config.initialize_user_config()
    config.save_host_credentials("https://first.example.test", "", "doctor-one")
    settings = config._load_yaml(config.user_settings_path())
    assert settings["host_client_id"] == "doctor-one"
    original = config.user_settings_path().read_bytes()
    def fail(*args):
        raise OSError("disk unavailable")
    monkeypatch.setattr(config.os, "replace", fail)
    with pytest.raises(ConfigError, match="not saved"):
        config.save_host_credentials("https://second.example.test", "", "doctor-two")
    assert config.user_settings_path().read_bytes() == original
    assert not list(tmp_path.glob(".settings-*.tmp"))


def test_recovery_is_bounded_and_overflow_is_visible(build_session):
    stt, _ = build_session()
    for i in range(20):
        stt._retain_failed(f"failure {i}")
    stt._warn("Review")
    assert len(stt._recovery) == 16
    assert stt._recovery_truncated
    assert "ظرفیت" in stt.warning


def test_render_failure_does_not_disable_ui_mailbox(caplog):
    mailbox = Mailbox()
    closed = []
    def fail():
        raise ValueError("private transcript must not appear in logs")
    mailbox.post("render", fail)
    mailbox.post("close", lambda: closed.append(True))
    mailbox.drain()
    assert closed == [True]
    assert "private transcript" not in caplog.text


def test_processing_failure_recovers_complete_accumulated_utterance(build_session):
    stt, _ = build_session()
    stt.terminology.apply = Mock(side_effect=ValueError("failed"))
    stt._on_transcript(TranscriptEvent("بخش اول", True, speech_final=False))
    stt._on_transcript(TranscriptEvent("بخش دوم", True, speech_final=True))
    assert "بخش اول" in stt.recovery_text()
    assert "بخش دوم" in stt.recovery_text()
    assert stt._errors[0].category is ErrorCategory.CONFIG


def test_unlimited_retries_cannot_accumulate_unbounded_network_workers(build_session):
    stt, _ = build_session()
    stt._abandoned_workers = [SimpleNamespace(is_alive=lambda: True)] * 3
    with pytest.raises(ProviderError, match="abandoned network workers") as error:
        stt._run_session()
    assert error.value.category is ErrorCategory.CONFIG


def test_native_check_requires_explicit_interactive_opt_in():
    import subprocess
    import sys
    result = subprocess.run([sys.executable, "scripts/windows_injection_check.py"], capture_output=True, text=True)
    assert result.returncode == 2
    assert "no native input" in result.stdout
