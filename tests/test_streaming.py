"""STT provider error classification & reconnect policy tests (task
sections 7, 9). No real network access or Deepgram credentials are used."""
from __future__ import annotations

import re
import socket

import pytest

from medical_stt.stt.base import ErrorCategory, ProviderError
from medical_stt.stt.deepgram_provider import classify_deepgram_exception
from medical_stt.stt.reconnect import ReconnectPolicy, decide, exponential_backoff_delay


class _FakeStatusError(Exception):
    def __init__(self, status_code):
        super().__init__("fake")
        self.status_code = status_code


def test_classify_auth_error():
    assert classify_deepgram_exception(_FakeStatusError(401)) == ErrorCategory.AUTH
    assert classify_deepgram_exception(_FakeStatusError(403)) == ErrorCategory.AUTH


def test_classify_rate_limit_error():
    assert classify_deepgram_exception(_FakeStatusError(429)) == ErrorCategory.RATE_LIMIT


def test_classify_config_error():
    assert classify_deepgram_exception(_FakeStatusError(400)) == ErrorCategory.CONFIG


def test_classify_server_error():
    assert classify_deepgram_exception(_FakeStatusError(503)) == ErrorCategory.SERVER_DISCONNECT


def test_classify_network_error():
    assert classify_deepgram_exception(ConnectionError("boom")) == ErrorCategory.NETWORK
    assert classify_deepgram_exception(socket.gaierror("dns fail")) == ErrorCategory.NETWORK


def test_classify_timeout_error():
    assert classify_deepgram_exception(TimeoutError("slow")) == ErrorCategory.TIMEOUT
    assert classify_deepgram_exception(socket.timeout("slow")) == ErrorCategory.TIMEOUT


def test_classify_unknown_error_defaults_safely():
    assert classify_deepgram_exception(ValueError("weird")) == ErrorCategory.UNKNOWN


def test_error_category_retryability():
    assert not ErrorCategory.AUTH.is_retryable
    assert not ErrorCategory.CONFIG.is_retryable
    assert not ErrorCategory.SHUTDOWN.is_retryable
    assert ErrorCategory.NETWORK.is_retryable
    assert ErrorCategory.TIMEOUT.is_retryable
    assert ErrorCategory.RATE_LIMIT.is_retryable
    assert ErrorCategory.SERVER_DISCONNECT.is_retryable
    assert ErrorCategory.MICROPHONE.is_retryable


def test_reconnect_never_retries_auth_error():
    policy = ReconnectPolicy(base_delay=1.0, max_delay=10.0, jitter=0.0, max_attempts=0)
    error = ProviderError(ErrorCategory.AUTH, "invalid key")
    decision = decide(policy, error, attempt=1)
    assert decision.should_retry is False


def test_reconnect_never_retries_config_error():
    policy = ReconnectPolicy(base_delay=1.0, max_delay=10.0, jitter=0.0, max_attempts=0)
    error = ProviderError(ErrorCategory.CONFIG, "bad params")
    decision = decide(policy, error, attempt=1)
    assert decision.should_retry is False


def test_reconnect_retries_network_error_with_backoff():
    policy = ReconnectPolicy(base_delay=1.0, max_delay=10.0, jitter=0.0, max_attempts=0)
    error = ProviderError(ErrorCategory.NETWORK, "timeout")
    decision = decide(policy, error, attempt=1)
    assert decision.should_retry is True
    assert decision.delay_seconds == 1.0

    decision2 = decide(policy, error, attempt=3)
    assert decision2.delay_seconds == 4.0  # 1 * 2^(3-1)


def test_reconnect_respects_max_attempts():
    policy = ReconnectPolicy(base_delay=1.0, max_delay=10.0, jitter=0.0, max_attempts=2)
    error = ProviderError(ErrorCategory.NETWORK, "timeout")
    assert decide(policy, error, attempt=2).should_retry is True
    assert decide(policy, error, attempt=3).should_retry is False


def test_backoff_capped_at_max_delay():
    policy = ReconnectPolicy(base_delay=1.0, max_delay=5.0, jitter=0.0, max_attempts=0)
    delay = exponential_backoff_delay(policy, attempt=10)
    assert delay == 5.0


def test_backoff_jitter_bounded():
    policy = ReconnectPolicy(base_delay=2.0, max_delay=30.0, jitter=0.5, max_attempts=0)
    for attempt in range(1, 6):
        for _ in range(20):
            delay = exponential_backoff_delay(policy, attempt)
            capped = min(policy.max_delay, policy.base_delay * (2 ** (attempt - 1)))
            assert 0.0 <= delay <= capped * 1.5 + 1e-9


def test_provider_error_message_never_includes_none_cause_leak():
    error = ProviderError(ErrorCategory.NETWORK, "connection reset")
    assert "connection reset" in str(error)


# -- credential path -----------------------------------------------------
#
# These guard the core security claim: the provider authenticates with a
# short-lived host-issued session token and never with a Deepgram API key.


class _FakeConnection:
    def __init__(self):
        self.handlers = {}
        self.listening = False
        self.finalized = False

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def on(self, event, handler):
        self.handlers[event] = handler

    def start_listening(self):
        self.listening = True

    def send_finalize(self):
        self.finalized = True

    def send_close_stream(self):
        pass


def test_provider_authenticates_with_a_session_token_not_an_api_key(monkeypatch):
    import deepgram

    from medical_stt.config import Settings
    from medical_stt.stt.deepgram_provider import DeepgramProvider

    captured = {}
    connection = _FakeConnection()

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

    provider = DeepgramProvider(
        Settings(host_url="https://stt.example.com", host_secret="secret"),
        token_provider=lambda: "short-lived-token",
    )
    provider.start(lambda _e: None, lambda _e: None)

    assert captured["access_token"] == "short-lived-token"
    assert "api_key" not in captured, "the provider must never authenticate with an API key"
    assert connection.listening is True


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


def test_nova_3_sends_keyterm_not_keywords(monkeypatch):
    from medical_stt.config import Settings
    from medical_stt.stt.deepgram_provider import DeepgramProvider

    captured = {}
    connection = _FakeConnection()
    _install_fake_client(monkeypatch, connection, captured)

    provider = DeepgramProvider(
        Settings(host_url="https://stt.example.com", host_secret="secret", model="nova-3", language="fa"),
        keyterms=["ICU", "هایپرتنشن"],
        token_provider=lambda: "token",
    )
    provider.validate_config()
    provider.start(lambda _e: None, lambda _e: None)

    params = captured["connect"]
    assert params["keyterm"] == ["ICU", "هایپرتنشن"]
    assert "keywords" not in params, "keywords is rejected by Nova-3"


def test_legacy_model_sends_keywords_not_keyterm(monkeypatch):
    from medical_stt.config import Settings
    from medical_stt.stt.deepgram_provider import DeepgramProvider

    captured = {}
    connection = _FakeConnection()
    _install_fake_client(monkeypatch, connection, captured)

    provider = DeepgramProvider(
        Settings(host_url="https://stt.example.com", host_secret="secret", model="nova-2", language="en"),
        keyterms=["ICU"],
        token_provider=lambda: "token",
    )
    provider.validate_config()
    provider.start(lambda _e: None, lambda _e: None)

    params = captured["connect"]
    assert params["keywords"] == ["ICU"]
    assert "keyterm" not in params, "keyterm prompting is Nova-3 only"


def test_empty_keyterms_send_neither_parameter(monkeypatch):
    from medical_stt.config import Settings
    from medical_stt.stt.deepgram_provider import DeepgramProvider

    captured = {}
    connection = _FakeConnection()
    _install_fake_client(monkeypatch, connection, captured)

    provider = DeepgramProvider(
        Settings(host_url="https://stt.example.com", host_secret="secret"),
        keyterms=[],
        token_provider=lambda: "token",
    )
    provider.start(lambda _e: None, lambda _e: None)

    assert "keyterm" not in captured["connect"]
    assert "keywords" not in captured["connect"]


def test_validate_config_rejects_persian_on_a_legacy_model():
    from medical_stt.config import Settings
    from medical_stt.stt.deepgram_provider import DeepgramProvider

    provider = DeepgramProvider(
        Settings(host_url="https://stt.example.com", host_secret="secret", model="nova-2", language="fa"),
        token_provider=lambda: "token",
    )
    with pytest.raises(ProviderError) as excinfo:
        provider.validate_config()
    assert excinfo.value.category is ErrorCategory.CONFIG
    assert "nova-3" in str(excinfo.value)


def test_validate_config_rejects_an_unknown_model_before_connecting(monkeypatch):
    from medical_stt.config import Settings
    from medical_stt.stt.deepgram_provider import DeepgramProvider

    captured = {}
    _install_fake_client(monkeypatch, _FakeConnection(), captured)

    provider = DeepgramProvider(
        Settings(host_url="https://stt.example.com", host_secret="secret", model="nova-9"),
        token_provider=lambda: "token",
    )
    with pytest.raises(ProviderError) as excinfo:
        provider.validate_config()
    assert excinfo.value.category is ErrorCategory.CONFIG
    assert captured == {}, "no connection may be opened for an invalid model"


def test_validate_config_rejects_an_invalid_language():
    from medical_stt.config import Settings
    from medical_stt.stt.deepgram_provider import DeepgramProvider

    provider = DeepgramProvider(
        Settings(host_url="https://stt.example.com", host_secret="secret", language="fa_IR"),
        token_provider=lambda: "token",
    )
    with pytest.raises(ProviderError) as excinfo:
        provider.validate_config()
    assert excinfo.value.category is ErrorCategory.CONFIG


def test_a_fresh_token_is_fetched_for_every_connection(monkeypatch):
    """A reconnect must not present a token that has already expired."""
    import deepgram

    from medical_stt.config import Settings
    from medical_stt.stt.deepgram_provider import DeepgramProvider

    issued = []
    connection = _FakeConnection()

    class FakeClient:
        def __init__(self, **kwargs):
            issued.append(kwargs.get("access_token"))

        @property
        def listen(self):
            class V1:
                def connect(self, **params):
                    return connection

            class V2:
                v1 = V1()

            return V2()

    monkeypatch.setattr(deepgram, "DeepgramClient", FakeClient)

    tokens = iter(["token-1", "token-2"])
    provider = DeepgramProvider(
        Settings(host_url="https://stt.example.com", host_secret="secret"),
        token_provider=lambda: next(tokens),
    )
    provider.start(lambda _e: None, lambda _e: None)
    provider.start(lambda _e: None, lambda _e: None)

    assert issued == ["token-1", "token-2"]


# -- single-language Persian ASR (Phase 6: no code-switching claim) --------


def test_provider_requests_one_configured_language(monkeypatch):
    """The ASR runs in Persian; English terms are handled by keyterms and
    local terminology rules, so no language detection may be requested.

    If this ever changes, README > "Deepgram / streaming" and the
    Limitations section must change with it -- an unfounded "code
    switching" claim is a clinical-accuracy hazard.
    """
    from medical_stt.config import Settings
    from medical_stt.stt.deepgram_provider import DeepgramProvider

    captured = {}
    connection = _FakeConnection()
    _install_fake_client(monkeypatch, connection, captured)

    provider = DeepgramProvider(
        Settings(host_url="https://stt.example.com", host_secret="secret"),
        keyterms=["ICU"],
        token_provider=lambda: "token",
    )
    provider.start(lambda _e: None, lambda _e: None)

    params = captured["connect"]
    assert params["language"] == "fa"
    for unsupported in ("detect_language", "multilingual", "language_hints", "code_switching"):
        assert unsupported not in params, f"{unsupported} would imply language switching"


def test_settings_default_to_persian_and_never_a_switching_locale():
    from medical_stt.config import Settings

    settings = Settings()
    assert settings.language == "fa"
    assert "," not in settings.language, "a multi-language list would be code switching"


def test_readme_does_not_claim_code_switching():
    """The README may *deny* language switching, but never claim it.

    A blunt substring check would also forbid the honest disclaimer, so the
    check looks at the sentence: a forbidden phrase is only an offence when
    it is not negated, and the README must contain the negated disclaimer
    somewhere (silence about the limitation is not acceptable either).
    """
    from pathlib import Path

    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    forbidden = ("code-switch", "code switch", "seamlessly switches", "switches between languages")
    negations = ("not ", "never", "no ", "rather than", "instead of", "without", "n't")

    def negated(sentence: str) -> bool:
        return any(negation in sentence for negation in negations)

    # Strip markdown emphasis so "**not**" still counts as a negation.
    sentences = [
        re.sub(r"[*_`]", "", part.strip().lower())
        for part in re.split(r"(?<=[.!?\n])\s+", readme)
        if part.strip()
    ]
    offences = [s for s in sentences if any(f in s for f in forbidden) and not negated(s)]
    assert not offences, f"README claims language switching: {offences}"
    assert any(negated(s) and any(f in s for f in forbidden) for s in sentences), (
        "README must explicitly deny unrestricted code-switching (see the language-scope section)"
    )
