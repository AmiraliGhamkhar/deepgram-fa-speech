"""Host service core tests (host/core.py).

These import no web framework, so they run anywhere with no FastAPI,
uvicorn or httpx installed and never touch the network.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "host"))

import core  # type: ignore[import-not-found]  # noqa: E402

API_KEY = "dg_key_not_real"
SHARED_SECRET = "s" * 32


def _settings(**overrides) -> "core.HostSettings":
    base = dict(
        deepgram_api_key=API_KEY,
        shared_secret=SHARED_SECRET,
        default_ttl_seconds=30,
        max_ttl_seconds=3600,
        rate_limit_requests=3,
        rate_limit_window_seconds=60,
    )
    base.update(overrides)
    return core.HostSettings(**base)


# -- configuration -------------------------------------------------------


def test_missing_deepgram_key_refuses_to_start():
    with pytest.raises(core.ConfigurationError) as excinfo:
        core.HostSettings.from_env({"HOST_SHARED_SECRET": SHARED_SECRET})
    assert "DEEPGRAM_API_KEY" in str(excinfo.value)


def test_short_shared_secret_refuses_to_start():
    with pytest.raises(core.ConfigurationError) as excinfo:
        core.HostSettings.from_env({"DEEPGRAM_API_KEY": API_KEY, "HOST_SHARED_SECRET": "tooshort"})
    assert "24 characters" in str(excinfo.value)


def test_missing_shared_secret_refuses_to_start_in_legacy_mode():
    with pytest.raises(core.ConfigurationError) as excinfo:
        core.HostSettings.from_env({"DEEPGRAM_API_KEY": API_KEY})
    assert "HOST_SHARED_SECRET" in str(excinfo.value)
    assert "24 characters" in str(excinfo.value)


@pytest.mark.parametrize("legacy_secret", [None, "tooshort"])
def test_registry_mode_ignores_legacy_shared_secret(tmp_path, legacy_secret):
    client_id = "doctor-01"
    device_secret = "device-secret-used-only-by-this-test"
    registry_path = tmp_path / "clients.txt"
    registry_path.write_text(
        f"{client_id}:{core.hash_client_secret(device_secret)}\n", encoding="utf-8"
    )
    env = {"DEEPGRAM_API_KEY": API_KEY, "HOST_CLIENTS_FILE": str(registry_path)}
    if legacy_secret is not None:
        env["HOST_SHARED_SECRET"] = legacy_secret

    settings = core.HostSettings.from_env(env)

    assert settings.shared_secret == (legacy_secret or "")
    registry = settings.build_client_registry()
    identity = core.authenticate_client(f"Bearer {device_secret}", client_id, registry)
    assert identity is not None and identity.client_id == client_id
    assert core.authenticate_client(f"Bearer {device_secret}", None, registry) is None


def test_settings_load_from_environment():
    settings = core.HostSettings.from_env({
        "DEEPGRAM_API_KEY": API_KEY,
        "HOST_SHARED_SECRET": SHARED_SECRET,
        "HOST_RATE_LIMIT_REQUESTS": "12",
    })
    assert settings.rate_limit_requests == 12
    assert settings.allow_http is False


def test_non_numeric_rate_limit_falls_back_to_default():
    settings = core.HostSettings.from_env({
        "DEEPGRAM_API_KEY": API_KEY,
        "HOST_SHARED_SECRET": SHARED_SECRET,
        "HOST_RATE_LIMIT_REQUESTS": "not-a-number",
    })
    assert settings.rate_limit_requests == 30


# -- authentication ------------------------------------------------------


def test_correct_bearer_secret_is_authorized():
    assert core.authorize(f"Bearer {SHARED_SECRET}", _settings()) is True


def test_bearer_scheme_is_case_insensitive():
    assert core.authorize(f"bearer {SHARED_SECRET}", _settings()) is True


def test_wrong_secret_is_rejected():
    assert core.authorize("Bearer wrong-secret-value", _settings()) is False


def test_missing_or_malformed_header_is_rejected():
    settings = _settings()
    assert core.authorize(None, settings) is False
    assert core.authorize("", settings) is False
    assert core.authorize(SHARED_SECRET, settings) is False  # no scheme
    assert core.authorize(f"Basic {SHARED_SECRET}", settings) is False


def test_authorization_does_not_reveal_which_part_failed():
    """No partial-match oracle: both wrong-scheme and wrong-secret fail."""
    settings = _settings()
    assert core.authorize(f"Basic {SHARED_SECRET}", settings) == core.authorize("Basic x", settings)


# -- rate limiting -------------------------------------------------------


def test_rate_limit_blocks_after_the_configured_count():
    clock = [1000.0]
    limiter = core.RateLimiter(limit=2, window_seconds=60, clock=lambda: clock[0])

    assert limiter.allow("1.2.3.4")[0] is True
    assert limiter.allow("1.2.3.4")[0] is True
    allowed, retry_after = limiter.allow("1.2.3.4")
    assert allowed is False
    assert retry_after > 0


def test_rate_limit_window_resets():
    clock = [1000.0]
    limiter = core.RateLimiter(limit=1, window_seconds=60, clock=lambda: clock[0])
    assert limiter.allow("ip")[0] is True
    assert limiter.allow("ip")[0] is False
    clock[0] += 61
    assert limiter.allow("ip")[0] is True


def test_rate_limit_is_per_client():
    limiter = core.RateLimiter(limit=1, window_seconds=60, clock=lambda: 0.0)
    assert limiter.allow("a")[0] is True
    assert limiter.allow("b")[0] is True


# -- TTL ----------------------------------------------------------------


def test_ttl_defaults_when_absent():
    assert core.normalize_ttl(None, _settings()) == 30


def test_ttl_is_clamped_to_the_maximum():
    assert core.normalize_ttl(99999, _settings()) == 3600


def test_ttl_is_clamped_to_a_sane_minimum():
    assert core.normalize_ttl(1, _settings()) == 5


def test_invalid_ttl_falls_back_to_the_default():
    assert core.normalize_ttl("nonsense", _settings()) == 30


def test_stale_limiter_entries_are_reclaimed():
    """Inactive client keys must not accumulate forever."""
    clock = [1000.0]
    limiter = core.RateLimiter(
        limit=5, window_seconds=60, clock=lambda: clock[0], purge_interval_calls=8
    )
    for index in range(50):
        limiter.allow(f"client-{index}")
    assert limiter.tracked_clients == 50

    clock[0] += 61  # every previous window is now expired
    for _ in range(8):
        limiter.allow("client-0")
    assert limiter.tracked_clients < 50, "expired client keys were not reclaimed"


def test_limiter_table_is_capped_even_for_active_clients():
    limiter = core.RateLimiter(
        limit=5,
        window_seconds=60,
        clock=lambda: 1000.0,
        max_tracked_clients=10,
        purge_interval_calls=1,
    )
    for index in range(200):
        limiter.allow(f"client-{index}")
    assert limiter.tracked_clients <= 10


def test_limiter_still_limits_after_a_purge():
    clock = [1000.0]
    limiter = core.RateLimiter(
        limit=2, window_seconds=60, clock=lambda: clock[0], purge_interval_calls=1
    )
    assert limiter.allow("ip")[0] is True
    assert limiter.allow("ip")[0] is True
    clock[0] += 120
    assert limiter.allow("ip")[0] is True


# -- HTTPS enforcement (trusted proxy handling) --------------------------


def test_direct_http_request_is_rejected():
    settings = _settings()
    assert core.request_is_secure("http", "203.0.113.9", None, settings) is False


def test_direct_http_with_forged_forwarded_proto_is_rejected():
    settings = _settings()
    assert core.request_is_secure("http", "203.0.113.9", "https", settings) is False
    # Even a forged header that lists several protocols is not trusted.
    assert core.request_is_secure("http", "203.0.113.9", "https, http", settings) is False


def test_direct_https_is_allowed():
    settings = _settings()
    assert core.request_is_secure("https", "203.0.113.9", None, settings) is True


def test_trusted_proxy_with_forwarded_proto_https_is_allowed():
    settings = _settings()
    assert core.request_is_secure("http", "127.0.0.1", "https", settings) is True
    assert core.request_is_secure("http", "127.0.0.1", "HTTPS", settings) is True
    # When a proxy appends its own observation, the LAST value is the one the
    # trusted immediate peer asserted. A forged leading "https" followed by
    # the proxy's real "http" must be rejected, not accepted.
    assert core.request_is_secure("http", "::1", "https, http", settings) is False
    assert core.request_is_secure("http", "::1", "http, https", settings) is True


def test_forged_leading_forwarded_proto_cannot_fake_https_through_a_proxy():
    """SECURITY.md: the trusted proxy's *last* hop decides, not the client's.

    A common proxy config appends rather than overwrites, so the client's
    leading value reaches the app untouched. Trusting it would let a plain
    HTTP client forge HTTPS; only the trailing (proxy-asserted) value counts.
    """
    settings = _settings()
    assert core.request_is_secure("http", "127.0.0.1", "https, http", settings) is False
    assert core.request_is_secure("http", "127.0.0.1", "https,http", settings) is False
    assert core.request_is_secure("http", "127.0.0.1", "https,", settings) is False


def test_trusted_proxy_without_the_header_is_still_plaintext():
    settings = _settings()
    assert core.request_is_secure("http", "127.0.0.1", None, settings) is False
    assert core.request_is_secure("http", "127.0.0.1", "http", settings) is False


def test_forwarded_allow_ips_is_configurable_and_defaults_to_loopback():
    settings = core.HostSettings.from_env(
        {
            "DEEPGRAM_API_KEY": API_KEY,
            "HOST_SHARED_SECRET": SHARED_SECRET,
            "HOST_FORWARDED_ALLOW_IPS": "10.0.0.7, 10.0.0.8",
        }
    )
    assert settings.forwarded_allow_ips == ("10.0.0.7", "10.0.0.8")
    assert core.request_is_secure("http", "10.0.0.7", "https", settings) is True
    assert core.request_is_secure("http", "10.0.0.9", "https", settings) is False

    default = core.HostSettings.from_env(
        {"DEEPGRAM_API_KEY": API_KEY, "HOST_SHARED_SECRET": SHARED_SECRET}
    )
    assert default.forwarded_allow_ips == core.DEFAULT_FORWARDED_ALLOW_IPS
    # An empty override must not silently trust everything.
    empty = core.HostSettings.from_env(
        {
            "DEEPGRAM_API_KEY": API_KEY,
            "HOST_SHARED_SECRET": SHARED_SECRET,
            "HOST_FORWARDED_ALLOW_IPS": "",
        }
    )
    assert empty.forwarded_allow_ips == core.DEFAULT_FORWARDED_ALLOW_IPS


def test_unknown_peer_is_never_a_trusted_proxy():
    settings = _settings()
    assert core.peer_is_trusted_proxy(None, settings) is False
    assert core.peer_is_trusted_proxy("", settings) is False
    assert core.peer_is_trusted_proxy("127.0.0.1", settings) is True


# -- Deepgram grant ------------------------------------------------------


def test_grant_returns_the_issued_token():
    seen = {}

    def transport(url, api_key, ttl, timeout):
        seen.update(url=url, api_key=api_key, ttl=ttl, timeout=timeout)
        return {"access_token": "issued-token", "expires_in": 30}

    token, expires_in = core.grant_session(_settings(), 30, transport=transport)
    assert (token, expires_in) == ("issued-token", 30)
    assert seen["url"] == core.DEEPGRAM_GRANT_URL
    assert seen["api_key"] == API_KEY


def test_grant_error_does_not_leak_the_api_key():
    def transport(url, api_key, ttl, timeout):
        raise core.GrantError("Deepgram refused the token request (HTTP 403)")

    with pytest.raises(core.GrantError) as excinfo:
        core.grant_session(_settings(), 30, transport=transport)
    assert API_KEY not in str(excinfo.value)
    assert excinfo.value.status == 502


def test_grant_response_without_token_is_rejected():
    with pytest.raises(core.GrantError):
        core.grant_session(_settings(), 30, transport=lambda *a: {"expires_in": 30})


def test_grant_response_with_bad_expiry_is_rejected():
    with pytest.raises(core.GrantError):
        core.grant_session(_settings(), 30, transport=lambda *a: {"access_token": "t", "expires_in": 0})


def test_grant_hits_deepgram_over_https_only():
    assert core.DEEPGRAM_GRANT_URL.startswith("https://")


# -- Deepgram grant transport failures -----------------------------------
#
# These exercise the default httpx transport with a stubbed httpx.post, so
# every network failure mode is proven to become a controlled GrantError
# (and therefore a 502/503/504 response) instead of an unhandled 500.


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, json_error=False):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("not json")
        return self._payload


def _patch_httpx(monkeypatch, behaviour):
    import httpx

    if isinstance(behaviour, BaseException):
        def post(*_args, **_kwargs):
            raise behaviour

        monkeypatch.setattr(httpx, "post", post)
    else:
        monkeypatch.setattr(httpx, "post", lambda *a, **k: behaviour)


def test_grant_timeout_becomes_504(monkeypatch):
    import httpx

    _patch_httpx(monkeypatch, httpx.TimeoutException("timed out"))
    with pytest.raises(core.GrantError) as excinfo:
        core.grant_session(_settings(), 30)
    assert excinfo.value.status == 504
    assert API_KEY not in str(excinfo.value)


def test_grant_connection_failure_becomes_503(monkeypatch):
    import httpx

    _patch_httpx(monkeypatch, httpx.ConnectError("connection refused"))
    with pytest.raises(core.GrantError) as excinfo:
        core.grant_session(_settings(), 30)
    assert excinfo.value.status == 503
    assert API_KEY not in str(excinfo.value)


def test_grant_generic_request_error_becomes_503(monkeypatch):
    import httpx

    _patch_httpx(monkeypatch, httpx.RequestError("boom"))
    with pytest.raises(core.GrantError) as excinfo:
        core.grant_session(_settings(), 30)
    assert excinfo.value.status == 503


def test_grant_non_success_response_is_mapped(monkeypatch):
    _patch_httpx(monkeypatch, _FakeResponse(status_code=500))
    with pytest.raises(core.GrantError) as excinfo:
        core.grant_session(_settings(), 30)
    assert excinfo.value.status == 503

    _patch_httpx(monkeypatch, _FakeResponse(status_code=403))
    with pytest.raises(core.GrantError) as excinfo:
        core.grant_session(_settings(), 30)
    assert excinfo.value.status == 502


def test_grant_malformed_json_becomes_502(monkeypatch):
    _patch_httpx(monkeypatch, _FakeResponse(status_code=200, json_error=True))
    with pytest.raises(core.GrantError) as excinfo:
        core.grant_session(_settings(), 30)
    assert excinfo.value.status == 502


def test_grant_malformed_payload_becomes_502(monkeypatch):
    _patch_httpx(monkeypatch, _FakeResponse(status_code=200, payload={"nope": True}))
    with pytest.raises(core.GrantError) as excinfo:
        core.grant_session(_settings(), 30)
    assert excinfo.value.status == 502


def test_grant_success_through_the_default_transport(monkeypatch):
    _patch_httpx(
        monkeypatch,
        _FakeResponse(status_code=200, payload={"access_token": "issued", "expires_in": 30}),
    )
    assert core.grant_session(_settings(), 30) == ("issued", 30)


# -- failed-authentication attribution -----------------------------------


def test_auth_failure_key_prefers_the_peer_address():
    assert core.auth_failure_key("203.0.113.9", "doctor-01") == "auth:203.0.113.9"
    assert core.auth_failure_key("::1", None) == "auth:::1"


def test_auth_failure_key_falls_back_to_a_valid_client_id():
    """A WSGI environ can carry no peer address at all."""
    assert core.auth_failure_key(None, "doctor-01") == "auth-id:doctor-01"
    assert core.auth_failure_key("", "  doctor-02  ") == "auth-id:doctor-02"


def test_auth_failure_key_is_none_when_nothing_can_be_attributed():
    """None means "charge no lockout", never "share one global bucket".

    A budget shared by every unattributable request turns the brute-force
    control into a denial of service: 20 failures from anywhere would lock
    every legitimate clinician out for a whole window.
    """
    assert core.auth_failure_key(None, None) is None
    assert core.auth_failure_key("", "") is None
    # An unusable client id must not become a key either: it is attacker
    # chosen, and an over-long or control-character id would also end up in
    # the log line.
    assert core.auth_failure_key(None, "a" * 65) is None
    assert core.auth_failure_key(None, "bad id!") is None
    assert core.auth_failure_key(None, 12345) is None


def test_auth_failure_keys_do_not_collide_between_peer_and_client_id():
    """The two namespaces must not be able to shadow each other."""
    assert core.auth_failure_key("doctor-01", None) != core.auth_failure_key(None, "doctor-01")
