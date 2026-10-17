"""Host-side logic for the Medical STT session service.

Deliberately small and free of web-framework imports so it can be unit
tested without installing FastAPI:

* `HostSettings` is read from the environment.
* `ClientRegistry` maps a `client_id` to a *hash* of that client's secret,
  so the host can tell 50 doctors apart without ever holding a plaintext
  secret.
* `authenticate_client()` checks the presented secret in constant time.
* `TokenBucketRateLimiter` is a burst-tolerant, bounded token bucket used
  for the per-client and global limits.
* `grant_session()` exchanges the Deepgram API key -- which lives *only*
  here, in this process's environment -- for a short-lived Deepgram
  session token, and returns that token to the authenticated client.
* `AsyncGrantClient` performs that exchange on the asyncio event loop via a
  reused `httpx.AsyncClient` connection pool, so 50 simultaneous token
  requests never block the loop.

Nothing in this module logs the Deepgram key, a client secret, or the
issued token.
"""
from __future__ import annotations

import hashlib
import math
import hmac
import logging
import os
import re
import threading
import time
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Tuple

log = logging.getLogger("medical_stt.host")

#: Deepgram's documented default and maximum for temporary token TTL.
DEFAULT_TTL_SECONDS = 30
MAX_TTL_SECONDS = 3600

#: Deepgram's token endpoint (default region). Regional hosts accept the
#: same key and mint tokens that are valid in every region; see
#: https://developers.deepgram.com/reference/regional-endpoints
DEEPGRAM_REGION_HOSTS = {
    "us": "api.deepgram.com",
    "eu": "api.eu.deepgram.com",
    "au": "api.au.deepgram.com",
    "in": "api.in.deepgram.com",
}
DEEPGRAM_GRANT_URL = "https://api.deepgram.com/v1/auth/grant"


def deepgram_grant_url(region: str) -> str:
    """The grant endpoint for a region; always https."""
    try:
        return f"https://{DEEPGRAM_REGION_HOSTS[region]}/v1/auth/grant"
    except KeyError:
        raise ConfigurationError(
            f"DEEPGRAM_REGION must be one of {sorted(DEEPGRAM_REGION_HOSTS)}, got {region!r}"
        ) from None

#: Peers that may be trusted to set `X-Forwarded-Proto` when TLS is
#: terminated by a reverse proxy. Loopback only by default: the proxy is
#: expected to run on the same machine as the service.
DEFAULT_FORWARDED_ALLOW_IPS = ("127.0.0.1", "::1")

#: Upper bound on the number of client keys the in-memory rate limiter
#: keeps. Without this, a stream of unique client addresses (a scanner, a
#: botnet, or a load balancer that forwards unpredictable addresses) would
#: grow the table without bound.
MAX_TRACKED_CLIENTS = 10_000

#: `allow()` calls between opportunistic purges of expired client keys.
PURGE_INTERVAL_CALLS = 256

#: Largest request body the host will read, in bytes. A session request is a
#: single small JSON object; anything larger is either a mistake or an
#: attempt to exhaust memory, and is refused before it is buffered.
MAX_REQUEST_BODY_BYTES = 4_096

#: Longest accepted `client_id`. Bounds the per-request work and the log
#: line; real client ids are short.
MAX_CLIENT_ID_LENGTH = 64

#: The whole accepted alphabet for a `client_id`: ASCII alphanumerics plus
#: `-`, `_` and `.`. ASCII on purpose -- `str.isalnum()` is Unicode-aware, so
#: a permissive check admits Cyrillic `саfé-cyrillic`, fullwidth `ＡＢＣ` and
#: other homoglyphs that are *distinct* rate-limit keys and registry entries
#: while looking identical in a text editor or a log line. Revoking a device
#: is "delete its line from the registry", which a confusable id defeats.
#: Mirrored in `host/provision.py` and `medical_stt/config.py`; all three must
#: move together or the client accepts an id the host refuses to load.
CLIENT_ID_PATTERN = r"[A-Za-z0-9._-]{1,%d}" % MAX_CLIENT_ID_LENGTH

#: Generated session ids are UUID4 *hex*: 32 lowercase hex characters, the
#: hyphenless form, because the id is embedded in log lines and returned in a
#: JSON field where a fixed-width opaque token is easier to grep than a
#: hyphenated UUID.
SESSION_ID_BYTES = 16

#: Characters a Deepgram API key may contain. Anything else -- a smart quote
#: or a non-breaking space left by a paste, a stray newline, a fullwidth
#: letter -- makes the HTTP transport refuse the `Authorization` header, and
#: the key must fail at startup rather than as a 500 on the clinic's first
#: Start press. Deliberately strict and checked once, at config time.
_API_KEY_SAFE_RE = re.compile(r"\A[\x21-\x7e]+\Z")
_CLIENT_ID_RE = re.compile(CLIENT_ID_PATTERN)

#: Identity used when a deployment has not provisioned per-device
#: credentials yet. A single shared secret maps onto this one id, so legacy
#: clients need no `X-Client-Id` header -- but the host then has exactly one
#: identity to rate limit, which is why provisioning is required for 50+.
LEGACY_CLIENT_ID = "legacy"


class ConfigurationError(RuntimeError):
    """The host is missing required environment configuration."""


def check_api_key_shape(api_key: str) -> None:
    """Refuse a Deepgram key that cannot be sent, before serving any request.

    The key itself is never echoed: a configuration error is logged and may
    end up in a journald/cloud log, so only the *offset* and the character
    *class* of the problem are named, never the value or a prefix of it.
    """
    if not api_key:
        raise ConfigurationError("DEEPGRAM_API_KEY is empty after stripping whitespace.")
    match = _API_KEY_SAFE_RE.match(api_key)
    if match is not None:
        return
    # Point at the first character the transport could not encode, without
    # naming it. A non-match above guarantees at least one such character.
    offset = min(
        (index for index, ch in enumerate(api_key) if not ("\x21" <= ch <= "\x7e")),
        default=len(api_key) - 1,
    )
    offender = api_key[offset]
    if offender.isspace():
        kind = "whitespace (a stray newline or space from a paste?)"
    elif ord(offender) < 0x21:
        kind = "a control character"
    else:
        kind = "a non-ASCII character"
    raise ConfigurationError(
        f"DEEPGRAM_API_KEY is not a printable ASCII token: problem at position "
        f"{offset} ({kind}). Re-copy the key from the Deepgram console rather than "
        "from a document or chat message."
    )


class GrantError(RuntimeError):
    """Deepgram refused to issue a session token.

    `status` is the application-level HTTP status the client should see
    (504 for an upstream timeout, 502 for an unusable upstream response,
    503 for an unavailable upstream). The Deepgram key and the upstream
    response body are never part of the message.
    """

    def __init__(self, message: str, status: int = 502, retry_after: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class HostSettings:
    """Configuration read from the environment at startup."""

    deepgram_api_key: str
    shared_secret: str
    #: Deepgram region the grant request goes to (`us`, `eu`, `au`, `in`).
    #: The token it returns is valid in every region, so this only decides
    #: where the host's own traffic terminates.
    deepgram_region: str = "us"
    default_ttl_seconds: int = DEFAULT_TTL_SECONDS
    max_ttl_seconds: int = MAX_TTL_SECONDS
    rate_limit_requests: int = 30
    rate_limit_window_seconds: int = 60
    allow_http: bool = False
    grant_timeout_seconds: float = 10.0
    #: Exact peer IPs allowed to set `X-Forwarded-Proto`. Anything else is
    #: treated as a direct client, so a forged header cannot fake HTTPS.
    forwarded_allow_ips: Tuple[str, ...] = DEFAULT_FORWARDED_ALLOW_IPS

    # -- client identity (Phase 4) --------------------------------------
    #: Registry file of `client_id:sha256hex` lines. Empty means "fall back
    #: to the legacy single shared secret", which keeps an existing
    #: deployment working while it is migrated to per-device credentials.
    clients_file: str = ""

    # -- rate limiting (Phase 4B) ---------------------------------------
    #: Per-client sustained rate, in requests per window. A clinician needs
    #: one token per Start plus the occasional reconnect (the client's own
    #: backoff saturates at 15-30 s between attempts), so this is generous
    #: for a person and tight for a loop.
    client_rate_limit_requests: int = 6
    #: Per-client burst. Every token is a potential Deepgram stream drawn
    #: from the pool the whole clinic shares, so one device must not be
    #: able to open dozens of them in a second. The morning surge is
    #: absorbed by the *global* bucket, not this one.
    client_burst: int = 10
    #: Global sustained ceiling across all clients.
    global_rate_limit_requests: int = 600
    #: Global burst. Must comfortably exceed the target session count.
    global_burst: int = 1_200
    #: Failed authentication attempts tolerated per IP per window. Keyed on
    #: IP because brute-force is abuse, not legitimate shared-NAT traffic.
    auth_failure_limit: int = 20

    # -- request bounds (Phase 6) ---------------------------------------
    max_request_body_bytes: int = MAX_REQUEST_BODY_BYTES
    #: Concurrent in-flight Deepgram grants before the host sheds load.
    max_inflight_grants: int = 100
    max_active_requests: int = 128
    request_body_timeout_seconds: float = 2.0
    grant_deadline_seconds: float = 7.0

    # -- Deepgram pool (Phase 3) ----------------------------------------
    grant_connect_timeout_seconds: float = 5.0
    grant_max_connections: int = 100
    grant_max_keepalive_connections: int = 20

    # -- observability (Phase 12) ---------------------------------------
    #: When set, `/metrics` requires `Authorization: Bearer <token>`.
    metrics_admin_token: str = ""

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "HostSettings":
        source = os.environ if env is None else env
        api_key = (source.get("DEEPGRAM_API_KEY") or "").strip()
        if not api_key:
            raise ConfigurationError(
                "DEEPGRAM_API_KEY is not set. The Deepgram key must exist only "
                "in this service's environment, never in the client."
            )
        # Checked here rather than on the first request: a key carrying a
        # pasted smart quote would otherwise surface as a 500 from the HTTP
        # transport, per clinician, forever.
        check_api_key_shape(api_key)
        secret = (source.get("HOST_SHARED_SECRET") or "").strip()
        clients_file = (source.get("HOST_CLIENTS_FILE") or "").strip()
        if not clients_file and len(secret) < 24:
            # The shared secret is used only by legacy single-identity mode.
            # Registry mode authenticates each device with its own secret and
            # must not require an unused global credential.
            raise ConfigurationError(
                "HOST_SHARED_SECRET is not set or is shorter than 24 characters. "
                "Generate one with: python -c \"import secrets;"
                "print(secrets.token_urlsafe(32))\""
            )

        def _number(name: str, fallback: Any, convert: Callable[[str], Any]) -> Any:
            raw = source.get(name)
            if not raw:
                return fallback
            try:
                value = convert(raw)
                if isinstance(value, float) and not math.isfinite(value):
                    raise ValueError("nonfinite")
                return value
            except (ValueError, OverflowError):
                log.warning("ignoring non-numeric %s=%r", name, raw)
                return fallback

        def _int(name: str, fallback: int) -> int:
            return _number(name, fallback, int)

        def _float(name: str, fallback: float) -> float:
            return _number(name, fallback, float)

        max_ttl = min(_int("HOST_MAX_TTL_SECONDS", MAX_TTL_SECONDS), MAX_TTL_SECONDS)
        default_ttl = _int("HOST_DEFAULT_TTL_SECONDS", DEFAULT_TTL_SECONDS)
        window = max(1, _int("HOST_RATE_LIMIT_WINDOW_SECONDS", 60))
        region = (source.get("DEEPGRAM_REGION") or "us").strip().lower()
        deepgram_grant_url(region)  # refuse to start on an unknown region

        # `HOST_RATE_LIMIT_REQUESTS` was the old per-IP fixed-window budget.
        # Session requests are now limited per authenticated client_id, so
        # this value no longer has anything to apply to. It is still parsed
        # (it is part of HostSettings and of existing deployments' config),
        # but setting it would otherwise be a silent no-op -- which is the
        # kind of trap where an operator tunes a limit and nothing happens.
        # Say so explicitly instead.
        if (source.get("HOST_RATE_LIMIT_REQUESTS") or "").strip():
            log.warning(
                "HOST_RATE_LIMIT_REQUESTS is ignored: session requests are now "
                "limited per authenticated client_id, not per IP. Use "
                "HOST_CLIENT_RATE_LIMIT_REQUESTS / HOST_CLIENT_BURST for "
                "per-client limits and HOST_GLOBAL_RATE_LIMIT_REQUESTS / "
                "HOST_GLOBAL_BURST for the host-wide ceiling. See "
                "host/README.md > Rate limiting."
            )
        return cls(
            deepgram_api_key=api_key,
            shared_secret=secret,
            deepgram_region=region,
            default_ttl_seconds=max(5, min(default_ttl, max_ttl)),
            max_ttl_seconds=max(5, max_ttl),
            rate_limit_requests=max(1, _int("HOST_RATE_LIMIT_REQUESTS", 30)),
            rate_limit_window_seconds=window,
            allow_http=(source.get("HOST_ALLOW_HTTP") or "").strip().lower()
            in ("1", "true", "yes"),
            grant_timeout_seconds=max(1.0, _float("HOST_GRANT_TIMEOUT_SECONDS", 10.0)),
            forwarded_allow_ips=parse_forwarded_allow_ips(
                source.get("HOST_FORWARDED_ALLOW_IPS")
            ),
            clients_file=clients_file,
            client_rate_limit_requests=max(1, _int("HOST_CLIENT_RATE_LIMIT_REQUESTS", 6)),
            client_burst=max(1, _int("HOST_CLIENT_BURST", 10)),
            global_rate_limit_requests=max(1, _int("HOST_GLOBAL_RATE_LIMIT_REQUESTS", 600)),
            global_burst=max(1, _int("HOST_GLOBAL_BURST", 1200)),
            auth_failure_limit=max(1, _int("HOST_AUTH_FAILURE_LIMIT", 20)),
            max_request_body_bytes=max(
                64, _int("HOST_MAX_REQUEST_BODY_BYTES", MAX_REQUEST_BODY_BYTES)
            ),
            max_inflight_grants=max(1, _int("HOST_MAX_INFLIGHT_GRANTS", 100)),
            max_active_requests=max(1, _int("HOST_MAX_ACTIVE_REQUESTS", 128)),
            request_body_timeout_seconds=max(0.1, _float("HOST_REQUEST_BODY_TIMEOUT_SECONDS", 2.0)),
            grant_deadline_seconds=max(0.1, _float("HOST_GRANT_DEADLINE_SECONDS", 7.0)),
            grant_connect_timeout_seconds=max(
                0.5, _float("HOST_GRANT_CONNECT_TIMEOUT_SECONDS", 5.0)
            ),
            grant_max_connections=max(1, _int("HOST_GRANT_MAX_CONNECTIONS", 100)),
            grant_max_keepalive_connections=max(
                1, _int("HOST_GRANT_MAX_KEEPALIVE_CONNECTIONS", 20)
            ),
            metrics_admin_token=(source.get("HOST_METRICS_ADMIN_TOKEN") or "").strip(),
        )

    @property
    def grant_url(self) -> str:
        return deepgram_grant_url(self.deepgram_region)

    def build_client_registry(self) -> "ClientRegistry":
        """Load the provisioned client registry.

        With no `HOST_CLIENTS_FILE` the service still runs, authenticated by
        the legacy shared secret alone. That path is a single identity
        mapped to `LEGACY_CLIENT_ID`, so an existing deployment keeps working
        while it is migrated to per-device credentials -- and it is exactly
        why the 50-clinician deployment must provision first.
        """
        if not self.clients_file:
            return ClientRegistry({LEGACY_CLIENT_ID: hash_client_secret(self.shared_secret)})
        return ClientRegistry.from_file(self.clients_file)


def parse_forwarded_allow_ips(raw: Optional[str]) -> Tuple[str, ...]:
    """Parse `HOST_FORWARDED_ALLOW_IPS` into a tuple of exact peer IPs.

    Comma-separated. An empty value falls back to loopback, which is the
    safe default: only a proxy running on this machine may assert the
    original scheme.
    """
    if raw is None or not raw.strip():
        return DEFAULT_FORWARDED_ALLOW_IPS
    entries = tuple(part.strip() for part in raw.split(",") if part.strip())
    return entries or DEFAULT_FORWARDED_ALLOW_IPS


def peer_is_trusted_proxy(peer_host: Optional[str], settings: HostSettings) -> bool:
    """True if `peer_host` is an explicitly configured reverse proxy.

    Exact match only (no subnets/wildcards): a mistake here would let any
    client claim its plaintext request arrived over HTTPS.
    """
    if not peer_host:
        return False
    return peer_host in settings.forwarded_allow_ips


def request_is_secure(
    scheme: str,
    peer_host: Optional[str],
    forwarded_proto: Optional[str],
    settings: HostSettings,
) -> bool:
    """Decide whether a request arrived over HTTPS.

    `X-Forwarded-Proto` is trusted *only* when the immediate peer is a
    configured reverse proxy. A direct HTTP client that forges the header
    is still rejected.

    When the header carries several comma-separated hops, the **last** value
    is the one used: that is the observation made by the proxy we actually
    trust (the immediate peer). The leading value is whatever the connecting
    client claimed, so trusting it would let a forged
    `X-Forwarded-Proto: https` survive a proxy that appends rather than
    overwrites -- see SECURITY.md ("uses the last hop rather than the
    first") and README > Deploying the host.
    """
    if (scheme or "").lower() == "https":
        return True
    if not peer_is_trusted_proxy(peer_host, settings):
        return False
    hops = [part.strip().lower() for part in (forwarded_proto or "").split(",")]
    if not hops or not hops[-1]:
        return False
    return hops[-1] == "https"


def normalize_ttl(raw: Any, settings: HostSettings) -> int:
    """Clamp a requested TTL into the allowed range."""
    try:
        requested = int(raw) if raw is not None else settings.default_ttl_seconds
    except (TypeError, ValueError, OverflowError):
        requested = settings.default_ttl_seconds
    return max(5, min(requested, settings.max_ttl_seconds))


# ---------------------------------------------------------------------------
# Client identity
# ---------------------------------------------------------------------------
#
# A hospital's 50 doctors sit behind one NAT, so a per-IP limit cannot tell
# them apart: it would reject 19 legitimate clinicians on a shared address.
# The host therefore authenticates each *device* with its own `client_id`
# and its own secret, and rate limits per client identity.
#
# Secrets are stored only as SHA-256 hashes. A hash is sufficient here because
# the secret is a 256-bit random value generated by `host/provision.py`, not a
# user-chosen password: there is no dictionary to attack, and an attacker who
# reads the host's memory still cannot use a hash to authenticate.


class RateLimiter:
    """Fixed-window request counter with bounded memory.

    Retained for the *failed-authentication* limiter, where a small fixed
    window over a raw IP is exactly the right shape: brute-force attempts
    are not legitimate traffic and must not be burst-tolerant.

    In-process and therefore per-worker; for a single small service that is
    the right trade-off (no Redis, no extra moving parts). Put a reverse
    proxy in front for multi-worker deployments.

    Client keys are reclaimed opportunistically during `allow()`: entries
    whose most recent hit is older than the window can no longer block
    anything, so they are dropped. The table is also capped, so a stream of
    unique client addresses cannot grow memory without bound.
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        max_tracked_clients: int = MAX_TRACKED_CLIENTS,
        purge_interval_calls: int = PURGE_INTERVAL_CALLS,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._max_tracked_clients = max(1, max_tracked_clients)
        self._purge_interval_calls = max(1, purge_interval_calls)
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._calls = 0

    def is_locked(self, client_id: str) -> Tuple[bool, int]:
        """True if this key is currently over its limit. Consumes nothing.

        Used to gate *failed* authentication: legitimate traffic must not
        spend the brute-force budget, so the budget is only charged when a
        request actually fails to authenticate.
        """
        now = self._clock()
        cutoff = now - self._window
        with self._lock:
            hits = self._hits.get(client_id)
            if not hits:
                return False, 0
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self._limit:
                retry_after = max(1, int(hits[0] + self._window - now) + 1)
                return True, retry_after
            return False, 0

    @property
    def tracked_clients(self) -> int:
        """Number of client keys currently held (test/observability hook)."""
        with self._lock:
            return len(self._hits)

    def allow(self, client_id: str) -> Tuple[bool, int]:
        """Return (allowed, seconds until the window resets)."""
        now = self._clock()
        cutoff = now - self._window
        with self._lock:
            self._calls += 1
            # Periodic sweep first, so an over-limit client cannot prevent
            # reclamation by never getting an allowed request.
            if self._calls % self._purge_interval_calls == 0:
                self._purge(cutoff)
            hits = self._hits[client_id]
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self._limit:
                retry_after = max(1, int(hits[0] + self._window - now) + 1)
                return False, retry_after
            hits.append(now)
            if len(self._hits) > self._max_tracked_clients:
                self._purge(cutoff)
            return True, 0

    def _purge(self, cutoff: float) -> None:
        """Drop keys whose newest hit is already outside the window.

        Called with `self._lock` held.
        """
        stale = [key for key, hits in self._hits.items() if not hits or hits[-1] <= cutoff]
        for key in stale:
            del self._hits[key]
        if len(self._hits) <= self._max_tracked_clients:
            return
        # Still over the cap means more than `max_tracked_clients` *active*
        # clients in one window: evict the least recently active keys. This
        # can only under-count abusive clients, never lock anyone out.
        ordered = sorted(self._hits.items(), key=lambda item: item[1][-1])
        for key, _ in ordered[: len(self._hits) - self._max_tracked_clients]:
            del self._hits[key]

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
            self._calls = 0


def parse_token_response(payload: Any) -> Tuple[str, int]:
    """Extract (access_token, expires_in) from a Deepgram grant response."""
    if not isinstance(payload, dict):
        raise GrantError("Deepgram returned an unexpected response", 502)
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise GrantError("Deepgram did not return a session token", 502)
    try:
        expires_in = int(payload.get("expires_in", 0))
    except (TypeError, ValueError, OverflowError):
        expires_in = 0
    if expires_in <= 0:
        raise GrantError("Deepgram returned an invalid token expiry", 502)
    return token, expires_in


#: Injection point for tests: takes (url, api_key, ttl, timeout) and returns
#: the decoded JSON body of the grant response.
GrantTransport = Callable[[str, str, int, float], Any]


#: 2xx codes that may not carry a body, so they may not carry *this* body.
#: Refusing them costs nothing and forecloses "the status said 2xx, so the
#: payload we were handed must have come from Deepgram".
GRANT_BODYLESS_STATUSES = frozenset({204, 205, 206})


def response_is_a_grant(status_code: int) -> bool:
    """True only for a 2xx that can carry the grant body.

    Anything else is refused -- the whole 3xx family above all.
    `follow_redirects` is off, so no credential is ever re-sent to a redirect
    target, but the *body* of that answer would still be parsed as a token if
    the status were not checked: a captive portal, an Apache `Redirect` rule or
    a TLS-intercepting proxy that replies with JSON would otherwise mint a
    session nobody obtained from Deepgram. A `No Content`/`Partial Content`
    answer is refused for the same reason in reverse: a body there did not
    come from the token endpoint's successful path.
    """
    return 200 <= status_code < 300 and status_code not in GRANT_BODYLESS_STATUSES


def _grant_response_error(status_code: int, headers: Any) -> GrantError:
    """The `GrantError` for an upstream answer `response_is_a_grant` refused.

    The status is mapped for the client and any `Retry-After` is carried
    through, so Deepgram's own backoff hint survives the proxy hop instead of
    being replaced by the client guessing.
    """
    return GrantError(
        f"Deepgram refused the token request (HTTP {status_code})",
        _grant_error_status(status_code),
        retry_after=_retry_after(headers.get("Retry-After") if headers is not None else None),
    )


def _httpx_grant(url: str, api_key: str, ttl_seconds: int, timeout: float) -> Any:
    """Default transport: a plain HTTPS POST to Deepgram.

    Every network failure is converted into a `GrantError` carrying an
    application-level status, so a timeout or an unreachable upstream can
    never escape the route as an uncontrolled 500. Error messages contain
    no credential and no upstream response body.

    Kept in step with `AsyncGrantClient.grant` (2xx-only, `Retry-After`,
    every failure mapped) because tests, `scripts/measure_capacity.py` and
    the benchmarks drive the synchronous path: a difference between the two
    means the harness validates behaviour production never runs.
    """
    import httpx

    try:
        try:
            response = httpx.post(
                url,
                headers={"Authorization": f"Token {api_key}", "Content-Type": "application/json"},
                json={"ttl_seconds": ttl_seconds},
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise GrantError("Deepgram token request timed out", 504) from exc
        except httpx.RequestError as exc:
            # Connection refused/reset, DNS failure, TLS failure, ...
            raise GrantError("cannot reach Deepgram", 503) from exc

        if not response_is_a_grant(response.status_code):
            raise _grant_response_error(response.status_code, response.headers)
        try:
            return response.json()
        except ValueError as exc:
            raise GrantError("Deepgram returned a non-JSON response", 502) from exc
    except GrantError:
        raise
    except Exception as exc:  # noqa: BLE001 - see AsyncGrantClient.grant
        raise GrantError("Deepgram token request failed", 502) from exc


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
        settings.grant_url, settings.deepgram_api_key, ttl_seconds, settings.grant_timeout_seconds
    )
    token, expires_in = parse_token_response(payload)
    log.info("issued a Deepgram session token (ttl_requested=%ds)", ttl_seconds)
    return token, expires_in


# ---------------------------------------------------------------------------
# Per-client identity
# ---------------------------------------------------------------------------


def hash_client_secret(secret: str) -> str:
    """Return the SHA-256 hex digest stored on the host for a secret.

    The host never stores the secret itself, so a leaked registry file or a
    memory dump yields hashes, not usable credentials.
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def is_valid_client_id(client_id: Any) -> bool:
    """True for a syntactically acceptable client id.

    Bounded on purpose: the id becomes a rate-limit key and appears in log
    lines, so it must be short and free of control characters. The class is
    pinned to ASCII (see `CLIENT_ID_PATTERN`) rather than `str.isalnum()`,
    which would admit Unicode homoglyphs of an existing id.
    """
    if not isinstance(client_id, str):
        return False
    if _CLIENT_ID_RE.fullmatch(client_id) is None:
        return False
    # `.` and `..` pass the character class but identify nothing; they would
    # also become odd file names in `host/provision.py --secrets-dir`.
    return any(ch.isdigit() or ch.isalpha() for ch in client_id)


def normalize_client_id(client_id: Any) -> str:
    """Return a stripped, validated client id, or `""` when unusable."""
    if client_id is None:
        return ""
    candidate = client_id.strip() if isinstance(client_id, str) else ""
    return candidate if is_valid_client_id(candidate) else ""


@dataclass(frozen=True)
class ClientIdentity:
    """An authenticated client device."""

    client_id: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.client_id


class ClientRegistry:
    """Maps `client_id` -> SHA-256 hash of that client's secret.

    Provisioning format, one `client_id:sha256hex` entry per line in
    `HOST_CLIENTS_FILE` (the recommended way to manage 50+ devices):

        doctor-01:8f2c...deadbeef
        doctor-02:1a90...c0ffee

    Only hashes are ever held here. Comparison is constant time, and a miss
    is indistinguishable from a wrong secret so the endpoint cannot be used
    to enumerate valid client ids.
    """

    def __init__(self, clients: Optional[Mapping[str, str]] = None) -> None:
        self._clients: Dict[str, str] = dict(clients or {})

    @classmethod
    def from_file(cls, path: str) -> "ClientRegistry":
        """Load a registry file, ignoring blank lines and `#` comments.

        Two conditions are refused rather than tolerated, because both leave a
        running service that no device can use:

        * **zero clients** -- an empty, whitespace-only or comment-only file
          (a truncated `rsync`, a half-finished first provisioning) starts a
          host that answers `401` to every clinician, forever, while looking
          perfectly healthy;
        * a **malformed line** -- one bad entry would otherwise silently
          provision a device that can never authenticate.

        A *duplicated* `client_id` is accepted with a warning: `host.provision`
        appends, so re-provisioning one device must not require rewriting the
        file (and refusing would take the whole clinic down to fix one line).
        The last entry wins; the warning names the ids so the operator can
        delete the dead lines.
        """
        clients: Dict[str, str] = {}
        duplicated: List[str] = []
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            # A missing or unreadable registry must be diagnosable from the
            # log alone: a bare traceback would name a line of core.py, not
            # the file the operator misconfigured.
            raise ConfigurationError(
                f"cannot read the client registry {path!r}: {exc.strerror or exc}. "
                "Check HOST_CLIENTS_FILE and restart."
            ) from exc
        for lineno, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            client_id, separator, digest = line.partition(":")
            client_id, digest = client_id.strip(), digest.strip().lower()
            if not separator or not is_valid_client_id(client_id) or not _is_sha256_hex(digest):
                # A malformed line is a configuration error: refuse it
                # rather than silently provisioning a device that can never
                # authenticate.
                raise ConfigurationError(
                    f"{path} line {lineno}: expected 'client_id:sha256hex' "
                    f"(generate one with: python -m host.provision)"
                )
            if client_id in clients:
                duplicated.append(client_id)
            clients[client_id] = digest
        if not clients:
            raise ConfigurationError(
                f"{path} contains no client entries. A registry file that "
                "authenticates nobody is never what an operator wants: every "
                "device would be rejected with 401 (and then locked out by the "
                "brute-force budget) while the service looks healthy. Restore "
                "the file, or unset HOST_CLIENTS_FILE to run in legacy "
                "single-secret mode. Mint entries with: python -m host.provision"
            )
        if duplicated:
            shown = ", ".join(duplicated[:5]) + (", ..." if len(duplicated) > 5 else "")
            log.warning(
                "%s: %d duplicated client_id line(s) (%s); the last entry wins, "
                "so the earlier secret(s) no longer authenticate that device. "
                "Delete the superseded line(s).",
                path, len(duplicated), shown,
            )
        return cls(clients)

    def __len__(self) -> int:
        return len(self._clients)

    @property
    def client_ids(self) -> Tuple[str, ...]:
        """Registered ids. Safe to log: ids are not secrets."""
        return tuple(sorted(self._clients))

    def verify(self, client_id: str, secret: str) -> Optional[ClientIdentity]:
        """Return the identity for a valid (client_id, secret) pair, else None.

        An unknown client id still performs a hash comparison against a
        dummy so that response timing does not reveal whether the id exists.
        """
        expected = self._clients.get(client_id)
        presented = hash_client_secret(secret)
        if expected is None:
            # Constant-ish work for an unknown id.
            hmac.compare_digest(presented, _UNKNOWN_CLIENT_DIGEST)
            return None
        if not hmac.compare_digest(presented, expected):
            return None
        return ClientIdentity(client_id=client_id)


#: A fixed digest compared against when the client id is unknown, so a miss
#: costs the same as a wrong secret.
_UNKNOWN_CLIENT_DIGEST = hash_client_secret("unknown-client-placeholder")


def _is_sha256_hex(value: str) -> bool:
    return len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def auth_failure_key(peer_host: Optional[str], client_id: Optional[str]) -> Optional[str]:
    """Brute-force budget key for one request, or None when unattributable.

    The failed-authentication limiter only slows down someone guessing
    secrets if it can tell the guesser apart from the clinicians it is
    protecting. A budget shared by everybody is not a security control, it
    is a denial of service: 20 bad requests from anywhere would lock every
    provisioned device out for a whole window.

    Attribution order:

    1. the immediate peer address, when the server supplied one;
    2. otherwise the presented (validated) client id. A WSGI/Passenger
       deployment can omit the peer address entirely, because `a2wsgi`
       populates `scope["client"]` only when *both* `REMOTE_ADDR` and
       `REMOTE_PORT` are present in the environ;
    3. otherwise nothing. The failure is still counted and logged, but no
       lockout is applied, because there is no key that would not equally
       apply to every legitimate legacy client.

    Authentication itself is enforced identically in all three cases: this
    function decides only who an *abuse* budget is charged to.
    """
    if peer_host:
        return f"auth:{peer_host}"
    normalized = normalize_client_id(client_id)
    if normalized:
        return f"auth-id:{normalized}"
    return None


def authenticate_client(
    authorization_header: Optional[str],
    client_id: Optional[str],
    registry: ClientRegistry,
) -> Optional[ClientIdentity]:
    """Authenticate `Authorization: Bearer <secret>` together with a client id.

    Both parts identify *which* device is asking, which is what makes
    per-client rate limiting and per-device revocation possible.

    When the registry holds only the legacy single-secret identity, an
    omitted client id resolves to `LEGACY_CLIENT_ID` so an un-migrated
    deployment keeps working. Once real clients are provisioned, the header
    becomes mandatory -- an id that is absent then resolves to `legacy`,
    which no longer matches anything, and the request is rejected.
    """
    if not authorization_header:
        return None
    scheme, _, presented = authorization_header.partition(" ")
    if scheme.strip().lower() != "bearer":
        return None
    secret = presented.strip()
    if not secret:
        return None

    resolved_id = normalize_client_id(client_id) or LEGACY_CLIENT_ID
    return registry.verify(resolved_id, secret)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class TokenBucketRateLimiter:
    """Burst-tolerant token bucket with bounded, self-expiring state.

    A fixed window is the wrong shape for session startup: 50 clinicians
    pressing Start at the same moment is *legitimate* traffic that must
    succeed, whereas a client looping on one request is not. A token bucket
    separates the two -- `burst` tokens are available immediately (so a
    50-client morning surge passes) and `refill_per_second` limits the
    sustained rate.

    Memory is bounded twice over: buckets idle for a full refill period are
    dropped, and the table is hard-capped with least-recently-used eviction.
    """

    def __init__(
        self,
        capacity: int,
        refill_per_second: float,
        clock: Callable[[], float] = time.monotonic,
        max_tracked_keys: int = MAX_TRACKED_CLIENTS,
        purge_interval_calls: int = PURGE_INTERVAL_CALLS,
    ) -> None:
        self._capacity = max(1, capacity)
        self._refill_per_second = max(0.001, float(refill_per_second))
        self._clock = clock
        self._max_tracked_keys = max(1, max_tracked_keys)
        self._purge_interval_calls = max(1, purge_interval_calls)
        # key -> [tokens, last_refill_monotonic]
        self._buckets: OrderedDict[str, list] = OrderedDict()
        self._lock = threading.Lock()
        self._calls = 0

    @property
    def tracked_keys(self) -> int:
        """Number of buckets currently held (test/observability hook)."""
        with self._lock:
            return len(self._buckets)

    def allow(self, key: str, cost: float = 1.0) -> Tuple[bool, int]:
        """Return (allowed, retry_after_seconds).

        A rejected request does **not** consume a token, so a client that
        waits for `retry_after` and retries will be served.
        """
        now = self._clock()
        with self._lock:
            self._calls += 1
            if self._calls % self._purge_interval_calls == 0:
                self._purge(now)

            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = [float(self._capacity), now]
                self._buckets[key] = bucket
            else:
                self._buckets.move_to_end(key)
                elapsed = max(0.0, now - bucket[1])
                bucket[0] = min(
                    float(self._capacity), bucket[0] + elapsed * self._refill_per_second
                )
                bucket[1] = now

            if bucket[0] >= cost:
                bucket[0] -= cost
                if len(self._buckets) > self._max_tracked_keys:
                    self._evict_locked()
                return True, 0

            missing = cost - bucket[0]
            retry_after = max(1, int(missing / self._refill_per_second) + 1)
            if len(self._buckets) > self._max_tracked_keys:
                self._evict_locked()
            return False, retry_after

    def _purge(self, now: float) -> None:
        """Drop buckets that have refilled to capacity (i.e. idle).

        Called with the lock held.
        """
        full_refill = self._capacity / self._refill_per_second
        stale = [
            key
            for key, bucket in self._buckets.items()
            if now - bucket[1] >= full_refill
        ]
        for key in stale:
            del self._buckets[key]

    def _evict_locked(self) -> None:
        """Enforce the hard cap by dropping the least recently used buckets."""
        while len(self._buckets) > self._max_tracked_keys:
            self._buckets.popitem(last=False)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()
            self._calls = 0


# ---------------------------------------------------------------------------
# Async Deepgram grant (the Phase 3 fix)
# ---------------------------------------------------------------------------


class AsyncGrantClient:
    """Reused `httpx.AsyncClient` pool for Deepgram token requests.

    The pre-hardening route called blocking `httpx.post()` from inside an
    `async def` handler, so every token request froze the whole event loop
    for the duration of the Deepgram round trip and 50 simultaneous clients
    serialized behind each other.

    One client -- created once at application startup and closed at
    shutdown -- keeps a warm connection pool instead of paying a TCP+TLS
    handshake per request, and the explicit per-phase timeouts guarantee a
    slow or hostile upstream cannot pin the loop open indefinitely.
    """

    def __init__(
        self,
        connect_timeout: float = 5.0,
        read_timeout: float = 5.0,
        write_timeout: float = 5.0,
        pool_timeout: float = 5.0,
        max_connections: int = 100,
        max_keepalive_connections: int = 20,
        client: Any = None,
        grant_url: str = DEEPGRAM_GRANT_URL,
    ) -> None:
        import httpx

        self._grant_url = grant_url
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=connect_timeout,
                read=read_timeout,
                write=write_timeout,
                pool=pool_timeout,
            ),
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=max_keepalive_connections,
            ),
            # Explicit, not left to the library default: this request carries
            # the Deepgram API key in an Authorization header, so a redirect
            # must never be followed to a host nobody configured. A token
            # endpoint has no legitimate reason to redirect.
            follow_redirects=False,
        )
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def grant(self, api_key: str, ttl_seconds: int) -> Any:
        """POST the grant request and return the decoded JSON body.

        Every failure becomes a `GrantError` with an application-level
        status, so a network problem can never surface as an uncontrolled
        500. No credential or upstream body enters the message.

        "Every failure" is the whole point of the outer handler: httpx raises
        things that are neither `TimeoutException` nor `RequestError` -- a
        `TypeError`/`UnicodeEncodeError` while encoding the `Authorization`
        header, a `RuntimeError` once the pool has been closed, an
        `InvalidURL` -- and each of those used to escape as a bare 500 that
        the host's own counters never recorded, leaving an operator with a
        fully down service that reported no failures at all.
        """
        import httpx

        try:
            try:
                response = await self._client.post(
                    self._grant_url,
                    headers={
                        "Authorization": f"Token {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"ttl_seconds": ttl_seconds},
                )
            except httpx.TimeoutException as exc:
                raise GrantError("Deepgram token request timed out", 504) from exc
            except httpx.RequestError as exc:
                raise GrantError("cannot reach Deepgram", 503) from exc

            if not response_is_a_grant(response.status_code):
                raise _grant_response_error(response.status_code, response.headers)
            try:
                return response.json()
            except ValueError as exc:
                raise GrantError("Deepgram returned a non-JSON response", 502) from exc
        except GrantError:
            raise
        except Exception as exc:  # noqa: BLE001 - the 500 must not escape
            log.error("deepgram grant failed unexpectedly (%s)", type(exc).__name__)
            raise GrantError("Deepgram token request failed", 502) from exc

    async def aclose(self) -> None:
        """Close the pool. Idempotent, so shutdown paths may repeat it."""
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            await self._client.aclose()


def _grant_error_status(status_code: int) -> int:
    """Map a Deepgram grant status onto the status the client should see.

    401/403/400/404 here mean the *host's own* Deepgram key is wrong or
    lacks Member permission -- an operator problem, not the client's -- so
    they surface as 502 rather than 401. 429 and 5xx are upstream
    availability problems, so they are 503.
    """
    return 502 if status_code in (400, 401, 403, 404) else 503


async def grant_session_async(
    settings: HostSettings,
    ttl_seconds: int,
    client: AsyncGrantClient,
) -> Tuple[str, int]:
    """`grant_session` without blocking the event loop."""
    payload = await client.grant(settings.deepgram_api_key, ttl_seconds)
    token, expires_in = parse_token_response(payload)
    return token, expires_in


def new_session_id() -> str:
    """A random, unguessable id used only for logs and metrics."""
    import uuid

    return uuid.UUID(bytes=os.urandom(SESSION_ID_BYTES), version=4).hex


def _retry_after(value: Optional[str]) -> Optional[int]:
    try:
        seconds = float(value) if value else 0.0
        return min(60, max(1, math.ceil(seconds))) if math.isfinite(seconds) and seconds > 0 else None
    except (ValueError, OverflowError):
        return None
