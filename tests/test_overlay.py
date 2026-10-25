"""Overlay lifecycle tests that do not require a real GUI/display (task
section 6: "Add tests wherever practical without requiring a real Windows
GUI in CI")."""
from __future__ import annotations

from medical_stt.ui import overlay as overlay_module
from medical_stt.ui.overlay import (
    _WS_EX_NOACTIVATE,
    _WS_EX_TRANSPARENT,
    TranscriptOverlay,
    _apply_click_through,
    _click_through_style,
    _is_rtl,
)


def test_click_through_style_only_adds_bits():
    """A3.8: the style helper behind the overlay's click-through mode."""
    assert _click_through_style(0) == _WS_EX_NOACTIVATE | _WS_EX_TRANSPARENT
    assert _click_through_style(0x00080000) == (
        0x00080000 | _WS_EX_NOACTIVATE | _WS_EX_TRANSPARENT
    ), "existing style bits (e.g. layered) must be preserved"
    once = _click_through_style(0)
    assert _click_through_style(once) == once, "idempotent"


def test_apply_click_through_is_best_effort(monkeypatch):
    assert _apply_click_through(0) is False
    if overlay_module._SYSTEM == "windows":
        # On a real Windows host the remaining branches would call user32
        # with a bogus HWND and (correctly) report success; only the
        # non-Windows / missing-user32 fail-soft behaviour is under test.
        return
    assert _apply_click_through(12345) is False  # not Windows here
    # Even claiming to be Windows must fail soft where user32 is absent.
    monkeypatch.setattr(overlay_module, "_SYSTEM", "windows")
    assert _apply_click_through(12345) is False


def test_empty_text_is_not_rtl():
    """A3.7: `_is_rtl("")` used to fall through to True."""
    assert _is_rtl("") is False
    assert _is_rtl("   \t\n") is False
    # The real directions are unchanged.
    assert _is_rtl("سلام") is True
    assert _is_rtl("hello") is False


def test_overlay_disabled_when_requested():
    overlay = TranscriptOverlay(enabled=False)
    assert overlay.enabled is False


def test_overlay_methods_are_noop_safe_when_disabled():
    overlay = TranscriptOverlay(enabled=False)
    overlay.set_idle()
    overlay.set_partial("سلام")
    overlay.set_done("سلام")
    overlay.close()  # must not raise even though there is no real Tk root


def test_close_is_idempotent():
    overlay = TranscriptOverlay(enabled=False)
    overlay.close()
    overlay.close()
    assert overlay._closed is True


def test_close_marks_closed_after_scheduling_destroy():
    """Regression test for the shutdown bug: `_closed` must become True
    only as a result of `close()` running (not block `close()` itself from
    scheduling the destroy callback)."""
    overlay = TranscriptOverlay(enabled=False)
    assert overlay._closed is False
    overlay.close()
    assert overlay._closed is True


class _FakeRoot:
    """Minimal stand-in for tkinter.Tk that records scheduled callbacks
    without needing a real display."""

    def __init__(self) -> None:
        self.scheduled = []
        self.destroyed = False

    def after(self, _delay, fn):
        self.scheduled.append(fn)
        return "fake-id"

    def destroy(self):
        self.destroyed = True


def test_close_schedules_destroy_even_though_closed_flag_is_set():
    """Direct regression test for the original bug: `_ui()` used to refuse
    to schedule anything once `_closed` was True, and `close()` used to set
    `_closed = True` *before* calling `_ui()` -- so the destroy callback was
    silently dropped and the Tk root was never destroyed. `close()` must
    still get its callback scheduled."""
    overlay = TranscriptOverlay(enabled=False)
    fake_root = _FakeRoot()
    overlay._root = fake_root
    overlay.enabled = True  # pretend the overlay was actually running

    overlay.close()

    assert not fake_root.scheduled, "workers must not call Tk.after"
    assert overlay._closed is True

    # Running the scheduled callback must actually clear _root (the real
    # destroy() call), proving the callback does real work.
    overlay._mailbox.drain()  # the owning Tk thread pumps the mailbox
    assert overlay._root is None

