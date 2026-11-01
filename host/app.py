"""Medical STT host service -- the only place the Deepgram API key exists.

Endpoints
---------
`GET  /healthz`   liveness probe; reveals nothing.
`GET  /readyz`    readiness probe; reports whether the Deepgram pool is up.
`GET  /metrics`   operational counters (admin-protected).
`POST /v1/session` exchanges the caller's client credentials for a
                  short-lived Deepgram session token.

The desktop client then opens its WebSocket straight to Deepgram with
`Authorization: Bearer <short-lived token>`, exactly as documented in
Deepgram's "Token-Based Auth" guide. That keeps the audio path low
latency while the long-lived API key never leaves this host.

Where the pieces live
---------------------
============================  ===============================================
`core`                        facade over the host's business logic
`config`                      settings, environment parsing, Deepgram setup
`identity`, `ratelimit`       who you are, and how often you may ask
`limits`                      admission control: per-client, global, in-flight
`grant`                       the Deepgram token exchange (sync and async)
`request_guard`               HTTPS decisions and TTL clamping
`hardening`                   security headers, plaintext refusal, probes
`session_service`             the `/v1/session` use case
============================  ===============================================

This module is only the composition root: it builds those objects, hands them
the app's shared state, and starts uvicorn. Kept small on purpose -- everything
here is wiring, and everything that is not wiring has a module of its own.

Concurrency model
-----------------
The host issues *tokens only*; it never carries audio. Scaling to 50
concurrent clinicians therefore needs three things, all wired below:

1. **No blocking I/O on the event loop.** The Deepgram grant goes through
   a single reused `httpx.AsyncClient` (`core.AsyncGrantClient`) with
   explicit connect/read/write/pool timeouts, created at startup and closed
   on shutdown. The previous synchronous `httpx.post()` inside `async def`
   stalled the entire loop for the full Deepgram round trip.
2. **Per-client identity, not per-IP rate limiting.** 50 doctors on one
   hospital NAT share an address, so limits are keyed on the authenticated
   `client_id`, with a separate global ceiling. The per-IP limiter survives
   only for *failed* authentication, where a shared address is irrelevant.
3. **A bounded, observable request path.** Bodies are size-capped, rate
   limit state is bounded, and every request carries a `session_id`.

Run it behind TLS (see host/README.md and the root README > Deploying the
host). Plain HTTP is refused unless `HOST_ALLOW_HTTP=1`, which exists only
for local development.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

try:  # allows `python host/app.py` from a checkout
    from .config import ConfigurationError, HostSettings
    from .core import AsyncGrantClient, grant_session_async
    from .hardening import SECURITY_HEADERS, install_hardening, install_ops_routes
    from .hardening import token_matches as _token_matches
    from .limits import Limiters
    from .metrics import new_metrics
    from .session_service import SessionService
except ImportError:  # pragma: no cover - direct script execution
    from config import ConfigurationError, HostSettings  # type: ignore[no-index]
    from core import AsyncGrantClient, grant_session_async  # type: ignore[no-index]
    from hardening import (  # type: ignore[no-index]
        SECURITY_HEADERS,
        install_hardening,
        install_ops_routes,
    )
    from hardening import token_matches as _token_matches  # type: ignore[no-index]
    from limits import Limiters  # type: ignore[no-index]
    from metrics import new_metrics  # type: ignore[no-index]
    from session_service import SessionService  # type: ignore[no-index]

log = logging.getLogger("medical_stt.host")

#: `SECURITY_HEADERS` and `_token_matches` are re-exported for the tests that
#: assert the hardening headers and the constant-time comparison directly; they
#: are owned by `hardening`.
__all__ = [
    "CLIENT_ID_HEADER",
    "SECURITY_HEADERS",
    "create_app",
    "main",
    "_token_matches",
]

#: Header carrying the provisioned client id. Not a credential on its own:
#: the secret in `Authorization` is what authenticates.
CLIENT_ID_HEADER = "X-Client-Id"


async def _grant(settings: HostSettings, ttl: int, client: Any) -> Any:
    """Resolve the module-level grant function at call time.

    Deliberately indirect: `grant_session_async` is the single seam tests and
    operators replace, and a direct reference captured by the closure would
    bind it at import time.
    """
    return await grant_session_async(settings, ttl, client)


def _publish_capacity(
    metrics: Any, settings: HostSettings, registry: Any, limiters: Limiters
) -> None:
    """Publish capacity facts as soon as the app exists.

    Not in `lifespan`: a WSGI deployment (host/passenger_wsgi.py, via a2wsgi)
    never runs the ASGI lifespan protocol, so anything set only there would
    read as a permanent zero on cPanel.
    """
    metrics.set_gauge("inflight_grants_capacity", limiters.inflight.capacity)
    metrics.set_gauge("registry_clients", len(registry))
    metrics.set_gauge("inflight_grants", limiters.inflight.in_flight)


def _log_deployment_warnings(settings: HostSettings) -> None:
    """Say the two things an operator must know but cannot infer.

    Both are supported configurations, so neither is fatal -- but each turns
    into a bug report ("the service is at capacity", "metrics are public")
    if nobody ever says it out loud at startup.
    """
    if not settings.metrics_admin_token:
        log.warning(
            "HOST_METRICS_ADMIN_TOKEN is not set: /metrics is readable by "
            "anyone who can reach this port. It exposes no credential and no "
            "transcript, but it does expose traffic volume and the number of "
            "registered clients."
        )

    if settings.clients_file:
        return
    # Single-identity mode is a supported single-clinician configuration, and
    # the docs say it is not a multi-clinician one -- but nothing in the
    # runtime ever said so. With one identity, every device on the site shares
    # a *single* token bucket, so 50 clinicians pressing Start at once get 10
    # tokens and 40 429s. That reads to the clinicians as "the service is at
    # capacity" and to nobody as a configuration mistake, which is exactly the
    # trap this warning refuses to leave silent.
    log.warning(
        "running in single-identity (legacy) mode: HOST_CLIENTS_FILE is not "
        "set, so every device is rate limited as one shared identity -- at "
        "most %d burst and %d sustained requests per %ds across the whole "
        "site. One clinician is fine; a clinic is not. Provision per-device "
        "credentials (python -m host.provision --count N --out "
        "/secure/host/clients.txt) and set HOST_CLIENTS_FILE. Revoking one "
        "device also revokes everything else while in this mode. Devices "
        "that send X-Client-Id cannot authenticate at all here -- the id "
        "resolves to a registry entry that does not exist -- so a clinic "
        "that provisions ids on the clients before setting "
        "HOST_CLIENTS_FILE on the host sees only 401s; the 401 detail "
        "names this trap.",
        settings.client_burst,
        settings.client_rate_limit_requests,
        settings.rate_limit_window_seconds,
    )


def _build_lifespan(settings: HostSettings, registry: Any) -> Any:
    """Startup/shutdown hooks: log the shape, then release the pool."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info(
            "host ready: clients=%d client_burst=%d global_burst=%d "
            "max_inflight=%d deepgram_region=%s",
            len(registry),
            settings.client_burst,
            settings.global_burst,
            settings.max_inflight_grants,
            settings.deepgram_region,
        )
        try:
            yield
        finally:
            # Graceful shutdown: stop accepting work, then release the pool.
            await app.state.grant_client.aclose()
            log.info("host shut down cleanly (closed the Deepgram connection pool)")

    return lifespan


def create_app(settings: Optional[HostSettings] = None) -> FastAPI:
    """Build the ASGI app. Settings come from the environment by default."""
    resolved = settings or HostSettings.from_env()
    metrics = new_metrics()
    registry = resolved.build_client_registry()
    limiters = Limiters.from_settings(resolved)

    _publish_capacity(metrics, resolved, registry, limiters)
    _log_deployment_warnings(resolved)

    app = FastAPI(
        title="Medical STT host",
        version="2.0.0",
        docs_url=None,  # no interactive docs on a credentialed service
        redoc_url=None,
        # Without this, /openapi.json stays public and enumerates the whole
        # API surface even though both doc UIs are disabled.
        openapi_url=None,
        lifespan=_build_lifespan(resolved, registry),
    )
    # Built here rather than in `lifespan` so the app is usable even when a
    # caller drives the ASGI app without running startup/shutdown (the test
    # client does this); production always runs `lifespan`, which closes it.
    app.state.grant_client = AsyncGrantClient(
        connect_timeout=resolved.grant_connect_timeout_seconds,
        read_timeout=resolved.grant_timeout_seconds,
        write_timeout=resolved.grant_timeout_seconds,
        pool_timeout=resolved.grant_timeout_seconds,
        max_connections=resolved.grant_max_connections,
        max_keepalive_connections=resolved.grant_max_keepalive_connections,
        grant_url=resolved.grant_url,
    )
    app.state.settings = resolved
    app.state.metrics = metrics
    app.state.registry = registry
    # Exposed for the concurrency/soak tests to assert that limiter state
    # stays bounded, and for operational introspection.
    app.state.client_limiter = limiters.client
    app.state.global_limiter = limiters.global_
    app.state.auth_failure_limiter = limiters.auth_failure
    app.state.inflight = limiters.inflight
    app.state.active_requests = limiters.active

    install_hardening(app, resolved, metrics, limiters.active)
    install_ops_routes(app, resolved, metrics)

    sessions = SessionService(resolved, registry, metrics, limiters, _grant)

    @app.post("/v1/session")
    async def create_session(
        request: Request,
        authorization: Optional[str] = Header(default=None),
        x_client_id: Optional[str] = Header(default=None, alias=CLIENT_ID_HEADER),
    ) -> JSONResponse:
        """Exchange device credentials for a short-lived Deepgram token."""
        return await sessions.create_session(request)

    return app


# Fail fast at import if the Deepgram key is absent: this service has no
# reason to run without it.
try:
    app = create_app()
except ConfigurationError as _exc:  # pragma: no cover - misconfiguration path
    raise SystemExit(f"host configuration error: {_exc}") from _exc


def _parse_bind(raw: str) -> bool:
    """True if `HOST_BIND` is an IP address or a plausible hostname."""
    host = raw.strip()
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return len(host) <= 253 and all(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in host.rstrip(".").split(".")
        )


def _parse_port(raw: str) -> Optional[int]:
    """Parse `HOST_PORT`, logging (not raising) on an unusable value.

    A bare `int()` traceback reads like a crash in the service rather than a
    typo in one environment variable, and it is the first thing an operator
    hits when a panel rewrites `HOST_PORT` to `""` or `"8443 "`.
    """
    try:
        port = int(raw)
    except ValueError:
        log.error("HOST_PORT must be an integer; got %r", raw)
        return None
    if not 1 <= port <= 65535:
        log.error("HOST_PORT must be between 1 and 65535; got %d", port)
        return None
    return port


def _warn_about_tls(settings: HostSettings, certfile: str) -> None:
    """Explain what plaintext serving means, once, before uvicorn starts."""
    if certfile:
        return
    if settings.allow_http:
        log.warning("HOST_ALLOW_HTTP is set: serving plaintext (development only)")
        return
    # Not fatal, and not a misconfiguration in the common case: a reverse proxy
    # that terminates TLS and forwards X-Forwarded-Proto from an address in
    # HOST_FORWARDED_ALLOW_IPS is exactly how this is meant to be deployed
    # behind Apache or a load balancer. But when there is no such proxy the app
    # rejects every request with 400 and the container healthcheck fails
    # forever, which looks like a hung service rather than a missing
    # certificate -- so say which it is, once, at startup.
    log.warning(
        "no TLS certificate (HOST_TLS_CERTFILE) and HOST_ALLOW_HTTP is not set: "
        "this process serves plaintext while the app requires HTTPS, so every "
        "request will be rejected with 400 unless a trusted reverse proxy "
        "terminates TLS and sends X-Forwarded-Proto: https from an address in "
        "HOST_FORWARDED_ALLOW_IPS (currently %s). Without such a proxy, set "
        "HOST_TLS_CERTFILE/HOST_TLS_KEYFILE.",
        ",".join(settings.forwarded_allow_ips) or "<none>",
    )


def main() -> int:  # pragma: no cover - operational entry point
    """Run the service. TLS is provided by uvicorn's cert files."""
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    # Import-time create_app already validated settings and emitted warnings.
    settings = app.state.settings
    host = os.getenv("HOST_BIND", "0.0.0.0").strip()
    if not _parse_bind(host):
        log.error("HOST_BIND must be a non-empty IP address or hostname")
        return 2
    port = _parse_port(os.getenv("HOST_PORT", "8443").strip())
    if port is None:
        return 2
    certfile = os.getenv("HOST_TLS_CERTFILE", "")
    keyfile = os.getenv("HOST_TLS_KEYFILE", "")
    _warn_about_tls(settings, certfile)

    # One worker is the supported topology: the in-memory client registry and
    # limiters are per process. Scale with a reverse proxy and a shared
    # registry rather than with threads -- see host/README.md.
    uvicorn.run(
        app,
        host=host,
        port=port,
        ssl_certfile=certfile or None,
        ssl_keyfile=keyfile or None,
        log_level="info",
        # Proxied deployments need the original scheme to pass the HTTPS
        # check. Uvicorn applies the same trusted-peer rule as the app, so
        # both use one configured list.
        proxy_headers=True,
        forwarded_allow_ips=",".join(settings.forwarded_allow_ips),
        timeout_graceful_shutdown=15,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
