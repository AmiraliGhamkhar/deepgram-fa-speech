"""End-to-end pipeline test using a fake STTProvider (no network, no real
Deepgram credentials, no real GUI/audio device)."""
from __future__ import annotations

import threading

import pytest

from medical_stt.injection.backend import DryRunBackend
from medical_stt.injection.text_injector import TextInjector
from medical_stt.stt.base import ErrorCategory, OnError, OnTranscript, ProviderError, STTProvider, TranscriptEvent, WordInfo


class FakeProvider(STTProvider):
    """Delivers a scripted sequence of events, then stops."""

    def __init__(self, events, fail_validate: bool = False) -> None:
        self.events = events
        self.fail_validate = fail_validate
        self.sent_audio = []
        self.stopped = threading.Event()

    def validate_config(self) -> None:
        if self.fail_validate:
            raise ProviderError(ErrorCategory.CONFIG, "bad config")

    def start(self, on_transcript: OnTranscript, on_error: OnError) -> None:
        for event in self.events:
            if self.stopped.is_set():
                return
            on_transcript(event)
        # Simulate a clean end of stream.
        on_error(ProviderError(ErrorCategory.SHUTDOWN, "test finished"))

    def send_audio(self, chunk: bytes) -> None:
        self.sent_audio.append(chunk)

    def stop(self) -> None:
        self.stopped.set()


@pytest.fixture(autouse=True)
def _fake_api_key(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key-not-real")
    yield
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)


def test_final_transcript_flows_through_terminology_and_injection(monkeypatch):
    from medical_stt import app as app_module

    provider = FakeProvider([TranscriptEvent(text="بیمار در آی سی یو بستری شد", is_final=True)])
    stt = app_module.LiveMedicalSTT(provider=provider)

    backend = DryRunBackend()
    stt.injector = TextInjector(backend=backend, paste_settle_seconds=0.0)
    stt.overlay.enabled = False  # no real display in CI

    stt._on_transcript(TranscriptEvent(text="بیمار در آی سی یو بستری شد", is_final=True))

    assert backend.pasted, "expected a paste call for the final transcript"
    assert "ICU" in backend.pasted[-1]


def test_interim_transcript_does_not_inject(monkeypatch):
    from medical_stt import app as app_module

    provider = FakeProvider([])
    stt = app_module.LiveMedicalSTT(provider=provider)
    backend = DryRunBackend()
    stt.injector = TextInjector(backend=backend, paste_settle_seconds=0.0)
    stt.overlay.enabled = False

    stt._on_transcript(TranscriptEvent(text="بیمار در آی", is_final=False))
    assert backend.pasted == []


def test_final_segments_inject_only_after_speech_final(monkeypatch):
    from medical_stt import app as app_module

    stt = app_module.LiveMedicalSTT(provider=FakeProvider([]))
    backend = DryRunBackend()
    stt.injector = TextInjector(backend=backend, paste_settle_seconds=0.0)
    stt.overlay.enabled = False

    stt._on_transcript(TranscriptEvent("فشار خون", is_final=True, speech_final=False))
    assert backend.pasted == []
    stt._on_transcript(TranscriptEvent("120/80 mmHg", is_final=True, speech_final=True))
    assert len(backend.pasted) == 1
    assert "فشار خون 120/80 mmHg" in backend.pasted[0]


def test_low_confidence_medical_term_is_preserved(monkeypatch):
    from medical_stt import app as app_module

    stt = app_module.LiveMedicalSTT(provider=FakeProvider([]))
    backend = DryRunBackend()
    stt.injector = TextInjector(backend=backend, paste_settle_seconds=0.0)
    stt.overlay.enabled = False
    event = TranscriptEvent(
        "هایپرتنشن",
        is_final=True,
        speech_final=True,
        words=(WordInfo("هایپرتنشن", 0.0, 1.0, 0.4),),
    )

    stt._on_transcript(event)
    assert "هایپرتنشن" in backend.pasted[0]
    assert "hypertension" not in backend.pasted[0]
    assert stt.last_confidence_warning is not None


def test_dangerous_term_not_injected(monkeypatch):
    from medical_stt import app as app_module

    provider = FakeProvider([])
    stt = app_module.LiveMedicalSTT(provider=provider)
    backend = DryRunBackend()
    stt.injector = TextInjector(backend=backend, paste_settle_seconds=0.0)
    stt.overlay.enabled = False

    stt._on_transcript(TranscriptEvent(text="بیمار ناشتا است", is_final=True))
    assert backend.pasted
    assert "NPO" not in backend.pasted[-1]
    assert "ناشتا" in backend.pasted[-1]


def test_config_error_raised_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    from medical_stt import app as app_module
    from medical_stt.config import ConfigError

    with pytest.raises(ConfigError):
        app_module.LiveMedicalSTT(provider=FakeProvider([]))
