"""HTTP request security decisions: TLS, trusted proxies, TTL clamping.

`X-Forwarded-Proto` is only trusted from an explicitly configured proxy, and
only its **last** hop, because that is the observation made by the peer we
actually talk to. Everything here is a pure function of the request metadata
and the settings, so the rules can be tested without a server.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

try:  # package import from a full checkout
    from .config import HostSettings
except ImportError:  # pragma: no cover - cPanel copies only the host/ dir
    from config import HostSettings  # type: ignore[no-index]

log = logging.getLogger("medical_stt.host")

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
