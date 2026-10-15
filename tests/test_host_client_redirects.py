"""The desktop session client must never forward its secret through a redirect."""
from __future__ import annotations

import http.server
import json
import threading
from contextlib import contextmanager
from typing import Iterator

import pytest

from medical_stt.host_client import HostSessionClient, _NoRedirectHandler, _OPENER_FACTORY
from medical_stt.stt.base import ErrorCategory, ProviderError


@contextmanager
def _serve(handler: type[http.server.BaseHTTPRequestHandler]) -> Iterator[http.server.ThreadingHTTPServer]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_default_opener_refuses_redirect_without_contacting_target() -> None:
    secret = "synthetic-session-client-secret-for-test"
    source_requests: list[dict[str, object]] = []
    target_requests: list[dict[str, object]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def _record(self, destination: list[dict[str, object]]) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            destination.append({
                "path": self.path,
                "method": self.command,
                "body": self.rfile.read(length),
                "headers": dict(self.headers),
            })

        def log_message(self, *_args: object) -> None:
            return

    class Target(Handler):
        def do_GET(self) -> None:
            self._respond()

        def do_POST(self) -> None:
            self._respond()

        def _respond(self) -> None:
            self._record(target_requests)
            payload = json.dumps({"access_token": "not-a-real-token", "expires_in": 30}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    class Redirector(Handler):
        target_url = ""

        def do_POST(self) -> None:
            self._record(source_requests)
            self.send_response(302)
            self.send_header("Location", self.target_url)
            self.send_header("Content-Length", "0")
            self.end_headers()

    with _serve(Target) as target:
        Redirector.target_url = f"http://127.0.0.1:{target.server_port}/v1/session"
        with _serve(Redirector) as redirect:
            client = HostSessionClient(
                f"http://127.0.0.1:{redirect.server_port}", secret, timeout=5, client_id="test-device"
            )
            # Exercise the shipping opener, not an injected stub. The loopback
            # HTTP target is the same mechanism as audit/repro/F-001_redirect_leak.py.
            with pytest.raises(ProviderError) as error:
                client.fetch_session()

    assert error.value.category is ErrorCategory.CONFIG
    assert len(source_requests) == 1
    assert target_requests == []
    request = source_requests[0]
    headers = dict(request["headers"])
    assert headers.pop("Authorization") == f"Bearer {secret}"
    assert secret not in repr({**request, "headers": headers})
    assert secret not in str(error.value)


def test_shipping_opener_installs_no_redirect_handler() -> None:
    # urllib's OpenerDirector.handlers is a list, not a dict/values mapping.
    assert any(isinstance(handler, _NoRedirectHandler) for handler in _OPENER_FACTORY.handlers)
