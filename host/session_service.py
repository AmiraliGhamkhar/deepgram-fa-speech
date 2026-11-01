"""The `/v1/session` use case: exchange device credentials for a Deepgram token.

This module owns the whole conversation with a caller, in the order it happens:

1. **authenticate** the device (bearer secret + optional `X-Client-Id`),
2. **lock out** an identity that has already failed too often,
3. **charge** the brute-force budget only when authentication really failed,
4. **admit** the request through the global then per-client limiter,
5. **cap** the request body (declared length, then chunk by chunk),
6. **validate** the requested TTL and clamp it to the configured range,
7. **acquire** an in-flight grant slot, and
8. **mint** the token, reconciling metrics on every exit path.

Splitting that sequence out of `app.create_app` keeps the app factory a
composition root: it wires objects together and contains no request logic.
Every ordering decision above is load-bearing and is explained where it
happens, because most of them exist to fix a specific production bug (a shared
hospital NAT exhausting one budget, a chunked body exhausting memory, a
cancelled request vanishing from `/metrics`).

**Nothing here logs a credential, a token or a transcript.** The only
identifiers in log lines are the authenticated `client_id` and a random
`session_id`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse

try:  # package import from a full checkout
    from .config import HostSettings
    from .grant import GrantError, new_session_id
    from .identity import (
        ClientIdentity,
        ClientRegistry,
        auth_failure_key,
        authenticate_client,
    )
    from .limits import Limiters
    from .request_guard import normalize_ttl
except ImportError:  # pragma: no cover - cPanel copies only the host/ dir
    from config import HostSettings  # type: ignore[no-index]
    from grant import GrantError, new_session_id  # type: ignore[no-index]
    from identity import (  # type: ignore[no-index]
        ClientIdentity,
        ClientRegistry,
        auth_failure_key,
        authenticate_client,
    )
    from limits import Limiters  # type: ignore[no-index]
    from request_guard import normalize_ttl  # type: ignore[no-index]

log = logging.getLogger("medical_stt.host")

#: Mints a session token. Injected so the app can keep its module-level
#: `grant_session_async` name patchable, and so a test can substitute a stub.
GrantFn = Callable[[HostSettings, int, Any], Awaitable[Tuple[str, int]]]


class SessionRefusal(Exception):
    """A guard said no. Carries the response and the metric outcome."""

    def __init__(
        self, status_code: int, detail: str, outcome: str, retry_after: int = 0
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.outcome = outcome
        self.retry_after = retry_after

    @property
    def headers(self) -> Optional[dict]:
        return {"Retry-After": str(self.retry_after)} if self.retry_after else None


@dataclass
class SessionAttempt:
    """Metrics bookkeeping for one *authenticated* session attempt.

    `MetricRecorder.failure` plus the success path must always add up to
    `session_starts_total`. When they do not reconcile, an operator reading
    `/metrics` during an incident cannot tell "no traffic" from "all traffic
    failing" -- the only question this service's metrics exist to answer.
    """

    metrics: Any
    client_id: str
    session_id: Optional[str] = None

    def failure(self, outcome: str) -> None:
        """Charge an authenticated request that minted no token.

        `outcome` is a fixed, small vocabulary (never a raw client value) and
        `client_id` is the *authenticated* id, so the per-client series cannot
        be inflated by fabricated ids.
        """
        self.metrics.increment("session_failures_total")
        self.metrics.increment(
            "session_requests_by_client_total", client_id=self.client_id, outcome=outcome
        )

    def success(self) -> None:
        self.metrics.increment("session_success_total")
        self.metrics.increment(
            "session_requests_by_client_total", client_id=self.client_id, outcome="ok"
        )

    def refuse(self, refusal: SessionRefusal) -> JSONResponse:
        """`failure` plus the JSON error response for that outcome."""
        self.failure(refusal.outcome)
        if self.session_id is not None:
            log.error(
                "session_error session_id=%s reason=%s status=%d",
                self.session_id,
                refusal.outcome,
                refusal.status_code,
            )
        return JSONResponse(
            {"detail": refusal.detail},
            status_code=refusal.status_code,
            headers=refusal.headers,
        )


class SessionService:
    """Authenticates devices and mints short-lived Deepgram tokens."""

    def __init__(
        self,
        settings: HostSettings,
        registry: ClientRegistry,
        metrics: Any,
        limiters: Limiters,
        grant: GrantFn,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._metrics = metrics
        self._limiters = limiters
        self._grant = grant
        #: One-shot flag, so a deployment that cannot attribute failed
        #: authentication is reported once instead of on every rejected
        #: request.
        self._unattributable_failure_warned = False

    # -- the use case ----------------------------------------------------
    async def create_session(self, request: Request) -> JSONResponse:
        metrics = self._metrics

        # `None` when the server supplied no peer address (a WSGI/Passenger
        # environ without REMOTE_PORT, for example); never a placeholder that
        # would silently merge every such request into one bucket.
        peer = request.client.host if request.client else None
        authorization = request.headers.get("authorization")
        x_client_id = request.headers.get("x-client-id")

        identity = authenticate_client(authorization, x_client_id, self._registry)
        if identity is None:
            return self._refuse_unauthenticated(peer, x_client_id)

        # Counted only for authenticated callers: brute-force noise must not
        # inflate the session-start metric.
        metrics.increment("session_starts_total")
        attempt = SessionAttempt(metrics, identity.client_id)

        try:
            self._enforce_rate_limits(identity, peer)
            declared = request.headers.get("content-length")
            self._check_declared_length(declared)

            attempt.session_id = new_session_id()
            log.info(
                "session_started session_id=%s client_id=%s",
                attempt.session_id,
                identity.client_id,
            )
            raw = await self._read_body(request, attempt)
            ttl = self._parse_ttl(raw)
            token, expires_in = await self._mint_token(attempt, ttl, request)
        except SessionRefusal as refusal:
            return attempt.refuse(refusal)

        attempt.success()
        log.info(
            "token_issued session_id=%s client_id=%s",
            attempt.session_id,
            identity.client_id,
        )
        return JSONResponse(
            {
                "access_token": token,
                "expires_in": expires_in,
                "session_id": attempt.session_id,
                "client_id": identity.client_id,
            }
        )

    # -- authentication and abuse guard ----------------------------------
    def _refuse_unauthenticated(
        self, peer: Optional[str], x_client_id: Optional[str]
    ) -> JSONResponse:
        settings = self._settings
        metrics = self._metrics
        auth_failure_limiter = self._limiters.auth_failure
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

        # Charge the brute-force budget *only* on a real failure. Charging
        # every request would make a hospital's 50 legitimate colleagues
        # exhaust each other's budget on a shared NAT address, which is
        # precisely the bug this design fixes.
        retry_after = 0
        if auth_key is not None:
            _allowed, retry_after = auth_failure_limiter.allow(auth_key)
        elif not self._unattributable_failure_warned:
            # Said once, not per request: this is a deployment property, not
            # something the caller can act on each time.
            self._unattributable_failure_warned = True
            log.warning(
                "failed authentication cannot be attributed to a peer address "
                "or a client id, so no brute-force lockout is applied to it "
                "(authentication is still enforced). Run behind a server that "
                "supplies REMOTE_ADDR/REMOTE_PORT, or provision client ids, to "
                "re-enable the lockout."
            )
        metrics.increment("auth_failures_total")
        # Never log the presented credential or the client id.
        log.warning("rejected unauthenticated session request from %s", peer or "unknown")
        headers = {"Retry-After": str(retry_after)} if retry_after else None
        detail = "invalid credentials"
        if x_client_id and not settings.clients_file:
            # The un-migrated-deployment trap: this host has exactly one
            # shared-secret identity, so a presented client id resolves to a
            # registry entry that does not exist and the correct secret still
            # 401s. Name the trap so the operator can fix the deployment
            # instead of rotating a good secret. This reveals nothing about
            # which ids exist -- in this mode the answer is "none beyond the
            # shared one" by definition.
            detail = (
                "invalid credentials: this host uses a single shared secret and "
                "does not accept a client id (X-Client-Id); clear the device id "
                "in the client, or provision per-device identities and set "
                "HOST_CLIENTS_FILE"
            )
        return JSONResponse({"detail": detail}, status_code=401, headers=headers)

    def _enforce_rate_limits(self, identity: ClientIdentity, peer: Optional[str]) -> None:
        """Layered limits, keyed on identity rather than on the NAT address.

        Global first: a request the global ceiling refuses must not also burn
        one of the client's own tokens. With the old order, global saturation
        drained every client's personal budget on requests that were never
        going to be served anyway.
        """
        limiters = self._limiters
        allowed, retry_after = limiters.global_.allow("global")
        if allowed:
            allowed, retry_after = limiters.client.allow(f"client:{identity.client_id}")
        if allowed:
            return
        self._metrics.increment("rate_limited_total")
        log.warning(
            "rate limit hit client_id=%s peer=%s retry_after=%ds",
            identity.client_id,
            peer or "unknown",
            retry_after,
        )
        raise SessionRefusal(
            429, "too many session requests", "rate_limited", retry_after=retry_after
        )

    # -- bounded request body --------------------------------------------
    def _check_declared_length(self, declared: Optional[str]) -> None:
        """Refuse an oversized body before reading a single byte of it."""
        if declared is None:
            return
        try:
            over_cap = int(declared) > self._settings.max_request_body_bytes
        except ValueError:
            raise SessionRefusal(400, "invalid request", "invalid_request") from None
        if over_cap:
            self._metrics.increment("request_body_rejections_total")
            log.warning("session refused reason=declared_body_over_cap bytes=%s", declared)
            raise SessionRefusal(413, "request body too large", "body_too_large")

    async def _read_body(self, request: Request, attempt: SessionAttempt) -> bytes:
        """Read the body *in chunks*, applying the cap while streaming.

        `await request.body()` would buffer an entire chunked request (which
        declares no Content-Length) before the size check could run, so any
        authenticated client could exhaust host memory with
        `Transfer-Encoding: chunked` and take the shared token service down for
        every clinician. Chunk by chunk, the connection is aborted at the cap.
        """
        settings = self._settings
        cap = settings.max_request_body_bytes

        async def read_capped() -> bytes:
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > cap:
                    raise OverflowError("body cap")
                raw.extend(chunk)
            return bytes(raw)

        try:
            return await asyncio.wait_for(
                read_capped(), settings.request_body_timeout_seconds
            )
        except asyncio.TimeoutError:
            self._metrics.increment("request_body_rejections_total")
            raise SessionRefusal(408, "request body timed out", "body_timeout") from None
        except OverflowError:
            self._metrics.increment("request_body_rejections_total")
            raise SessionRefusal(413, "request body too large", "body_too_large") from None
        except asyncio.CancelledError:
            # Client disconnected mid-body: counted, then re-raised so the
            # cancellation still unwinds normally.
            attempt.failure("cancelled")
            log.info("session_error session_id=%s reason=cancelled", attempt.session_id)
            raise
        except Exception:
            raise SessionRefusal(
                400, "incomplete request body", "incomplete_body"
            ) from None

    def _parse_ttl(self, raw: bytes) -> int:
        """Validate the JSON envelope and clamp the requested TTL."""
        try:
            body = json.loads(raw) if raw else {}
            if not isinstance(body, dict):
                raise ValueError("object required")
            requested = body.get("ttl_seconds")
        except (ValueError, UnicodeError, RecursionError, OverflowError):
            raise SessionRefusal(
                422,
                "ttl_seconds must be a bounded integer in a JSON object",
                "invalid_request",
            ) from None
        if requested is not None and (
            type(requested) is not int
            or not 5 <= requested <= self._settings.max_ttl_seconds
        ):
            raise SessionRefusal(
                422,
                "ttl_seconds must be a bounded integer in a JSON object",
                "invalid_request",
            )
        return normalize_ttl(requested, self._settings)

    # -- token issuance ---------------------------------------------------
    async def _mint_token(
        self, attempt: SessionAttempt, ttl: int, request: Request
    ) -> Tuple[str, int]:
        settings = self._settings
        metrics = self._metrics
        inflight = self._limiters.inflight

        if not inflight.acquire():
            # Explicit controlled error, not a silent drop and not an unbounded
            # wait for a Deepgram slot.
            metrics.increment("grant_capacity_rejections_total")
            raise SessionRefusal(
                503,
                "host is at capacity, retry shortly",
                "upstream_saturated",
                retry_after=1,
            )

        started = time.monotonic()
        metrics.increment("token_requests_total")
        metrics.set_gauge("inflight_grants", inflight.in_flight)
        try:
            try:
                return await asyncio.wait_for(
                    self._grant(settings, ttl, request.app.state.grant_client),
                    timeout=settings.grant_deadline_seconds,
                )
            except asyncio.TimeoutError as exc:
                raise GrantError("Deepgram grant deadline exceeded", 504) from exc
        except GrantError as exc:
            metrics.increment("token_request_failures_total")
            if exc.status in (503, 504):
                # Upstream unreachable or too slow: an operator problem with
                # Deepgram, as opposed to a 502 (our own key is wrong) or a 429
                # we shed ourselves.
                metrics.increment("deepgram_connection_failures_total")
            # The message never contains the Deepgram key or its body.
            raise SessionRefusal(
                exc.status,
                str(exc),
                "upstream_error",
                retry_after=exc.retry_after or 0,
            ) from None
        except asyncio.CancelledError:
            # Client disconnected mid-grant (or the server is shutting down).
            # `CancelledError` is a BaseException: without this handler the
            # request vanished from both counters and success + failures
            # stopped equalling starts.
            metrics.increment("token_request_failures_total")
            attempt.failure("cancelled")
            log.info("session_error session_id=%s reason=cancelled", attempt.session_id)
            raise
        except Exception as exc:  # noqa: BLE001 - the grant path must reconcile
            # `grant_session_async` maps every known upstream failure onto
            # `GrantError`, so reaching here means an unexpected internal
            # fault. It is still a request that produced no token: record it
            # that way before letting the framework turn it into a 500.
            metrics.increment("token_request_failures_total")
            attempt.failure("internal_error")
            log.error(
                "session_error session_id=%s reason=unexpected (%s)",
                attempt.session_id,
                type(exc).__name__,
            )
            raise
        finally:
            inflight.release()
            metrics.set_gauge("inflight_grants", inflight.in_flight)
            metrics.observe("token_request_latency", time.monotonic() - started)
