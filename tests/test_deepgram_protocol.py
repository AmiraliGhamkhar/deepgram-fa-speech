"""Deepgram protocol details the provider must honour.

Each test here corresponds to a documented Deepgram behaviour that the
provider used to ignore (docs/DEEPGRAM_CONCURRENCY_ANALYSIS.md):

* the close frame carries Deepgram's own error code -- `1008 DATA-xxxx` is
  undecodable audio (a configuration fault, never fixed by reconnecting),
  `1011 NET-xxxx` is a disconnect worth retrying;
* a stream with no audio is closed after ~10 s unless `KeepAlive` is sent;
* an empty binary frame is treated as a close request;
* `UtteranceEnd` with `last_word_end: -1` is to be ignored;
* `tag`/`extra` tie a request back to a device and a host session;
* regional endpoints accept the same token at a different base URL.
"""
from __future__ import annotations

import threading

import pytest

from medical_stt.config import Settings
from medical_stt.stt.base import ErrorCategory, ProviderError

websockets_exceptions = pytest.importorskip("websockets.exceptions")
websockets_frames = pytest.importorskip("websockets.frames")


def _closed(code: int, reason: str):
    """A `ConnectionClosedError` as the websockets library raises it."""
    return websockets_exceptions.ConnectionClosedError(websockets_frames.Close(code, reason), None)


class _FakeConnection:
    def __init__(self, blocking: bool = False):
        self.handlers = {}
        self.media: list[bytes] = []
        self.keep_alives = 0
        self.blocking = blocking
        self.release = threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def on(self, event, handler):
        self.handlers[event] = handler

    def start_listening(self):
        if self.blocking:
            self.release.wait(timeout=5.0)

    def send_media(self, chunk):
        self.media.append(chunk)

    def send_keep_alive(self):
        self.keep_alives += 1

    def send_finalize(self):
        pass

    def send_close_stream(self):
        pass


def _install_fake_client(monkeypatch, connection, captured):
    import deepgram

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        @property
        def listen(self):
            class V1:
                def connect(self, **params):
                    captured["connect"] = params
                    return connection

            class V2:
                v1 = V1()

            return V2()

    monkeypatch.setattr(deepgram, "DeepgramClient", FakeClient)


def _started_provider(monkeypatch, settings: Settings | None = None):
    """Start a provider against the fake SDK; `start()` returns immediately
    because the fake `start_listening` does not block, so the connection is
    handed back for the test to drive."""
    from medical_stt.stt.deepgram_provider import DeepgramProvider

    captured: dict = {}
    connection = _FakeConnection()
    _install_fake_client(monkeypatch, connection, captured)
    errors: list = []
    events: list = []
    provider = DeepgramProvider(
        settings or Settings(host_url="https://stt.example.com", host_secret="secret"),
        token_provider=lambda: "short-lived-token",
    )
    provider.start(events.append, errors.append)
    return provider, connection, captured, events, errors


# -- close codes -------------------------------------------------------------


@pytest.mark.parametrize(
    "code, reason, category",
    [
        (1008, "DATA-0000", ErrorCategory.CONFIG),
        (1011, "NET-0000", ErrorCategory.SERVER_DISCONNECT),
        (1011, "NET-0001", ErrorCategory.SERVER_DISCONNECT),
        (1011, "NET-0002", ErrorCategory.SERVER_DISCONNECT),
        (1000, "", ErrorCategory.SERVER_DISCONNECT),
    ],
)
def test_close_codes_are_classified(code, reason, category):
    from medical_stt.stt.deepgram_provider import classify_deepgram_exception

    assert classify_deepgram_exception(_closed(code, reason)) is category


def test_undecodable_audio_is_not_retried():
    """`1008 DATA-0000` means the audio cannot be decoded with the configured
    encoding/sample rate. Reconnecting with the same settings would loop
    forever, so it must be non-retryable."""
    from medical_stt.stt.deepgram_provider import classify_deepgram_exception

    assert classify_deepgram_exception(_closed(1008, "DATA-0000")).is_retryable is False
    assert classify_deepgram_exception(_closed(1011, "NET-0001")).is_retryable is True


def test_close_details_name_deepgrams_error_code():
    from medical_stt.stt.deepgram_provider import close_details

    assert close_details(_closed(1011, "NET-0001")) == "1011 NET-0001"
    assert close_details(_closed(1000, "")) == "1000"
    assert close_details(websockets_exceptions.ConnectionClosedError(None, None)) == "no close frame"


def test_a_close_frame_reaches_the_error_message_and_the_log(monkeypatch, caplog):
    """The reason is the one line that makes a reconnect loop diagnosable."""
    from deepgram.core.events import EventType

    from medical_stt.stt import ConnectionClosed

    _provider, connection, _captured, _events, errors = _started_provider(monkeypatch)
    with caplog.at_level("WARNING", logger="medical_stt.stt.deepgram"):
        connection.handlers[EventType.ERROR](_closed(1011, "NET-0001"))

    assert len(errors) == 1
    assert isinstance(errors[0], ConnectionClosed)
    assert errors[0].category is ErrorCategory.SERVER_DISCONNECT
    assert "1011 NET-0001" in str(errors[0])
    assert any("1011 NET-0001" in record.getMessage() for record in caplog.records)


def test_a_data_close_stops_the_session_as_a_config_error(monkeypatch):
    from deepgram.core.events import EventType

    _provider, connection, _captured, _events, errors = _started_provider(monkeypatch)
    connection.handlers[EventType.ERROR](_closed(1008, "DATA-0000"))

    assert errors[0].category is ErrorCategory.CONFIG
    assert "DATA-0000" in str(errors[0])


# -- keep-alive and empty frames ---------------------------------------------


def _live_provider(monkeypatch):
    """A provider whose connection stays open: `start()` runs on a thread
    and blocks in the fake `start_listening` until the test releases it."""
    from medical_stt.stt.deepgram_provider import DeepgramProvider

    captured: dict = {}
    connection = _FakeConnection(blocking=True)
    _install_fake_client(monkeypatch, connection, captured)
    provider = DeepgramProvider(
        Settings(host_url="https://stt.example.com", host_secret="secret"),
        token_provider=lambda: "short-lived-token",
    )
    thread = threading.Thread(target=provider.start, args=(lambda _e: None, lambda _e: None), daemon=True)
    thread.start()
    assert provider.wait_ready(5.0)
    return provider, connection, thread


def test_keep_alive_is_sent_on_the_open_connection(monkeypatch):
    provider, connection, thread = _live_provider(monkeypatch)
    try:
        provider.keep_alive()
        assert connection.keep_alives == 1
    finally:
        connection.release.set()
        thread.join(timeout=5.0)


def test_keep_alive_without_a_connection_is_a_network_error():
    from medical_stt.stt.deepgram_provider import DeepgramProvider

    provider = DeepgramProvider(
        Settings(host_url="https://stt.example.com", host_secret="secret"),
        token_provider=lambda: "t",
    )
    with pytest.raises(ProviderError) as info:
        provider.keep_alive()
    assert info.value.category is ErrorCategory.NETWORK


def test_empty_audio_frames_are_never_forwarded(monkeypatch):
    """Deepgram treats an empty binary frame as a close request."""
    provider, connection, thread = _live_provider(monkeypatch)
    try:
        provider.send_audio(b"")
        provider.send_audio(b"\x00\x01")
        assert connection.media == [b"\x00\x01"]
    finally:
        connection.release.set()
        thread.join(timeout=5.0)


def test_default_providers_accept_keep_alive_as_a_no_op():
    """Providers without an idle timeout need not implement it."""
    from medical_stt.stt.base import STTProvider

    class Minimal(STTProvider):
        def validate_config(self):
            pass

        def start(self, on_transcript, on_error):
            pass

        def send_audio(self, chunk):
            pass

        def stop(self):
            pass

    assert Minimal().keep_alive() is None


# -- UtteranceEnd ---------------------------------------------------------------


def test_utterance_end_with_last_word_end_minus_one_is_ignored(monkeypatch):
    from deepgram.core.events import EventType
    from deepgram.listen.v1.types import ListenV1UtteranceEnd

    _provider, connection, _captured, events, _errors = _started_provider(monkeypatch)
    on_message = connection.handlers[EventType.MESSAGE]

    on_message(ListenV1UtteranceEnd(channel=[0, 1], last_word_end=-1))
    assert events == []

    on_message(ListenV1UtteranceEnd(channel=[0, 1], last_word_end=3.2))
    assert len(events) == 1 and events[0].speech_final is True


# -- attribution and region -------------------------------------------------------


def test_connection_is_tagged_with_the_device_and_host_session(monkeypatch):
    from medical_stt.stt.deepgram_provider import DeepgramProvider

    captured: dict = {}
    connection = _FakeConnection()
    _install_fake_client(monkeypatch, connection, captured)
    provider = DeepgramProvider(
        Settings(host_url="https://stt.example.com", host_secret="secret", host_client_id="ward-7-pc-03"),
        token_provider=lambda: "short-lived-token",
    )
    provider._host_session_id = "0123456789abcdef0123456789abcdef"  # what the host handed back
    provider.start(lambda _e: None, lambda _e: None)

    params = captured["connect"]
    assert params["tag"] == ["ward-7-pc-03"]
    assert params["extra"] == ["session_id:0123456789abcdef0123456789abcdef"]
    assert "mip_opt_out" in params and params["mip_opt_out"] is None, "Deepgram's default unless opted out"


def test_no_device_id_means_no_tag(monkeypatch):
    _provider, _connection, captured, _events, _errors = _started_provider(monkeypatch)
    assert "tag" not in captured["connect"]
    assert "extra" not in captured["connect"]


def test_mip_opt_out_is_sent_only_when_enabled(monkeypatch):
    settings = Settings(host_url="https://stt.example.com", host_secret="secret", mip_opt_out=True)
    _provider, _connection, captured, _events, _errors = _started_provider(monkeypatch, settings)
    assert captured["connect"]["mip_opt_out"] is True


@pytest.mark.parametrize(
    "region, host",
    [("us", "api.deepgram.com"), ("eu", "api.eu.deepgram.com"), ("au", "api.au.deepgram.com"), ("in", "api.in.deepgram.com")],
)
def test_the_region_selects_the_base_url_for_the_same_token(monkeypatch, region, host):
    settings = Settings(host_url="https://stt.example.com", host_secret="secret", deepgram_region=region)
    _provider, _connection, captured, _events, _errors = _started_provider(monkeypatch, settings)

    environment = captured["environment"]
    assert environment.base == f"https://{host}"
    assert environment.production == f"wss://{host}"
    assert captured["access_token"] == "short-lived-token"


def test_an_unknown_region_is_a_configuration_error():
    from medical_stt.config import ConfigError, deepgram_api_host, validate_settings

    with pytest.raises(ConfigError):
        deepgram_api_host("mars")
    errors = validate_settings(
        Settings(host_url="https://stt.example.com", host_secret="secret", deepgram_region="mars")
    )
    assert any("deepgram_region" in error for error in errors)


def test_utterance_end_ms_is_validated_to_deepgrams_range():
    from medical_stt.config import validate_settings

    base = dict(host_url="https://stt.example.com", host_secret="secret")
    assert not [e for e in validate_settings(Settings(utterance_end_ms=1000, **base)) if "utterance_end_ms" in e]
    assert not [e for e in validate_settings(Settings(utterance_end_ms=5000, **base)) if "utterance_end_ms" in e]
    for bad in (0, 999, 5001, 20000):
        assert any("utterance_end_ms" in e for e in validate_settings(Settings(utterance_end_ms=bad, **base)))
