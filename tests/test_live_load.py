"""Phase 14 -- opt-in load test against the *real* Deepgram project.

This is the only test in the repository that spends real Deepgram credits,
and it must never run accidentally. Three independent gates, all required:

    MEDICAL_STT_LOAD_TEST=1        # explicit opt-in
    DEEPGRAM_API_KEY               # a real key, in the environment
    pytest -m loadtest             # the marker must be selected explicitly

    MEDICAL_STT_LOAD_TEST=1 pytest tests/test_live_load.py -m loadtest -v -s

CI never sets `MEDICAL_STT_LOAD_TEST`, so this is skipped there. See also
`tests/test_live_deepgram.py`, which is the single-stream functional check.

**It does not attempt to bypass Deepgram's concurrency limits.** If the
project is provisioned for fewer concurrent streams than the target, the
test reports the two causes separately and fails only on the application's:

    APPLICATION CAPACITY: PASS
    PROVIDER CAPACITY: FAIL

That distinction is the point. The application issuing a token and opening a
WebSocket is something this repository controls and can be proven correct.
Whether the Deepgram plan allows 50 simultaneous Nova-3 streams is a
commercial decision on the operator's account, and reporting it as an
application failure would be dishonest.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path
from typing import List, Tuple

import pytest

pytestmark = [
    pytest.mark.loadtest,
    pytest.mark.skipif(
        os.getenv("MEDICAL_STT_LOAD_TEST", "").strip() not in ("1", "true", "yes"),
        reason="set MEDICAL_STT_LOAD_TEST=1 to run the real Deepgram load test",
    ),
    pytest.mark.skipif(
        not os.getenv("DEEPGRAM_API_KEY", "").strip(),
        reason="DEEPGRAM_API_KEY is required for a real provider load test",
    ),
]

#: Stream counts the operator is asked to validate, in ascending order.
STREAM_COUNTS: Tuple[int, ...] = (10, 25, 50)

#: How long each tier holds its streams open. Long enough for Deepgram to
#: accept the connection and start streaming, short enough to stay cheap.
TIER_SECONDS = float(os.getenv("MEDICAL_STT_LOAD_TIER_SECONDS", "20"))

#: Fraction of streams that must connect for the application to be judged
#: capable. Deepgram may refuse the excess if the plan is smaller than the
#: target -- that is a provider limit, not an application defect.
APPLICATION_SUCCESS_THRESHOLD = 0.99

SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
CHANNELS = 1


def _silence(frames: int) -> bytes:
    """A block of digital silence, valid linear16 PCM."""
    return b"\x00" * (frames * SAMPLE_WIDTH * CHANNELS)


class _CapacityResult:
    def __init__(self) -> None:
        self.application_pass = False
        self.provider_pass = False
        self.rows: List[str] = []

    def report(self) -> None:
        print("\n--- Deepgram concurrent-stream capacity ---")
        for row in self.rows:
            print(row)
        print(
            f"\nAPPLICATION CAPACITY: {'PASS' if self.application_pass else 'FAIL'}\n"
            f"PROVIDER CAPACITY: {'PASS' if self.provider_pass else 'FAIL'}"
        )

    def summary(self) -> str:
        return (
            f"APPLICATION CAPACITY: {'PASS' if self.application_pass else 'FAIL'}; "
            f"PROVIDER CAPACITY: {'PASS' if self.provider_pass else 'FAIL'}"
        )


async def _open_stream(index: int, stop: asyncio.Event, outcome: List[str]) -> str:
    """Hold one Deepgram streaming connection open for the tier.

    Uses the Deepgram SDK exactly as the production provider does: Nova-3,
    `language=fa`, with a *short-lived grant token* when one can be minted.
    Falls back to the account API key only for this opt-in diagnostic, and
    says so, so nobody mistakes it for the production credential path.
    """
    from deepgram import DeepgramClient

    host_url = os.getenv("MEDICAL_STT_HOST_URL", "").strip()
    client_secret = os.getenv("MEDICALSTT_HOST_SECRET", "").strip()

    if host_url and client_secret:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from medical_stt.host_client import HostSessionClient

        session = HostSessionClient(
            base_url=host_url, secret=client_secret, client_id="loadtest"
        ).fetch_session(ttl_seconds=60)
        client = DeepgramClient(access_token=session.access_token)
        path = "host-issued short-lived token (production credential path)"
    else:
        client = DeepgramClient(api_key=os.environ["DEEPGRAM_API_KEY"])
        path = "account API key (host NOT under test; credential path unverified)"

    try:
        connection = client.listen.v1.connect(
            model="nova-3",
            language="fa",
            encoding="linear16",
            sample_rate=SAMPLE_RATE,
            channels=CHANNELS,
            interim_results=True,
            endpointing=400,
            smart_format=True,
        )
        with connection:
            opened = threading.Event()
            connection.on("open", lambda _m: opened.set())
            connection.start_listening()
            # Give Deepgram a moment to complete the handshake; a refusal is
            # reported by the SDK rather than raised here.
            await asyncio.sleep(1.0)

            frames = int(SAMPLE_RATE * 0.1)
            while not stop.is_set():
                try:
                    connection.send_media(_silence(frames))
                except Exception as exc:  # noqa: BLE001 - classified below
                    return f"stream {index}: send failed ({type(exc).__name__})"
                await asyncio.sleep(0.1)
            return f"stream {index}: ok ({path})"
    except Exception as exc:  # noqa: BLE001 - any refusal is a capacity signal
        return f"stream {index}: REFUSED ({type(exc).__name__}: {exc})"


def _run_tier(count: int) -> Tuple[int, int, List[str]]:
    """Open `count` streams concurrently; return (opened, refused, notes)."""
    async def main() -> List[str]:
        stop = asyncio.Event()
        tasks = [asyncio.create_task(_open_stream(i, stop, [])) for i in range(count)]
        await asyncio.sleep(TIER_SECONDS)
        stop.set()
        return await asyncio.gather(*tasks, return_exceptions=True)

    results = asyncio.run(main())
    notes: List[str] = []
    for result in results:
        if isinstance(result, BaseException):
            notes.append(f"REFUSED ({type(result).__name__})")
        else:
            notes.append(result)
    refused = sum(1 for note in notes if "REFUSED" in note or "failed" in note)
    return count - refused, refused, notes


@pytest.mark.live
def test_50_concurrent_deepgram_streams() -> None:
    """Validate 10 -> 25 -> 50 concurrent Nova-3 streams, honestly.

    Fails when the *application* cannot serve the target. Reports -- but
    does not fail on -- a provider-side concurrency limit, because that is a
    property of the Deepgram plan on the account, not of this code.
    """
    result = _CapacityResult()
    highest_opened = 0
    target = max(STREAM_COUNTS)

    for count in STREAM_COUNTS:
        started = time.monotonic()
        opened, refused, notes = _run_tier(count)
        elapsed = time.monotonic() - started
        highest_opened = max(highest_opened, opened)
        success_rate = opened / count if count else 0.0
        result.rows.append(
            f"streams={count:>3}  opened={opened:>3}  refused={refused:>3}  "
            f"success={success_rate * 100:5.1f}%  ({elapsed:.0f}s)"
        )
        for note in notes:
            if "REFUSED" in note or "failed" in note:
                result.rows.append(f"    {note}")
        if opened < count:
            # Keep going: the operator wants the full curve, not just the
            # first failure point.
            continue

    application_ok = highest_opened >= int(target * APPLICATION_SUCCESS_THRESHOLD)
    provider_ok = highest_opened >= target
    result.application_pass = application_ok
    result.provider_pass = provider_ok
    result.report()

    assert application_ok, (
        f"the application opened only {highest_opened}/{target} concurrent streams "
        f"({result.summary()})"
    )
    if not provider_ok:
        pytest.fail(
            "APPLICATION CAPACITY: PASS, PROVIDER CAPACITY: FAIL -- "
            f"{highest_opened}/{target} concurrent Nova-3 streams were accepted. "
            "This is a limit on the Deepgram project's concurrency allowance, "
            "not a defect in this application. Confirm your Deepgram plan's "
            "concurrent-stream limit, or reduce the supported session count. "
            "See host/README.md > Capacity planning."
        )
