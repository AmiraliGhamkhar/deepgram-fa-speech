"""The cPanel/Passenger WSGI entry point, exercised as a real WSGI callable.

These run in a *subprocess*: `host/passenger_wsgi.py` builds its application
at import time and starts a dedicated event-loop thread, so importing it
inside the test process would leave both alive for the rest of the session and
would race with whatever environment other host tests happen to have set.

Everything here is offline. The Deepgram grant is never reached -- only
`/healthz`, which needs no upstream.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("a2wsgi")

ROOT = Path(__file__).resolve().parent.parent

# Imported in the child, before passenger_wsgi, so the child can compare
# object identities across the two modules.
_PROBE = textwrap.dedent(
    """
    import io, json, sys

    sys.path.insert(0, %r)

    import app as host_app
    import passenger_wsgi as pw

    facts = {
        "same_app": pw.application.app is host_app.app,
        "same_grant_client": (
            pw.application.app.state.grant_client is host_app.app.state.grant_client
        ),
        "same_metrics": pw.application.app.state.metrics is host_app.app.state.metrics,
        "middleware_uses_dedicated_loop": pw.application.loop is pw._loop_thread._loop,
        "loop_is_running": pw.application.loop.is_running(),
    }

    # Drive one real WSGI request through the bridge. This is the only proof
    # that the loop handed to ASGIMiddleware can actually serve the app: a
    # middleware bound to a loop that is not running would hang here.
    environ = {
        "REQUEST_METHOD": "GET",
        "SCRIPT_NAME": "",
        "PATH_INFO": "/healthz",
        "QUERY_STRING": "",
        "SERVER_NAME": "host.example.com",
        "SERVER_PORT": "443",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": "https",
        "wsgi.input": io.BytesIO(b""),
        "wsgi.errors": sys.stderr,
        "wsgi.multithread": True,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
        "REMOTE_ADDR": "203.0.113.7",
        "REMOTE_PORT": "51000",
        "HTTP_HOST": "host.example.com",
    }
    status_holder = {}

    def start_response(status, headers, exc_info=None):
        status_holder["status"] = status
        status_holder["headers"] = headers

    body = b"".join(pw.application(environ, start_response))
    facts["healthz_status"] = int(status_holder["status"].split(" ", 1)[0])
    facts["healthz_body"] = json.loads(body.decode("utf-8"))
    facts["healthz_headers"] = dict(status_holder["headers"])

    print(json.dumps(facts))
    """
)


def _run_probe() -> dict:
    env = dict(os.environ)
    env.update(
        {
            # A syntactically valid key and secret: nothing here talks to
            # Deepgram, but HostSettings.from_env() refuses to build an app
            # without them, and refusing is the behaviour the entry point's
            # "fail fast at deploy time" docstring promises.
            "DEEPGRAM_API_KEY": "dg_test_key_not_real",
            "HOST_SHARED_SECRET": "wsgi-probe-secret-" + "x" * 16,
            "HOST_ALLOW_HTTP": "1",
            "HOST_METRICS_ADMIN_TOKEN": "",
            "PYTHONPATH": str(ROOT / "host"),
        }
    )
    # The child imports the flat `app`/`passenger_wsgi` module names, exactly
    # as cPanel does when only the host/ directory is copied.
    env.pop("HOST_CLIENTS_FILE", None)
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE % str(ROOT / "host")],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def probe() -> dict:
    return _run_probe()


def test_the_wsgi_entry_point_serves_healthz(probe):
    """The bridge works end to end, not just at import time."""
    assert probe["healthz_status"] == 200
    assert probe["healthz_body"]["status"] == "ok"


def test_the_wsgi_entry_point_reuses_the_single_built_app(probe):
    """One app, one connection pool, one metrics registry.

    `host/app.py` builds `app = create_app()` at import time. Calling
    `create_app()` again here produced a *second* FastAPI app: a second
    `AsyncGrantClient` (a whole second httpx pool to Deepgram that nothing
    ever closes), a second metrics registry and a second set of rate
    limiters. Requests were served by the copy, so `/metrics` and the
    throttling state described an instance that was not serving anything.
    """
    assert probe["same_app"] is True
    assert probe["same_grant_client"] is True
    assert probe["same_metrics"] is True


def test_the_dedicated_loop_thread_is_the_one_serving_requests(probe):
    """The module's own loop must be the loop the middleware runs on.

    `ASGIMiddleware` was constructed without `loop=`, so a2wsgi started a
    second loop thread and the one built here -- the entire justification for
    this module's design, since a reused `httpx.AsyncClient` must live on one
    loop -- sat idle doing nothing but a startup `asyncio.sleep(0)`.
    """
    assert probe["middleware_uses_dedicated_loop"] is True
    assert probe["loop_is_running"] is True


def test_the_wsgi_response_carries_the_hardening_headers(probe):
    """Security headers must survive the WSGI bridge, not only uvicorn."""
    headers = {name.lower(): value for name, value in probe["healthz_headers"].items()}
    assert headers.get("x-content-type-options") == "nosniff"
    assert headers.get("referrer-policy")
