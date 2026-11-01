"""Host-side logic for the Medical STT session service -- facade.

The service is deliberately free of web-framework imports so it can be unit
tested without installing FastAPI. That logic now lives in modules split by
responsibility:

==========================  =================================================
`config`                    `HostSettings`, environment parsing, the Deepgram
                            vocabulary (regions, TTL bounds), API-key shape
`identity`                  hashed secrets, the client registry, constant-time
                            authentication, brute-force budget keys
`ratelimit`                 token-bucket (per-client + global) and fixed-window
                            (failed authentication) limiters
`grant`                     exchanging the host's key for a short-lived
                            Deepgram session token, sync and async
`request_guard`             HTTPS/`X-Forwarded-Proto` decisions and TTL clamping
==========================  =================================================

**Why this module still exists:** `host/app.py`, `host/passenger_wsgi.py`, the
provisioning tool, `scripts/measure_capacity.py` and the test suite all import
from `host.core`. Re-exporting keeps that single, documented entry point while
the code itself stays small enough to review.

**Deployment shape:** `host/` is also copied on its own to cPanel, so every
module here imports both as `host.<module>` and as a flat top-level module.

**Security invariants (unchanged):** nothing here logs the Deepgram key, a
client secret, or an issued token; the Deepgram key is used only to mint
short-lived tokens; secrets are stored only as SHA-256 hashes.
"""
from __future__ import annotations

try:  # package import from a full checkout
    from .config import (
        CLIENT_ID_PATTERN,
        DEEPGRAM_GRANT_URL,
        DEEPGRAM_REGION_HOSTS,
        DEFAULT_FORWARDED_ALLOW_IPS,
        DEFAULT_TTL_SECONDS,
        LEGACY_CLIENT_ID,
        MAX_CLIENT_ID_LENGTH,
        MAX_REQUEST_BODY_BYTES,
        MAX_TTL_SECONDS,
        SESSION_ID_BYTES,
        ConfigurationError,
        HostSettings,
        _API_KEY_SAFE_RE,
        _CLIENT_ID_RE,
        check_api_key_shape,
        deepgram_grant_url,
        parse_forwarded_allow_ips,
    )
    from .grant import (
        GRANT_BODYLESS_STATUSES,
        AsyncGrantClient,
        GrantError,
        GrantTransport,
        _grant_error_status,
        _grant_response_error,
        _httpx_grant,
        _retry_after,
        grant_session,
        grant_session_async,
        new_session_id,
        parse_token_response,
        response_is_a_grant,
    )
    from .identity import (
        ClientIdentity,
        ClientRegistry,
        _UNKNOWN_CLIENT_DIGEST,
        _is_sha256_hex,
        authenticate_client,
        auth_failure_key,
        hash_client_secret,
        is_valid_client_id,
        normalize_client_id,
    )
    from .ratelimit import (
        MAX_TRACKED_CLIENTS,
        PURGE_INTERVAL_CALLS,
        RateLimiter,
        TokenBucketRateLimiter,
    )
    from .request_guard import normalize_ttl, peer_is_trusted_proxy, request_is_secure
except ImportError:  # pragma: no cover - cPanel copies only the host/ dir
    from config import (  # type: ignore[no-index]
        CLIENT_ID_PATTERN,
        DEEPGRAM_GRANT_URL,
        DEEPGRAM_REGION_HOSTS,
        DEFAULT_FORWARDED_ALLOW_IPS,
        DEFAULT_TTL_SECONDS,
        LEGACY_CLIENT_ID,
        MAX_CLIENT_ID_LENGTH,
        MAX_REQUEST_BODY_BYTES,
        MAX_TTL_SECONDS,
        SESSION_ID_BYTES,
        ConfigurationError,
        HostSettings,
        _API_KEY_SAFE_RE,
        _CLIENT_ID_RE,
        check_api_key_shape,
        deepgram_grant_url,
        parse_forwarded_allow_ips,
    )
    from grant import (  # type: ignore[no-index]
        GRANT_BODYLESS_STATUSES,
        AsyncGrantClient,
        GrantError,
        GrantTransport,
        _grant_error_status,
        _grant_response_error,
        _httpx_grant,
        _retry_after,
        grant_session,
        grant_session_async,
        new_session_id,
        parse_token_response,
        response_is_a_grant,
    )
    from identity import (  # type: ignore[no-index]
        ClientIdentity,
        ClientRegistry,
        _UNKNOWN_CLIENT_DIGEST,
        _is_sha256_hex,
        authenticate_client,
        auth_failure_key,
        hash_client_secret,
        is_valid_client_id,
        normalize_client_id,
    )
    from ratelimit import (  # type: ignore[no-index]
        MAX_TRACKED_CLIENTS,
        PURGE_INTERVAL_CALLS,
        RateLimiter,
        TokenBucketRateLimiter,
    )
    from request_guard import (  # type: ignore[no-index]
        normalize_ttl,
        peer_is_trusted_proxy,
        request_is_secure,
    )

# Names are intentionally re-exported, including the underscore-prefixed
# helpers that tests call directly; listing them keeps a linter from "tidying"
# this facade into a broken one.
__all__ = [
    "CLIENT_ID_PATTERN",
    "ConfigurationError",
    "DEEPGRAM_GRANT_URL",
    "DEEPGRAM_REGION_HOSTS",
    "DEFAULT_FORWARDED_ALLOW_IPS",
    "DEFAULT_TTL_SECONDS",
    "AsyncGrantClient",
    "ClientIdentity",
    "ClientRegistry",
    "GRANT_BODYLESS_STATUSES",
    "GrantError",
    "GrantTransport",
    "HostSettings",
    "LEGACY_CLIENT_ID",
    "MAX_CLIENT_ID_LENGTH",
    "MAX_REQUEST_BODY_BYTES",
    "MAX_TRACKED_CLIENTS",
    "MAX_TTL_SECONDS",
    "PURGE_INTERVAL_CALLS",
    "RateLimiter",
    "SESSION_ID_BYTES",
    "TokenBucketRateLimiter",
    "authenticate_client",
    "auth_failure_key",
    "check_api_key_shape",
    "deepgram_grant_url",
    "grant_session",
    "grant_session_async",
    "hash_client_secret",
    "is_valid_client_id",
    "new_session_id",
    "normalize_client_id",
    "normalize_ttl",
    "parse_forwarded_allow_ips",
    "parse_token_response",
    "peer_is_trusted_proxy",
    "request_is_secure",
    "response_is_a_grant",
    "_API_KEY_SAFE_RE",
    "_CLIENT_ID_RE",
    "_UNKNOWN_CLIENT_DIGEST",
    "_grant_error_status",
    "_grant_response_error",
    "_httpx_grant",
    "_is_sha256_hex",
    "_retry_after",
]
