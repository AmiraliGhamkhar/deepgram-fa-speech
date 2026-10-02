"""One-off end-to-end check: the real client talking to the real host.

Not part of the test suite (it needs `fastapi`/`httpx`); run it manually
after changing the credential path:

    pip install -r host/requirements-dev.txt
    python scripts/verify_client_host.py

Only the outbound call to Deepgram is stubbed, so this exercises the real
FastAPI app, the real auth/rate-limit/HTTPS logic, and the real client
`HostSessionClient`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "host"))

os.environ["DEEPGRAM_API_KEY"] = "dg_fake_host_side_only"
os.environ["HOST_SHARED_SECRET"] = "s" * 40
os.environ["HOST_ALLOW_HTTP"] = "1"  # localhost only

import core  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from medical_stt.host_client import HostSessionClient  # noqa: E402
from medical_stt.stt.base import ErrorCategory, ProviderError  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{(' -- ' + detail) if detail else ''}")
    if not condition:
        FAILURES.append(label)


def main() -> int:
    import app as host_app

    granted = []

    def fake_grant(url, api_key, ttl, timeout):
        granted.append({"url": url, "api_key": api_key, "ttl": ttl})
        return {"access_token": "issued.jwt.token", "expires_in": 30}

    core._httpx_grant = fake_grant
    host_app.grant_session.__globals__["_httpx_grant"] = fake_grant

    service = host_app.create_app(core.HostSettings.from_env(os.environ))
    client = TestClient(service)

    print("1. health check")
    response = client.get("/healthz")
    check("healthz returns ok", response.status_code == 200 and response.json()["status"] == "ok")

    print("2. unauthenticated request is rejected")
    response = client.post("/v1/session", json={})
    check("401 without a secret", response.status_code == 401, str(response.status_code))
    response = client.post("/v1/session", headers={"Authorization": "Bearer wrong"}, json={})
    check("401 with a wrong secret", response.status_code == 401)

    print("3. authenticated request issues a session")
    secret = os.environ["HOST_SHARED_SECRET"]
    response = client.post(
        "/v1/session",
        headers={"Authorization": f"Bearer {secret}"},
        json={"ttl_seconds": 30},
    )
    body = response.json()
    check("200 with the shared secret", response.status_code == 200, str(response.status_code))
    check("token issued", body.get("access_token") == "issued.jwt.token")
    check("expiry present", body.get("expires_in") == 30)
    check("host used the Deepgram key", bool(granted) and granted[0]["api_key"] == os.environ["DEEPGRAM_API_KEY"])
    check("host called Deepgram over https", granted[0]["url"].startswith("https://"))

    print("4. the real client can consume the host")
    session = HostSessionClient(
        "http://127.0.0.1:8443", secret, opener=client_post_opener(client)
    ).fetch_session(ttl_seconds=30)
    check("client received the token", session.access_token == "issued.jwt.token")
    check("client saw the expiry", session.expires_in == 30)

    print("5. rate limiting")
    limited = host_app.create_app(
        core.HostSettings.from_env({**os.environ, "HOST_RATE_LIMIT_REQUESTS": "2"})
    )
    limited_client = TestClient(limited)
    for _ in range(2):
        limited_client.post("/v1/session", headers={"Authorization": f"Bearer {secret}"}, json={})
    response = limited_client.post(
        "/v1/session", headers={"Authorization": f"Bearer {secret}"}, json={}
    )
    check("429 after the limit", response.status_code == 429, str(response.status_code))

    print("6. no secret leaks into error text")
    try:
        HostSessionClient("http://127.0.0.1:8443", "wrong-secret", opener=client_post_opener(client)).fetch_session()
    except ProviderError as exc:
        check("auth error is not retryable", exc.category is ErrorCategory.AUTH)
        check("secret absent from message", "wrong-secret" not in str(exc))

    print("7. plaintext HTTP is refused in production mode")
    strict = host_app.create_app(
        core.HostSettings.from_env({**os.environ, "HOST_ALLOW_HTTP": "0"})
    )
    strict_client = TestClient(strict, base_url="http://stt.example.com")
    response = strict_client.post(
        "/v1/session", headers={"Authorization": f"Bearer {secret}"}, json={}
    )
    check("plain http rejected", response.status_code == 400, str(response.status_code))
    response = TestClient(strict, base_url="https://stt.example.com").post(
        "/v1/session", headers={"Authorization": f"Bearer {secret}"}, json={}
    )
    check("https accepted", response.status_code == 200, str(response.status_code))
    response = TestClient(strict, base_url="http://stt.example.com").post(
        "/v1/session",
        headers={"Authorization": f"Bearer {secret}", "X-Forwarded-Proto": "https"},
        json={},
    )
    check("https via proxy header accepted", response.status_code == 200, str(response.status_code))

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        return 1
    print("All checks passed.")
    return 0


def client_post_opener(test_client: TestClient):
    """Adapt a TestClient into the `opener(request, timeout)` shape."""

    def opener(request, timeout):
        response = test_client.request(
            request.method,
            request.full_url,
            content=request.data,
            headers=dict(request.headers),
        )
        if response.status_code >= 400:
            import urllib.error

            raise urllib.error.HTTPError(
                request.full_url, response.status_code, "error", {}, None
            )
        return response.content

    return opener


if __name__ == "__main__":
    raise SystemExit(main())
