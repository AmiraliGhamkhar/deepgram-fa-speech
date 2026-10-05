"""WSGI entry point for cPanel / Passenger and other WSGI-only hosts.

cPanel's "Setup Python App" (Passenger) and most shared hosts speak WSGI.
This service is ASGI (FastAPI on uvicorn), so this module bridges the two
with `a2wsgi` and runs the async parts on a dedicated, long-lived event
loop.

Why a *dedicated* loop thread rather than `asyncio.run` per request:
`AsyncGrantClient` holds one reused `httpx.AsyncClient` across requests,
and an `httpx.AsyncClient` must be created, used and closed on the *same*
event loop. A per-request loop would break the pool or leak connections
under load -- the exact failure this project's 50-session hardening fixed.

Passenger configuration (cPanel > Setup Python App):

    Application root:         the directory containing this file
    Application URL:          your subdomain
    Application startup file: passenger_wsgi.py
    Expose as:                application (the WSGI callable below)

Environment (cPanel > the app's "Environment variables" or an .env loader):

    DEEPGRAM_API_KEY=<your key>          # required
    HOST_SHARED_SECRET=<24+ chars>       # legacy single-clinician mode
    HOST_CLIENTS_FILE=/home/<user>/secure/clients.txt   # multi-clinician
    HOST_ALLOW_HTTP=1                    # Apache already terminates TLS
    HOST_FORWARDED_ALLOW_IPS=127.0.0.1   # Apache forwards from loopback

Eager construction: an operator who forgot DEEPGRAM_API_KEY sees the
deploy fail immediately (Passenger logs the traceback) instead of getting
an error on the first Start press from the clinic.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Optional

from a2wsgi import ASGIMiddleware

try:  # package import from a full checkout
    from .app import create_app
    from .core import HostSettings
except ImportError:  # pragma: no cover - cPanel copies only the host/ dir
    from app import create_app  # type: ignore[no-index]
    from core import HostSettings  # type: ignore[no-index]


class _EventLoopThread:
    """One background thread owning one asyncio event loop forever."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready = threading.Event()
        self._lock = threading.Lock()

    def start(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None:
                return self._loop

            def _run() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                self._ready.set()
                loop.run_forever()

            threading.Thread(target=_run, name="asgi-adapter-loop", daemon=True).start()
            self._ready.wait()
            assert self._loop is not None  # noqa: S101 - set before the event
            return self._loop


_loop_thread = _EventLoopThread()
_asgi_app: Optional[Any] = None


def _build_asgi_app() -> Any:
    """Create the FastAPI app once, failing fast on misconfiguration."""
    global _asgi_app
    if _asgi_app is None:
        _asgi_app = create_app(HostSettings.from_env())
    return _asgi_app


def _bind_middleware_to_loop() -> None:
    """Run a no-op coroutine on the adapter's loop at import time.

    `a2wsgi.ASGIMiddleware` starts its own loop thread internally; the
    touch here proves the machinery works in this process before the first
    real request arrives.
    """
    loop = _loop_thread.start()
    asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(timeout=5)


try:  # pragma: no cover - import-time eager construction by design
    application = ASGIMiddleware(_build_asgi_app())
    _bind_middleware_to_loop()
except Exception:  # noqa: BLE001 - Passenger logs the traceback and refuses to boot
    raise
