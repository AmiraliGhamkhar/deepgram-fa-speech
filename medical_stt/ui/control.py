"""Floating Start/Stop control window -- the application's primary UI.

Borderless, always on top, draggable, and keyboard-quittable. It owns one
`SessionController`: pressing **شروع** builds a `LiveMedicalSTT` (which
validates configuration and obtains a short-lived session from the host on
first use), and pressing **توقف** requests a full clean shutdown.

Tkinter runs on its own thread, following the same pattern as
`overlay.py`: every widget mutation is scheduled onto that thread with
`after(0, ...)`, so the audio/network threads never touch Tk.

The host shared secret typed here is passed straight to
`config.save_host_credentials`, which protects it with Windows DPAPI. It is
never written to `settings.yaml` and never logged.
"""
from __future__ import annotations

import logging
import platform
import threading
import time
from typing import Optional, Protocol

from ..config import ConfigError, get_settings, initialize_user_config, save_host_credentials

log = logging.getLogger("medical_stt.ui.control")

_SYSTEM = platform.system().lower()

_TK_AVAILABLE = False
try:
    import tkinter as tk
    _TK_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on the Python build
    _TK_AVAILABLE = False

_BG = "#1e1e2e"
_BORDER = "#45475a"
_FG = "#cdd6f4"
_MUTED = "#a6adc8"
_OK = "#a6e3a1"
_REC = "#f38ba8"
_ACCENT = "#89b4fa"


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
        self._running = False
        self._stopping = False
        self._close_requested = False
        self._status: Optional["tk.Label"] = None
        self._detail: Optional["tk.Label"] = None
        self._toggle: Optional["tk.Button"] = None
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
        return self._ready.wait(timeout=5.0)

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
        if completed or not self._controller.is_running:
            self._running = False
            self._set_button("شروع", _OK)
            self._set_status("\u25cf آماده", _MUTED)
            if self._close_requested:
                self._destroy_window()
            return

        self._running = True
        self._close_requested = False
        self._set_status("\u25cf توقف کامل نشد", _REC, "جلسه هنوز در حال پایان است.")

    # -- Tk thread --------------------------------------------------------

    def _run(self) -> None:
        try:
            self._root = tk.Tk()
            self._build(self._root)
            self._root.protocol("WM_DELETE_WINDOW", self.close)
            self._root.bind("<Escape>", lambda _e: self.close())
            self._ready.set()
            self._root.mainloop()
        except tk.TclError as exc:
            log.error("failed to create the control window: %s", exc)
            self._ready.set()

    def _build(self, root: "tk.Tk") -> None:
        root.title("Medical STT")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.resizable(False, False)
        root.configure(bg=_BORDER)
        try:
            root.attributes("-alpha", 0.97)
        except tk.TclError:  # pragma: no cover - platform dependent
            pass

        inner = tk.Frame(root, bg=_BG, padx=14, pady=10)
        inner.pack(fill="both", expand=True)

        header = tk.Frame(inner, bg=_BG)
        header.pack(fill="x")
        title = tk.Label(
            header, text="Medical STT", bg=_BG, fg=_FG, anchor="w",
            font=("Segoe UI", 10, "bold"),
        )
        title.pack(side="left")
        quit_btn = tk.Label(header, text="\u2715", bg=_BG, fg=_MUTED, cursor="hand2", padx=4)
        quit_btn.pack(side="right")
        quit_btn.bind("<Button-1>", lambda _e: self.close())

        self._status = tk.Label(
            inner, text="\u25cf آماده", bg=_BG, fg=_MUTED, anchor="e", justify="right",
            font=("Segoe UI", 10),
        )
        self._status.pack(fill="x", pady=(6, 0))

        self._toggle = tk.Button(
            inner, text="شروع", command=self._on_toggle, bg=_OK, fg="#11111b",
            activebackground=_OK, activeforeground="#11111b", relief="flat",
            font=("Segoe UI", 12, "bold"), cursor="hand2", bd=0, pady=6,
        )
        self._toggle.pack(fill="x", pady=(8, 0))

        self._detail = tk.Label(
            inner, text="", bg=_BG, fg=_MUTED, anchor="e", justify="right",
            wraplength=320, font=("Segoe UI", 8),
        )
        self._detail.pack(fill="x", pady=(6, 0))

        tk.Frame(inner, bg=_BORDER, height=1).pack(fill="x", pady=8)

        # Host settings. The secret is write-only as far as this window is
        # concerned: it is loaded masked and saved straight to DPAPI.
        self._url_var = tk.StringVar(value=self._current_host_url())
        self._secret_var = tk.StringVar(value="")

        tk.Label(inner, text="میزبان (https)", bg=_BG, fg=_MUTED, anchor="e",
                 font=("Segoe UI", 8)).pack(fill="x")
        tk.Entry(inner, textvariable=self._url_var, bg="#313244", fg=_FG,
                 insertbackground=_FG, relief="flat", justify="left",
                 font=("Segoe UI", 9)).pack(fill="x", pady=(2, 0), ipady=3)

        tk.Label(inner, text="کلید مشترک", bg=_BG, fg=_MUTED, anchor="e",
                 font=("Segoe UI", 8)).pack(fill="x", pady=(6, 0))
        tk.Entry(inner, textvariable=self._secret_var, show="\u2022", bg="#313244",
                 fg=_FG, insertbackground=_FG, relief="flat", justify="left",
                 font=("Segoe UI", 9)).pack(fill="x", pady=(2, 0), ipady=3)

        save = tk.Button(inner, text="ذخیره تنظیمات", command=self._on_save_settings,
                         bg="#313244", fg=_FG, activebackground="#45475a",
                         activeforeground=_FG, relief="flat", font=("Segoe UI", 9),
                         cursor="hand2", bd=0, pady=4)
        save.pack(fill="x", pady=(8, 0))

        # Drag support: the window is borderless, so there is no title bar
        # to move it with.
        for widget in (root, inner, header, title, self._status, self._detail):
            widget.bind("<ButtonPress-1>", self._on_drag_start)
            widget.bind("<B1-Motion>", self._on_drag_move)

        root.geometry("+40+40")

    # -- dragging ---------------------------------------------------------

    def _on_drag_start(self, event: object) -> None:
        self._drag_origin = (getattr(event, "x_root", 0), getattr(event, "y_root", 0))

    def _on_drag_move(self, event: object) -> None:
        if self._root is None:
            return
        origin = getattr(self, "_drag_origin", None)
        if origin is None:
            return
        x = getattr(event, "x_root", 0) - origin[0]
        y = getattr(event, "y_root", 0) - origin[1]
        self._root.geometry(f"+{x}+{y}")

    # -- state / actions --------------------------------------------------

    @staticmethod
    def _current_host_url() -> str:
        try:
            return get_settings().host_url
        except ConfigError:
            return ""

    def _ui(self, fn: object, *, force: bool = False) -> None:
        """Schedule `fn` on the Tk thread.

        `force=True` is for the shutdown path only: once `close()` has
        marked the window closed, nothing else may be scheduled -- except
        the destroy callback itself.
        """
        if self._root is None:
            return
        if self._closed and not force:
            return
        try:
            self._root.after(0, fn)  # type: ignore[arg-type]
        except RuntimeError:  # pragma: no cover - interpreter shutting down
            pass

    def _set_status(self, text: str, color: str, detail: str = "") -> None:
        def _() -> None:
            if self._status is not None:
                self._status.config(text=text, fg=color)
            if self._detail is not None:
                self._detail.config(text=detail)

        self._ui(_)

    def _on_toggle(self) -> None:
        if self._stopping:
            return
        if self._running:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        try:
            self._controller.start()
        except ConfigError as exc:
            self._running = False
            self._set_status("\u25cf پیکربندی نامعتبر", _REC, str(exc))
            return
        except RuntimeError as exc:
            self._running = False
            self._set_status("\u25cf شروع ناموفق", _REC, str(exc))
            return

        self._running = True
        self._set_status("\u25cf در حال ضبط...", _REC)
        self._set_button("توقف", _ACCENT)
        threading.Thread(target=self._watch, name="control-watch", daemon=True).start()

    def _stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        self._set_status("\u25cf در حال توقف...", _MUTED)

        def stop_controller() -> None:
            try:
                completed = self._controller.stop()
            except Exception:  # noqa: BLE001 - shutdown status must reach the UI
                log.exception("session controller stop raised")
                completed = False
            self._ui(lambda: self._finish_stop(completed))

        threading.Thread(target=stop_controller, name="control-stop", daemon=True).start()

    def _watch(self) -> None:
        """Reflect a session that ended on its own (error, or clean exit)."""
        while self._controller.is_running:
            time.sleep(0.2)
        if not self._running:
            return  # the user already pressed Stop; _stop() updated the UI
        self._running = False
        self._set_button("شروع", _OK)
        error = self._controller.last_error
        if error:
            self._set_status("\u25cf خطا", _REC, error)
        else:
            self._set_status("\u25cf آماده", _MUTED)

    def _set_button(self, text: str, color: str) -> None:
        def _() -> None:
            if self._toggle is not None:
                self._toggle.config(text=text, bg=color, activebackground=color)

        self._ui(_)

    def _on_save_settings(self) -> None:
        url = self._url_var.get().strip() if self._url_var is not None else ""
        secret = self._secret_var.get().strip() if self._secret_var is not None else ""
        try:
            initialize_user_config()
            save_host_credentials(url, secret)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self._set_status("\u25cf ذخیره نشد", _REC, str(exc))
            return
        # Clear the entry immediately: the secret now lives in the
        # DPAPI-protected store and should not linger on screen.
        if self._secret_var is not None:
            self._secret_var.set("")
        self._set_status(
            "\u25cf تنظیمات ذخیره شد",
            _OK,
            "کلید مشترک با DPAPI محافظت شد." if secret else "نشانی میزبان ذخیره شد.",
        )
