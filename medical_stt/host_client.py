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

#: Refuse to follow redirects: a redirect could send the shared secret to
#: a host we did not intend to authenticate against.
_OPENER_FACTORY = urllib.request.build_opener(urllib.request.HTTPHandler(), urllib.request.HTTPSHandler())


class HostProtocolError(RuntimeError):
    """The host replied with something we cannot use."""


@dataclass(frozen=True)
class HostSession:
    """A short-lived Deepgram session credential."""

    access_token: str
    expires_in: int


class HostSessionClient:
    """Fetches short-lived Deepgram session tokens from the host."""

    def __init__(
        self,
        base_url: str,
        secret: str,
        timeout: float = 10.0,
        opener: Optional[Callable[[urllib.request.Request, float], Any]] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._secret = secret
        self._timeout = timeout
        self._open = opener or self._default_open

    def _default_open(self, request: urllib.request.Request, timeout: float) -> bytes:
        with _OPENER_FACTORY.open(request, timeout=timeout) as response:
            return response.read()

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
            },
        )

        try:
            raw = self._open(request, self._timeout)
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
        host = exc.url or "the host"
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
        return HostSession(access_token=token, expires_in=expires_in)
