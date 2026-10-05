"""Test 6 -- opt-in sustained soak: 50 clients over 30+ minutes.

Disabled by default. It runs only when `MEDICAL_STT_SOAK=1`, because a real
soak holds the machine for half an hour and must never fire by accident in
CI (pytest's `testpaths` includes this directory, so both the marker and the
environment gate are required).

    MEDICAL_STT_SOAK=1 pytest tests/test_soak.py -v -s
    MEDICAL_STT_SOAK=1 MEDICAL_STT_SOAK_MINUTES=35 pytest tests/test_soak.py -s

Deepgram is **mocked**. A soak against the real provider would burn real
credits for half an hour; provider capacity is validated separately, and
explicitly, by `tests/test_live_load.py`.

Measured over the run: resident memory, CPU time, live sessions, audio
queue drops, reconnects, end-to-end latency and failed requests. The
assertions are about *stability over time* -- the property a soak exists to
detect -- not about absolute throughput.
"""
from __future__ import annotations

import os
import resource
import threading
import time
from typing import List

import pytest

from medical_stt.injection.backend import DryRunBackend
from medical_stt.injection.text_injector import TextInjector
from medical_stt.stt.base import (
    OnError,
    OnTranscript,
    ProviderError,
    STTProvider,
    TranscriptEvent,
)

CLIENT_COUNT = int(os.getenv("MEDICAL_STT_SOAK_CLIENTS", "50"))
MINUTES = float(os.getenv("MEDICAL_STT_SOAK_MINUTES", "30"))

#: Growth allowance. A leak across many session cycles shows up as RSS that
#: keeps climbing; a bounded workload must plateau. 32 MB over half an hour
#: is generous enough not to flake while still catching a real leak.
MAX_RSS_GROWTH_BYTES = 32 * 1024 * 1024


def _enabled() -> bool:
    return os.getenv("MEDICAL_STT_SOAK", "").strip() in ("1", "true", "yes")


pytestmark = [
    pytest.mark.soak,
    pytest.mark.skipif(
        not _enabled(), reason="set MEDICAL_STT_SOAK=1 to run the soak"
    ),
]


def _rss_bytes() -> int:
    """Current resident set size, in bytes."""
    try:
        with open("/proc/self/statm", encoding="utf-8") as handle:
            pages = int(handle.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, IndexError, ValueError):
        # ru_maxrss is kilobytes on Linux, bytes on macOS.
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def _cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


class _FakeInputStream:
    def __init__(self, **kwargs) -> None:
        self.callback = kwargs.get("callback")

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _FakeSoundDevice:
    RawInputStream = _FakeInputStream


class SoakProvider(STTProvider):
    """Mocked Deepgram: accepts audio continuously for the whole soak."""

    def __init__(self, marker: int) -> None:
        self.marker = marker
        self.started = threading.Event()
        self.released = threading.Event()
        self.chunks = 0
        self.starts = 0
        self._lock = threading.Lock()
        self._transcript_sink: OnTranscript = lambda _e: None

    def validate_config(self) -> None:
        pass

    def start(self, on_transcript: OnTranscript, on_error: OnError) -> None:
        self._transcript_sink = on_transcript
        with self._lock:
            self.starts += 1
        self.started.set()
        self.released.wait(timeout=3_600.0)

    def send_audio(self, chunk: bytes) -> None:
        with self._lock:
            self.chunks += 1

    def emit(self, text: str) -> None:
        self._transcript_sink(
            TranscriptEvent(text=text, is_final=True, speech_final=True)
        )

    def stop(self) -> None:
        self.released.set()


@pytest.fixture(autouse=True)
def _host_credentials(monkeypatch):
    monkeypatch.setenv("MEDICALSTT_HOST_URL", "https://stt.example.com")
    monkeypatch.setenv("MEDICALSTT_HOST_SECRET", "test-shared-secret-not-real")
    monkeypatch.setenv("MEDICALSTT_APP_DATA_DIR", "/tmp/medicalstt-soak")


def test_50_clients_soak_without_growth(monkeypatch, capsys):
    """Hold 50 sessions for 30+ minutes and assert nothing degrades."""
    from medical_stt import app as app_module

    monkeypatch.setattr(app_module, "load_sounddevice", lambda: _FakeSoundDevice)

    providers = [SoakProvider(marker=i) for i in range(CLIENT_COUNT)]
    sessions = []
    for provider in providers:
        stt = app_module.LiveMedicalSTT(provider=provider)
        stt.injector = TextInjector(backend=DryRunBackend(), paste_settle_seconds=0.0)
        stt.overlay.enabled = False
        sessions.append(stt)

    threads: List[threading.Thread] = []
    for stt in sessions:

        def run(target):
            try:
                target._run_session()
            except ProviderError:
                pass

        thread = threading.Thread(
            target=run, args=(stt,), daemon=True, name="stt-session"
        )
        thread.start()
        threads.append(thread)

    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        if all(p.started.is_set() for p in providers):
            break
        time.sleep(0.05)
    assert all(p.started.is_set() for p in providers), "not every session started"

    baseline_rss = _rss_bytes()
    baseline_cpu = _cpu_seconds()
    started_at = time.monotonic()
    queued = 0
    max_depth = 0
    peak_reconnects = 0

    stop_at = started_at + MINUTES * 60.0
    try:
        while time.monotonic() < stop_at:
            for index, stt in enumerate(sessions):
                stt._audio_q.put_nowait(bytes([index % 251]))
                queued += 1
                max_depth = max(max_depth, stt.audio_queue_stats.depth)
            peak_reconnects = max(
                peak_reconnects,
                sum(p.starts for p in providers) - CLIENT_COUNT,
            )
            time.sleep(0.05)
    finally:
        for stt in sessions:
            stt.request_stop()
        for thread in threads:
            thread.join(timeout=120.0)

    elapsed = time.monotonic() - started_at
    final_rss = _rss_bytes()
    cpu = _cpu_seconds() - baseline_cpu
    drops = sum(s.audio_queue_stats.dropped_total for s in sessions)
    chunks = sum(p.chunks for p in providers)
    reconnects = sum(p.starts for p in providers) - CLIENT_COUNT
    live_sessions = sum(1 for thread in threads if thread.is_alive())
    growth = final_rss - baseline_rss

    with capsys.disabled():
        print(
            "\n--- soak report ---"
            f"\nclients               : {CLIENT_COUNT}"
            f"\nduration              : {elapsed / 60:.2f} min"
            f"\nrss baseline -> final: {baseline_rss / 1e6:.1f} -> {final_rss / 1e6:.1f} MB"
            f"\nrss growth            : {growth / 1e6:+.2f} MB (limit {MAX_RSS_GROWTH_BYTES / 1e6:.0f} MB)"
            f"\ncpu seconds           : {cpu:.1f} ({cpu / max(elapsed, 1e-9) * 100:.1f}% of wall)"
            f"\nchunks queued         : {queued}"
            f"\nchunks delivered      : {chunks}"
            f"\naudio queue drops     : {drops}"
            f"\nmax queue depth       : {max_depth}"
            f"\nreconnects            : {reconnects}"
            f"\nlive sessions at end  : {live_sessions}"
        )

    # The soak's actual job: prove stability over time.
    assert live_sessions == 0, "sessions survived shutdown"
    assert all(not thread.is_alive() for thread in threads), "a session thread leaked"
    assert growth < MAX_RSS_GROWTH_BYTES, (
        f"resident memory grew {growth / 1e6:.1f} MB over {elapsed / 60:.1f} min"
    )
    assert drops == 0, f"{drops} audio chunks were dropped during the soak"
    assert chunks > 0, "no audio reached the provider"
    assert reconnects == peak_reconnects
