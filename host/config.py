"""Host configuration, read from the environment at startup.

The host is the only process that holds the Deepgram API key, so its
configuration is validated **before the first request**: a key that the HTTP
transport cannot even encode, an unknown region, or a missing shared secret is
a startup failure, not a 500 on a clinic's first Start press.

Every clamping decision logs the *name* of the setting and never its value:
environment values are routinely mistyped credentials.
"""
from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Tuple

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from .identity import ClientRegistry

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
                # Env values may be mistyped secrets. Name the setting, not
                # the value it carried, in an operator-visible log.
                log.warning("ignoring non-numeric %s; check the environment value", name)
                return fallback

        def _int(name: str, fallback: int) -> int:
            return _number(name, fallback, int)

        def _float(name: str, fallback: float) -> float:
            return _number(name, fallback, float)

        requested_max_ttl = _int("HOST_MAX_TTL_SECONDS", MAX_TTL_SECONDS)
        max_ttl = max(5, min(requested_max_ttl, MAX_TTL_SECONDS))
        if max_ttl != requested_max_ttl:
            log.warning("clamping HOST_MAX_TTL_SECONDS to supported bounds")
        requested_body_bytes = _int("HOST_MAX_REQUEST_BODY_BYTES", MAX_REQUEST_BODY_BYTES)
        body_bytes = max(64, requested_body_bytes)
        if body_bytes != requested_body_bytes:
            log.warning("clamping HOST_MAX_REQUEST_BODY_BYTES to supported bounds")
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
            max_request_body_bytes=body_bytes,
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

        With no `HOST_CLIENTS_FILE` the service still runs, authenticated by the
        legacy shared secret alone. That path is a single identity mapped to
        `LEGACY_CLIENT_ID`, so an existing deployment keeps working while it is
        migrated to per-device credentials -- and it is exactly why the
        50-clinician deployment must provision first.

        Imported lazily: `identity` depends on this module for the id grammar,
        so a module-level import here would be a cycle.
        """
        try:  # package import from a full checkout
            from .identity import ClientRegistry, hash_client_secret
        except ImportError:  # pragma: no cover - cPanel copies only the host/ dir
            from identity import ClientRegistry, hash_client_secret  # type: ignore[no-index]

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
