"""Host session client tests: error classification and secret hygiene.

No network access and no real credentials: the opener is a stub.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from medical_stt.host_client import HostSessionClient, SESSION_PATH
from medical_stt.stt.base import ErrorCategory, ProviderError

SECRET = "super-secret-shared-value-not-real"
HOST = "https://stt.example.com"


def _ok_response(token: str = "jwt-token-value", expires_in: int = 30) -> bytes:
    return json.dumps({"access_token": token, "expires_in": expires_in}).encode()


def _client(handler) -> HostSessionClient:
    """Build a client whose opener calls `handler(request, timeout)`."""
    return HostSessionClient(HOST, SECRET, opener=lambda request, timeout: handler(request, timeout))


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(HOST + SESSION_PATH, code, "err", {}, None)  # type: ignore[arg-type]


def test_successful_session_returns_token_and_expiry():
    captured = {}

    def handler(request, timeout):
        captured["url"] = request.full_url
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.data.decode())
        return _ok_response()

    session = _client(handler).fetch_session(ttl_seconds=45)

    assert session.access_token == "jwt-token-value"
    assert session.expires_in == 30
    assert captured["url"] == HOST + SESSION_PATH
    assert captured["auth"] == f"Bearer {SECRET}"
    assert captured["body"] == {"ttl_seconds": 45}


def test_rejected_secret_is_auth_and_never_retried():
    with pytest.raises(ProviderError) as excinfo:
        _client(lambda _r, _t: (_ for _ in ()).throw(_http_error(401))).fetch_session()
    assert excinfo.value.category is ErrorCategory.AUTH
    assert not excinfo.value.category.is_retryable


def test_rate_limit_maps_to_rate_limit():
    with pytest.raises(ProviderError) as excinfo:
        _client(lambda _r, _t: (_ for _ in ()).throw(_http_error(429))).fetch_session()
    assert excinfo.value.category is ErrorCategory.RATE_LIMIT


def test_bad_request_maps_to_config():
    with pytest.raises(ProviderError) as excinfo:
        _client(lambda _r, _t: (_ for _ in ()).throw(_http_error(400))).fetch_session()
    assert excinfo.value.category is ErrorCategory.CONFIG


def test_server_error_maps_to_server_disconnect():
    with pytest.raises(ProviderError) as excinfo:
        _client(lambda _r, _t: (_ for _ in ()).throw(_http_error(503))).fetch_session()
    assert excinfo.value.category is ErrorCategory.SERVER_DISCONNECT


def test_network_failure_maps_to_network():
    def handler(request, timeout):
        raise urllib.error.URLError("connection refused")

    with pytest.raises(ProviderError) as excinfo:
        _client(handler).fetch_session()
    assert excinfo.value.category is ErrorCategory.NETWORK


def test_timeout_maps_to_timeout():
    def handler(request, timeout):
        raise TimeoutError("too slow")

    with pytest.raises(ProviderError) as excinfo:
        _client(handler).fetch_session()
    assert excinfo.value.category is ErrorCategory.TIMEOUT


def test_error_messages_never_contain_the_secret():
    """A secret must never leak through an exception message or cause."""
    try:
        _client(lambda _r, _t: (_ for _ in ()).throw(_http_error(401))).fetch_session()
    except ProviderError as exc:
        rendered = f"{exc} {exc.__cause__ or ''}"
        assert SECRET not in rendered


def test_missing_secret_fails_before_any_request():
    calls = []

    def handler(request, timeout):  # pragma: no cover - must not run
        calls.append(request)
        return _ok_response()

    client = HostSessionClient(HOST, "", opener=handler)
    with pytest.raises(ProviderError) as excinfo:
        client.fetch_session()
    assert excinfo.value.category is ErrorCategory.CONFIG
    assert calls == []


def test_response_without_token_is_rejected():
    with pytest.raises(ProviderError) as excinfo:
        _client(lambda _r, _t: b"{}").fetch_session()
    assert excinfo.value.category is ErrorCategory.UNKNOWN


def test_non_json_response_is_rejected():
    with pytest.raises(ProviderError) as excinfo:
        _client(lambda _r, _t: b"<html>not json</html>").fetch_session()
    assert excinfo.value.category is ErrorCategory.UNKNOWN


def test_trailing_slash_in_base_url_is_tolerated():
    seen = {}

    def handler(request, timeout):
        seen["url"] = request.full_url
        return _ok_response()

    HostSessionClient(HOST + "/", SECRET, opener=handler).fetch_session()
    assert seen["url"] == HOST + SESSION_PATH
