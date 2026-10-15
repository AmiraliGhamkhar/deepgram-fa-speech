#!/usr/bin/env python3
"""F-001 evidence: the shipping redirect refusal is not pinned by any test.

Demonstrates three facts with a real loopback HTTP exchange (no external
network):

1. With the guard in place (as shipped), a 302 from the configured host is
   refused and NOTHING arrives at the redirect target.
2. With the guard removed from the *shipping* opener factory -- the exact
   mutation `M11-opener-factory-wiring` -- the client follows the redirect and
   re-sends `Authorization: Bearer <host shared secret>` and `X-Client-Id` to
   the attacker-chosen host, then treats its response as a valid session.
3. That mutation is what `python audit/repro/mutate.py M11-opener-factory-wiring`
   applies to the scratch tree: the full test suite still passes (813 passed),
   i.e. no test detects the unwiring.

The secret used here is a literal placeholder, not a real credential.
"""
from __future__ import annotations

import http.server
import json
import socketserver
import sys
import threading
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import medical_stt.host_client as host_client  # noqa: E402

FAKE_SECRET = "<placeholder>"
FAKE_CLIENT_ID = "doctor-01"

received: list[dict] = []


class _Target(http.server.BaseHTTPRequestHandler):
    """The attacker-controlled redirect target: records what arrives."""

    def _record(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length:
            self.rfile.read(length)
        received.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "x_client_id": self.headers.get("X-Client-ID"),
                "method": self.command,
            }
        )
        body = json.dumps({"access_token": "attacker-issued-token", "expires_in": 30}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        # urllib rewrites a redirected 301/302/303 POST to GET, but keeps the
        # Authorization header (only content-* headers are stripped).
        self._record()
        body = json.dumps({"access_token": "attacker-issued-token", "expires_in": 30}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        self._record()
        body = json.dumps({"access_token": "attacker-issued-token", "expires_in": 30}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:  # silence
        return


class _Redirector(http.server.BaseHTTPRequestHandler):
    """The (compromised / misconfigured) configured host: answers 302."""

    target_url = ""

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.send_response(302)
        self.send_header("Location", self.target_url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args: object) -> None:  # silence
        return


def _serve(handler: type[http.server.BaseHTTPRequestHandler]) -> tuple[socketserver.TCPServer, int]:
    server = socketserver.TCPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def _attempt() -> str:
    client = host_client.HostSessionClient(
        f"http://127.0.0.1:{redirect_port}", FAKE_SECRET, timeout=5, client_id=FAKE_CLIENT_ID
    )
    try:
        session = client.fetch_session()
        return f"returned a session: access_token={session.access_token!r}"
    except Exception as exc:  # noqa: BLE001 - the outcome is the evidence
        return f"refused: {type(exc).__name__}: {exc}"


if __name__ == "__main__":
    target_server, target_port = _serve(_Target)
    _Redirector.target_url = f"http://127.0.0.1:{target_port}/v1/session"
    redirect_server, redirect_port = _serve(_Redirector)

    print("== 1. as shipped ==")
    received.clear()
    print("client:", _attempt())
    print("requests seen by the redirect target:", len(received))

    print("\n== 2. shipping opener rebuilt WITHOUT _NoRedirectHandler (M11) ==")
    rebuilt = urllib.request.build_opener(
        urllib.request.HTTPHandler(), urllib.request.HTTPSHandler()
    )
    original_factory = host_client._OPENER_FACTORY
    original_open = host_client.HostSessionClient._default_open
    host_client._OPENER_FACTORY = rebuilt
    received.clear()
    try:
        print("client:", _attempt())
    finally:
        host_client._OPENER_FACTORY = original_factory
        host_client.HostSessionClient._default_open = original_open
    print("requests seen by the redirect target:", len(received))
    for entry in received:
        print("   target received:", entry)

    redirect_server.shutdown()
    target_server.shutdown()
