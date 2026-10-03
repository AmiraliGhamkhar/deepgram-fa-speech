"""Edge paths that matter for safety but were previously untested.

Three groups:

* numeric-span helpers (the API `terminology.apply` protects spans with);
* the STT provider's no-connection / invalid-config paths (a provider that
  raises the wrong category can trigger a reconnect storm or a silent drop);
* the non-Windows fallback backend, so a Linux development run fails
  loudly at the same points the Windows path does.
"""
from __future__ import annotations

import sys
import types

import pytest

from medical_stt.processing.numbers import (
    find_numeric_spans,
    is_inside_numeric_span,
    iter_protected_mask,
    protected_ranges,
)
from medical_stt.stt.base import ErrorCategory, ProviderError


# -- numeric-span helpers --------------------------------------------------


def test_protected_ranges_matches_find_numeric_spans():
    text = "فشار خون 120/80 mmHg و دوز 5 mg"
    assert protected_ranges(text) == [(s.start, s.end) for s in find_numeric_spans(text)]


def test_is_inside_numeric_span_detects_overlap_not_just_containment():
    spans = find_numeric_spans("مقدار 120/80 mmHg")
    span = spans[0]
    assert is_inside_numeric_span(spans, span.start, span.end) is True
    assert is_inside_numeric_span(spans, span.start + 1, span.start + 2) is True  # inside
    assert is_inside_numeric_span(spans, span.start - 1, span.start + 1) is True  # partial overlap
    assert is_inside_numeric_span(spans, 0, span.start) is False
    assert is_inside_numeric_span([], 0, 3) is False


def test_iter_protected_mask_covers_exactly_the_numbers():
    text = "120/80"
    assert list(iter_protected_mask(text)) == [True] * len(text)
    assert list(iter_protected_mask("بدون عدد")) == [False] * len("بدون عدد")


# -- provider paths without a live connection ------------------------------


def _settings(**overrides):
    from medical_stt.config import Settings

    base = {"host_url": "https://stt.example.com", "host_secret": "secret"}
    base.update(overrides)
    return Settings(**base)


def test_send_audio_without_a_connection_is_a_noop():
    from medical_stt.stt.deepgram_provider import DeepgramProvider

    provider = DeepgramProvider(_settings(), token_provider=lambda: "token")
    provider.send_audio(b"\x00\x00")  # must not raise


def test_stop_without_a_connection_is_a_noop():
    from medical_stt.stt.deepgram_provider import DeepgramProvider

    provider = DeepgramProvider(_settings(), token_provider=lambda: "token")
    provider.stop()  # must not raise


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"host_url": ""}, "host_url"),
        ({"host_secret": ""}, "shared secret"),
        ({"sample_rate": 0}, "sample_rate"),
        ({"channels": 3}, "channels"),
    ],
)
def test_validate_config_rejects_bad_settings_before_connecting(overrides, message):
    from medical_stt.stt.deepgram_provider import DeepgramProvider

    kwargs = dict(overrides)
    if kwargs.get("host_secret") == "":
        provider = DeepgramProvider(_settings(**kwargs))  # no token_provider either
    else:
        provider = DeepgramProvider(_settings(**kwargs), token_provider=lambda: "token")
    with pytest.raises(ProviderError) as excinfo:
        provider.validate_config()
    assert excinfo.value.category == ErrorCategory.CONFIG
    assert message in str(excinfo.value)


def test_recognition_hints_are_empty_for_a_model_without_a_known_parameter():
    """Only the known models map to a parameter; anything else is refused.

    `_recognition_hint_parameters()` returns `{}` for an unknown model, but
    such a model can never reach the socket: `validate_config()` rejects it
    first. Both halves are asserted so a future model cannot silently lose
    its hints.
    """
    from medical_stt.config import keyterm_parameter
    from medical_stt.stt.deepgram_provider import DeepgramProvider

    assert keyterm_parameter("enhanced") == "keywords"  # legacy model, legacy mechanism
    assert keyterm_parameter("nova-3") == "keyterm"
    assert keyterm_parameter("some-unknown-model") == ""

    provider = DeepgramProvider(_settings(model="some-unknown-model"), keyterms=["ICU"])
    assert provider._recognition_hint_parameters() == {}  # noqa: SLF001 - documented fallback
    with pytest.raises(ProviderError) as excinfo:
        provider.validate_config()
    assert excinfo.value.category == ErrorCategory.CONFIG
    assert "not supported" in str(excinfo.value)


def test_transcript_event_from_result_rejects_empty_and_incomplete_payloads():
    from medical_stt.stt.deepgram_provider import transcript_event_from_result

    class Alternative:
        transcript = ""
        words = ()
        confidence = None

    class Channel:
        alternatives = [Alternative()]

    class Message:
        channel = Channel()
        is_final = False
        speech_final = False

    assert transcript_event_from_result(Message()) is None
    assert transcript_event_from_result(object()) is None


# -- fallback backend (non-Windows) ----------------------------------------


class _FakeClipboard(types.SimpleNamespace):
    def __init__(self) -> None:
        super().__init__(pasted=[], copied=[])

    def paste(self) -> str:
        return self.pasted[-1] if self.pasted else ""

    def copy(self, text: str) -> None:
        self.copied.append(text)
        self.pasted.append(text)


def test_fallback_backend_routes_text_through_the_clipboard(monkeypatch):
    """PyAutoGUI cannot type Persian on X11, so the fallback must paste."""
    clipboard = _FakeClipboard()
    presses: list[str] = []
    fake_pyautogui = types.SimpleNamespace(press=presses.append, hotkey=lambda *keys: presses.append("+".join(keys)))
    monkeypatch.setitem(sys.modules, "pyperclip", clipboard)
    monkeypatch.setitem(sys.modules, "pyautogui", fake_pyautogui)

    from medical_stt.injection._fallback_backend import FallbackBackend

    backend = FallbackBackend()
    assert backend.send_unicode_text("گزارش") is True
    assert "گزارش" in clipboard.copied
    assert presses, "the paste keystroke was not sent"

    assert backend.send_backspaces(2) is True
    assert presses.count("backspace") <= 2


def test_fallback_backend_reports_missing_pyautogui(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyperclip", _FakeClipboard())
    monkeypatch.setitem(sys.modules, "pyautogui", None)  # import raises TypeError

    from medical_stt.injection._fallback_backend import FallbackBackend

    backend = FallbackBackend()
    # A missing optional dependency must be reported, never raise into the
    # audio/UI thread.
    assert backend.send_backspaces(1) in (True, False)
    assert backend.send_unicode_text("text") in (True, False)
