"""Control-window stop status tests without requiring Tk or a display."""
from __future__ import annotations

import threading
import time

from medical_stt.ui.control import ControlWindow


class _TimedOutController:
    last_error = None
    is_running = True

    def __init__(self) -> None:
        self.stop_entered = threading.Event()
        self.release_stop = threading.Event()
        self.stop_thread_id = None

    def stop(self, timeout: float = 8.0) -> bool:
        self.stop_thread_id = threading.get_ident()
        self.stop_entered.set()
        self.release_stop.wait(timeout=1.0)
        return False


class _StoppedController:
    last_error = None
    is_running = False

    def stop(self, timeout: float = 8.0) -> bool:
        return True


class _FakeRoot:
    def __init__(self) -> None:
        self.destroyed = False

    def after(self, _delay, callback):
        callback()

    def destroy(self):
        self.destroyed = True


def _capture_status(window: ControlWindow):
    statuses = []
    stop_incomplete = threading.Event()

    def set_status(text, color, detail=""):
        statuses.append((text, color, detail))
        if "توقف کامل نشد" in text:
            stop_incomplete.set()

    window._set_status = set_status
    window._ui = lambda callback, force=False: callback()
    return statuses, stop_incomplete


def test_stop_is_nonblocking_and_timeout_does_not_report_ready():
    controller = _TimedOutController()
    window = ControlWindow(controller)
    window._running = True
    statuses, stop_incomplete = _capture_status(window)

    started = time.monotonic()
    window._stop()
    elapsed = time.monotonic() - started
    try:
        assert elapsed < 0.2, "Tk must not wait for the controller's bounded join"
        assert controller.stop_entered.wait(timeout=1.0)
        assert controller.stop_thread_id != threading.get_ident()
        assert window._stopping is True
        assert window._running is True
        assert statuses[-1][0] != "● آماده"
    finally:
        controller.release_stop.set()

    assert stop_incomplete.wait(timeout=1.0)
    assert window._stopping is False
    assert window._running is True
    assert "توقف" in statuses[-1][0]


def test_close_timeout_keeps_the_window_open_and_reports_incomplete_stop():
    controller = _TimedOutController()
    controller.release_stop.set()
    window = ControlWindow(controller)
    fake_root = _FakeRoot()
    window._root = fake_root
    statuses, stop_incomplete = _capture_status(window)

    window.close()

    assert stop_incomplete.wait(timeout=1.0)
    assert fake_root.destroyed is False
    assert window._closed is False
    assert window._close_requested is False
    assert "توقف" in statuses[-1][0]


def test_close_destroys_the_window_after_a_confirmed_stop():
    window = ControlWindow(_StoppedController())
    fake_root = _FakeRoot()
    window._root = fake_root
    _capture_status(window)

    window.close()

    deadline = time.monotonic() + 1.0
    while not fake_root.destroyed and time.monotonic() < deadline:
        time.sleep(0.005)
    assert fake_root.destroyed is True
    assert window._root is None
    assert window._closed is True
