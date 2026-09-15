"""Overlay lifecycle tests that do not require a real GUI/display (task
section 6: "Add tests wherever practical without requiring a real Windows
GUI in CI")."""
from __future__ import annotations

from medical_stt.ui.overlay import TranscriptOverlay


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

    assert len(fake_root.scheduled) == 1, "destroy callback was not scheduled"
    assert overlay._closed is True

    # Running the scheduled callback must actually clear _root (the real
    # destroy() call), proving the callback does real work.
    fake_root.scheduled[0]()
    assert overlay._root is None

