"""ASGI-level tests for the host service (host/app.py).

These prove the HTTPS enforcement middleware end to end: a direct client
whose plaintext request carries a forged `X-Forwarded-Proto: https` is
still rejected, while a configured reverse proxy is trusted.

Skipped when FastAPI is not installed (it is a host-only dependency); CI
installs `host/requirements-dev.txt` so the tests do run there.
"""
from __future__ import annotations

import hashlib
import importlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOST_DIR = ROOT / "host"

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, str(HOST_DIR))

SECRET = "s" * 40
FAKE_GRANT = {"access_token": "issued-token", "expires_in": 30}


@pytest.fixture()
def host(monkeypatch):
    """Import (or reload) the host ASGI app with test configuration.

    The Deepgram grant is stubbed at the *async* transport
    (`AsyncGrantClient.grant`) because Phase 3 moved token issuance onto the
    event loop via a reused `httpx.AsyncClient`. The stub returns a fixed
    payload, so no real Deepgram credit is ever spent in CI.
    """
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg_fake_host_side_only")
    monkeypatch.setenv("HOST_SHARED_SECRET", SECRET)
    monkeypatch.delenv("HOST_ALLOW_HTTP", raising=False)
    monkeypatch.delenv("HOST_FORWARDED_ALLOW_IPS", raising=False)
    monkeypatch.delenv("HOST_CLIENTS_FILE", raising=False)

    core = importlib.import_module("core")
    app_module = importlib.import_module("app")
    importlib.reload(app_module)

    async def fake_grant(_self, _api_key, _ttl):
        return dict(FAKE_GRANT)

    monkeypatch.setattr(core.AsyncGrantClient, "grant", fake_grant)

    service = app_module.create_app(core.HostSettings.from_env())
    return service


def _post(client: TestClient, **kwargs):
    headers = {"Authorization": f"Bearer {SECRET}"}
    headers.update(kwargs.pop("headers", {}))
    return client.post("/v1/session", headers=headers, json={}, **kwargs)


# -- HTTPS enforcement matrix --------------------------------------------


def test_direct_plain_http_is_rejected(host):
    client = TestClient(host, base_url="http://stt.example.com", client=("203.0.113.9", 51000))
    assert _post(client).status_code == 400


def test_direct_http_with_forged_forwarded_proto_is_rejected(host):
    client = TestClient(host, base_url="http://stt.example.com", client=("203.0.113.9", 51000))
    response = _post(client, headers={"X-Forwarded-Proto": "https"})
    assert response.status_code == 400, "a client must not be able to forge HTTPS"


def test_direct_https_is_allowed(host):
    client = TestClient(host, base_url="https://stt.example.com", client=("203.0.113.9", 51000))
    assert _post(client).status_code == 200


def test_trusted_proxy_with_forwarded_proto_is_allowed(host):
    client = TestClient(host, base_url="http://stt.example.com", client=("127.0.0.1", 51000))
    assert _post(client, headers={"X-Forwarded-Proto": "https"}).status_code == 200


def test_trusted_proxy_without_the_header_is_rejected(host):
    client = TestClient(host, base_url="http://stt.example.com", client=("127.0.0.1", 51000))
    assert _post(client).status_code == 400


def test_untrusted_proxy_with_forwarded_proto_is_rejected(host):
    client = TestClient(host, base_url="http://stt.example.com", client=("198.51.100.7", 51000))
    assert _post(client, headers={"X-Forwarded-Proto": "https"}).status_code == 400


def test_forwarded_proto_from_a_second_hop_is_not_trusted(host):
    """Only the immediate peer decides; a chain cannot smuggle trust."""
    client = TestClient(host, base_url="http://stt.example.com", client=("203.0.113.9", 51000))
    response = _post(
        client,
        headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-For": "127.0.0.1",
        },
    )
    assert response.status_code == 400


def test_healthz_is_not_exempt_from_the_https_check(host):
    client = TestClient(host, base_url="http://stt.example.com", client=("203.0.113.9", 51000))
    assert client.get("/healthz").status_code == 400


# -- route behaviour -----------------------------------------------------


def test_healthz_returns_ok(host):
    client = TestClient(host, base_url="https://stt.example.com")
    response = client.get("/healthz")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


def test_wrong_secret_is_rejected(host):
    client = TestClient(host, base_url="https://stt.example.com")
    response = client.post("/v1/session", headers={"Authorization": "Bearer nope"}, json={})
    assert response.status_code == 401


def test_session_returns_the_issued_token(host):
    client = TestClient(host, base_url="https://stt.example.com")
    body = _post(client).json()
    assert body["access_token"] == "issued-token"
    assert body["expires_in"] == 30
    # Phase 5: every session carries a correlation id for logs and metrics.
    assert len(body["session_id"]) == 32


def test_upstream_timeout_is_reported_as_504(host, monkeypatch):
    core = importlib.import_module("core")

    async def failing_transport(_self, _api_key, _ttl):
        raise core.GrantError("Deepgram token request timed out", 504)

    monkeypatch.setattr(core.AsyncGrantClient, "grant", failing_transport)
    client = TestClient(host, base_url="https://stt.example.com")
    response = _post(client)
    assert response.status_code == 504
    assert "timed out" in response.json()["detail"]


def test_upstream_unavailable_is_reported_as_503(host, monkeypatch):
    core = importlib.import_module("core")

    async def failing_transport(_self, _api_key, _ttl):
        raise core.GrantError("cannot reach Deepgram", 503)

    monkeypatch.setattr(core.AsyncGrantClient, "grant", failing_transport)
    client = TestClient(host, base_url="https://stt.example.com")
    assert _post(client).status_code == 503


def test_rate_limit_returns_429_with_retry_after(host, monkeypatch):
    """The per-client token bucket sheds a looping client with Retry-After.

    Phase 4 replaced the old fixed-window per-IP counter with a per-client
    token bucket; the observable contract (429 plus a usable Retry-After) is
    deliberately unchanged, so this regression test still pins it.
    """
    core = importlib.import_module("core")
    app_module = importlib.import_module("app")

    async def fake_grant(_self, _api_key, _ttl):
        return dict(FAKE_GRANT)

    monkeypatch.setattr(core.AsyncGrantClient, "grant", fake_grant)
    service = app_module.create_app(
        core.HostSettings.from_env(
            {**os.environ, "HOST_CLIENT_BURST": "2", "HOST_CLIENT_RATE_LIMIT_REQUESTS": "1"}
        )
    )
    client = TestClient(service, base_url="https://stt.example.com")
    assert _post(client).status_code == 200
    assert _post(client).status_code == 200
    limited = _post(client)
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1


def test_allow_http_serves_plaintext_for_development(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg_fake_host_side_only")
    monkeypatch.setenv("HOST_SHARED_SECRET", SECRET)
    monkeypatch.setenv("HOST_ALLOW_HTTP", "1")
    core = importlib.import_module("core")
    app_module = importlib.import_module("app")
    service = app_module.create_app(core.HostSettings.from_env())
    client = TestClient(service, base_url="http://localhost:8443")
    assert client.get("/healthz").status_code == 200


# -- failed-authentication attribution -----------------------------------
#
# A WSGI/Passenger environ can reach the app with no peer address at all
# (`a2wsgi` sets `scope["client"]` only when both REMOTE_ADDR and
# REMOTE_PORT are present). Keying the brute-force budget on a placeholder
# merged every such request into one bucket, so 20 failures from anywhere
# locked every clinician out for a whole window: an anti-abuse control that
# works as a denial of service.


def _unattributable_service(monkeypatch, **overrides):
    """An app whose Deepgram grant is stubbed and whose auth budget is tiny."""
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg_fake_host_side_only")
    monkeypatch.setenv("HOST_SHARED_SECRET", SECRET)
    monkeypatch.setenv("HOST_ALLOW_HTTP", "1")
    monkeypatch.delenv("HOST_CLIENTS_FILE", raising=False)
    core = importlib.import_module("core")
    app_module = importlib.import_module("app")
    importlib.reload(app_module)

    async def fake_grant(_self, _api_key, _ttl):
        return dict(FAKE_GRANT)

    monkeypatch.setattr(core.AsyncGrantClient, "grant", fake_grant)
    env = {**os.environ, "HOST_AUTH_FAILURE_LIMIT": "2", **overrides}
    return app_module.create_app(core.HostSettings.from_env(env)), core


def test_unattributable_failed_auth_cannot_lock_out_legitimate_clients(monkeypatch):
    service, _core = _unattributable_service(monkeypatch)
    # client=None reproduces a deployment where the server supplies no peer.
    client = TestClient(service, base_url="http://stt.example.com", client=None)

    # More failures than the brute-force budget: every one is a real
    # authentication failure (401), never a lockout that would also apply to
    # somebody else.
    for _ in range(4):
        response = client.post(
            "/v1/session", headers={"Authorization": "Bearer wrong-secret"}, json={}
        )
        assert response.status_code == 401, response.status_code

    assert _post(client).status_code == 200, "a legitimate client was locked out"


def test_unattributable_failed_auth_is_still_limited_per_client_id(monkeypatch, tmp_path):
    """The budget is not dropped, only re-attributed, when a client id exists."""
    registry = tmp_path / "clients.txt"
    secrets = {}
    lines = []
    for client_id in ("doctor-01", "doctor-02"):
        secret = f"secret-for-{client_id}-0123456789abcdef"
        secrets[client_id] = secret
        digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        lines.append(f"{client_id}:{digest}")
    registry.write_text("\n".join(lines) + "\n", encoding="utf-8")

    service, _core = _unattributable_service(
        monkeypatch, HOST_CLIENTS_FILE=str(registry)
    )
    client = TestClient(service, base_url="http://stt.example.com", client=None)

    # HOST_AUTH_FAILURE_LIMIT=2: the first two failures charge doctor-01's
    # own budget, and from then on that identity is locked out.
    for _ in range(2):
        response = client.post(
            "/v1/session",
            headers={"Authorization": "Bearer not-the-secret", "X-Client-Id": "doctor-01"},
            json={},
        )
        assert response.status_code == 401, response.status_code

    # doctor-01 exhausted its own budget, so even its correct secret is
    # refused until the window resets...
    locked = client.post(
        "/v1/session",
        headers={
            "Authorization": f"Bearer {secrets['doctor-01']}",
            "X-Client-Id": "doctor-01",
        },
        json={},
    )
    assert locked.status_code == 429
    assert int(locked.headers["Retry-After"]) >= 1

    # ...while a colleague on the same peer-less deployment is unaffected.
    colleague = client.post(
        "/v1/session",
        headers={
            "Authorization": f"Bearer {secrets['doctor-02']}",
            "X-Client-Id": "doctor-02",
        },
        json={},
    )
    assert colleague.status_code == 200


# -- metrics accounting ---------------------------------------------------


def test_capacity_gauges_are_published_without_running_the_lifespan(host):
    """A WSGI deployment never runs the ASGI lifespan, so these are set at
    build time: otherwise they would read as a permanent zero on cPanel."""
    client = TestClient(host, base_url="https://stt.example.com", client=("203.0.113.9", 51000))
    body = client.get("/metrics").text
    assert "registry_clients 1" in body
    assert "inflight_grants_capacity 100" in body
    assert "inflight_grants 0" in body


def test_metrics_auth_failure_is_not_counted_as_a_session_auth_failure(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg_fake_host_side_only")
    monkeypatch.setenv("HOST_SHARED_SECRET", SECRET)
    monkeypatch.setenv("HOST_ALLOW_HTTP", "1")
    monkeypatch.setenv("HOST_METRICS_ADMIN_TOKEN", "admin-token")
    core = importlib.import_module("core")
    app_module = importlib.import_module("app")
    importlib.reload(app_module)
    service = app_module.create_app(core.HostSettings.from_env())
    client = TestClient(service, base_url="http://stt.example.com")

    assert client.get("/metrics").status_code == 401
    body = client.get("/metrics", headers={"Authorization": "Bearer admin-token"}).text

    assert "metrics_auth_failures_total 1" in body
    assert "auth_failures_total 0" in body, (
        "a scraper with a stale token must not look like credential brute forcing"
    )


@pytest.mark.parametrize(
    ("status", "expected_connection_failures"),
    [(502, 0), (503, 1), (504, 1)],
)
def test_upstream_failures_are_counted_as_deepgram_connection_failures(
    host, monkeypatch, status, expected_connection_failures
):
    core = importlib.import_module("core")

    async def failing(_self, _api_key, _ttl):
        raise core.GrantError("upstream problem", status)

    monkeypatch.setattr(core.AsyncGrantClient, "grant", failing)
    client = TestClient(host, base_url="https://stt.example.com", client=("203.0.113.9", 51000))
    assert _post(client).status_code == status

    body = client.get("/metrics").text
    assert f"deepgram_connection_failures_total {expected_connection_failures}" in body
    assert "token_request_failures_total 1" in body


def test_declared_oversized_body_is_counted_like_a_chunked_one(host):
    """Both refusal paths must be observable, not just the streaming one."""
    client = TestClient(host, base_url="https://stt.example.com", client=("203.0.113.9", 51000))
    response = client.post(
        "/v1/session",
        headers={"Authorization": f"Bearer {SECRET}", "Content-Length": "999999"},
        content=b'{"ttl_seconds": 30}',
    )
    assert response.status_code == 413
    assert "request_body_rejections_total 1" in client.get("/metrics").text
