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

Concurrency model
-----------------
The host issues *tokens only*; it never carries audio. Scaling to 50
concurrent clinicians therefore needs three things, all here:

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

import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, PlainTextResponse

try:  # allows `python host/app.py` from a checkout
    from .core import (
        AsyncGrantClient,
        ConfigurationError,
        GrantError,
        HostSettings,
        RateLimiter,
        TokenBucketRateLimiter,
        auth_failure_key,
        authenticate_client,
        grant_session_async,
        new_session_id,
        normalize_ttl,
        request_is_secure,
    )
    from .metrics import new_metrics
except ImportError:  # pragma: no cover - direct script execution
    from core import (  # type: ignore[no-index]
        AsyncGrantClient,
        ConfigurationError,
        GrantError,
        HostSettings,
        RateLimiter,
        TokenBucketRateLimiter,
        auth_failure_key,
        authenticate_client,
        grant_session_async,
        new_session_id,
        normalize_ttl,
        request_is_secure,
    )
    from metrics import new_metrics  # type: ignore[no-index]

log = logging.getLogger("medical_stt.host")

#: Header carrying the provisioned client id. Not a credential on its own:
#: the secret in `Authorization` is what authenticates.
CLIENT_ID_HEADER = "X-Client-Id"

#: Security headers added to every response. Conservative and safe for a
#: JSON API with no HTML surface: `nosniff` and a deny-all frame policy.
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}


class _InflightLimiter:
    """Bounded semaphore for concurrent upstream grants.

    50 simultaneous token requests must all be servable, but an unbounded
    number of them must not be allowed to pile up if Deepgram slows down:
    each one holds a response object and a socket. Past the cap the host
    returns 503 immediately -- an explicit, controlled error rather than a
    silent queue that grows without limit.
    """

    def __init__(self, capacity: int) -> None:
        self._capacity = max(1, capacity)
        self._in_flight = 0

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def capacity(self) -> int:
        return self._capacity

    def acquire(self) -> bool:
        if self._in_flight >= self._capacity:
            return False
        self._in_flight += 1
        return True

    def release(self) -> None:
        if self._in_flight > 0:
            self._in_flight -= 1


def create_app(settings: Optional[HostSettings] = None) -> FastAPI:
    """Build the ASGI app. Settings come from the environment by default."""
    resolved = settings or HostSettings.from_env()
    metrics = new_metrics()
    registry = resolved.build_client_registry()

    # Per-client: generous and burst tolerant, so 50 colleagues starting at
    # once all succeed and only a genuinely looping client is throttled.
    client_limiter = TokenBucketRateLimiter(
        capacity=resolved.client_burst,
        refill_per_second=resolved.client_rate_limit_requests / resolved.rate_limit_window_seconds,
    )
    # Global ceiling: protects the host from the aggregate, while staying far
    # above the 50-concurrent-session target.
    global_limiter = TokenBucketRateLimiter(
        capacity=resolved.global_burst,
        refill_per_second=resolved.global_rate_limit_requests / resolved.rate_limit_window_seconds,
    )
    # Failed authentication is keyed on IP on purpose: it is abuse, not
    # legitimate traffic, so a shared NAT address must not exempt it.
    auth_failure_limiter = RateLimiter(
        limit=resolved.auth_failure_limit,
        window_seconds=resolved.rate_limit_window_seconds,
    )
    inflight = _InflightLimiter(resolved.max_inflight_grants)

    #: One-shot flag, so a deployment that cannot attribute failed
    #: authentication is reported once instead of on every rejected request.
    unattributable_failure_warned = [False]

    # Capacity facts, published as soon as the app exists rather than in
    # `lifespan`: a WSGI deployment (host/passenger_wsgi.py, via a2wsgi) never
    # runs the ASGI lifespan protocol, so anything set only there would read
    # as a permanent zero on cPanel.
    metrics.set_gauge("inflight_grants_capacity", inflight.capacity)
    metrics.set_gauge("registry_clients", len(registry))
    metrics.set_gauge("inflight_grants", inflight.in_flight)

    if not resolved.metrics_admin_token:
        # Not fatal -- a loopback-only or proxy-shielded deployment may
        # reasonably leave it open -- but it is a fact an operator should see
        # once at startup rather than discover from a scrape in the wild.
        log.warning(
            "HOST_METRICS_ADMIN_TOKEN is not set: /metrics is readable by "
            "anyone who can reach this port. It exposes no credential and no "
            "transcript, but it does expose traffic volume and the number of "
            "registered clients."
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info(
            "host ready: clients=%d client_burst=%d global_burst=%d max_inflight=%d",
            len(registry), resolved.client_burst, resolved.global_burst, resolved.max_inflight_grants,
        )
        try:
            yield
        finally:
            # Graceful shutdown: stop accepting work, then release the pool.
            await app.state.grant_client.aclose()
            log.info("host shut down cleanly (closed the Deepgram connection pool)")

    app = FastAPI(
        title="Medical STT host",
        version="2.0.0",
        docs_url=None,   # no interactive docs on a credentialed service
        redoc_url=None,
        lifespan=lifespan,
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
    )
    app.state.settings = resolved
    app.state.metrics = metrics
    app.state.registry = registry
    # Exposed for the concurrency/soak tests to assert that limiter state
    # stays bounded, and for operational introspection.
    app.state.client_limiter = client_limiter
    app.state.global_limiter = global_limiter
    app.state.auth_failure_limiter = auth_failure_limiter
    app.state.inflight = inflight

    @app.middleware("http")
    async def harden(request: Request, call_next: Any) -> Any:
        """Refuse plaintext requests and add security headers.

        `X-Forwarded-Proto` is honoured only when the immediate peer is a
        configured reverse proxy (`HOST_FORWARDED_ALLOW_IPS`, loopback by
        default), so a direct HTTP client cannot forge the header to make a
        plaintext request look like TLS.
        """
        peer_host = request.client.host if request.client else None
        secure = request_is_secure(
            request.url.scheme,
            peer_host,
            request.headers.get("x-forwarded-proto"),
            resolved,
        )
        if not secure and not resolved.allow_http:
            response: Any = JSONResponse({"detail": "HTTPS is required"}, status_code=400)
        else:
            response = await call_next(request)
        for header, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response

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
        return JSONResponse({"status": "ready" if ready else "starting"}, status_code=200 if ready else 503)

    @app.get("/metrics")
    async def prometheus_metrics(request: Request, authorization: Optional[str] = Header(default=None)) -> Any:
        """Operational metrics. Requires the admin token when one is set.

        Counters and durations only: no token, secret, client credential or
        transcript ever reaches this registry.
        """
        if resolved.metrics_admin_token:
            if not _token_matches(authorization, resolved.metrics_admin_token):
                # Counted separately from `auth_failures_total`: a scraper
                # with a stale token must not look like somebody brute
                # forcing session credentials.
                metrics.increment("metrics_auth_failures_total")
                return JSONResponse({"detail": "invalid credentials"}, status_code=401)
        return PlainTextResponse(
            request.app.state.metrics.render(), media_type="text/plain; version=0.0.4"
        )

    @app.post("/v1/session")
    async def create_session(
        request: Request,
        authorization: Optional[str] = Header(default=None),
        x_client_id: Optional[str] = Header(default=None, alias=CLIENT_ID_HEADER),
    ) -> Any:
        # `None` when the server supplied no peer address (a WSGI/Passenger
        # environ without REMOTE_PORT, for example); never a placeholder that
        # would silently merge every such request into one bucket.
        peer = request.client.host if request.client else None

        # -- abuse guard: a locked-out identity is refused without any work ---
        auth_key = auth_failure_key(peer, x_client_id)
        if auth_key is not None:
            locked, lock_retry_after = auth_failure_limiter.is_locked(auth_key)
            if locked:
                metrics.increment("rate_limited_total")
                return JSONResponse(
                    {"detail": "too many failed authentication attempts"},
                    status_code=429,
                    headers={"Retry-After": str(lock_retry_after)},
                )

        identity = authenticate_client(authorization, x_client_id, registry)
        if identity is None:
            # Charge the brute-force budget *only* on a real failure.
            # Charging every request would make a hospital's 50 legitimate
            # colleagues exhaust each other's budget on a shared NAT address,
            # which is precisely the bug this work fixes.
            retry_after = 0
            if auth_key is not None:
                _allowed, retry_after = auth_failure_limiter.allow(auth_key)
            elif not unattributable_failure_warned[0]:
                # Said once, not per request: this is a deployment property,
                # not something the caller can act on each time.
                unattributable_failure_warned[0] = True
                log.warning(
                    "failed authentication cannot be attributed to a peer "
                    "address or a client id, so no brute-force lockout is "
                    "applied to it (authentication is still enforced). Run "
                    "behind a server that supplies REMOTE_ADDR/REMOTE_PORT, or "
                    "provision client ids, to re-enable the lockout."
                )
            metrics.increment("auth_failures_total")
            # Never log the presented credential or the client id.
            log.warning(
                "rejected unauthenticated session request from %s", peer or "unknown"
            )
            headers = {"Retry-After": str(retry_after)} if retry_after else None
            return JSONResponse(
                {"detail": "invalid credentials"}, status_code=401, headers=headers
            )

        # Counted only for authenticated callers: brute-force noise must not
        # inflate the session-start metric.
        metrics.increment("session_starts_total")

        # -- layered rate limits, keyed on identity, not on the NAT IP ----
        allowed, retry_after = client_limiter.allow(f"client:{identity.client_id}")
        if allowed:
            allowed, retry_after = global_limiter.allow("global")
        if not allowed:
            metrics.increment("rate_limited_total")
            log.warning(
                "rate limit hit client_id=%s peer=%s retry_after=%ds",
                identity.client_id, peer or "unknown", retry_after,
            )
            return JSONResponse(
                {"detail": "too many session requests"},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )

        # -- bounded request body ----------------------------------------
        #
        # The declared length is refused before reading anything, and the
        # body itself is read *in chunks* with the same cap applied while
        # streaming. `await request.body()` would buffer an entire chunked
        # request (which declares no Content-Length) before the size check
        # could run, so any authenticated client could exhaust host memory
        # with `Transfer-Encoding: chunked` and take the shared token
        # service down for every clinician. Chunk-by-chunk, the connection
        # is aborted at the cap instead.
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > resolved.max_request_body_bytes:
                    metrics.increment("request_body_rejections_total")
                    log.warning(
                        "session refused reason=declared_body_over_cap bytes=%s", declared
                    )
                    return JSONResponse({"detail": "request body too large"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "invalid request"}, status_code=400)

        session_id = new_session_id()
        log.info("session_started session_id=%s client_id=%s", session_id, identity.client_id)

        body: Any = {}
        try:
            raw = b""
            received = 0
            async for chunk in request.stream():
                received += len(chunk)
                if received > resolved.max_request_body_bytes:
                    metrics.increment("request_body_rejections_total")
                    log.warning(
                        "session aborted session_id=%s reason=body_over_cap bytes=%d",
                        session_id, received,
                    )
                    return JSONResponse({"detail": "request body too large"}, status_code=413)
                raw += chunk
        except Exception:  # noqa: BLE001 - a truncated body is simply empty
            raw = b""
        if raw:
            try:
                import json

                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                body = {}
        ttl = normalize_ttl(body.get("ttl_seconds") if isinstance(body, dict) else None, resolved)

        if not inflight.acquire():
            # Explicit controlled error, not a silent drop and not an
            # unbounded wait for a Deepgram slot.
            metrics.increment("token_request_failures_total")
            metrics.increment("session_failures_total")
            log.error("session_error session_id=%s reason=upstream_saturated", session_id)
            return JSONResponse(
                {"detail": "host is at capacity, retry shortly"},
                status_code=503,
                headers={"Retry-After": "1"},
            )

        started = time.monotonic()
        metrics.increment("token_requests_total")
        metrics.set_gauge("inflight_grants", inflight.in_flight)
        try:
            token, expires_in = await grant_session_async(
                resolved, ttl, request.app.state.grant_client
            )
        except GrantError as exc:
            elapsed = time.monotonic() - started
            metrics.observe("token_request_latency", elapsed)
            metrics.increment("token_request_failures_total")
            metrics.increment("session_failures_total")
            if exc.status in (503, 504):
                # Upstream unreachable or too slow: an operator problem with
                # Deepgram, as opposed to a 502 (our own key is wrong) or a
                # 429 we shed ourselves.
                metrics.increment("deepgram_connection_failures_total")
            # The message never contains the Deepgram key or its body.
            log.error("session_error session_id=%s status=%s", session_id, exc.status)
            return JSONResponse({"detail": str(exc)}, status_code=exc.status)
        finally:
            inflight.release()
            metrics.set_gauge("inflight_grants", inflight.in_flight)

        elapsed = time.monotonic() - started
        metrics.observe("token_request_latency", elapsed)
        metrics.increment("session_success_total")
        log.info("token_issued session_id=%s client_id=%s", session_id, identity.client_id)

        return JSONResponse(
            {
                "access_token": token,
                "expires_in": expires_in,
                "session_id": session_id,
                "client_id": identity.client_id,
            }
        )

    return app


def _token_matches(authorization: Optional[str], expected: str) -> bool:
    """Constant-time admin-token comparison for `/metrics`.

    `hmac.compare_digest` raises `TypeError` on non-ASCII input, so both
    sides are compared as UTF-8 bytes: a caller who sends a non-ASCII
    bearer token gets 401, never a 500.
    """
    import hmac

    if not authorization:
        return False
    scheme, _, presented = authorization.partition(" ")
    if scheme.strip().lower() != "bearer":
        return False
    return hmac.compare_digest(
        presented.strip().encode("utf-8"), expected.encode("utf-8")
    )


# Fail fast at import if the Deepgram key is absent: this service has no
# reason to run without it.
try:
    app = create_app()
except ConfigurationError as _exc:  # pragma: no cover - misconfiguration path
    raise SystemExit(f"host configuration error: {_exc}") from _exc


def main() -> int:  # pragma: no cover - operational entry point
    """Run the service. TLS is provided by uvicorn's cert files."""
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    try:
        settings = HostSettings.from_env()
    except ConfigurationError as exc:
        log.error("host configuration error: %s", exc)
        return 2
    host = os.getenv("HOST_BIND", "0.0.0.0")
    raw_port = os.getenv("HOST_PORT", "8443").strip()
    try:
        port = int(raw_port)
    except ValueError:
        # A bare int() traceback here reads like a crash in the service rather
        # than a typo in one environment variable, and it is the first thing
        # an operator hits when a panel rewrites HOST_PORT to "" or "8443 ".
        log.error("HOST_PORT must be an integer; got %r", raw_port)
        return 2
    if not 1 <= port <= 65535:
        log.error("HOST_PORT must be between 1 and 65535; got %d", port)
        return 2
    certfile = os.getenv("HOST_TLS_CERTFILE", "")
    keyfile = os.getenv("HOST_TLS_KEYFILE", "")

    if not certfile:
        if settings.allow_http:
            log.warning("HOST_ALLOW_HTTP is set: serving plaintext (development only)")
        else:
            # Not fatal, and not a misconfiguration in the common case: a
            # reverse proxy that terminates TLS and forwards
            # X-Forwarded-Proto from an address in HOST_FORWARDED_ALLOW_IPS is
            # exactly how this is meant to be deployed behind Apache or a
            # load balancer. But when there is no such proxy the app rejects
            # every request with 400 and the container healthcheck fails
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
