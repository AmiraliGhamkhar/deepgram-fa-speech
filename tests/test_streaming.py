"""STT provider error classification & reconnect policy tests (task
sections 7, 9). No real network access or Deepgram credentials are used."""
from __future__ import annotations

import socket


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
