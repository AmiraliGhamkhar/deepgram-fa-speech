"""Explicitly opt-in real-provider load test using the production credential path.

Requires MEDICAL_STT_LOAD_TEST=1, -m loadtest, MEDICALSTT_HOST_URL and either
MEDICAL_STT_LOAD_CLIENTS_FILE (JSON list of client_id/secret objects) or the
MEDICALSTT_HOST_SECRET / MEDICALSTT_HOST_CLIENT_ID pair. No account API key
is read or required. Keep the credential file outside Git (e.g. *.secret).
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import time
from pathlib import Path

import pytest

from medical_stt.config import Settings
from medical_stt.stt.deepgram_provider import DeepgramProvider
from medical_stt.stt.runner import stream_audio

pytestmark = [pytest.mark.loadtest, pytest.mark.live]
STREAM_COUNTS = (10, 25, 50)


def _credentials(count: int) -> list[dict[str, str]]:
    path = os.getenv("MEDICAL_STT_LOAD_CLIENTS_FILE", "")
    if path:
        clients = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(clients, list) or len(clients) < count:
            pytest.fail("load credential file must contain enough provisioned clients")
        if any(not isinstance(c, dict) or not c.get("client_id") or not c.get("secret") for c in clients):
            pytest.fail("load credential file entries require client_id and secret")
        return clients[:count]
    secret = os.getenv("MEDICALSTT_HOST_SECRET", "")
    if not secret:
        pytest.skip("host credentials are required")
    return [{"client_id": os.getenv("MEDICALSTT_HOST_CLIENT_ID", ""), "secret": secret}] * count


def _stream(client: dict[str, str], seconds: float) -> tuple[str, float, float]:
    settings = Settings(host_url=os.environ["MEDICALSTT_HOST_URL"],
                        host_secret=client["secret"], host_client_id=client["client_id"])
    provider = DeepgramProvider(settings)
    ready_at = 0.0
    def ready() -> None:
        nonlocal ready_at
        ready_at = time.monotonic()
    try:
        # Silence, paced in real time. The listener runs on its own worker.
        stream_audio(provider, (bytes(3200) for _ in range(max(1, int(seconds * 10)))),
                     lambda event: None, lambda error: None, bytes_per_second=32000, on_ready=ready)
        return "ok", ready_at, time.monotonic()
    except Exception as exc:
        # No raw exception representations: SDKs can embed credentials.
        return getattr(getattr(exc, "category", None), "value", type(exc).__name__), ready_at, time.monotonic()


def test_50_concurrent_deepgram_streams(pytestconfig) -> None:
    if os.getenv("MEDICAL_STT_LOAD_TEST") != "1" or "loadtest" not in pytestconfig.option.markexpr:
        pytest.skip("set MEDICAL_STT_LOAD_TEST=1 and select -m loadtest to run the real Deepgram load test")
    if not os.getenv("MEDICALSTT_HOST_URL"):
        pytest.skip("MEDICALSTT_HOST_URL is required")
    seconds = float(os.getenv("MEDICAL_STT_LOAD_TIER_SECONDS", "20"))
    for count in STREAM_COUNTS:
        clients = _credentials(count)
        started = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
            results = list(pool.map(lambda c: _stream(c, seconds), clients))
        categories = [row[0] for row in results]
        successes = categories.count("ok")
        failures = {result: categories.count(result) for result in set(categories) if result != "ok"}
        assert successes / count >= .99, f"End-to-end stream failures by category: {failures}"
        successful = [row for row in results if row[0] == "ok"]
        # Count overlapping actual ready-to-close intervals, not merely a
        # collection of successful streams that could have run serially.
        overlap = min(row[2] for row in successful) - max(row[1] for row in successful)
        print(f"streams={count} successful={successes} overlap={overlap:.1f}s "
              f"elapsed={time.monotonic()-started:.1f}s")
        assert overlap >= min(5.0, seconds / 4), "Streams did not remain concurrently ready long enough"
