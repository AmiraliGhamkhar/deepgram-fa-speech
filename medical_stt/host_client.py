"""Client for the self-hosted STT service.

The only credential this process ever holds is a short-lived session
token minted by the host from the Deepgram API key. The Deepgram key
itself never leaves the host.

The client authenticates with the shared secret on every call, over
HTTPS, and maps failures onto the existing `ErrorCategory` taxonomy so
`AUTH`/`CONFIG` are never retried by the reconnect loop.

Implemented with `urllib.request` from the standard library on purpose:
the packaged client gains no new dependency for a single HTTPS POST.
"""
from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .stt.base import ErrorCategory, ProviderError

log = logging.getLogger("medical_stt.host_client")

#: Endpoint on the host that exchanges the shared secret for a temporary
#: Deepgram session token.
SESSION_PATH = "/v1/session"

#: A session grant is a small JSON object. Bound the response even if a
#: broken proxy or host streams an unexpectedly large body.
MAX_HOST_RESPONSE_BYTES = 64 * 1024


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect instead of following it.

    `urllib.request.build_opener` installs the stdlib `HTTPRedirectHandler`
    by default, which silently follows 301/302/303 -- and re-sends the
    `Authorization` header to whatever URL the redirect names. For this
    client that would deliver the shared secret (and the client id) to an
    arbitrary redirect target, whose response would then be trusted as a
    valid session. A token service never legitimately redirects, so any
    3xx is treated as a failed, non-retried host error.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HostProtocolError(
            f"host attempted a redirect (HTTP {code}); refusing to send credentials anywhere else"
        )


#: Opener that refuses redirects. `HostProtocolError` raised from
#: `redirect_request` is caught in `fetch_session` and mapped onto the
#: provider error taxonomy.
_OPENER_FACTORY = urllib.request.build_opener(
    urllib.request.HTTPHandler(),
    urllib.request.HTTPSHandler(),
    _NoRedirectHandler(),
)


class HostProtocolError(RuntimeError):
    """The host replied with something we cannot use."""


class HostResponseTooLarge(HostProtocolError):
    """The session response exceeded the small protocol size limit."""


@dataclass(frozen=True)
class HostSession:
    """A short-lived Deepgram session credential."""

    access_token: str
    expires_in: int
    #: Host-side correlation id for logs and metrics. Never a credential.
    session_id: str = ""


def _parse_retry_after(raw: Optional[str]) -> Optional[float]:
    """Parse a `Retry-After` header into seconds.

    Only the delta-seconds form is accepted. The HTTP-date form is legal in
    RFC 9110 but a token service never needs it, and parsing a date
    correctly across clock skew is more machinery than the situation
    justifies. A malformed value returns None so the caller falls back to
    its own backoff instead of trusting a garbage hint.
    """
    if not raw:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    return seconds if seconds > 0 else None


class HostSessionClient:
    """Fetches short-lived Deepgram session tokens from the host."""

    def __init__(
        self,
        base_url: str,
        secret: str,
        timeout: float = 10.0,
        opener: Optional[Callable[[urllib.request.Request, float], Any]] = None,
        client_id: str = "",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._secret = secret
        self._timeout = timeout
        self._open = opener or self._default_open
        self._client_id = client_id

    def _default_open(self, request: urllib.request.Request, timeout: float) -> bytes:
        with _OPENER_FACTORY.open(request, timeout=timeout) as response:
            raw = response.read(MAX_HOST_RESPONSE_BYTES + 1)
        if len(raw) > MAX_HOST_RESPONSE_BYTES:
            raise HostResponseTooLarge("host returned an oversized session response")
        return raw

    def fetch_session(self, ttl_seconds: int = 30) -> HostSession:
        """Exchange the shared secret for a temporary session token.

        The returned token is a credential: it is never logged, never
        persisted, and is used immediately by the provider for the
        WebSocket handshake.
        """
        if not self._base_url:
            raise ProviderError(ErrorCategory.CONFIG, "host_url is not configured")
        if not self._secret:
            raise ProviderError(
                ErrorCategory.CONFIG, "no host shared secret is configured"
            )

        url = f"{self._base_url}{SESSION_PATH}"
        body = json.dumps({"ttl_seconds": int(ttl_seconds)}).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._secret}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "MedicalSTT",
                # Identifies *which* device is asking, so the host can rate
                # limit per clinician instead of per hospital NAT address.
                # Omitted when unconfigured: an un-migrated host resolves it
                # to its single legacy identity.
                **({"X-Client-Id": self._client_id} if self._client_id else {}),
            },
        )

        try:
            raw = self._open(request, self._timeout)
        except HostResponseTooLarge:
            raise ProviderError(
                ErrorCategory.UNKNOWN, "host returned an oversized session response"
            ) from None
        except HostProtocolError as exc:
            # A redirect was refused. The configured host is reported (not
            # the redirect target), and the credential never left for it.
            raise ProviderError(
                ErrorCategory.CONFIG,
                f"host redirected the session request: {exc} (configured host: {self._base_url})",
            ) from None
        except urllib.error.HTTPError as exc:
            raise self._classify_http_error(exc) from None
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, socket.timeout):
                raise ProviderError(
                    ErrorCategory.TIMEOUT, f"host request timed out: {self._base_url}"
                ) from None
            raise ProviderError(
                ErrorCategory.NETWORK, f"cannot reach host {self._base_url}: {reason}"
            ) from None
        except (socket.timeout, TimeoutError):
            raise ProviderError(
                ErrorCategory.TIMEOUT, f"host request timed out: {self._base_url}"
            ) from None
        except OSError as exc:
            raise ProviderError(
                ErrorCategory.NETWORK, f"cannot reach host {self._base_url}: {exc}"
            ) from None

        return self._parse_session(raw)

    @staticmethod
    def _classify_http_error(exc: urllib.error.HTTPError) -> ProviderError:
        """Map an HTTP status onto the provider error taxonomy.

        The message never includes the shared secret or the response body,
        which could echo back request headers.
        """
        status = exc.code
        # `exc.url` is the *effective* URL, which a followed redirect would
        # have attacker-chosen (and which may echo into logs/UI). The
        # opener refuses redirects, so this is always the configured host
        # -- but the configured host is what an operator can act on anyway.
        host = exc.url or "the host"
        retry_after = _parse_retry_after(
            exc.headers.get("Retry-After") if exc.headers is not None else None
        )
        if status in (301, 302, 303, 307, 308):
            # The opener refuses to follow redirects; a 3xx that still
            # surfaces as an HTTPError (307/308 are not auto-handled, and
            # a proxy may also emit one) means the host is misconfigured
            # or is redirecting credentials elsewhere. Never retried.
            return ProviderError(
                ErrorCategory.CONFIG,
                f"host redirected the session request (HTTP {status}) at {host}",
            )
        if status in (401, 403):
            # Most likely a wrong/mismatched shared secret: never retry.
            return ProviderError(
                ErrorCategory.AUTH,
                f"host rejected the shared secret (HTTP {status}) at {host}",
            )
        if status == 429:
            return ProviderError(
                ErrorCategory.RATE_LIMIT,
                f"host rate limit reached (HTTP 429) at {host}",
                retry_after=retry_after,
            )
        if status in (400, 404, 405, 422):
            return ProviderError(
                ErrorCategory.CONFIG,
                f"host rejected the session request (HTTP {status}) at {host}",
            )
        if status >= 500:
            return ProviderError(
                ErrorCategory.SERVER_DISCONNECT,
                f"host is unavailable (HTTP {status}) at {host}",
                retry_after=retry_after,
            )
        return ProviderError(
            ErrorCategory.UNKNOWN, f"unexpected host response (HTTP {status}) at {host}"
        )

    @staticmethod
    def _parse_session(raw: bytes) -> HostSession:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ProviderError(
                ErrorCategory.UNKNOWN,
                "host returned a non-JSON session response",
            ) from None
        if not isinstance(payload, dict):
            raise ProviderError(
                ErrorCategory.UNKNOWN, "host returned an unexpected session payload"
            )
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise ProviderError(
                ErrorCategory.UNKNOWN, "host response did not contain a session token"
            )
        try:
            expires_in = int(payload.get("expires_in", 0))
        except (TypeError, ValueError):
            expires_in = 0
        if expires_in <= 0:
            raise ProviderError(
                ErrorCategory.UNKNOWN, "host response did not contain a valid expiry"
            )
        session_id = payload.get("session_id")
        return HostSession(
            access_token=token,
            expires_in=expires_in,
            session_id=session_id if isinstance(session_id, str) else "",
        )
