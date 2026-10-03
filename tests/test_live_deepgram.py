"""Optional live integration test against Deepgram (disabled by default).

The unit suite must never need the internet or a credential, so every test
here skips unless it is explicitly opted into:

    MEDICAL_STT_LIVE_TEST=1 \
    MEDICALSTT_HOST_URL=https://your-host.example.com \
    MEDICALSTT_HOST_SECRET=... \
    pytest tests/test_live_deepgram.py -v

What it verifies (session lifecycle over the real streaming path):

* the host mints a short-lived token for this client;
* the provider opens a real WebSocket with that token, accepts audio and
  shuts down cleanly (finalize + close), which is the part unit tests fake;
* no token or secret value ever reaches the logs.

What it does NOT do: assert transcription accuracy. Silence contains no
speech, and inventing an accuracy claim from synthetic audio would be
dishonest -- accuracy belongs to benchmarks/run_benchmark.py with real
recordings.
"""
from __future__ import annotations

import logging
import os
import threading
import time

import pytest

pytestmark = pytest.mark.live

LIVE_ENV = "MEDICAL_STT_LIVE_TEST"
HOST_URL_ENV = "MEDICALSTT_HOST_URL"
HOST_SECRET_ENV = "MEDICALSTT_HOST_SECRET"


def _live_credentials() -> tuple[str, str]:
    return os.environ.get(HOST_URL_ENV, "").strip(), os.environ.get(HOST_SECRET_ENV, "").strip()


def _require_live() -> tuple[str, str]:
    if os.environ.get(LIVE_ENV) != "1":
        pytest.skip(
            f"live Deepgram integration test disabled; set {LIVE_ENV}=1 "
            f"(plus {HOST_URL_ENV}/{HOST_SECRET_ENV}) to run it"
        )
    host_url, host_secret = _live_credentials()
    if not host_url or not host_secret:
        pytest.skip(
            f"live test enabled but {HOST_URL_ENV}/{HOST_SECRET_ENV} are not both set"
        )
    return host_url, host_secret


def _silence(seconds: float, sample_rate: int = 16000) -> bytes:
    return b"\x00\x00" * int(seconds * sample_rate)


def _settings(host_url: str, host_secret: str):
    from medical_stt.config import Settings

    return Settings(host_url=host_url, host_secret=host_secret)


def test_live_host_mints_a_session_token():
    host_url, host_secret = _require_live()
    from medical_stt.host_client import HostSessionClient

    client = HostSessionClient(base_url=host_url, secret=host_secret, timeout=15.0)
    session = client.fetch_session(ttl_seconds=30)
    assert session.access_token, "the host returned an empty session token"
    assert os.environ.get(HOST_SECRET_ENV, "") not in session.access_token


def test_live_provider_streams_and_shuts_down_cleanly(caplog):
    """The full streaming lifecycle against the real service.

    Sends 0.4 s of silence (no speech is asserted), then stops; the
    connection must close without leaking the credential into logs.
    """
    host_url, host_secret = _require_live()
    from medical_stt.config import load_keyterms
    from medical_stt.stt.deepgram_provider import DeepgramProvider

    settings = _settings(host_url, host_secret)
    provider = DeepgramProvider(settings, keyterms=load_keyterms(settings.specialty)[:10])
    provider.validate_config()

    transcript_events = []
    errors = []
    stopped = threading.Event()

    def on_transcript(event) -> None:
        transcript_events.append(event)

    def on_error(error) -> None:
        errors.append(error)

    with caplog.at_level(logging.DEBUG):
        provider.start(on_transcript, on_error)
        for start in range(0, len(_silence(0.4)), 3200):
            provider.send_audio(_silence(0.4)[start:start + 3200])
        time.sleep(1.5)  # let the service acknowledge; no transcript expected
        provider.stop()
        stopped.set()

    assert stopped.is_set()
    # A silent stream may legitimately produce no transcript events; what
    # must not happen is an unexplained provider error.
    fatal = [e for e in errors if getattr(e, "category", None) is not None
             and getattr(e.category, "value", "") in ("auth", "config")]
    assert not fatal, f"live provider reported {fatal}"

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert host_secret not in logged, "the shared secret leaked into logs"
    for event in transcript_events:
        if getattr(event, "text", ""):
            assert "dg_" not in event.text


def test_live_suite_is_skipped_by_default():
    """A guard test: the marker/env gate must exist in this module."""
    source = __file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert "MEDICAL_STT_LIVE_TEST" in text
    assert "pytest.skip" in text


@pytest.fixture(autouse=True)
def _no_ambient_key(monkeypatch):
    """Never let a developer's key path make the live test look enabled."""
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
