"""Control-window stop status tests without requiring Tk or a display."""
from __future__ import annotations

import re
import threading
import time
from types import SimpleNamespace

from medical_stt.ui import control
from medical_stt.ui.control import ControlWindow, _darken


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


# -- fake-Tk harness -------------------------------------------------------
#
# `_build` runs the real widget-construction code against these stand-ins,
# so layout/theming regressions are caught without a display. `after()`
# deliberately does NOT invoke its callback: `_pump` reschedules itself
# through it and would recurse forever.


class _FakeTclError(Exception):
    pass


class _FakeStringVar:
    def __init__(self, value=""):
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value


class _FakeWidget:
    kind = "Widget"

    def __init__(self, parent=None, **kwargs):
        self.parent = parent
        self.kwargs = dict(kwargs)
        self.children = []
        if parent is not None and hasattr(parent, "children"):
            parent.children.append(self)
        self.pack_kwargs = {}
        self.bindings = {}
        self.tag_bindings = {}
        self.config_calls = []
        self.items = {}
        self._next_item = 0
        self.calls = []
        self.destroyed = False
        self._x = 500
        self._y = 300
        self.geometry_calls = []
        self._text = ""
        self.grabbed = False
        self.transient_of = None
        self.clipboard = ""

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def find(self, kind):
        return [w for w in self.walk() if w.kind == kind]

    def pack(self, **kwargs):
        self.pack_kwargs = kwargs

    def bind(self, sequence, fn=None):
        self.bindings[sequence] = fn

    def tag_bind(self, tag, sequence, fn):
        self.tag_bindings[(tag, sequence)] = fn

    def config(self, **kwargs):
        self.config_calls.append(kwargs)
        self.kwargs.update(kwargs)

    configure = config

    def _new_item(self, item_kind, args, kwargs):
        self._next_item += 1
        self.items[self._next_item] = {"kind": item_kind, "args": args, **kwargs}
        return self._next_item

    def create_oval(self, *args, **kwargs):
        return self._new_item("oval", args, kwargs)

    def create_text(self, *args, **kwargs):
        return self._new_item("text", args, kwargs)

    def itemconfig(self, item, **kwargs):
        self.items[item].update(kwargs)

    def focus_set(self):
        self.calls.append("focus_set")

    def title(self, text=None):
        self.calls.append(("title", text))

    def overrideredirect(self, _value=None):
        pass

    def attributes(self, *_args):
        pass

    def resizable(self, *_args):
        pass

    def protocol(self, *args):
        self.calls.append(("protocol",) + args)

    def mainloop(self):
        pass

    def after(self, _delay, _fn=None):
        return "after-id"

    def destroy(self):
        self.destroyed = True

    def geometry(self, spec=None):
        if spec:
            self.geometry_calls.append(spec)
            match = re.fullmatch(r"\+(-?\d+)\+(-?\d+)", spec)
            if match:
                self._x, self._y = int(match.group(1)), int(match.group(2))

    def winfo_x(self):
        return self._x

    def winfo_y(self):
        return self._y

    def insert(self, _index, text):
        self._text += text

    def get(self, *_args):
        return self._text

    def clipboard_clear(self):
        self.clipboard = ""

    def clipboard_append(self, text):
        self.clipboard += text

    def transient(self, parent=None):
        self.transient_of = parent

    def grab_set(self):
        self.grabbed = True


def _fake_widget_class(kind):
    return type(f"_Fake{kind}", (_FakeWidget,), {"kind": kind})


def _install_fake_tk(monkeypatch):
    fake_tk = SimpleNamespace(TclError=_FakeTclError, StringVar=_FakeStringVar)
    for kind in ("Tk", "Frame", "Label", "Button", "Entry", "Canvas", "Toplevel", "Text"):
        setattr(fake_tk, kind, _fake_widget_class(kind))
    monkeypatch.setattr(control, "_TK_AVAILABLE", True)
    monkeypatch.setattr(control, "tk", fake_tk, raising=False)
    monkeypatch.setattr(
        control, "get_settings",
        lambda: SimpleNamespace(host_url="https://stt.example.com", host_client_id="doctor-01"),
    )
    return fake_tk


def _built_window(monkeypatch, controller=None):
    _install_fake_tk(monkeypatch)
    window = ControlWindow(controller or _StoppedController())
    assert window.start() is True
    window._ui = lambda callback, force=False, key="action": callback()
    return window


# -- A1: circular Start/Stop button ------------------------------------------


def test_darken_scales_hex_colours_and_passes_garbage_through():
    assert _darken("#ffffff") == "#e6e6e6"
    assert _darken("#000000") == "#000000"
    assert _darken("red") == "red"
    assert _darken("#12345") == "#12345"
    assert _darken("#gggggg") == "#gggggg"


def test_toggle_is_a_centered_canvas_circle(monkeypatch):
    window = _built_window(monkeypatch)
    canvas = window._toggle

    assert canvas.kind == "Canvas"
    assert canvas.kwargs["width"] == control._TOGGLE_DIAMETER
    assert canvas.kwargs["height"] == control._TOGGLE_DIAMETER
    assert canvas.kwargs["bg"] == control._BG, "no rectangular halo around the circle"
    assert canvas.kwargs["highlightthickness"] == 0
    assert canvas.kwargs["cursor"] == "hand2"
    assert canvas.pack_kwargs.get("anchor") == "center"
    assert canvas.pack_kwargs.get("fill") != "x", "a circle must not stretch"

    oval = canvas.items[window._toggle_oval]
    x0, y0, x1, y1 = oval["args"]
    assert x1 - x0 == y1 - y0, "the oval's bounding box is square, so it is a circle"
    assert oval["fill"] == control._OK
    text = canvas.items[window._toggle_text]
    assert text["text"] == "شروع"
    assert text["fill"] == control._ON_LIGHT

    # Click target and hover live on the shared tag; keyboard stays usable.
    assert ("toggle", "<Button-1>") in canvas.tag_bindings
    assert ("toggle", "<Enter>") in canvas.tag_bindings
    assert ("toggle", "<Leave>") in canvas.tag_bindings
    assert "<Return>" in canvas.bindings and "<space>" in canvas.bindings
    # The circle must never be a drag handle: a press starts/stops.
    assert "<ButtonPress-1>" not in canvas.bindings
    assert "<B1-Motion>" not in canvas.bindings


def test_set_button_repaints_the_circle(monkeypatch):
    window = _built_window(monkeypatch)
    canvas = window._toggle

    window._set_button("توقف", control._ACCENT)
    assert canvas.items[window._toggle_oval]["fill"] == control._ACCENT
    assert canvas.items[window._toggle_text]["text"] == "توقف"

    window._set_button("شروع", control._OK)
    assert canvas.items[window._toggle_oval]["fill"] == control._OK
    assert canvas.items[window._toggle_text]["text"] == "شروع"


def test_hover_darkens_the_circle_and_leave_restores_it(monkeypatch):
    window = _built_window(monkeypatch)
    canvas = window._toggle

    window._on_toggle_hover()
    assert canvas.items[window._toggle_oval]["fill"] == _darken(control._OK)
    window._on_toggle_leave()
    assert canvas.items[window._toggle_oval]["fill"] == control._OK


def test_circle_click_starts_the_session(monkeypatch):
    started = threading.Event()

    class _Controller(_StoppedController):
        def start(self):
            started.set()

    window = _built_window(monkeypatch, _Controller())
    window._toggle.tag_bindings[("toggle", "<Button-1>")](_FakeEvent(0, 0))
    assert started.wait(timeout=1.0)


# -- A2: one flat theme, centralized colours ----------------------------------


def test_no_inline_hex_colours_outside_the_palette_module():
    """The palette is centralized so the two windows cannot drift apart."""
    from pathlib import Path

    ui_dir = Path(control.__file__).parent
    for name in ("control.py", "overlay.py"):
        source = (ui_dir / name).read_text(encoding="utf-8")
        assert not re.findall(r"#[0-9a-fA-F]{6}", source), f"{name} still spells a colour inline"


def test_secondary_buttons_share_the_flat_style(monkeypatch):
    from medical_stt.ui import colors

    window = _built_window(monkeypatch)
    buttons = {b.kwargs.get("text"): b for b in window._root.find("Button")}
    for label in ("بررسی اتصال", "متن بازیابی", "ذخیره تنظیمات"):
        button = buttons[label]
        assert button.kwargs["bg"] == colors.SURFACE
        assert button.kwargs["relief"] == "flat"
        assert button.kwargs["bd"] == 0
        assert "<Enter>" in button.bindings and "<Leave>" in button.bindings

    save = buttons["ذخیره تنظیمات"]
    save.bindings["<Enter>"](_FakeEvent(0, 0))
    assert save.kwargs["bg"] == colors.BORDER
    save.bindings["<Leave>"](_FakeEvent(0, 0))
    assert save.kwargs["bg"] == colors.SURFACE


def test_client_id_row_matches_the_other_settings_rows(monkeypatch):
    from medical_stt.ui import colors

    window = _built_window(monkeypatch)
    entry = next(
        e for e in window._root.find("Entry") if e.kwargs.get("textvariable") is window._client_id_var
    )
    assert entry.kwargs["bg"] == colors.SURFACE
    assert entry.kwargs["insertbackground"] == colors.FG
    assert entry.pack_kwargs.get("ipady") == 3
    label = next(w for w in window._root.find("Label") if w.kwargs.get("text") == "شناسه دستگاه")
    assert label.kwargs["font"] == ("Segoe UI", 8)


def test_recovery_popup_is_themed_modal_and_confirms_copy(monkeypatch):
    from medical_stt.ui import colors

    class _Controller(_StoppedController):
        def recovery_text(self):
            return "متن بازیابی نمونه"

        def clear_recovery(self):
            pass

    window = _built_window(monkeypatch, _Controller())
    window._show_recovery()

    popup = window._root.find("Toplevel")[0]
    assert popup.kwargs.get("bg") == colors.BG
    assert popup.transient_of is window._root
    assert popup.grabbed is True
    assert "<Escape>" in popup.bindings

    text = popup.find("Text")[0]
    assert text.kwargs["bg"] == colors.SURFACE
    assert text.kwargs["insertbackground"] == colors.FG
    assert "متن بازیابی نمونه" in text.get()

    copy_button = next(b for b in popup.find("Button") if b.kwargs.get("text") == "کپی با تأیید شما")
    assert copy_button.kwargs["bg"] == colors.SURFACE, "popup buttons use the same flat factory"
    copy_button.kwargs["command"]()
    assert "متن بازیابی نمونه" in popup.clipboard
    feedback = [w for w in popup.find("Label") if w.kwargs.get("text") == "کپی شد"]
    assert feedback, "copy() must say so instead of being a silent no-op"


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
