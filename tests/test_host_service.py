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
