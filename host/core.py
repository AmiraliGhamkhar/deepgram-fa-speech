"""Host-side logic for the Medical STT session service.

Deliberately small and free of web-framework imports so it can be unit
tested without installing FastAPI:

* `HostSettings` is read from the environment.
* `authorize()` checks the shared secret in constant time.
* `RateLimiter` is a per-client fixed-window counter.
* `grant_session()` exchanges the Deepgram API key -- which lives *only*
  here, in this process's environment -- for a short-lived Deepgram
  session token, and returns that token to the authenticated client.

Nothing in this module logs the Deepgram key or the issued token.
"""
from __future__ import annotations

import hmac
import logging
import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, Optional, Tuple

log = logging.getLogger("medical_stt.host")

#: Deepgram's documented default and maximum for temporary token TTL.
DEFAULT_TTL_SECONDS = 30
MAX_TTL_SECONDS = 3600

#: Deepgram's token endpoint.
DEEPGRAM_GRANT_URL = "https://api.deepgram.com/v1/auth/grant"


class ConfigurationError(RuntimeError):
    """The host is missing required environment configuration."""


class GrantError(RuntimeError):
    """Deepgram refused to issue a session token.

    `status` is the upstream HTTP status, used to pick a sane status code
    for the client. The Deepgram key is never part of the message.
    """

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class HostSettings:
    """Configuration read from the environment at startup."""

    deepgram_api_key: str
    shared_secret: str
    default_ttl_seconds: int = DEFAULT_TTL_SECONDS
    max_ttl_seconds: int = MAX_TTL_SECONDS
    rate_limit_requests: int = 30
    rate_limit_window_seconds: int = 60
    allow_http: bool = False
    grant_timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "HostSettings":
        source = os.environ if env is None else env
        api_key = (source.get("DEEPGRAM_API_KEY") or "").strip()
        if not api_key:
            raise ConfigurationError(
                "DEEPGRAM_API_KEY is not set. The Deepgram key must exist only "
                "in this service's environment, never in the client."
            )
        secret = (source.get("HOST_SHARED_SECRET") or "").strip()
        if len(secret) < 24:
            # Short secrets make brute forcing a 401-space trivial; refuse
            # to start rather than accept one.
            raise ConfigurationError(
                "HOST_SHARED_SECRET is not set or is shorter than 24 characters. "
                "Generate one with: python -c \"import secrets;"
                "print(secrets.token_urlsafe(32))\""
            )

        def _int(name: str, fallback: int) -> int:
            raw = source.get(name)
            if not raw:
                return fallback
            try:
                return int(raw)
            except ValueError:
                log.warning("ignoring non-numeric %s=%r", name, raw)
                return fallback

        def _float(name: str, fallback: float) -> float:
            raw = source.get(name)
            if not raw:
                return fallback
            try:
                return float(raw)
            except ValueError:
                log.warning("ignoring non-numeric %s=%r", name, raw)
                return fallback

        max_ttl = min(_int("HOST_MAX_TTL_SECONDS", MAX_TTL_SECONDS), MAX_TTL_SECONDS)
        default_ttl = _int("HOST_DEFAULT_TTL_SECONDS", DEFAULT_TTL_SECONDS)
        return cls(
            deepgram_api_key=api_key,
            shared_secret=secret,
            default_ttl_seconds=max(5, min(default_ttl, max_ttl)),
            max_ttl_seconds=max(5, max_ttl),
            rate_limit_requests=max(1, _int("HOST_RATE_LIMIT_REQUESTS", 30)),
            rate_limit_window_seconds=max(1, _int("HOST_RATE_LIMIT_WINDOW_SECONDS", 60)),
            allow_http=(source.get("HOST_ALLOW_HTTP") or "").strip().lower()
            in ("1", "true", "yes"),
            grant_timeout_seconds=max(1.0, _float("HOST_GRANT_TIMEOUT_SECONDS", 10.0)),
        )


def normalize_ttl(raw: Any, settings: HostSettings) -> int:
    """Clamp a requested TTL into the allowed range."""
    try:
        requested = int(raw) if raw is not None else settings.default_ttl_seconds
    except (TypeError, ValueError):
        requested = settings.default_ttl_seconds
    return max(5, min(requested, settings.max_ttl_seconds))


def authorize(authorization_header: Optional[str], settings: HostSettings) -> bool:
    """Constant-time check of `Authorization: Bearer <shared secret>`."""
    if not authorization_header:
        return False
    scheme, _, presented = authorization_header.partition(" ")
    if scheme.strip().lower() != "bearer":
        return False
    return hmac.compare_digest(presented.strip(), settings.shared_secret)


class RateLimiter:
    """Fixed-window per-client request counter.

    In-process and therefore per-worker; for a single small service that is
    the right trade-off (no Redis, no extra moving parts). Put a reverse
    proxy in front for multi-worker deployments.
    """

    def __init__(self, limit: int, window_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, client_id: str) -> Tuple[bool, int]:
        """Return (allowed, seconds until the window resets)."""
        now = self._clock()
        cutoff = now - self._window
        with self._lock:
            hits = self._hits[client_id]
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self._limit:
                retry_after = max(1, int(hits[0] + self._window - now) + 1)
                return False, retry_after
            hits.append(now)
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


def parse_token_response(payload: Any) -> Tuple[str, int]:
    """Extract (access_token, expires_in) from a Deepgram grant response."""
    if not isinstance(payload, dict):
        raise GrantError("Deepgram returned an unexpected response", 502)
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise GrantError("Deepgram did not return a session token", 502)
    try:
        expires_in = int(payload.get("expires_in", 0))
    except (TypeError, ValueError):
        expires_in = 0
    if expires_in <= 0:
        raise GrantError("Deepgram returned an invalid token expiry", 502)
    return token, expires_in


#: Injection point for tests: takes (url, api_key, ttl, timeout) and returns
#: the decoded JSON body of the grant response.
GrantTransport = Callable[[str, str, int, float], Any]


def _httpx_grant(url: str, api_key: str, ttl_seconds: int, timeout: float) -> Any:
    """Default transport: a plain HTTPS POST to Deepgram."""
    import httpx

    response = httpx.post(
        url,
        headers={"Authorization": f"Token {api_key}", "Content-Type": "application/json"},
        json={"ttl_seconds": ttl_seconds},
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise GrantError(
            f"Deepgram refused the token request (HTTP {response.status_code})",
            # 401/403 here means the *host's own* Deepgram key is wrong or
            # lacks Member permission: an operator problem, not the
            # client's, so it is surfaced as 502 rather than 401.
            502 if response.status_code in (401, 403) else 503,
        )
    try:
        return response.json()
    except ValueError as exc:
        raise GrantError("Deepgram returned a non-JSON response", 502) from exc


def grant_session(
    settings: HostSettings,
    ttl_seconds: int,
    transport: Optional[GrantTransport] = None,
) -> Tuple[str, int]:
    """Mint a short-lived Deepgram session token.

    This is the only place in the whole system where the Deepgram API key
    is used, and it is used solely to obtain a token that expires within
    seconds.
    """
    send = transport or _httpx_grant
    payload = send(
        DEEPGRAM_GRANT_URL, settings.deepgram_api_key, ttl_seconds, settings.grant_timeout_seconds
    )
    token, expires_in = parse_token_response(payload)
    log.info("issued a Deepgram session token (ttl_requested=%ds)", ttl_seconds)
    return token, expires_in
