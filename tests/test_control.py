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
        self.geometry_calls: list[str] = []
        self._x = 500
        self._y = 300

    def after(self, _delay, callback):
        callback()

    def destroy(self):
        self.destroyed = True

    def geometry(self, spec):
        self.geometry_calls.append(spec)

    def winfo_x(self):
        return self._x

    def winfo_y(self):
        return self._y


class _FakeEvent:
    def __init__(self, x_root: int, y_root: int) -> None:
        self.x_root = x_root
        self.y_root = y_root


def _capture_status(window: ControlWindow):
    statuses = []
    stop_incomplete = threading.Event()

    def set_status(text, color, detail=""):
        statuses.append((text, color, detail))
        if "توقف کامل نشد" in text:
            stop_incomplete.set()

    window._set_status = set_status
    window._ui = lambda callback, force=False, key="action": callback()
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


def test_drag_moves_the_window_with_the_cursor_not_to_the_delta():
    """`geometry("+x+y")` is absolute: the window must follow the cursor.

    Regression pin for audit N-02: the handler once passed the raw drag
    delta as an absolute position, so the first drag parked the window
    near the screen origin instead of moving it with the pointer.
    """
    window = ControlWindow(_StoppedController())
    fake_root = _FakeRoot()
    window._root = fake_root

    window._on_drag_start(_FakeEvent(520, 320))
    window._on_drag_move(_FakeEvent(600, 400))

    # Window at +500+300, pointer moved +80/+80 -> +580+380.
    assert fake_root.geometry_calls == ["+580+380"]


def test_drag_without_a_window_or_origin_is_ignored():
    window = ControlWindow(_StoppedController())
    window._on_drag_move(_FakeEvent(0, 0))  # no _root yet
    window._root = _FakeRoot()
    window._on_drag_move(_FakeEvent(0, 0))  # no drag origin yet
    assert window._root.geometry_calls == []


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
