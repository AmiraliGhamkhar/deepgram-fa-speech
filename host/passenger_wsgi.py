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
The loop is handed to `ASGIMiddleware(loop=...)` explicitly: with no `loop`
argument a2wsgi starts a second loop thread of its own, and the one built
here would sit idle while the app ran on the other.

One app, not two: importing `host.app` already builds the module-level
`app = create_app()` (that is the fail-fast behaviour described below), so
this module reuses it instead of calling `create_app()` again. A second app
means a second `AsyncGrantClient` -- a whole second httpx connection pool to
Deepgram that nothing ever closes -- plus a second metrics registry and a
second set of rate limiters, so `/metrics` and the throttling state would
describe an instance that is not the one serving requests.

a2wsgi does not implement the ASGI lifespan protocol, so the app's startup
log line and its shutdown handler (`grant_client.aclose()`) never run here.
That is acceptable rather than something to work around: Passenger recycles
by killing the process, and the OS reclaims the sockets. It is also why the
capacity gauges and `app.state.grant_client` are built in `create_app` and
not in `lifespan`.

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
    from . import app as _host_app
    from .app import create_app
    from .core import HostSettings
except ImportError:  # pragma: no cover - cPanel copies only the host/ dir
    import app as _host_app  # type: ignore[no-index]
    from app import create_app
    from core import HostSettings


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
    """Return the FastAPI app once, failing fast on misconfiguration.

    Reuses the app `host.app` built at import time rather than constructing a
    second one -- see the module docstring for what a second app costs.
    `create_app` stays as the fallback for a caller that imported this module
    without `host.app` having built one (nothing does today, but the entry
    point must not fail because of an import-order assumption).
    """
    global _asgi_app
    if _asgi_app is None:
        already_built = getattr(_host_app, "app", None)
        _asgi_app = already_built if already_built is not None else create_app(HostSettings.from_env())
    return _asgi_app


def _smoke_test_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Run a no-op coroutine on the loop that will serve requests.

    Proves the loop thread is actually running in this process before the
    first real request arrives, so a failure shows up in Passenger's boot log
    instead of as a timeout on the clinic's first Start press.
    """
    asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(timeout=5)


def _build_application() -> Any:
    loop = _loop_thread.start()
    application = ASGIMiddleware(_build_asgi_app(), loop=loop)
    _smoke_test_loop(loop)
    return application


try:  # pragma: no cover - import-time eager construction by design
    application = _build_application()
except Exception:  # noqa: BLE001 - Passenger logs the traceback and refuses to boot
    raise
