"""ASGI-level tests for the host service (host/app.py).

These prove the HTTPS enforcement middleware end to end: a direct client
whose plaintext request carries a forged `X-Forwarded-Proto: https` is
still rejected, while a configured reverse proxy is trusted.

Skipped when FastAPI is not installed (it is a host-only dependency); CI
installs `host/requirements-dev.txt` so the tests do run there.
"""
from __future__ import annotations

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
    """Import (or reload) the host ASGI app with test configuration."""
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg_fake_host_side_only")
    monkeypatch.setenv("HOST_SHARED_SECRET", SECRET)
    monkeypatch.delenv("HOST_ALLOW_HTTP", raising=False)
    monkeypatch.delenv("HOST_FORWARDED_ALLOW_IPS", raising=False)

    core = importlib.import_module("core")
    app_module = importlib.import_module("app")
    importlib.reload(app_module)
    monkeypatch.setattr(core, "_httpx_grant", lambda *a, **k: dict(FAKE_GRANT))

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


def test_session_returns_the_issued_token(host, monkeypatch):
    core = importlib.import_module("core")
    monkeypatch.setattr(core, "_httpx_grant", lambda *a, **k: dict(FAKE_GRANT))
    client = TestClient(host, base_url="https://stt.example.com")
    body = _post(client).json()
    assert body == {"access_token": "issued-token", "expires_in": 30}


def test_upstream_timeout_is_reported_as_504(host, monkeypatch):
    core = importlib.import_module("core")

    def failing_transport(*_args, **_kwargs):
        raise core.GrantError("Deepgram token request timed out", 504)

    monkeypatch.setattr(core, "_httpx_grant", failing_transport)
    client = TestClient(host, base_url="https://stt.example.com")
    response = _post(client)
    assert response.status_code == 504
    assert "timed out" in response.json()["detail"]


def test_upstream_unavailable_is_reported_as_503(host, monkeypatch):
    core = importlib.import_module("core")

    def failing_transport(*_args, **_kwargs):
        raise core.GrantError("cannot reach Deepgram", 503)

    monkeypatch.setattr(core, "_httpx_grant", failing_transport)
    client = TestClient(host, base_url="https://stt.example.com")
    assert _post(client).status_code == 503


def test_rate_limit_returns_429_with_retry_after(host, monkeypatch):
    core = importlib.import_module("core")
    service = importlib.import_module("app").create_app(
        core.HostSettings.from_env({**os.environ, "HOST_RATE_LIMIT_REQUESTS": "2"})
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
