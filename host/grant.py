"""Exchanging the host's Deepgram key for a short-lived session token.

This is the only place in the whole system where the Deepgram API key is used,
and it is used solely to obtain a token that expires within seconds. No
credential and no upstream response body ever enters a message, a log line or
an exception.

Both a synchronous and an asynchronous transport exist: production uses
`AsyncGrantClient` (one reused `httpx.AsyncClient` pool, so 50 simultaneous
token requests never block the event loop), while tests,
`scripts/measure_capacity.py` and the benchmarks drive `grant_session`. The two
are kept in step on purpose -- a difference between them would mean the
harness validates behaviour production never runs.
"""
from __future__ import annotations

import logging
import math
import os
from typing import Any, Callable, Optional, Tuple

try:  # package import from a full checkout
    from .config import DEEPGRAM_GRANT_URL, SESSION_ID_BYTES, HostSettings
except ImportError:  # pragma: no cover - cPanel copies only the host/ dir
    from config import (  # type: ignore[no-index]
        DEEPGRAM_GRANT_URL,
        SESSION_ID_BYTES,
        HostSettings,
    )

log = logging.getLogger("medical_stt.host")


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
    # Provider contract: delta-seconds only, not HTTP-date. Unrecognized date
    # values are ignored; ordinary bounded reconnect backoff still applies.
    try:
        seconds = float(value) if value else 0.0
        return min(60, max(1, math.ceil(seconds))) if math.isfinite(seconds) and seconds > 0 else None
    except (ValueError, OverflowError):
        return None
