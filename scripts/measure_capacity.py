#!/usr/bin/env python3
"""Measure the 50-concurrent-session acceptance criteria and report PASS/FAIL.

Runs entirely against a **mocked** Deepgram grant, so it costs nothing and
needs no credentials. It reports the numbers from README > Capacity, which
an operator can re-run after any change to the host.

    python scripts/measure_capacity.py            # 50 clients
    python scripts/measure_capacity.py --clients 25

What it measures, and the thresholds it judges (Phase 15 of the hardening
work):

    50 simultaneous session requests   >= 99% successful, 0 legitimate 429
    token acquisition p95              < 2s
    event loop during token issuance   not blocked (50 x 50ms in << 2.5s)
    memory growth per completed session  none proportional to session count
    active sessions after shutdown     == 0

Real Deepgram latency is explicitly NOT measured here and is not claimed:
this is a local harness against a stub. Use the opt-in
`tests/test_live_load.py` for anything about the real provider.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import resource
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
HOST_DIR = ROOT / "host"
sys.path.insert(0, str(HOST_DIR))

os.environ.setdefault("DEEPGRAM_API_KEY", "dg_fake_host_side_only")
os.environ.setdefault("HOST_SHARED_SECRET", "s" * 40)
os.environ.setdefault("HOST_ALLOW_HTTP", "1")

import core  # type: ignore[import-not-found]  # noqa: E402
import provision  # type: ignore[import-not-found]  # noqa: E402
from app import create_app  # type: ignore[import-not-found]  # noqa: E402

#: Mocked upstream latency, so "is the loop blocked?" is a real question.
GRANT_LATENCY_SECONDS = 0.05

#: The NAT address all simulated clinicians share.
NAT_IP = "198.51.100.77"


def rss_bytes() -> int:
    try:
        with open("/proc/self/statm", encoding="utf-8") as handle:
            return int(handle.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, IndexError, ValueError):
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def build_clients(count: int, tmp: Path) -> Dict[str, str]:
    """Provision `count` real client identities."""
    registry = tmp / "clients.txt"
    clients: Dict[str, str] = {}
    with registry.open("w", encoding="utf-8") as handle:
        for _ in range(count):
            client_id = provision.generate_client_id("doctor")
            secret = provision.generate_secret()
            clients[client_id] = secret
            handle.write(provision.registry_line(client_id, secret) + "\n")
    return clients


def make_app(clients: Dict[str, str], tmp: Path, burst: int) -> Any:
    settings = core.HostSettings.from_env(
        {
            "DEEPGRAM_API_KEY": "dg_fake_host_side_only",
            "HOST_SHARED_SECRET": "s" * 40,
            "HOST_CLIENTS_FILE": str(tmp / "clients.txt"),
            "HOST_ALLOW_HTTP": "1",
            "HOST_CLIENT_BURST": str(burst),
            "HOST_CLIENT_RATE_LIMIT_REQUESTS": "600",
            # Sized for the whole measurement (5 batches of `burst`), so the
            # global limiter never fires and the memory figure below measures
            # real served traffic rather than rejected requests.
            "HOST_GLOBAL_BURST": str(burst * 6),
            "HOST_GLOBAL_RATE_LIMIT_REQUESTS": "600",
        }
    )

    async def grant(_self, _api_key, _ttl):
        await asyncio.sleep(GRANT_LATENCY_SECONDS)
        return {"access_token": "issued-token", "expires_in": 30}

    core.AsyncGrantClient.grant = grant  # type: ignore[method-assign]
    return create_app(settings)


async def _one(app: Any, client_id: str, secret: str) -> Tuple[int, float]:
    import httpx

    transport = httpx.ASGITransport(app=app, client=(NAT_IP, 51000))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://stt.example.com"
    ) as http:
        started = time.monotonic()
        response = await http.post(
            "/v1/session",
            headers={"Authorization": f"Bearer {secret}", "X-Client-Id": client_id},
            json={},
        )
        return response.status_code, time.monotonic() - started


async def _run_all(app: Any, clients: Dict[str, str]) -> List[Tuple[int, float]]:
    return list(
        await asyncio.gather(*(_one(app, cid, sec) for cid, sec in clients.items()))
    )


def percentile(values: List[float], fraction: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return ordered[index]


def main(argv: Any = None) -> int:
    import tempfile

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", type=int, default=50)
    args = parser.parse_args(argv)

    count = max(1, args.clients)
    results: List[Tuple[str, bool, str]] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        clients = build_clients(count, tmp)

        # --- 50 simultaneous requests, one shared NAT -------------------
        app = make_app(clients, tmp, burst=count * 2)
        loop_time = asyncio.run(_run_all(app, clients))
        statuses = [status for status, _ in loop_time]
        latencies = [elapsed for _status, elapsed in loop_time]
        success = statuses.count(200)
        limited = statuses.count(429)

        results.append(
            (
                f"{count} simultaneous session requests",
                success >= int(count * 0.99) and limited == 0,
                f"{success}/{count} ok, {limited} x 429",
            )
        )

        p95 = percentile(latencies, 0.95)
        results.append(
            ("token acquisition p95 < 2s", p95 < 2.0, f"{p95 * 1000:.1f} ms")
        )

        # --- event loop not blocked -------------------------------------
        started = time.monotonic()
        asyncio.run(_run_all(app, clients))
        elapsed = time.monotonic() - started
        serialized = GRANT_LATENCY_SECONDS * count
        results.append(
            (
                "event loop free during issuance",
                elapsed < serialized / 2,
                f"{elapsed:.3f}s vs {serialized:.2f}s if serialized",
            )
        )

        # --- memory vs completed sessions -------------------------------
        before = rss_bytes()
        for _ in range(5):
            asyncio.run(_run_all(app, clients))
        after = rss_bytes()
        growth = after - before
        results.append(
            (
                "no memory growth per session batch",
                growth < 32 * 1024 * 1024,
                f"{growth / 1e6:+.2f} MB over {count * 5} sessions",
            )
        )

        # --- state after shutdown ---------------------------------------
        results.append(
            (
                "active sessions after shutdown == 0",
                app.state.inflight.in_flight == 0,
                f"in-flight grants = {app.state.inflight.in_flight}",
            )
        )
        results.append(
            (
                "rate limiter state bounded",
                app.state.client_limiter.tracked_keys <= count,
                f"{app.state.client_limiter.tracked_keys} buckets for {count} clients",
            )
        )

    width = max(len(name) for name, _ok, _detail in results)
    print("\n=== 50-concurrent-session acceptance criteria ===")
    print("(mocked Deepgram; this is application capacity, not provider latency)\n")
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail}")
    failed = [name for name, ok, _ in results if not ok]
    print()
    if failed:
        print(f"RESULT: FAIL ({len(failed)} criterion/criteria unmet)")
        return 1
    print("RESULT: PASS (all application-side criteria met)")
    print(
        "\nNote: this does NOT validate Deepgram's concurrent-stream limit.\n"
        "Run the opt-in real-provider test to check that separately:\n"
        "  MEDICAL_STT_LOAD_TEST=1 pytest tests/test_live_load.py -m loadtest -s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
