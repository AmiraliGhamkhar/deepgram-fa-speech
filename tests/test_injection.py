"""Text injection tests using the DryRunBackend abstraction (task sections
6 & 9). No real Windows GUI or clipboard is touched."""
from __future__ import annotations

import sys
import types
import pytest

from medical_stt.injection.backend import DryRunBackend, FailingBackend
from medical_stt.injection.text_injector import TextInjector


def test_fallback_typed_path_respects_the_configured_settle():
    """B19(b): on non-Windows the 'type' path is a clipboard paste whose
    settle was hardcoded to 0, so paste_settle_seconds applied to paste
    mode but not to type mode."""
    from medical_stt.injection._fallback_backend import FallbackBackend

    backend = FallbackBackend(paste_settle_seconds=0.33)
    seen = {}
    backend.paste_text = lambda text, restore_clipboard, settle_seconds: seen.update(
        settle=settle_seconds, restore=restore_clipboard
    ) or True
    assert backend.send_unicode_text("سلام") is True
    assert seen["settle"] == 0.33
    assert seen["restore"] is True, "fallback type mode must not leave PHI on the clipboard"


def test_text_injector_threads_its_settle_into_the_default_backend():
    import platform

    if platform.system().lower() == "windows":
        return  # the Windows backend receives the settle per call instead
    injector = TextInjector(paste_settle_seconds=0.44)
    assert injector.backend.paste_settle_seconds == 0.44


def test_paste_text_uses_backend_and_records_call():
    backend = DryRunBackend()
    injector = TextInjector(backend=backend, restore_clipboard=True, paste_settle_seconds=0.0)
    assert injector.paste_text("سلام دنیا") is True
    assert backend.pasted == ["\u200fسلام دنیا"]


def test_paste_text_empty_string_returns_false():
    backend = DryRunBackend()
    injector = TextInjector(backend=backend)
    assert injector.paste_text("") is False


def test_paste_failure_is_reported_not_raised():
    backend = FailingBackend()
    injector = TextInjector(backend=backend)
    assert injector.paste_text("سلام") is False
    assert injector.last_error is not None


def test_type_text_failure_is_reported_not_raised():
    backend = FailingBackend()
    injector = TextInjector(backend=backend)
    assert injector.type_text("hello") is False
    assert injector.last_error is not None


def test_send_backspaces_zero_is_noop_success():
    backend = DryRunBackend()
    injector = TextInjector(backend=backend)
    assert injector.send_backspaces(0) is True
    assert backend.backspace_count == 0


def test_delta_typing_appends_new_suffix():
    backend = DryRunBackend()
    injector = TextInjector(backend=backend, enable_smart_rewrite=True)
    injector.type_delta_from_partial("سلام")
    injector.type_delta_from_partial("سلام دنیا")
    assert "".join(backend.typed) == "سلام دنیا" or backend.typed[-1] == " دنیا"


def test_delta_typing_backspaces_on_revision():
    backend = DryRunBackend()
    injector = TextInjector(backend=backend, enable_smart_rewrite=True)
    injector.type_delta_from_partial("hello wold")
    injector.type_delta_from_partial("hello world")
    # "wold" -> "world": common prefix "hello wo", then diverges.
    assert backend.backspace_count > 0


def test_delta_typing_never_splits_zwnj_join():
    backend = DryRunBackend()
    injector = TextInjector(backend=backend, enable_smart_rewrite=True)
    old = "می\u200cخواه"
    new = "می\u200cخواهم"
    injector.type_delta_from_partial(old)
    injector.type_delta_from_partial(new)
    # Should only need to type the appended "م", no backspaces into the
    # ZWNJ-joined stem.
    assert backend.backspace_count == 0
    assert "".join(backend.typed).endswith("م")


def test_reset_partial_clears_streaming_state():
    backend = DryRunBackend()
    injector = TextInjector(backend=backend, enable_smart_rewrite=True)
    injector.type_delta_from_partial("hello")
    injector.reset_partial()
    injector.type_delta_from_partial("hello")
    # After reset, "hello" is typed again in full (no backspaces expected
    # since there's no previous state to diff against).
    assert backend.backspace_count == 0


def test_utf16_len_counts_surrogate_pairs_as_two():
    # U+1F600 (emoji) is a non-BMP character encoded as a surrogate pair.
    text = "\U0001F600"
    assert TextInjector._utf16_len(text) == 2


def test_smart_rewrite_disabled_only_appends():
    backend = DryRunBackend()
    injector = TextInjector(backend=backend, enable_smart_rewrite=False)
    injector.type_delta_from_partial("abc")
    injector.type_delta_from_partial("abcdef")
    assert "".join(backend.typed) == "abcdef"
    assert backend.backspace_count == 0


def test_paste_text_mixed_persian_english_keeps_logical_content():
    backend = DryRunBackend()
    injector = TextInjector(backend=backend, paste_settle_seconds=0.0)
    injector.paste_text("MI در ECG مشاهده شد")
    pasted = backend.pasted[-1]
    assert "MI" in pasted and "ECG" in pasted


# -- line structure ------------------------------------------------------


def _paste(text: str) -> str:
    backend = DryRunBackend()
    injector = TextInjector(backend=backend, paste_settle_seconds=0.0)
    assert injector.paste_text(text) is True
    return backend.pasted[-1]


def test_newlines_are_preserved():
    pasted = _paste("Doctor Name\nDiagnosis\nBlood Pressure: 120/80")
    assert "\n" in pasted, "structured dictation must keep its line breaks"
    assert pasted.count("\n") == 2
    assert pasted.splitlines()[0].endswith("Doctor Name")
    assert pasted.splitlines()[2].endswith("Blood Pressure: 120/80")


def test_multiple_consecutive_lines_keep_their_boundaries():
    source_lines = [f"line {index}" for index in range(5)]
    lines = _paste("\n".join(source_lines)).split("\n")
    # Only the very first line carries the directional mark (LRM here,
    # because every strong character in this sample is Latin).
    assert lines[0][0] in ("\u200e", "\u200f")
    assert lines[0][1:] == source_lines[0]
    assert lines[1:] == source_lines[1:]


def test_horizontal_whitespace_is_still_normalized():
    pasted = _paste("سلام    دنیا\tآزمایش")
    assert "  " not in pasted
    assert "\t" not in pasted
    assert "سلام دنیا آزمایش" in pasted


def test_trailing_whitespace_on_a_line_is_trimmed_but_the_break_stays():
    pasted = _paste("خط اول   \nخط دوم")
    assert pasted.count("\n") == 1
    assert "   " not in pasted


def test_crlf_is_normalized_to_a_line_break():
    pasted = _paste("first\r\nsecond")
    assert "\r" not in pasted
    assert pasted.count("\n") == 1


def test_mixed_persian_english_multiline_keeps_every_line():
    text = "بیمار\nMI در ECG مشاهده شد\nدوز 5 mg IV"
    pasted = _paste(text)
    lines = pasted.split("\n")
    assert len(lines) == 3
    assert "MI" in lines[1] and "ECG" in lines[1]
    assert "5 mg IV" in lines[2]


def test_blank_lines_at_the_edges_are_removed():
    pasted = _paste("\n\nمتن\n\n")
    assert pasted.strip("\n").endswith("متن")
    assert pasted.startswith("\u200f")


# -- word separator between utterances ------------------------------------
#
# LiveMedicalSTT pastes each finalized utterance followed by a single space.
# normalize_injected_whitespace strips trailing whitespace on every line (a
# property tests/test_formatting_ownership.py pins), so without re-attaching
# that separator three consecutive dictations fused into one unbroken run --
# "فشار خون120/80 mmHgبیمار تب ندارد" -- inside the target EMR field.


def _paste_via_injector(text: str) -> str:
    backend = DryRunBackend()
    injector = TextInjector(backend=backend, paste_settle_seconds=0.0)
    assert injector.paste_text(text) is True
    return backend.pasted[-1]


def test_paste_text_keeps_the_callers_word_separator():
    assert _paste_via_injector("فشار خون ").endswith(" ")


def test_consecutive_utterances_stay_separate_tokens():
    backend = DryRunBackend()
    injector = TextInjector(backend=backend, paste_settle_seconds=0.0)
    for utterance in ("فشار خون 120/80 mmHg", "بیمار تب ندارد"):
        assert injector.paste_text(utterance + " ") is True
    joined = "".join(backend.pasted)
    assert "mmHg \u200fبیمار" in joined, f"utterances fused: {joined!r}"


def test_paste_text_without_trailing_space_adds_none():
    assert not _paste_via_injector("فشار خون").endswith(" ")


def test_paste_text_collapses_a_run_of_trailing_spaces_to_one_separator():
    assert _paste_via_injector("گزارش   ") == "\u200fگزارش "


def test_paste_text_does_not_downgrade_a_trailing_newline_to_a_space():
    """A line break is a stronger separator than a space and must survive as is."""
    pasted = _paste_via_injector("Diagnosis\nHbA1c 7.2\n")
    assert pasted.endswith("7.2"), f"unexpected tail: {pasted!r}"
    assert pasted.count("\n") == 1


def test_paste_text_still_refuses_whitespace_only_input():
    backend = DryRunBackend()
    injector = TextInjector(backend=backend, paste_settle_seconds=0.0)
    assert injector.paste_text("   ") is False
    assert backend.pasted == []


# -- backend failures must not escape the provider callback thread --------


class _RaisingBackend(DryRunBackend):
    """Raises a non-OSError, like the real optional dependencies do.

    pyperclip raises PyperclipException (an Exception) when no clipboard
    helper is installed; pyautogui raises FailSafeException (an Exception)
    when the pointer hits a screen corner. Neither is an OSError.
    """

    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self._exc = exc

    def send_unicode_text(self, text: str) -> bool:
        raise self._exc

    def send_backspaces(self, count: int) -> bool:
        raise self._exc

    def paste_text(self, text: str, restore_clipboard: bool, settle_seconds: float) -> bool:
        raise self._exc


class _ClipboardUnavailable(RuntimeError):
    """Stand-in for pyperclip.PyperclipException."""


def test_paste_text_reports_a_library_exception_instead_of_raising():
    injector = TextInjector(backend=_RaisingBackend(_ClipboardUnavailable("no clipboard helper")))
    assert injector.paste_text("گزارش") is False
    assert injector.last_error is not None and "no clipboard helper" in injector.last_error


def test_type_text_reports_a_library_exception_instead_of_raising():
    injector = TextInjector(backend=_RaisingBackend(_ClipboardUnavailable("no clipboard helper")))
    assert injector.type_text("گزارش") is False
    assert injector.last_error is not None


def test_send_backspaces_reports_a_library_exception_instead_of_raising():
    injector = TextInjector(backend=_RaisingBackend(_ClipboardUnavailable("failsafe triggered")))
    assert injector.send_backspaces(3) is False
    assert injector.last_error is not None


def test_interim_typing_survives_a_library_exception():
    """Interim updates type on the provider callback thread too.

    An exception escaping here killed that thread, so dictation stopped
    silently in the middle of a consult with no error surfaced to the UI.
    """
    injector = TextInjector(backend=_RaisingBackend(_ClipboardUnavailable("no clipboard helper")))
    injector.type_delta_from_partial("بیمار")
    injector.type_delta_from_partial("بیمار تب")
    assert injector.last_error is not None


# -- P0-13: non-Windows type mode is a paste, so restore the clipboard ------

class _FakeClipboard:
    def __init__(self, initial: str = "", *, unreadable: bool = False) -> None:
        self.value = initial
        self.unreadable = unreadable
        self.copies: list[str] = []
        self.paste_calls = 0

    def paste(self) -> str:
        self.paste_calls += 1
        if self.unreadable:
            raise RuntimeError("no clipboard helper")
        return self.value

    def copy(self, text: str) -> None:
        self.copies.append(text)
        self.value = text


class _VerificationFailsClipboard(_FakeClipboard):
    def paste(self) -> str:
        self.paste_calls += 1
        if self.paste_calls == 2:  # verification after our write
            return "unexpected clipboard owner"
        if self.unreadable:
            raise RuntimeError("no clipboard helper")
        return self.value


def _install_fallback_clipboard(monkeypatch, clipboard, *, on_hotkey=None):
    from medical_stt.injection import _fallback_backend as module

    hotkeys: list[tuple[str, ...]] = []

    def hotkey(*keys: str) -> None:
        hotkeys.append(keys)
        if on_hotkey is not None:
            on_hotkey()

    fake_pyautogui = types.SimpleNamespace(hotkey=hotkey, press=lambda *_: None)
    monkeypatch.setitem(sys.modules, "pyperclip", clipboard)
    monkeypatch.setitem(sys.modules, "pyautogui", fake_pyautogui)
    monkeypatch.setattr(module, "time", types.SimpleNamespace(sleep=lambda *_: None))
    return module.FallbackBackend(paste_settle_seconds=0), hotkeys


def test_fallback_type_mode_restores_previous_clipboard(monkeypatch):
    clipboard = _FakeClipboard("previous user copy")
    backend, hotkeys = _install_fallback_clipboard(monkeypatch, clipboard)

    assert backend.send_unicode_text("راز بیمار") is True
    assert hotkeys, "the paste keystroke was not sent"
    assert clipboard.value == "previous user copy"
    assert "راز بیمار" not in clipboard.value
    assert clipboard.copies == ["راز بیمار", "previous user copy"]


def test_fallback_type_mode_refuses_an_unreadable_clipboard(monkeypatch):
    clipboard = _FakeClipboard(unreadable=True)
    backend, hotkeys = _install_fallback_clipboard(monkeypatch, clipboard)

    assert backend.send_unicode_text("راز بیمار") is False
    assert clipboard.copies == [], "PHI was written despite an unreadable clipboard"
    assert hotkeys == []


def test_fallback_type_mode_restores_after_a_failed_write_verification(monkeypatch):
    clipboard = _VerificationFailsClipboard("previous user copy")
    backend, hotkeys = _install_fallback_clipboard(monkeypatch, clipboard)

    assert backend.send_unicode_text("راز بیمار") is False
    assert hotkeys == [], "paste must be skipped when clipboard verification fails"
    assert clipboard.value == "previous user copy"
    assert clipboard.copies == ["راز بیمار", "previous user copy"]


def test_fallback_restore_does_not_overwrite_a_newer_user_copy(monkeypatch):
    clipboard = _FakeClipboard("previous user copy")

    def newer_copy() -> None:
        clipboard.value = "newer user copy"

    backend, _hotkeys = _install_fallback_clipboard(
        monkeypatch, clipboard, on_hotkey=newer_copy
    )
    assert backend.send_unicode_text("راز بیمار") is True
    assert clipboard.value == "newer user copy"
    assert clipboard.copies == ["راز بیمار"], "restoration overwrote a newer copy"


@pytest.mark.parametrize('method,args', [('type_text', ('گزارش',)), ('paste_text', ('گزارش',)),
                                         ('send_backspaces', (2,))])
def test_backend_exception_logs_only_type_not_potential_phi(caplog, method, args):
    marker = 'SENSITIVE_TEST_TRANSCRIPT_DO_NOT_LOG'
    injector = TextInjector(backend=_RaisingBackend(RuntimeError(marker)))
    assert getattr(injector, method)(*args) is False
    assert marker not in caplog.text
    assert 'RuntimeError' in caplog.text


# -- delta bookkeeping: the baseline is committed only on success ------------


class _FlakyBackend(DryRunBackend):
    """DryRun backend whose type/backspace paths can be made to fail."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_type = False
        self.fail_backspaces = False

    def send_unicode_text(self, text: str) -> bool:
        if self.fail_type:
            return False
        return super().send_unicode_text(text)

    def send_backspaces(self, count: int) -> bool:
        if self.fail_backspaces:
            return False
        return super().send_backspaces(count)


def test_failed_delta_keeps_the_old_baseline():
    backend = _FlakyBackend()
    injector = TextInjector(backend=backend, enable_smart_rewrite=True)
    assert injector.type_delta_from_partial("hello") is True

    backend.fail_type = True
    assert injector.type_delta_from_partial("hello world") is False
    assert injector.last_error is not None
    assert injector._last_partial == "hello", "a failed send must not desync the baseline"

    backend.fail_type = False
    assert injector.type_delta_from_partial("hello world") is True
    assert "".join(backend.typed) == "hello world"


def test_failed_backspaces_keep_the_old_baseline_and_skip_typing():
    backend = _FlakyBackend()
    injector = TextInjector(backend=backend, enable_smart_rewrite=True)
    assert injector.type_delta_from_partial("hello wold") is True

    backend.fail_backspaces = True
    assert injector.type_delta_from_partial("hello world") is False
    assert injector._last_partial == "hello wold"
    assert "".join(backend.typed) == "hello wold", "typing must not run after failed erasure"


def test_legacy_non_prefix_revision_is_an_explicit_error():
    backend = _FlakyBackend()
    injector = TextInjector(backend=backend, enable_smart_rewrite=False)
    assert injector.type_delta_from_partial("abc") is True

    assert injector.type_delta_from_partial("xyz") is False
    assert injector.last_error == "non-prefix revision unsupported in legacy mode"
    assert injector._last_partial == "abc"

    assert injector.type_delta_from_partial("abcdef") is True
    assert "".join(backend.typed) == "abcdef"


def test_legacy_failed_append_keeps_the_old_baseline():
    backend = _FlakyBackend()
    injector = TextInjector(backend=backend, enable_smart_rewrite=False)
    assert injector.type_delta_from_partial("abc") is True
    backend.fail_type = True
    assert injector.type_delta_from_partial("abcdef") is False
    assert injector._last_partial == "abc"


def test_dry_run_parameter_is_gone():
    """DryRunBackend covers testing; a stored-but-unread flag only lied."""
    import inspect

    assert "dry_run" not in inspect.signature(TextInjector).parameters
    assert not hasattr(TextInjector(backend=DryRunBackend()), "dry_run")


def test_directional_mark_is_always_on_for_paste_and_opt_in_for_type():
    backend = DryRunBackend()
    injector = TextInjector(backend=backend, paste_settle_seconds=0.0)

    assert injector.paste_text("بیمار", add_rtl_mark=False) is True
    assert backend.pasted[-1].startswith("‏"), "paste always applies its one mark"

    assert injector.type_text("بیمار") is True
    assert backend.typed[-1] == "بیمار", "type mode adds no mark unless asked"

    assert injector.type_text("بیمار", add_directional_mark=True) is True
    assert backend.typed[-1].startswith("‏")


def test_fallback_backspaces_do_not_pay_pyautogui_pause_per_key(monkeypatch):
    pauses = []
    fake_pyautogui = types.SimpleNamespace(
        PAUSE=0.1, press=lambda _key: pauses.append(fake_pyautogui.PAUSE)
    )
    monkeypatch.setitem(sys.modules, "pyautogui", fake_pyautogui)

    from medical_stt.injection._fallback_backend import FallbackBackend

    assert FallbackBackend().send_backspaces(3) is True
    assert pauses == [0, 0, 0], "the per-key pause must be disabled inside the loop"
    assert fake_pyautogui.PAUSE == 0.1, "and restored afterwards"


def test_fallback_no_restore_replaces_dictated_text_with_placeholder(monkeypatch):
    from medical_stt.injection.backend import CLIPBOARD_CLEARED_PLACEHOLDER

    clipboard = _FakeClipboard("previous user copy")
    backend, _hotkeys = _install_fallback_clipboard(monkeypatch, clipboard)
    assert backend.paste_text("راز بیمار", restore_clipboard=False, settle_seconds=0) is True
    assert clipboard.value == CLIPBOARD_CLEARED_PLACEHOLDER
