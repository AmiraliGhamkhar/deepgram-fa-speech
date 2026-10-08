"""50-client concurrency proof for the host service (Phase 13, Tests 1-2).

These tests exist to *disprove* the pre-hardening behaviour rather than to
describe the new one. Before this work the host rate-limited by client IP,
so 50 clinicians behind one hospital NAT were rejected from request #31, and
`/v1/session` performed a blocking `httpx.post()` inside `async def`, so
those 50 requests serialized on the event loop. Both are asserted against
here with 50 genuinely concurrent in-flight requests.

No real Deepgram credentials and no real Deepgram credit are involved: the
grant is stubbed at the async transport, which is the same seam the real
route uses.

Concurrency is real, not simulated. Requests are driven through
`httpx.AsyncClient` + `ASGITransport` under `asyncio.gather`, and the stub
grant deliberately awaits, so the requests genuinely overlap on the event
loop. A serialized (blocking) implementation cannot pass the timing
assertion in `test_token_issuance_does_not_block_the_event_loop`.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOST_DIR = ROOT / "host"
if str(HOST_DIR) not in sys.path:
    sys.path.insert(0, str(HOST_DIR))

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

# `host.app` builds its module-level app at import time, exactly as it does
# in production, and refuses to start without a Deepgram key. These are
# obviously fake placeholders -- no real credential is present or needed,
# and no test reaches the real Deepgram endpoint.
os.environ.setdefault("DEEPGRAM_API_KEY", "dg_fake_host_side_only")
os.environ.setdefault("HOST_SHARED_SECRET", "s" * 40)
os.environ.setdefault("HOST_ALLOW_HTTP", "1")

import core  # type: ignore[import-not-found]  # noqa: E402
import provision  # type: ignore[import-not-found]  # noqa: E402

CLIENT_COUNT = 50

#: Every simulated client authenticates from this one address -- a hospital
#: NAT. The pre-fix limiter rejected the 31st request for exactly this
#: reason.
HOSPITAL_NAT_IP = "198.51.100.77"


def _make_registry(tmp_path: Path, count: int = CLIENT_COUNT) -> Tuple[Any, Dict[str, str]]:
    """Provision `count` real client identities via the shipping tool."""
    registry_file = tmp_path / "clients.txt"
    secrets: Dict[str, str] = {}
    for _ in range(count):
        client_id = provision.generate_client_id("doctor")
        secret = provision.generate_secret()
        secrets[client_id] = secret
        with registry_file.open("a", encoding="utf-8") as handle:
            handle.write(provision.registry_line(client_id, secret) + "\n")
    return core.ClientRegistry.from_file(str(registry_file)), secrets


def _settings(tmp_path: Path, registry_file: Path, **overrides: str) -> "core.HostSettings":
    env = {
        "DEEPGRAM_API_KEY": "dg_fake_host_side_only",
        "HOST_SHARED_SECRET": "s" * 40,
        "HOST_CLIENTS_FILE": str(registry_file),
        "HOST_ALLOW_HTTP": "1",
        # Sized for the 50-client target: a 50-clinician morning surge must
        # never be throttled, while a looping client still is.
        "HOST_CLIENT_BURST": str(CLIENT_COUNT * 2),
        "HOST_CLIENT_RATE_LIMIT_REQUESTS": "60",
        "HOST_GLOBAL_BURST": str(CLIENT_COUNT * 4),
        "HOST_GLOBAL_RATE_LIMIT_REQUESTS": "600",
    }
    env.update(overrides)
    return core.HostSettings.from_env(env)


def _build_app(monkeypatch, settings: "core.HostSettings", grant=None):
    """Create the ASGI app with a stubbed Deepgram grant."""
    core_module = importlib.import_module("core")
    app_module = importlib.import_module("app")

    async def stub_grant(_self, _api_key, _ttl):
        if grant is not None:
            return await grant()
        return {"access_token": "issued-token", "expires_in": 30}

    monkeypatch.setattr(core_module.AsyncGrantClient, "grant", stub_grant)
    return app_module.create_app(settings)


async def _request(
    app: Any, client_id: str, secret: str, *, peer: str = HOSPITAL_NAT_IP
) -> Tuple[int, Dict[str, Any]]:
    """Issue one real concurrent request through the ASGI stack."""
    transport = httpx.ASGITransport(app=app, client=(peer, 51000))
    async with httpx.AsyncClient(transport=transport, base_url="https://stt.example.com") as http:
        response = await http.post(
            "/v1/session",
            headers={
                "Authorization": f"Bearer {secret}",
                "X-Client-Id": client_id,
            },
            json={},
        )
        try:
            body = response.json()
        except ValueError:
            body = {}
        return response.status_code, body


async def _concurrent_requests(
    app: Any, clients: List[Tuple[str, str]], *, peer: str = HOSPITAL_NAT_IP
) -> List[Tuple[int, Dict[str, Any]]]:
    """Fire every client's request concurrently -- genuinely overlapping."""
    return await asyncio.gather(
        *(_request(app, client_id, secret, peer=peer) for client_id, secret in clients)
    )


# ===========================================================================
# Test 1 -- 50 simultaneous session requests
# ===========================================================================


def test_50_simultaneous_session_requests_all_succeed(monkeypatch, tmp_path):
    """Test 1: 50 clients hit /v1/session at once; all 50 must succeed.

    Before the fix this returned 429 for every request after the 30th,
    because the limiter was keyed on the shared NAT address.
    """
    registry_file = tmp_path / "clients.txt"
    registry, secrets_map = _make_registry(tmp_path)
    settings = _settings(tmp_path, registry_file)
    app = _build_app(monkeypatch, settings)

    clients = list(secrets_map.items())[:CLIENT_COUNT]
    assert len(clients) == CLIENT_COUNT

    results = asyncio.run(_concurrent_requests(app, clients))

    statuses = [status for status, _body in results]
    assert len(statuses) == CLIENT_COUNT
    assert statuses.count(200) == CLIENT_COUNT, (
        f"expected {CLIENT_COUNT} successful sessions, got {statuses}"
    )
    # Zero legitimate 429s is the actual requirement of this task.
    assert statuses.count(429) == 0, "a legitimate concurrent client was rate limited"
    assert all(body.get("access_token") == "issued-token" for _status, body in results)


def test_each_successful_session_gets_a_unique_session_id(monkeypatch, tmp_path):
    """Phase 5: session ids are unique, so logs and metrics can be correlated."""
    registry_file = tmp_path / "clients.txt"
    registry, secrets_map = _make_registry(tmp_path)
    app = _build_app(monkeypatch, _settings(tmp_path, registry_file))

    results = asyncio.run(_concurrent_requests(app, list(secrets_map.items())))
    session_ids = [body.get("session_id") for _status, body in results]

    assert all(isinstance(value, str) and value for value in session_ids)
    assert len(set(session_ids)) == CLIENT_COUNT, "session ids collided"


def test_token_issuance_does_not_block_the_event_loop(monkeypatch, tmp_path):
    """The event loop must stay free while Deepgram is being called.

    50 concurrent grants, each taking 50ms of simulated upstream latency.
    Served concurrently this completes in roughly one grant's latency
    (~50ms); a blocking implementation would take 50 x 50ms = ~2.5s, because
    every request would stall the loop for the full upstream round trip.
    """
    grant_latency = 0.05

    async def slow_grant() -> Dict[str, Any]:
        await asyncio.sleep(grant_latency)
        return {"access_token": "issued-token", "expires_in": 30}

    registry_file = tmp_path / "clients.txt"
    registry, secrets_map = _make_registry(tmp_path)
    app = _build_app(monkeypatch, _settings(tmp_path, registry_file), grant=slow_grant)

    clients = list(secrets_map.items())

    # A heartbeat that only advances if the loop is free.
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    async def run() -> List[Tuple[int, Dict[str, Any]]]:
        beat = asyncio.create_task(heartbeat())
        try:
            return await _concurrent_requests(app, clients)
        finally:
            beat.cancel()

    started = time.monotonic()
    results = asyncio.run(run())
    elapsed = time.monotonic() - started

    assert all(status == 200 for status, _ in results)
    serialized = grant_latency * CLIENT_COUNT
    assert elapsed < serialized / 2, (
        f"token issuance looks serialized: {elapsed:.2f}s for {CLIENT_COUNT} "
        f"x {grant_latency}s grants (blocking would be ~{serialized:.2f}s)"
    )
    # The loop stayed responsive: it serviced timer callbacks during the run.
    # The budget is a rate (10 ticks/s) with a small floor, not a fixed count:
    # `asyncio.sleep(0.005)` actually waits ~15ms on Windows and the loop is
    # CPU-saturated here, so a free loop measures 4-8 ticks over a 60-280ms
    # run -- the old fixed `> 10` was unreachable on that platform while a
    # blocking control run measures 0 ticks (the ready queue drains before
    # timers are ever consulted). Serialization itself is pinned above.
    min_ticks = max(3, elapsed * 10)
    assert ticks >= min_ticks, (
        f"the event loop was blocked during token issuance: {ticks} heartbeat "
        f"ticks in {elapsed:.3f}s (a free loop manages at least {min_ticks:.0f})"
    )


def test_concurrent_token_latency_p95_is_within_target(monkeypatch, tmp_path):
    """Phase 15 acceptance: token acquisition p95 < 2 seconds."""
    registry_file = tmp_path / "clients.txt"
    registry, secrets_map = _make_registry(tmp_path)
    app = _build_app(monkeypatch, _settings(tmp_path, registry_file))

    samples: List[float] = []

    async def timed_requests() -> List[Tuple[int, Dict[str, Any]]]:
        transport = httpx.ASGITransport(app=app, client=(HOSPITAL_NAT_IP, 51000))
        async with httpx.AsyncClient(
            transport=transport, base_url="https://stt.example.com"
        ) as http:

            async def one(client_id: str, secret: str) -> Tuple[int, Dict[str, Any]]:
                started = time.monotonic()
                response = await http.post(
                    "/v1/session",
                    headers={"Authorization": f"Bearer {secret}", "X-Client-Id": client_id},
                    json={},
                )
                samples.append(time.monotonic() - started)
                return response.status_code, response.json()

            return await asyncio.gather(
                *(one(cid, sec) for cid, sec in secrets_map.items())
            )

    results = asyncio.run(timed_requests())
    assert all(status == 200 for status, _ in results)

    samples.sort()
    p95 = samples[min(len(samples) - 1, int(len(samples) * 0.95))]
    assert p95 < 2.0, f"token acquisition p95 was {p95:.3f}s (target < 2s)"


# ===========================================================================
# Test 2 -- the same NAT
# ===========================================================================


def test_50_clients_behind_one_nat_are_all_allowed(monkeypatch, tmp_path):
    """Test 2a: 50 distinct clients, one source IP, every one allowed."""
    registry_file = tmp_path / "clients.txt"
    registry, secrets_map = _make_registry(tmp_path)
    app = _build_app(monkeypatch, _settings(tmp_path, registry_file))

    clients = list(secrets_map.items())
    # Explicitly one shared address, and more requests than the old 30/min
    # per-IP default: the exact case that used to fail.
    results = asyncio.run(_concurrent_requests(app, clients, peer=HOSPITAL_NAT_IP))

    assert [status for status, _ in results].count(200) == CLIENT_COUNT
    assert [status for status, _ in results].count(429) == 0


def test_one_abusive_client_is_throttled_without_affecting_the_others(
    monkeypatch, tmp_path
):
    """Test 2b: one looping client is limited; the other 49 are unaffected."""
    registry_file = tmp_path / "clients.txt"
    registry, secrets_map = _make_registry(tmp_path)
    # A deliberately tiny per-client burst so abuse is easy to provoke.
    settings = _settings(
        tmp_path, registry_file, HOST_CLIENT_BURST="3", HOST_CLIENT_RATE_LIMIT_REQUESTS="1"
    )
    app = _build_app(monkeypatch, settings)

    clients = list(secrets_map.items())
    abusive_id, abusive_secret = clients[0]
    legitimate = clients[1:]

    async def run() -> Tuple[List[int], List[int]]:
        transport = httpx.ASGITransport(app=app, client=(HOSPITAL_NAT_IP, 51000))
        async with httpx.AsyncClient(
            transport=transport, base_url="https://stt.example.com"
        ) as http:

            async def one(client_id: str, secret: str) -> int:
                response = await http.post(
                    "/v1/session",
                    headers={
                        "Authorization": f"Bearer {secret}",
                        "X-Client-Id": client_id,
                    },
                    json={},
                )
                return response.status_code

            abuse = await asyncio.gather(
                *(one(abusive_id, abusive_secret) for _ in range(25))
            )
            others = await asyncio.gather(*(one(cid, sec) for cid, sec in legitimate))
            return abuse, others

    abuse_statuses, other_statuses = asyncio.run(run())

    assert abuse_statuses.count(429) > 0, "the abusive client was never throttled"
    # The decisive assertion: limiting one client does not penalize the 49
    # colleagues sharing its NAT address.
    assert other_statuses.count(200) == len(legitimate), (
        f"throttling one client blocked legitimate clients: {other_statuses}"
    )


def test_failed_authentication_is_limited_per_ip(monkeypatch, tmp_path):
    """Brute force stays IP-scoped: a shared address must not exempt it."""
    registry_file = tmp_path / "clients.txt"
    registry, secrets_map = _make_registry(tmp_path)
    settings = _settings(tmp_path, registry_file, HOST_AUTH_FAILURE_LIMIT="5")
    app = _build_app(monkeypatch, settings)

    async def run() -> List[int]:
        transport = httpx.ASGITransport(app=app, client=(HOSPITAL_NAT_IP, 51000))
        async with httpx.AsyncClient(
            transport=transport, base_url="https://stt.example.com"
        ) as http:

            async def attempt() -> int:
                response = await http.post(
                    "/v1/session",
                    headers={
                        "Authorization": "Bearer definitely-not-the-secret",
                        "X-Client-Id": "doctor-attacker",
                    },
                    json={},
                )
                return response.status_code

            return await asyncio.gather(*(attempt() for _ in range(12)))

    statuses = asyncio.run(run())
    assert statuses.count(429) > 0, "brute force was never rate limited"
    assert statuses.count(401) > 0, "no request was actually authenticated"


def test_unknown_client_id_cannot_borrow_another_clients_secret(monkeypatch, tmp_path):
    """A valid secret presented under the wrong id must not authenticate."""
    registry_file = tmp_path / "clients.txt"
    registry, secrets_map = _make_registry(tmp_path)
    app = _build_app(monkeypatch, _settings(tmp_path, registry_file))

    client_id, secret = list(secrets_map.items())[0]

    async def run() -> int:
        transport = httpx.ASGITransport(app=app, client=(HOSPITAL_NAT_IP, 51000))
        async with httpx.AsyncClient(
            transport=transport, base_url="https://stt.example.com"
        ) as http:
            response = await http.post(
                "/v1/session",
                headers={
                    "Authorization": f"Bearer {secret}",
                    "X-Client-Id": "doctor-someone-else",
                },
                json={},
            )
            return response.status_code

    assert asyncio.run(run()) == 401


def test_client_id_is_mandatory_once_clients_are_provisioned(monkeypatch, tmp_path):
    """After migration, omitting the header must not fall back to legacy."""
    registry_file = tmp_path / "clients.txt"
    registry, secrets_map = _make_registry(tmp_path, count=1)
    app = _build_app(monkeypatch, _settings(tmp_path, registry_file))
    client_id, secret = list(secrets_map.items())[0]

    async def run() -> int:
        transport = httpx.ASGITransport(app=app, client=(HOSPITAL_NAT_IP, 51000))
        async with httpx.AsyncClient(
            transport=transport, base_url="https://stt.example.com"
        ) as http:
            response = await http.post(
                "/v1/session",
                headers={"Authorization": f"Bearer {secret}"},
                json={},
            )
            return response.status_code

    assert asyncio.run(run()) == 401


# ===========================================================================
# Test 6 (short form) -- resource bounds under concurrency
# ===========================================================================


def test_repeated_connect_disconnect_shows_no_growth_in_host_state(monkeypatch, tmp_path):
    """Phase 6: 50 clients x 5 sequential sessions must not accumulate state.

    250 sessions are issued in total. Afterwards the limiter tables must hold
    at most one bucket per known client -- no per-request growth -- and the
    metrics label cardinality must stay bounded.
    """
    registry_file = tmp_path / "clients.txt"
    registry, secrets_map = _make_registry(tmp_path)
    # Sized for the 250 requests this test issues, so the global ceiling is
    # not what is being measured here.
    settings = _settings(
        tmp_path,
        registry_file,
        HOST_CLIENT_BURST="20",
        HOST_CLIENT_RATE_LIMIT_REQUESTS="600",
        HOST_GLOBAL_BURST="400",
        HOST_GLOBAL_RATE_LIMIT_REQUESTS="600",
    )
    app = _build_app(monkeypatch, settings)

    clients = list(secrets_map.items())

    async def run() -> None:
        for _round in range(5):
            results = await _concurrent_requests(app, clients)
            assert [status for status, _ in results].count(200) == CLIENT_COUNT

    asyncio.run(run())

    # One bucket per client, never one per request.
    assert app.state.client_limiter.tracked_keys == CLIENT_COUNT
    assert app.state.global_limiter.tracked_keys == 1
    assert app.state.inflight.in_flight == 0, "an in-flight grant slot leaked"
    assert len(registry) == CLIENT_COUNT
    assert app.state.metrics.tracked_client_labels <= CLIENT_COUNT
    # Counters grow (that is their job); the *series* count must not.
    assert app.state.metrics.counter_value("session_success_total") == CLIENT_COUNT * 5


def test_limiter_state_is_bounded_under_a_flood_of_fabricated_client_ids(monkeypatch, tmp_path):
    """Phase 6: unbounded key growth must be impossible, even under abuse."""
    import core as core_module

    limiter = core_module.TokenBucketRateLimiter(
        capacity=10, refill_per_second=1.0, max_tracked_keys=64, purge_interval_calls=1
    )
    for index in range(5_000):
        limiter.allow(f"attacker-{index}")
    assert limiter.tracked_keys <= 64

    # The fixed-window limiter used for brute-force must be bounded too.
    window_limiter = core_module.RateLimiter(limit=5, window_seconds=60, max_tracked_clients=32)
    for index in range(5_000):
        window_limiter.allow(f"attacker-{index}")
    assert window_limiter.tracked_clients <= 32


def test_oversized_request_bodies_are_refused(monkeypatch, tmp_path):
    """Phase 6: no user-controlled input may cause unbounded memory growth."""
    registry_file = tmp_path / "clients.txt"
    registry, secrets_map = _make_registry(tmp_path, count=1)
    settings = _settings(tmp_path, registry_file, HOST_MAX_REQUEST_BODY_BYTES="256")
    app = _build_app(monkeypatch, settings)
    client_id, secret = list(secrets_map.items())[0]

    async def run() -> int:
        transport = httpx.ASGITransport(app=app, client=(HOSPITAL_NAT_IP, 51000))
        async with httpx.AsyncClient(
            transport=transport, base_url="https://stt.example.com"
        ) as http:
            response = await http.post(
                "/v1/session",
                headers={"Authorization": f"Bearer {secret}", "X-Client-Id": client_id},
                content=b'{"ttl_seconds":30,"pad":"' + b"A" * 4096 + b'"}',
            )
            return response.status_code

    assert asyncio.run(run()) == 413
