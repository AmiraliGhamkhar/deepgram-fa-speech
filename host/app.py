"""Medical STT host service -- the only place the Deepgram API key exists.

Endpoints
---------
`GET  /healthz`   liveness probe; reveals nothing.
`POST /v1/session` exchanges the caller's shared secret for a short-lived
                  Deepgram session token.

The desktop client then opens its WebSocket straight to Deepgram with
`Authorization: Bearer <short-lived token>`, exactly as documented in
Deepgram's "Token-Based Auth" guide. That keeps the audio path low
latency while the long-lived API key never leaves this host.

Run it behind TLS (see host/README.md and the root README > Deploying the
host). Plain HTTP is refused unless `HOST_ALLOW_HTTP=1`, which exists only
for local development.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

try:  # allows `python host/app.py` from a checkout
    from .core import (
        GrantError,
        HostSettings,
        RateLimiter,
        authorize,
        grant_session,
        normalize_ttl,
        request_is_secure,
    )
except ImportError:  # pragma: no cover - direct script execution
    from core import (  # type: ignore[no-index]
        GrantError,
        HostSettings,
        RateLimiter,
        authorize,
        grant_session,
        normalize_ttl,
        request_is_secure,
    )

log = logging.getLogger("medical_stt.host")


def create_app(settings: Optional[HostSettings] = None) -> FastAPI:
    """Build the ASGI app. Settings come from the environment by default."""
    resolved = settings or HostSettings.from_env()
    limiter = RateLimiter(resolved.rate_limit_requests, resolved.rate_limit_window_seconds)
    app = FastAPI(
        title="Medical STT host",
        version="1.0.0",
        docs_url=None,   # no interactive docs on a credentialed service
        redoc_url=None,
    )

    @app.middleware("http")
    async def enforce_https(request: Request, call_next: Any) -> Any:
        """Refuse plaintext requests (a secret must never cross the wire raw).

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
            return JSONResponse(
                {"detail": "HTTPS is required"},
                status_code=400,
            )
        return await call_next(request)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.post("/v1/session")
    async def create_session(
        request: Request,
        authorization: Optional[str] = Header(default=None),
    ) -> JSONResponse:
        client = request.client.host if request.client else "unknown"

        allowed, retry_after = limiter.allow(client)
        if not allowed:
            log.warning("rate limit hit for %s", client)
            return JSONResponse(
                {"detail": "too many session requests"},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )

        if not authorize(authorization, resolved):
            # Never log the presented value.
            log.warning("rejected unauthenticated session request from %s", client)
            return JSONResponse({"detail": "invalid credentials"}, status_code=401)

        body: Any = {}
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - an empty/invalid body is fine
            body = {}
        ttl = normalize_ttl(
            body.get("ttl_seconds") if isinstance(body, dict) else None, resolved
        )

        try:
            token, expires_in = grant_session(resolved, ttl)
        except GrantError as exc:
            log.error("session grant failed: %s", exc)
            return JSONResponse({"detail": str(exc)}, status_code=exc.status)

        return JSONResponse({"access_token": token, "expires_in": expires_in})

    return app


# Fail fast at import if the Deepgram key is absent: this service has no
# reason to run without it.
app = create_app()


def main() -> int:  # pragma: no cover - operational entry point
    """Run the service. TLS is provided by uvicorn's cert files."""
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    settings = HostSettings.from_env()
    host = os.getenv("HOST_BIND", "0.0.0.0")
    port = int(os.getenv("HOST_PORT", "8443"))
    certfile = os.getenv("HOST_TLS_CERTFILE", "")
    keyfile = os.getenv("HOST_TLS_KEYFILE", "")

    if settings.allow_http and not certfile:
        log.warning("HOST_ALLOW_HTTP is set: serving plaintext (development only)")

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
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
