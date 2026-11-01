"""Transport-level hardening and the operational routes of the host service.

Two concerns live here, both about *the HTTP envelope* rather than about
session issuance:

* **`install_hardening`** refuses plaintext requests and stamps every response
  -- including the 500s Starlette builds itself -- with conservative security
  headers.
* **`install_ops_routes`** adds the three unauthenticated-but-harmless probes
  (`/healthz`, `/readyz`) and the admin-protected `/metrics` scrape endpoint.

Keeping them out of `create_app` leaves that function as what it should be: a
composition root that wires objects together and contains no request logic.
"""
from __future__ import annotations

import hmac
import logging
import time
from typing import Any, Optional

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, PlainTextResponse

try:  # package import from a full checkout
    from .config import HostSettings
    from .limits import InflightLimiter
    from .request_guard import request_is_secure
except ImportError:  # pragma: no cover - cPanel copies only the host/ dir
    from config import HostSettings  # type: ignore[no-index]
    from limits import InflightLimiter  # type: ignore[no-index]
    from request_guard import request_is_secure  # type: ignore[no-index]

log = logging.getLogger("medical_stt.host")

#: Security headers added to every response. Conservative and safe for a JSON
#: API with no HTML surface: `nosniff` and a deny-all frame policy.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}

#: The one path admitted by the request gate; kept here so the middleware and
#: the route registration cannot drift apart.
SESSION_PATH = "/v1/session"


def token_matches(authorization: Optional[str], expected: str) -> bool:
    """Constant-time admin-token comparison for `/metrics`.

    `hmac.compare_digest` raises `TypeError` on non-ASCII input, so both sides
    are compared as UTF-8 bytes: a caller who sends a non-ASCII bearer token
    gets 401, never a 500.
    """
    if not authorization:
        return False
    scheme, _, presented = authorization.partition(" ")
    if scheme.strip().lower() != "bearer":
        return False
    return hmac.compare_digest(
        presented.strip().encode("utf-8"), expected.encode("utf-8")
    )


def install_hardening(
    app: FastAPI,
    settings: HostSettings,
    metrics: Any,
    active: InflightLimiter,
) -> None:
    """Register the request gate and the header-complete 500 handler."""

    @app.middleware("http")
    async def harden(request: Request, call_next: Any) -> Any:
        """Refuse plaintext requests and add security headers.

        `X-Forwarded-Proto` is honoured only when the immediate peer is a
        configured reverse proxy (`HOST_FORWARDED_ALLOW_IPS`, loopback by
        default), so a direct HTTP client cannot forge the header to make a
        plaintext request look like TLS.
        """
        is_session = request.url.path == SESSION_PATH and request.method == "POST"
        admitted = False
        started = time.monotonic()
        #: Conventional numeric status for "client closed request"; never sent.
        outcome = "499"
        if is_session:
            metrics.increment("session_http_requests_total")
        try:
            peer_host = request.client.host if request.client else None
            secure = request_is_secure(
                request.url.scheme,
                peer_host,
                request.headers.get("x-forwarded-proto"),
                settings,
            )
            if not secure and not settings.allow_http:
                response: Any = JSONResponse(
                    {"detail": "HTTPS is required"}, status_code=400
                )
            elif is_session and not active.acquire():
                response = JSONResponse(
                    {"detail": "host request capacity reached"},
                    status_code=503,
                    headers={"Retry-After": "1"},
                )
            else:
                admitted = is_session
                metrics.set_gauge("active_session_requests", active.in_flight)
                response = await call_next(request)
            outcome = str(response.status_code)
            for header, value in SECURITY_HEADERS.items():
                response.headers.setdefault(header, value)
            return response
        except Exception:
            outcome = "500"
            raise
        finally:
            if admitted:
                active.release()
            if is_session:
                metrics.set_gauge("active_session_requests", active.in_flight)
                metrics.increment("session_http_outcomes_total", status=outcome)
                metrics.observe("session_http_latency", time.monotonic() - started)

    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        """Unhandled 500s carry the security headers too.

        Starlette's ServerErrorMiddleware is the outermost layer and builds the
        500 response itself, so the `harden` middleware's re-raise path skipped
        its own header loop for exactly the responses an incident produces. No
        exception detail crosses the wire.
        """
        log.error("unhandled host error (%s)", type(exc).__name__)
        return JSONResponse(
            {"detail": "internal error"},
            status_code=500,
            headers=dict(SECURITY_HEADERS),
        )


def install_ops_routes(app: FastAPI, settings: HostSettings, metrics: Any) -> None:
    """Register the probes and the metrics scrape endpoint."""

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request) -> Any:
        """Readiness: is the Deepgram pool initialized?

        Deliberately not authenticated -- a load balancer must be able to
        probe it -- so it reports only a boolean.
        """
        grant_client = getattr(request.app.state, "grant_client", None)
        ready = grant_client is not None and not grant_client.closed
        return JSONResponse(
            {"status": "ready" if ready else "starting"},
            status_code=200 if ready else 503,
        )

    @app.get("/metrics")
    async def prometheus_metrics(
        request: Request, authorization: Optional[str] = Header(default=None)
    ) -> Any:
        """Operational metrics. Requires the admin token when one is set.

        Counters and durations only: no token, secret, client credential or
        transcript ever reaches this registry.
        """
        if settings.metrics_admin_token:
            if not token_matches(authorization, settings.metrics_admin_token):
                # Counted separately from `auth_failures_total`: a scraper with
                # a stale token must not look like somebody brute forcing
                # session credentials.
                metrics.increment("metrics_auth_failures_total")
                return JSONResponse({"detail": "invalid credentials"}, status_code=401)
        return PlainTextResponse(
            request.app.state.metrics.render(), media_type="text/plain; version=0.0.4"
        )
