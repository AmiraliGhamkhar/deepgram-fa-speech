"""Floating Start/Stop control window -- the application's primary UI.

Borderless, always on top, draggable, and keyboard-quittable. It owns one
`SessionController`: pressing **شروع** builds a `LiveMedicalSTT` (which
validates configuration and obtains a short-lived session from the host on
first use), and pressing **توقف** requests a full clean shutdown.

Tkinter runs on its owning thread; the production overlay is a Toplevel
on that interpreter. Worker threads post to bounded mailboxes. Only the Tk
thread calls `after` to drain them, so audio/network workers never enter Tcl.

The host shared secret typed here is passed straight to
`config.save_host_credentials`, which protects it with Windows DPAPI. It is
never written to `settings.yaml` and never logged.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import tempfile
import threading
import time
from typing import Any, Callable, Optional, Protocol

from .. import paths
from ..config import ConfigError, get_settings, initialize_user_config, save_host_credentials
from . import colors, dispatch, fonts

log = logging.getLogger("medical_stt.ui.control")

_SYSTEM = platform.system().lower()

_TK_AVAILABLE = False
try:
    import tkinter as tk
    _TK_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on the Python build
    _TK_AVAILABLE = False

# The palette itself lives in `colors.py`, shared with the overlay; these
# aliases keep the widget-building code below short.
_BG = colors.BG
_SURFACE = colors.SURFACE
_BORDER = colors.BORDER
_FG = colors.FG
_MUTED = colors.MUTED
_OK = colors.OK
_REC = colors.REC
_ACCENT = colors.ACCENT
_ON_LIGHT = colors.ON_LIGHT

#: 96-DPI design diameters of the Canvas-drawn circular controls (GUI R1):
#: the Start/Stop toggle, the three settings actions, and the quit mark.
#: `_circle_button` scales them by the window's actual DPI at build time.
_TOGGLE_DIAMETER = 72
_SECONDARY_DIAMETER = 52
_QUIT_DIAMETER = 20
STATUS_DOT = "\u25cf"


def _darken(color: str, factor: float = 0.9) -> str:
    """Darken a `#rrggbb` colour by `factor`, for the toggle's hover state.

    Anything that is not a six-digit hex literal is returned unchanged:
    a hover effect must never be able to break the button it decorates.
    """
    if len(color) != 7 or not color.startswith("#"):
        return color
    try:
        channels = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    except ValueError:
        return color
    return "#" + "".join(f"{max(0, min(255, round(c * factor))):02x}" for c in channels)


class SessionControllerLike(Protocol):
    """Structural description of `app.SessionController`.

    Declared as a Protocol so this module does not import `app` (which
    imports this module's package); any object with this shape works.
    """

    last_error: Optional[str]

    def start(self) -> None: ...

    def stop(self, timeout: float = 8.0) -> bool: ...

    @property
    def is_running(self) -> bool: ...


class ControlWindow:
    """Start/Stop control window. `run()` blocks until the user quits.

    Takes any object with the `SessionController` shape
    (`start`/`stop`/`is_running`/`last_error`) so this module does not
    import `app`, which imports this module's package.
    """

    def __init__(self, controller: "SessionControllerLike") -> None:
        self._controller = controller
        self._root: Optional["tk.Tk"] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._closed = False
        self._initialized = False
        self._mailbox = dispatch.Mailbox()
        self._drag_origin: Optional[tuple[int, int]] = None
        self._starting = False
        self._saving = False
        #: Connection test in flight. Its OWN flag: it used to borrow
        #: `_saving`, so a running test silently blocked saving (A3.4).
        self._testing = False
        #: Set when the window had to render without usable settings (A3.1).
        self._config_error = ""
        self._start_done = threading.Event()
        self._start_done.set()
        self._client_id_var: Optional["tk.StringVar"] = None
        self._running = False
        self._stopping = False
        self._close_requested = False
        self._status: Optional["tk.Label"] = None
        self._detail: Optional["tk.Label"] = None
        # The Start/Stop toggle is a Canvas-drawn circle, not a tk.Button:
        # `_toggle` is the canvas, `_toggle_oval`/`_toggle_text` its items.
        self._toggle: Optional["tk.Canvas"] = None
        self._toggle_oval: Optional[int] = None
        self._toggle_text: Optional[int] = None
        self._toggle_color: str = _OK
        self._toggle_hover = False
        self._settings_entries: list[Any] = []
        self._test_circle: Optional[Any] = None
        self._save_circle: Optional[Any] = None
        #: All three settings-action circles (test / recovery / save), so
        #: shared state reflections (the recording ring tint) reach the
        #: recovery circle too, not just the two `_refresh_settings_state`
        #: enables or disables.
        self._secondary_circles: list[Any] = []
        #: Pixel scale of THIS window (actual DPI / 96, `fonts` owns the
        #: query). Set once in `_run` before `_build`; 1.0 off Windows.
        self._dpi_scale: float = 1.0
        self._drag_widgets: tuple[Any, ...] = ()
        self._circle_states: dict[Any, dict[str, Any]] = {}
        self._action_row: Optional["tk.Frame"] = None
        self._quit: Optional["tk.Canvas"] = None
        self._url_var: Optional["tk.StringVar"] = None
        self._secret_var: Optional["tk.StringVar"] = None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> bool:
        """Create the window on its own thread. False if Tk is unavailable."""
        if not _TK_AVAILABLE:
            log.error("Tkinter is not available; the control window cannot start")
            return False
        self._thread = threading.Thread(target=self._run, name="control-ui", daemon=True)
        self._thread.start()
        return self._ready.wait(timeout=5.0) and self._initialized

    def run(self) -> None:
        """Start the window and block until it is closed."""
        if not self.start():
            raise RuntimeError("cannot start the control window (Tkinter unavailable)")
        if self._thread is not None:
            self._thread.join()

    def close(self) -> None:
        """Stop any session before destroying the window, without blocking Tk."""
        if self._closed or self._close_requested:
            return
        self._close_requested = True
        self._stop()

    def _destroy_window(self) -> None:
        def _destroy() -> None:
            if self._root is not None:
                self._save_window_position()
                try:
                    self._root.destroy()
                except tk.TclError:  # pragma: no cover - already gone
                    pass
                self._root = None

        self._ui(_destroy, force=True)
        self._closed = True

    def _finish_stop(self, completed: bool) -> None:
        """Update the UI after the controller's bounded join has returned."""
        self._stopping = False
        if completed or (not self._starting and not self._controller.is_running):
            self._running = False
            self._set_button("شروع", _OK)
            self._set_status(f"{STATUS_DOT} آماده", _MUTED, getattr(self._controller, "health", {}).get("warning", ""))
            if self._close_requested:
                self._destroy_window()
            return

        self._running = True
        self._close_requested = False
        self._set_status(f"{STATUS_DOT} توقف کامل نشد", _REC, "جلسه هنوز در حال پایان است.")

    # -- Tk thread --------------------------------------------------------

    def _run(self) -> None:
        try:
            # Before the first interpreter exists: a Tk root created while the
            # process is DPI-unaware gets bitmap-scaled on Windows (R2).
            fonts.enable_windows_dpi_awareness()
            self._root = tk.Tk()
            fonts.apply_tk_scaling(self._root)
            # Once per window, before any circle is built: fonts scale via
            # tk scaling above, Canvas geometry scales via this factor.
            self._dpi_scale = fonts.dpi_scale_factor(self._root)
            dispatch.install_owner(self._root, self._mailbox)
            self._build(self._root)
            # No WM_DELETE_WINDOW protocol: `_build` sets
            # overrideredirect(True), which removes the window-manager
            # frame entirely, so that protocol can never fire. Escape (and
            # the ✕ label in the header) are the close paths.
            self._root.bind("<Escape>", lambda _e: self.close())
            self._initialized = True
            self._ready.set()
            self._pump()
            self._root.mainloop()
        except tk.TclError as exc:
            log.error("failed to create the control window: %s", exc)
        except Exception as exc:  # noqa: BLE001 - any build fault, not just Tk's
            # `_build` touches settings and the filesystem, so it can raise
            # more than TclError (an unreadable settings file, a widget
            # option Tk refuses, ...). Uncaught, that killed the UI thread
            # with `_ready` never set: `start()` then blocked its full 5 s
            # and reported a bare "cannot start" with nothing in the log.
            # Only the exception TYPE is logged -- messages from the config
            # layer are user-facing text, not log material.
            log.error("control window build failed (%s)", type(exc).__name__)
        finally:
            # Idempotent: the success path has already set it before
            # `mainloop()`, so this only runs first for a failed build --
            # which is exactly the case that must not hang `start()`.
            self._ready.set()
            dispatch.clear_owner()

    def _pump(self) -> None:
        self._mailbox.drain()
        if self._root is not None:
            self._root.after(25, self._pump)

    def _build(self, root: "tk.Tk") -> None:
        root.title("Medical STT")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.resizable(False, False)
        root.configure(bg=_BORDER)
        # Deliberately fully opaque (R3/crisp): `-alpha` turns the control
        # surface into a layered window and blurs it on Windows. Only the
        # overlay is translucent (it needs WS_EX_TRANSPARENT hit testing).

        inner = tk.Frame(root, bg=_BG, padx=16, pady=12)
        inner.pack(fill="both", expand=True)

        header = tk.Frame(inner, bg=_BG)
        header.pack(fill="x")
        title = tk.Label(
            header, text="Medical STT", bg=_BG, fg=_FG, anchor="w",
            font=fonts.font(root, 10, "bold"),
        )
        title.pack(side="right")
        self._quit = self._circle_button(
            header, _QUIT_DIAMETER, self.close,
            text="\u2715", fill=_SURFACE, hover_fill=_REC, text_fill=_MUTED,
            font=fonts.font(root, 7, "bold"), tag="quit",
        )
        self._quit.pack(side="left", padx=(0, 6))

        self._status = tk.Label(
            inner, text=f"{STATUS_DOT} آماده", bg=_BG, fg=_MUTED, anchor="e", justify="right",
            font=fonts.font(root, 10),
        )
        self._status.pack(fill="x", pady=(8, 0))

        toggle = self._circle_button(
            inner, _TOGGLE_DIAMETER, self._on_toggle,
            fill=self._toggle_color, remember=True,
        )
        self._toggle = toggle
        # Centered, never fill="x": a circle must not stretch with the window.
        toggle.pack(anchor="center", pady=(8, 0))

        self._detail = tk.Label(
            inner, text="", bg=_BG, fg=_MUTED, anchor="e", justify="right",
            wraplength=320, font=fonts.font(root, 8),
        )
        self._detail.pack(fill="x", pady=(8, 0))

        separator = tk.Frame(inner, bg=_BORDER, height=1)
        separator.pack(fill="x", pady=10)

        # Host settings. The secret is write-only as far as this window is
        # concerned: it is loaded masked and saved straight to DPAPI.
        self._url_var = tk.StringVar(value=self._current_host_url())
        self._secret_var = tk.StringVar(value="")

        url_label = tk.Label(inner, text="میزبان (https)", bg=_BG, fg=_MUTED, anchor="e",
                 font=fonts.font(root, 8))
        url_label.pack(fill="x")
        url_entry = tk.Entry(inner, textvariable=self._url_var, bg=_SURFACE, fg=_FG,
                 insertbackground=_FG, relief="flat", justify="left",
                 font=fonts.font(root, 9))
        url_entry.pack(fill="x", pady=(2, 0), ipady=3)

        secret_label = tk.Label(inner, text="کلید مشترک", bg=_BG, fg=_MUTED, anchor="e",
                 font=fonts.font(root, 8))
        secret_label.pack(fill="x", pady=(6, 0))
        secret_entry = tk.Entry(inner, textvariable=self._secret_var, show="\u2022", bg=_SURFACE,
                 fg=_FG, insertbackground=_FG, relief="flat", justify="left",
                 font=fonts.font(root, 9))
        secret_entry.pack(fill="x", pady=(2, 0), ipady=3)

        try:
            client_id_value = get_settings().host_client_id
        except ConfigError as exc:
            # A malformed settings file must not kill the Tk thread: `_run`
            # only survives TclError, and a window that never renders can
            # never tell the user what to fix. Render with empty fields and
            # say so below instead.
            client_id_value = ""
            self._config_error = str(exc)
            log.error("control window opened with unusable settings: %s", exc)
        self._client_id_var = tk.StringVar(value=client_id_value)
        id_label = tk.Label(inner, text="شناسه دستگاه", bg=_BG, fg=_MUTED, anchor="e",
                 font=fonts.font(root, 8))
        id_label.pack(fill="x", pady=(6, 0))
        id_entry = tk.Entry(inner, textvariable=self._client_id_var, bg=_SURFACE, fg=_FG,
                 insertbackground=_FG, relief="flat", justify="right",
                 font=fonts.font(root, 9))
        id_entry.pack(fill="x", pady=(2, 0), ipady=3)
        self._settings_entries = [url_entry, secret_entry, id_entry]
        # The three settings actions are 52px circles in one RTL row: the
        # first action sits at the right edge, reading order follows Persian.
        self._action_row = tk.Frame(inner, bg=_BG)
        self._action_row.pack(fill="x", pady=(8, 0))
        for label, command, glyph in (
            ("بررسی\nاتصال", self._test_connection, "↗"),
            ("متن\nبازیابی", self._show_recovery, "≡"),
            ("ذخیره\nتنظیمات", self._on_save_settings, "✓"),
        ):
            circle = self._circle_button(
                self._action_row, _SECONDARY_DIAMETER, command,
                text=label, fill=_SURFACE, hover_fill=_BORDER, text_fill=_FG,
                font=fonts.font(root, 8), tag="action", glyph=glyph, on_disabled=command,
            )
            circle.pack(side="right", padx=4)
            self._secondary_circles.append(circle)
            if command == self._test_connection:
                self._test_circle = circle
            elif command == self._on_save_settings:
                self._save_circle = circle

        # Drag support: the window is borderless, so there is no title bar
        # to move it with.
        self._drag_widgets = (root, inner, header, title, self._status, self._detail,
                              url_label, secret_label, id_label, separator)
        for widget in self._drag_widgets:
            widget.bind("<ButtonPress-1>", self._on_drag_start)
            widget.bind("<B1-Motion>", self._on_drag_move)
            widget.bind("<ButtonRelease-1>", self._on_drag_end)

        if self._config_error:
            # Written directly, not via the mailbox: this IS the Tk thread,
            # and the pump has not started yet.
            self._status.config(text=f"{STATUS_DOT} خطای پیکربندی", fg=_REC)
            self._detail.config(text="فایل تنظیمات نامعتبر است؛ آن را اصلاح کنید و دوباره تلاش کنید")

        x, y = self._load_window_position()
        root.geometry(f"+{x}+{y}")

    # -- dragging ---------------------------------------------------------

    def _on_drag_start(self, event: object) -> None:
        # Tk's toplevel bindtag also receives descendant events. Entries and
        # circles must not initiate drags via the root binding.
        widget = getattr(event, "widget", None)
        if widget is not None and widget not in self._drag_widgets:
            return
        self._drag_origin = (getattr(event, "x_root", 0), getattr(event, "y_root", 0))
        self._drag_window_origin = (
            (self._root.winfo_x(), self._root.winfo_y()) if self._root is not None else (0, 0)
        )

    def _on_drag_move(self, event: object) -> None:
        if self._root is None:
            return
        origin = getattr(self, "_drag_origin", None)
        if origin is None:
            return
        # `geometry("+x+y")` sets an ABSOLUTE screen position, so the drag
        # delta offsets the PRESS-TIME window origin -- never the current
        # `winfo_x()`/`winfo_y()`. This handler used to add the cumulative
        # delta-from-press to the current position on every motion event:
        # once Tk applied the first move, each later event re-added the
        # whole delta and the window accelerated away from the cursor.
        # (The even older bug, audit N-02, passed the raw delta as the
        # position and parked the window near the screen origin.)
        wx, wy = getattr(self, "_drag_window_origin", (self._root.winfo_x(), self._root.winfo_y()))
        dx = getattr(event, "x_root", 0) - origin[0]
        dy = getattr(event, "y_root", 0) - origin[1]
        self._root.geometry(f"+{wx + dx}+{wy + dy}")

    def _on_drag_end(self, event: object = None) -> None:
        """A finished drag persists its position (R3/persistent drag).

        A release without a drag (a button click) must not touch the state
        file: `_drag_origin` is only set by `_on_drag_start`.
        """
        if getattr(self, "_drag_origin", None) is None:
            return
        self._drag_origin = None
        self._save_window_position()

    def _load_window_position(self) -> "tuple[int, int]":
        """Restore the last dragged position, or the default corner.

        Corrupt or absurd state falls back to the default rather than
        refusing to start; the file is a UI nicety, not configuration.
        """
        try:
            raw = json.loads(paths.ui_state_path().read_text(encoding="utf-8"))
            x, y = raw["x"], raw["y"]
            if type(x) is int and type(y) is int and abs(x) <= 100000 and abs(y) <= 100000:
                return x, y
        except Exception as exc:  # noqa: BLE001 - see docstring
            log.debug("window position not restored (%s)", type(exc).__name__)
        return 40, 40

    def _save_window_position(self) -> None:
        """Persist the window position (atomic, best effort)."""
        root = self._root
        if root is None:
            return
        try:
            x, y = int(root.winfo_x()), int(root.winfo_y())
            target = paths.ui_state_path()
            target.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(
                dir=str(target.parent), prefix=".ui-state-", suffix=".tmp"
            )
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump({"x": x, "y": y}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            except BaseException:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
        except Exception as exc:  # noqa: BLE001 - a UI nicety, never fatal
            log.debug("window position not saved (%s)", type(exc).__name__)

    # -- state / actions --------------------------------------------------

    @staticmethod
    def _current_host_url() -> str:
        try:
            return get_settings().host_url
        except ConfigError:
            return ""

    def _ui(self, fn: Callable[[], None], *, force: bool = False, key: str = "action") -> None:
        if self._closed and not force:
            return
        self._mailbox.post("close" if force else key, fn)

    def _refresh_settings_state(self) -> None:
        """Tk-thread-only visual reflection of the existing action guards."""
        busy = self._starting or self._running or self._stopping or self._saving
        # While recording, the three secondary circles carry a WARNING-tinted
        # ring (`_paint_circle`): one quiet visual link between the red
        # toggle and the settings actions that are locked until Stop.
        for circle in self._secondary_circles:
            state = self._circle_states.get(circle)
            if state is not None:
                state["recording_ring"] = bool(self._running)
        for entry in self._settings_entries:
            entry.config(state="disabled" if busy else "normal",
                         disabledbackground=_SURFACE, disabledforeground=_MUTED,
                         fg=_FG)
        if self._test_circle is not None:
            self._set_circle_enabled(self._test_circle, not (busy or self._testing))
        if self._save_circle is not None:
            # Connection tests deliberately do not block saving (A3.4).
            self._set_circle_enabled(self._save_circle, not busy)
        for circle in self._secondary_circles:
            # Test/save were just repainted by `_set_circle_enabled`; the
            # recovery circle's enablement never changes, but its ring does.
            if circle not in (self._test_circle, self._save_circle) and circle in self._circle_states:
                self._paint_circle(circle)

    def _set_status(self, text: str, color: str, detail: str = "") -> None:
        def _() -> None:
            self._refresh_settings_state()
            if self._status is not None:
                self._status.config(text=text, fg=color)
            if self._detail is not None:
                self._detail.config(text=detail)

        self._ui(_, key="status")

    def _on_toggle(self) -> None:
        if self._stopping:
            # Say so: a press that silently does nothing reads as a frozen UI.
            self._set_status(f"{STATUS_DOT} در حال توقف...", _MUTED)
            return
        if self._running or self._starting:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        if self._starting:
            # Unreachable from the circle (`_on_toggle` routes an in-flight
            # start to Stop), but a direct caller must still see why nothing
            # happened instead of a silently ignored press (A3.3).
            self._set_status(f"{STATUS_DOT} در حال اتصال...", _MUTED)
            return
        if self._saving:
            # Reachable: pressing the circle while a settings save was in
            # flight returned with no feedback at all, which reads as a dead
            # button on the application's primary control.
            self._set_status(f"{STATUS_DOT} در حال ذخیره…؛ برای شروع صبر کنید", _MUTED)
            return
        self._starting = True
        self._start_done.clear()
        self._set_status(f"{STATUS_DOT} در حال اتصال...", _MUTED)
        self._set_button("توقف", _REC)

        def prepare() -> None:
            error = ""
            try:
                self._controller.start()
            except Exception as exc:
                error = str(exc)
            finally:
                self._start_done.set()
            def finish() -> None:
                self._starting = False
                if self._stopping or self._closed:
                    return
                if error:
                    self._running = False
                    self._set_button("شروع", _OK)
                    self._set_status(f"{STATUS_DOT} شروع ناموفق", _REC, error)
                    return
                self._running = True
                self._refresh_settings_state()
                threading.Thread(target=self._watch, name="control-watch", daemon=True).start()
            self._ui(finish, key="start-complete")
        threading.Thread(target=prepare, name="control-start", daemon=True).start()

    def _stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        getattr(self._controller, "cancel_start", lambda: None)()
        self._set_status(f"{STATUS_DOT} در حال توقف...", _MUTED)

        def stop_controller() -> None:
            try:
                # Stop requested during construction must not be lost. Do not
                # block Tk, and do not destroy it until construction is finished.
                if not self._start_done.wait(timeout=8.0):
                    completed = False
                else:
                    completed = self._controller.stop()
            except Exception:  # noqa: BLE001 - shutdown status must reach the UI
                log.exception("session controller stop raised")
                completed = False
            self._ui(lambda: self._finish_stop(completed))

        threading.Thread(target=stop_controller, name="control-stop", daemon=True).start()

    def _watch(self) -> None:
        """Reflect a session that ended on its own (error, or clean exit)."""
        last_health = None
        while self._controller.is_running:
            health = getattr(self._controller, "health", {})
            state = (health.get("phase", "connecting"), health.get("warning", ""))
            if state != last_health and not self._stopping:
                phase, warning = state
                labels = {"recording": f"{STATUS_DOT} در حال ضبط...", "reconnecting": f"{STATUS_DOT} اتصال مجدد...", "stopping": f"{STATUS_DOT} در حال توقف...", "idle": f"{STATUS_DOT} آماده"}
                self._set_status(labels.get(phase, f"{STATUS_DOT} در حال اتصال..."), _REC if phase == "recording" else _MUTED, warning)
                last_health = state
            time.sleep(0.2)
        if not self._running:
            return  # the user already pressed Stop; _stop() updated the UI
        self._running = False
        self._set_button("شروع", _OK)
        error = self._controller.last_error
        if error:
            self._set_status(f"{STATUS_DOT} خطا", _REC, error)
        else:
            self._set_status(f"{STATUS_DOT} آماده", _MUTED, getattr(self._controller, "health", {}).get("warning", ""))

    def _circle_button(
        self, parent: Any, diameter: int, command: Callable[[], None], *,
        text: str = "شروع", fill: str = _OK, text_fill: str = _ON_LIGHT,
        font: Optional[tuple] = None, tag: str = "toggle",
        hover_fill: Optional[str] = None, remember: bool = False,
        glyph: str = "", on_disabled: Optional[Callable[[], None]] = None,
    ) -> "tk.Canvas":
        """One Canvas circle implementation for primary, secondary and quit.

        Oval, caption and optional glyph share the click tag. A click takes
        focus and activates; press/release and hover only recolor, with no
        animation. The dashed focus ring is independent of the 2px state ring.
        Disabled controls retain guarded feedback, never silently eat a click.
        All painting is called exclusively by the Tk/mailbox thread.

        `diameter` is the 96-DPI design size. Everything pixel-based in the
        box -- the canvas, the oval inset, the ring widths, the caption's
        wrap width and the glyph/caption offsets -- is derived from the
        SCALED diameter (`self._dpi_scale`, see `fonts.dpi_scale_factor`),
        computed once here at build time. At 96 DPI (and off Windows) the
        numbers are exactly the historical 72/52/20 px geometry.
        """
        scale = self._dpi_scale
        size = fonts.scaled_pixels(diameter, scale)
        inset = fonts.scaled_pixels(2, scale)
        ring_width = fonts.scaled_pixels(2, scale)
        focus_inset = fonts.scaled_pixels(1, scale)
        # A caption that wraps to two lines ("بررسی\nاتصال") grows toward
        # the glyph above it; 1pt smaller keeps the flat two-item layout
        # collision-free at every scale instead of moving anything.
        caption_font = font or fonts.font(parent, 12, "bold")
        if "\n" in text and len(caption_font) >= 2 and isinstance(caption_font[1], int):
            caption_font = (caption_font[0], max(1, caption_font[1] - 1)) + tuple(caption_font[2:])
        canvas = tk.Canvas(parent, width=size, height=size, bg=_BG,
                           highlightthickness=0, cursor="hand2", takefocus=1)
        oval = canvas.create_oval(inset, inset, size - inset, size - inset,
                                  fill=fill, outline=_BORDER, width=ring_width, tags=(tag,))
        label = canvas.create_text(
            size / 2, size * .67 if glyph else size / 2,
            text=text, fill=text_fill, font=caption_font,
            tags=(tag,), width=max(fonts.scaled_pixels(8, scale), size - fonts.scaled_pixels(10, scale)),
            justify="center",
        )
        labels = [label]
        if glyph:
            labels.append(canvas.create_text(size / 2, size * .28, text=glyph,
                          fill=text_fill, font=fonts.font(parent, 11), tags=(tag,)))
        focus = canvas.create_oval(focus_inset, focus_inset, size - focus_inset, size - focus_inset,
                                  fill="", outline=_ACCENT, width=focus_inset, dash=(2, 2),
                                  state="hidden")
        self._circle_states[canvas] = dict(
            oval=oval, labels=labels, focus=focus, fill=fill, text_fill=text_fill,
            hover_fill=hover_fill, hover=False, pressed=False, enabled=True,
            primary=remember, recording_ring=False,
        )

        def activate(_event: object = None) -> None:
            if self._circle_states[canvas]["enabled"]:
                command()
            elif on_disabled is not None:
                on_disabled()
            else:
                self._set_status(f"{STATUS_DOT} برای ادامه، ابتدا عملیات جاری را متوقف کنید", _MUTED)

        def press(_event: object) -> None:
            canvas.focus_set()
            self._circle_states[canvas]["pressed"] = True
            self._paint_circle(canvas)
            activate()

        def release(_event: object) -> None:
            self._circle_states[canvas]["pressed"] = False
            self._paint_circle(canvas)

        def hover(entered: bool) -> None:
            self._circle_states[canvas]["hover"] = entered
            if not entered:
                self._circle_states[canvas]["pressed"] = False
            if remember:
                self._toggle_hover = entered
            self._paint_circle(canvas)

        canvas.tag_bind(tag, "<Button-1>", press)
        canvas.tag_bind(tag, "<Enter>", lambda _e: hover(True))
        canvas.tag_bind(tag, "<Leave>", lambda _e: hover(False))
        canvas.bind("<ButtonRelease-1>", release)
        canvas.bind("<FocusIn>", lambda _e: canvas.itemconfig(focus, state="normal"))
        canvas.bind("<FocusOut>", lambda _e: canvas.itemconfig(focus, state="hidden"))
        canvas.bind("<Destroy>", lambda _e: self._circle_states.pop(canvas, None))
        canvas.bind("<Return>", activate)
        canvas.bind("<space>", activate)
        if remember:
            self._toggle_oval, self._toggle_text = oval, label
        return canvas

    def _paint_circle(self, canvas: Any) -> None:
        state = self._circle_states[canvas]
        fill = state["fill"]
        if not state["enabled"]:
            fill = colors.blend(fill, _BG)
        elif state["pressed"]:
            fill = _darken(fill, .8)
        elif state["hover"]:
            fill = state["hover_fill"] or _darken(fill)
        if state["primary"] and state["fill"] == _REC:
            ring = _REC
        elif state.get("recording_ring"):
            # Secondary circles while a session is recording (driven by
            # `_refresh_settings_state`): WARNING at blended 50% weight
            # over the normal ring -- palette-derived, like every colour.
            ring = colors.blend(colors.WARNING, _BORDER)
        else:
            ring = _BORDER
        canvas.itemconfig(state["oval"], fill=fill, outline=ring)
        ink = state["text_fill"] if state["enabled"] else colors.blend(state["text_fill"], _BG)
        for label in state["labels"]:
            canvas.itemconfig(label, fill=ink)

    def _set_circle_enabled(self, canvas: Any, enabled: bool) -> None:
        self._circle_states[canvas]["enabled"] = enabled
        self._circle_states[canvas]["pressed"] = False
        canvas.config(cursor="hand2" if enabled else "arrow")
        self._paint_circle(canvas)

    def _paint_toggle(self) -> None:
        if self._toggle is None:
            return
        state = self._circle_states[self._toggle]
        state["fill"] = self._toggle_color
        state["hover"] = self._toggle_hover
        self._paint_circle(self._toggle)

    def _set_button(self, text: str, color: str) -> None:
        def _() -> None:
            self._toggle_color = color
            self._paint_toggle()
            if self._toggle is not None and self._toggle_text is not None:
                self._toggle.itemconfig(self._toggle_text, text=text, fill=_ON_LIGHT)

        self._ui(_, key="button")

    def _on_save_settings(self) -> None:
        if self._saving:
            self._set_status(f"{STATUS_DOT} در حال ذخیره…", _MUTED)
            return
        if self._starting or self._running or self._stopping:
            self._set_status(f"{STATUS_DOT} برای ذخیره، ابتدا ضبط را متوقف کنید", _MUTED)
            return
        url = self._url_var.get().strip() if self._url_var is not None else ""
        secret = self._secret_var.get().strip() if self._secret_var is not None else ""
        client_id = self._client_id_var.get().strip() if self._client_id_var is not None else ""
        self._saving = True
        self._set_status(f"{STATUS_DOT} در حال ذخیره...", _MUTED)
        def save() -> None:
            error = ""
            try:
                initialize_user_config()
                save_host_credentials(url, secret, client_id)
            except Exception as exc:
                error = str(exc)
            def finish() -> None:
                self._saving = False
                if error:
                    self._set_status(f"{STATUS_DOT} ذخیره نشد", _REC, error)
                else:
                    if self._secret_var is not None:
                        self._secret_var.set("")
                    # An empty secret field means save_host_credentials kept
                    # the stored secret; claiming a plain "saved" would
                    # overstate what happened (A3.5).
                    self._set_status(
                        f"{STATUS_DOT} تنظیمات ذخیره شد" if secret else f"{STATUS_DOT} تنظیمات ذخیره شد (کلید قبلی حفظ شد)",
                        _OK,
                    )
            self._ui(finish, key="save-complete")
        threading.Thread(target=save, daemon=True, name="settings-save").start()

    def _test_connection(self) -> None:
        if self._stopping:
            # A stop is in flight, so the stored settings are being torn down
            # with the session; a test here would report on a configuration
            # nobody is about to use. Refuse it AND say so (A3.3).
            self._set_status(f"{STATUS_DOT} در حال توقف…", _MUTED)
            return
        if self._testing:
            self._set_status(f"{STATUS_DOT} در حال بررسی اتصال…", _MUTED)
            return
        if self._saving:
            self._set_status(f"{STATUS_DOT} در حال ذخیره…", _MUTED)
            return
        if self._starting or self._running:
            self._set_status(f"{STATUS_DOT} برای بررسی اتصال، ابتدا ضبط را متوقف کنید", _MUTED)
            return
        self._testing = True
        self._set_status(f"{STATUS_DOT} بررسی تنظیمات ذخیره‌شده...", _MUTED)
        def check() -> None:
            error = ""
            try:
                from ..host_client import HostSessionClient
                from ..config import validate_settings
                settings = get_settings()
                errors = validate_settings(settings)
                if errors:
                    raise ConfigError("; ".join(errors))
                HostSessionClient(settings.host_url, settings.host_secret,
                                  timeout=settings.host_timeout_seconds,
                                  client_id=settings.host_client_id).fetch_session()
            except Exception as exc:
                error = str(exc)
            def finish() -> None:
                self._testing = False
                self._set_status(f"{STATUS_DOT} اتصال ناموفق" if error else f"{STATUS_DOT} دریافت مجوز موفق", _REC if error else _OK, error)
            self._ui(finish, key="connection-test")
        threading.Thread(target=check, daemon=True, name="connection-test").start()

    def _show_recovery(self) -> None:
        # Explicit review only: never automatically paste into the focused EMR.
        popup = tk.Toplevel(self._root)
        popup.title("متن بازیابی — فقط در حافظه")
        popup.configure(bg=_BG)
        popup.transient(self._root)
        try:
            popup.grab_set()
        except tk.TclError:  # pragma: no cover - no grab without a viewable window
            pass
        popup.bind("<Escape>", lambda _e: popup.destroy())
        text = tk.Text(popup, wrap="word", width=70, height=16, bg=_SURFACE, fg=_FG,
                       insertbackground=_FG, relief="flat", font=fonts.font(popup, 9))
        text.pack(fill="both", expand=True)
        text.insert("1.0", getattr(self._controller, "recovery_text", lambda: "")())
        # Text has no widget-level justify option; a full-range tag is the
        # correct Tk API, applied before making the widget read-only.
        text.tag_configure("rtl", justify="right")
        text.tag_add("rtl", "1.0", "end")
        text.configure(state="disabled")
        feedback = tk.Label(popup, text="", bg=_BG, fg=_OK, anchor="e", font=fonts.font(popup, 8))
        feedback.pack(fill="x")
        def copy() -> None:
            popup.clipboard_clear()
            popup.clipboard_append(text.get("1.0", "end-1c"))
            feedback.config(text="کپی شد")
        actions = tk.Frame(popup, bg=_BG)
        actions.pack(anchor="e", pady=4)
        self._circle_button(actions, _SECONDARY_DIAMETER, copy, text="کپی", glyph="⧉",
                            fill=_SURFACE, hover_fill=_BORDER, text_fill=_FG,
                            font=fonts.font(popup, 8), tag="action").pack(side="right", padx=4)
        def clear() -> None:
            getattr(self._controller, "clear_recovery", lambda: None)()
            popup.destroy()
        self._circle_button(actions, _SECONDARY_DIAMETER, clear, text="پاک‌کردن", glyph="×",
                            fill=_SURFACE, hover_fill=_BORDER, text_fill=_FG,
                            font=fonts.font(popup, 8), tag="action").pack(side="right", padx=4)
