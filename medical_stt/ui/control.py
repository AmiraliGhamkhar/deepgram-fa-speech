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

Where the pieces live
---------------------
=========================  ==================================================
`ui.circles`               the canvas-drawn circular controls themselves
`ui.strings`               every user-visible caption, in one catalog
`ui.state`                 the persisted window position
`ui.dispatch`              the worker-thread -> Tk mailbox
`ui.fonts`, `ui.colors`    DPI-aware fonts and the single palette
=========================  ==================================================

This module is the conductor: it builds the window out of those parts, owns
the Start/Stop state machine, and keeps every Tk call on the Tk thread.
"""
from __future__ import annotations

import logging
import platform
import threading
import time
from typing import Any, Callable, Dict, Literal, Optional, Protocol

from ..config import ConfigError, get_settings, initialize_user_config, save_host_credentials
from . import dispatch, fonts, state, strings
from .circles import CircleButton, darken as _darken
from .colors import (
    ACCENT as _ACCENT,
    BG as _BG,
    BORDER as _BORDER,
    FG as _FG,
    MUTED as _MUTED,
    OK as _OK,
    ON_LIGHT as _ON_LIGHT,
    REC as _REC,
    SURFACE as _SURFACE,
)

log = logging.getLogger("medical_stt.ui.control")

_SYSTEM = platform.system().lower()

_TK_AVAILABLE = False
try:
    import tkinter as tk

    _TK_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on the Python build
    _TK_AVAILABLE = False

#: Diameters of the Canvas-drawn circular controls (GUI R1):
#: the Start/Stop toggle, the three settings actions, and the quit mark.
_TOGGLE_DIAMETER = 72
_SECONDARY_DIAMETER = 52
_QUIT_DIAMETER = 20
STATUS_DOT = strings.STATUS_DOT

#: Re-exported for callers that imported the helpers from this module before
#: they moved. `_darken` lives in `ui.circles` (the only user) and the status
#: glyph in `ui.strings`.
__all__ = [
    "ControlWindow",
    "STATUS_DOT",
    "SessionControllerLike",
    "_ACCENT",
    "_darken",
]


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
        #: The circles, keyed by the canvas the rest of the code (and the
        #: tests) identify them by.
        self._circles: Dict[Any, CircleButton] = {}
        self._toggle_button: Optional[CircleButton] = None
        self._toggle_color: str = _OK
        self._settings_entries: list[Any] = []
        self._test_button: Optional[CircleButton] = None
        self._save_button: Optional[CircleButton] = None
        self._quit_button: Optional[CircleButton] = None
        self._drag_widgets: tuple[Any, ...] = ()
        self._action_row: Optional["tk.Frame"] = None
        self._url_var: Optional["tk.StringVar"] = None
        self._secret_var: Optional["tk.StringVar"] = None

    # -- compatibility accessors ------------------------------------------
    # The toggle is the window's primary control, and both the code below and
    # the tests refer to its canvas and item ids directly.
    @property
    def _toggle(self) -> Any:
        return self._toggle_button.widget() if self._toggle_button else None

    @property
    def _toggle_oval(self) -> Optional[int]:
        return self._toggle_button.oval if self._toggle_button else None

    @property
    def _toggle_text(self) -> Optional[int]:
        return self._toggle_button.label if self._toggle_button else None

    @property
    def _test_circle(self) -> Any:
        return self._test_button.widget() if self._test_button else None

    @property
    def _save_circle(self) -> Any:
        return self._save_button.widget() if self._save_button else None

    @property
    def _quit(self) -> Any:
        return self._quit_button.widget() if self._quit_button else None

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
            self._set_button(strings.START, _OK)
            self._set_status(
                strings.READY,
                _MUTED,
                getattr(self._controller, "health", {}).get("warning", ""),
            )
            if self._close_requested:
                self._destroy_window()
            return

        self._running = True
        self._close_requested = False
        self._set_status(strings.STOP_INCOMPLETE, _REC, strings.STOP_INCOMPLETE_DETAIL)

    # -- Tk thread --------------------------------------------------------

    def _run(self) -> None:
        try:
            # Before the first interpreter exists: a Tk root created while the
            # process is DPI-unaware gets bitmap-scaled on Windows (R2).
            fonts.enable_windows_dpi_awareness()
            self._root = tk.Tk()
            fonts.apply_tk_scaling(self._root)
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
            self._root.after(30, self._pump)

    # -- construction -----------------------------------------------------

    def _build(self, root: "tk.Tk") -> None:
        root.title(strings.TITLE)
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
            header, text=strings.TITLE, bg=_BG, fg=_FG, anchor="w",
            font=fonts.font(root, 10, "bold"),
        )
        title.pack(side="right")
        self._quit_button = self._make_circle(
            header, _QUIT_DIAMETER, self.close,
            text=strings.GLYPH_QUIT, fill=_SURFACE, hover_fill=_REC,
            text_fill=_MUTED, font=fonts.font(root, 7, "bold"), tag="quit",
        )
        self._quit_button.pack(side="left", padx=(0, 6))

        self._status = tk.Label(
            inner, text=strings.READY, bg=_BG, fg=_MUTED, anchor="e",
            justify="right", font=fonts.font(root, 10),
        )
        self._status.pack(fill="x", pady=(8, 0))

        self._toggle_button = self._make_circle(
            inner, _TOGGLE_DIAMETER, self._on_toggle,
            text=strings.START, fill=self._toggle_color, text_fill=_ON_LIGHT,
            font=fonts.font(root, 12, "bold"), primary=True,
        )
        # Centered, never fill="x": a circle must not stretch with the window.
        self._toggle_button.pack(anchor="center", pady=(8, 0))

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

        url_label = self._form_label(inner, root, strings.HOST_LABEL)
        url_entry = self._form_entry(inner, root, self._url_var, justify="left")
        secret_label = self._form_label(inner, root, strings.SECRET_LABEL, top=6)
        secret_entry = self._form_entry(inner, root, self._secret_var, justify="left", show="\u2022")

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
        id_label = self._form_label(inner, root, strings.CLIENT_ID_LABEL, top=6)
        id_entry = self._form_entry(inner, root, self._client_id_var, justify="right")
        self._settings_entries = [url_entry, secret_entry, id_entry]

        # The three settings actions are 52px circles in one RTL row: the
        # first action sits at the right edge, reading order follows Persian.
        self._action_row = tk.Frame(inner, bg=_BG)
        self._action_row.pack(fill="x", pady=(8, 0))
        for label, command, glyph in (
            (strings.ACTION_TEST, self._test_connection, strings.GLYPH_TEST),
            (strings.ACTION_RECOVERY, self._show_recovery, strings.GLYPH_RECOVERY),
            (strings.ACTION_SAVE, self._on_save_settings, strings.GLYPH_SAVE),
        ):
            circle = self._make_circle(
                self._action_row, _SECONDARY_DIAMETER, command,
                text=label, fill=_SURFACE, hover_fill=_BORDER, text_fill=_FG,
                font=fonts.font(root, 8), tag="action", glyph=glyph,
                on_disabled=command,
            )
            circle.pack(side="right", padx=4)
            if command == self._test_connection:
                self._test_button = circle
            elif command == self._on_save_settings:
                self._save_button = circle

        # Drag support: the window is borderless, so there is no title bar
        # to move it with.
        self._drag_widgets = (
            root, inner, header, title, self._status, self._detail,
            url_label, secret_label, id_label, separator,
        )
        for widget in self._drag_widgets:
            widget.bind("<ButtonPress-1>", self._on_drag_start)
            widget.bind("<B1-Motion>", self._on_drag_move)
            widget.bind("<ButtonRelease-1>", self._on_drag_end)

        if self._config_error:
            # Written directly, not via the mailbox: this IS the Tk thread,
            # and the pump has not started yet.
            self._status.config(text=strings.CONFIG_ERROR_STATUS, fg=_REC)
            self._detail.config(text=strings.CONFIG_ERROR_DETAIL)

        x, y = self._load_window_position()
        root.geometry(f"+{x}+{y}")

    def _form_label(self, parent: Any, root: Any, text: str, top: int = 0) -> Any:
        """One right-aligned caption above its entry."""
        label = tk.Label(
            parent, text=text, bg=_BG, fg=_MUTED, anchor="e",
            font=fonts.font(root, 8),
        )
        label.pack(fill="x", pady=(top, 0) if top else 0)
        return label

    def _form_entry(
        self,
        parent: Any,
        root: Any,
        variable: Any,
        *,
        justify: Literal["left", "center", "right"],
        show: str = "",
    ) -> Any:
        options: Dict[str, Any] = dict(
            textvariable=variable, bg=_SURFACE, fg=_FG,
            insertbackground=_FG, relief="flat", justify=justify,
            font=fonts.font(root, 9),
        )
        if show:
            options["show"] = show
        entry = tk.Entry(parent, **options)
        entry.pack(fill="x", pady=(2, 0), ipady=3)
        return entry

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
        wx, wy = getattr(
            self, "_drag_window_origin", (self._root.winfo_x(), self._root.winfo_y())
        )
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
        """Restore the last dragged position, or the default corner."""
        return state.load_window_position()

    def _save_window_position(self) -> None:
        """Persist the window position (atomic, best effort)."""
        root = self._root
        if root is None:
            return
        state.save_window_position(int(root.winfo_x()), int(root.winfo_y()))

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
        for entry in self._settings_entries:
            entry.config(
                state="disabled" if busy else "normal",
                disabledbackground=_SURFACE,
                disabledforeground=_MUTED,
                fg=_FG,
            )
        if self._test_button is not None:
            self._test_button.set_enabled(not (busy or self._testing))
        if self._save_button is not None:
            # Connection tests deliberately do not block saving (A3.4).
            self._save_button.set_enabled(not busy)

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
            self._set_status(strings.STOPPING, _MUTED)
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
            self._set_status(strings.ALREADY_STARTING, _MUTED)
            return
        if self._saving:
            # Reachable: pressing the circle while a settings save was in
            # flight returned with no feedback at all, which reads as a dead
            # button on the application's primary control.
            self._set_status(strings.SAVING_BLOCKS_START, _MUTED)
            return
        self._starting = True
        self._start_done.clear()
        self._set_status(strings.CONNECTING, _MUTED)
        self._set_button(strings.STOP, _REC)

        def prepare() -> None:
            error = ""
            try:
                self._controller.start()
            except Exception as exc:  # noqa: BLE001 - reported in the UI instead
                error = str(exc)
            finally:
                self._start_done.set()

            def finish() -> None:
                self._starting = False
                if self._stopping or self._closed:
                    return
                if error:
                    self._running = False
                    self._set_button(strings.START, _OK)
                    self._set_status(strings.START_FAILED, _REC, error)
                    return
                self._running = True
                self._refresh_settings_state()
                threading.Thread(
                    target=self._watch, name="control-watch", daemon=True
                ).start()

            self._ui(finish, key="start-complete")

        threading.Thread(target=prepare, name="control-start", daemon=True).start()

    def _stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        getattr(self._controller, "cancel_start", lambda: None)()
        self._set_status(strings.STOPPING, _MUTED)

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
            current = (health.get("phase", "connecting"), health.get("warning", ""))
            if current != last_health and not self._stopping:
                phase, warning = current
                label = strings.PHASE_LABELS.get(phase, strings.PHASE_FALLBACK)
                self._set_status(label, _REC if phase == "recording" else _MUTED, warning)
                last_health = current
            time.sleep(0.2)
        if not self._running:
            return  # the user already pressed Stop; _stop() updated the UI
        self._running = False
        self._set_button(strings.START, _OK)
        error = self._controller.last_error
        if error:
            self._set_status(strings.FAILED, _REC, error)
        else:
            self._set_status(
                strings.READY,
                _MUTED,
                getattr(self._controller, "health", {}).get("warning", ""),
            )

    # -- the circular controls --------------------------------------------

    def _make_circle(
        self,
        parent: Any,
        diameter: int,
        command: Callable[[], None],
        *,
        text: str = strings.START,
        fill: str = _OK,
        text_fill: str = _ON_LIGHT,
        font: Optional[tuple] = None,
        tag: str = "toggle",
        hover_fill: Optional[str] = None,
        primary: bool = False,
        glyph: str = "",
        on_disabled: Optional[Callable[[], None]] = None,
    ) -> CircleButton:
        """Build one circle and remember it by its canvas."""
        button = CircleButton(
            tk, parent, diameter, command,
            text=text, fill=fill, text_fill=text_fill, font=font, tag=tag,
            hover_fill=hover_fill, primary=primary, glyph=glyph,
            on_disabled=on_disabled, on_blocked=self._explain_disabled_press,
        )
        self._circles[button.widget()] = button
        return button

    def _circle_button(
        self, parent: Any, diameter: int, command: Callable[[], None], **kwargs: Any
    ) -> Any:
        """`_make_circle` returning the raw canvas, as older callers expect."""
        return self._make_circle(parent, diameter, command, **kwargs).widget()

    def _set_circle_enabled(self, canvas: Any, enabled: bool) -> None:
        button = self._circles.get(canvas)
        if button is not None:
            button.set_enabled(enabled)

    def _explain_disabled_press(self) -> None:
        """A disabled press says why, instead of doing nothing."""
        self._set_status(strings.STOP_BLOCKS_ACTION, _MUTED)

    def _paint_toggle(self) -> None:
        if self._toggle_button is not None:
            self._toggle_button.paint()

    def _on_toggle_hover(self, _event: object = None) -> None:
        if self._toggle_button is not None:
            self._toggle_button.set_hovered(True)

    def _on_toggle_leave(self, _event: object = None) -> None:
        if self._toggle_button is not None:
            self._toggle_button.set_hovered(False)

    def _set_button(self, text: str, color: str) -> None:
        def _() -> None:
            self._toggle_color = color
            if self._toggle_button is not None:
                self._toggle_button.set_style(fill=color, text=text)

        self._ui(_, key="button")

    # -- settings actions --------------------------------------------------

    def _on_save_settings(self) -> None:
        if self._saving:
            self._set_status(strings.SAVING, _MUTED)
            return
        if self._starting or self._running or self._stopping:
            self._set_status(strings.STOP_BLOCKS_SAVE, _MUTED)
            return
        url = self._url_var.get().strip() if self._url_var is not None else ""
        secret = self._secret_var.get().strip() if self._secret_var is not None else ""
        client_id = self._client_id_var.get().strip() if self._client_id_var is not None else ""
        self._saving = True
        self._set_status(strings.SAVING_ELLIPSIS, _MUTED)

        def save() -> None:
            error = ""
            try:
                initialize_user_config()
                save_host_credentials(url, secret, client_id)
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                error = str(exc)

            def finish() -> None:
                self._saving = False
                if error:
                    self._set_status(strings.SAVE_FAILED, _REC, error)
                    return
                if self._secret_var is not None:
                    self._secret_var.set("")
                # An empty secret field means save_host_credentials kept the
                # stored secret; claiming a plain "saved" would overstate what
                # happened (A3.5).
                self._set_status(
                    strings.SAVED if secret else strings.SAVED_KEPT_SECRET, _OK
                )

            self._ui(finish, key="save-complete")

        threading.Thread(target=save, daemon=True, name="settings-save").start()

    def _test_connection(self) -> None:
        if self._stopping:
            # A stop is in flight, so the stored settings are being torn down
            # with the session; a test here would report on a configuration
            # nobody is about to use. Refuse it AND say so (A3.3).
            self._set_status(strings.STOPPING, _MUTED)
            return
        if self._testing:
            self._set_status(strings.TEST_IN_FLIGHT, _MUTED)
            return
        if self._saving:
            self._set_status(strings.SAVING, _MUTED)
            return
        if self._starting or self._running:
            self._set_status(strings.STOP_BLOCKS_TEST, _MUTED)
            return
        self._testing = True
        self._set_status(strings.TESTING, _MUTED)

        def check() -> None:
            error = ""
            try:
                from ..config import validate_settings
                from ..host_client import HostSessionClient

                settings = get_settings()
                errors = validate_settings(settings)
                if errors:
                    raise ConfigError("; ".join(errors))
                HostSessionClient(
                    settings.host_url,
                    settings.host_secret,
                    timeout=settings.host_timeout_seconds,
                    client_id=settings.host_client_id,
                ).fetch_session()
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                error = str(exc)

            def finish() -> None:
                self._testing = False
                self._set_status(
                    strings.TEST_FAILED if error else strings.TEST_OK,
                    _REC if error else _OK,
                    error,
                )

            self._ui(finish, key="connection-test")

        threading.Thread(target=check, daemon=True, name="connection-test").start()

    def _show_recovery(self) -> None:
        # Explicit review only: never automatically paste into the focused EMR.
        popup = tk.Toplevel(self._root)
        popup.title(strings.RECOVERY_TITLE)
        popup.configure(bg=_BG)
        popup.transient(self._root)
        try:
            popup.grab_set()
        except tk.TclError:  # pragma: no cover - no grab without a viewable window
            pass
        popup.bind("<Escape>", lambda _e: popup.destroy())
        text = tk.Text(
            popup, wrap="word", width=70, height=16, bg=_SURFACE, fg=_FG,
            insertbackground=_FG, relief="flat", font=fonts.font(popup, 9),
        )
        text.pack(fill="both", expand=True)
        text.insert("1.0", getattr(self._controller, "recovery_text", lambda: "")())
        # Text has no widget-level justify option; a full-range tag is the
        # correct Tk API, applied before making the widget read-only.
        text.tag_configure("rtl", justify="right")
        text.tag_add("rtl", "1.0", "end")
        text.configure(state="disabled")
        feedback = tk.Label(
            popup, text="", bg=_BG, fg=_OK, anchor="e", font=fonts.font(popup, 8)
        )
        feedback.pack(fill="x")

        def copy() -> None:
            popup.clipboard_clear()
            popup.clipboard_append(text.get("1.0", "end-1c"))
            feedback.config(text=strings.RECOVERY_COPIED)

        actions = tk.Frame(popup, bg=_BG)
        actions.pack(anchor="e", pady=4)
        self._make_circle(
            actions, _SECONDARY_DIAMETER, copy,
            text=strings.RECOVERY_COPY, glyph=strings.RECOVERY_COPY_GLYPH,
            fill=_SURFACE, hover_fill=_BORDER, text_fill=_FG,
            font=fonts.font(popup, 8), tag="action",
        ).pack(side="right", padx=4)

        def clear() -> None:
            getattr(self._controller, "clear_recovery", lambda: None)()
            popup.destroy()

        self._make_circle(
            actions, _SECONDARY_DIAMETER, clear,
            text=strings.RECOVERY_CLEAR, glyph=strings.RECOVERY_CLEAR_GLYPH,
            fill=_SURFACE, hover_fill=_BORDER, text_fill=_FG,
            font=fonts.font(popup, 8), tag="action",
        ).pack(side="right", padx=4)
