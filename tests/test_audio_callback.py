"""The microphone callback must stay real-time safe (no logging/IO)."""
from __future__ import annotations

import logging

import pytest

from medical_stt.audio import BoundedAudioQueue


@pytest.fixture(autouse=True)
def _host_credentials(monkeypatch):
    monkeypatch.setenv("MEDICALSTT_HOST_URL", "https://stt.example.com")
    monkeypatch.setenv("MEDICALSTT_HOST_SECRET", "test-shared-secret-not-real")
    yield


@pytest.fixture()
def stt():
    from medical_stt import app as app_module
    from medical_stt.stt.base import OnError, OnTranscript, STTProvider

    class IdleProvider(STTProvider):
        def validate_config(self) -> None:
            pass

        def start(self, on_transcript: OnTranscript, on_error: OnError) -> None:
            pass

        def send_audio(self, chunk: bytes) -> None:
            pass

        def stop(self) -> None:
            pass

    instance = app_module.LiveMedicalSTT(provider=IdleProvider())
    instance.overlay.enabled = False
    return instance


def test_callback_enqueues_without_logging(stt, caplog):
    stt._audio_q = BoundedAudioQueue(maxsize=1)  # noqa: SLF001 - test hook
    assert stt._audio_q.put_nowait(b"first") is True  # noqa: SLF001

    with caplog.at_level(logging.DEBUG):
        stt._on_audio(b"second", 1600, None, "input overflow")

    # The overflow and the device status are recorded as flags...
    assert stt._audio_drop_pending.is_set()  # noqa: SLF001
    assert stt._input_status_pending.is_set()  # noqa: SLF001
    # ...and nothing at all is logged from inside the callback.
    assert caplog.records == []


def test_worker_reports_the_flagged_problems(stt, caplog):
    stt._audio_q = BoundedAudioQueue(maxsize=1)  # noqa: SLF001 - test hook
    stt._audio_q.put_nowait(b"first")  # noqa: SLF001
    stt._on_audio(b"second", 1600, None, "input overflow")  # noqa: SLF001

    with caplog.at_level(logging.WARNING):
        stt._report_audio_health()  # noqa: SLF001

    messages = [record.getMessage() for record in caplog.records]
    assert any("microphone status" in message for message in messages)
    assert any("dropped_total" in message for message in messages)
    # Flags are cleared, so a sustained overload logs once per report.
    assert not stt._audio_drop_pending.is_set()  # noqa: SLF001
    assert not stt._input_status_pending.is_set()  # noqa: SLF001
