"""Text injection tests using the DryRunBackend abstraction (task sections
6 & 9). No real Windows GUI or clipboard is touched."""
from __future__ import annotations

from medical_stt.injection.backend import DryRunBackend, FailingBackend
from medical_stt.injection.text_injector import TextInjector


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
