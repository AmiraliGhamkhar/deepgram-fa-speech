"""Cursor-following transcript overlay with latest-value UI delivery.

Production uses a Toplevel on the control window's interpreter. A standalone
diagnostic overlay may own a Tk thread. All worker updates use a bounded
mailbox; only the owning GUI thread enters Tcl/Tk. Warning state is sticky
and backend acceptance is never described as target-document consumption.
"""
from __future__ import annotations

import logging
import platform
import threading
from typing import Callable, Optional

from ..processing.bidi import to_display
from . import colors, dispatch, fonts

log = logging.getLogger("medical_stt.ui.overlay")
_SYSTEM = platform.system().lower()

_TK_AVAILABLE = False
try:
    import tkinter as tk
    _TK_AVAILABLE = True
except ImportError:
    _TK_AVAILABLE = False


# Win32 extended-window-style bits that keep the cursor-following overlay
# from stealing a click meant for the EMR underneath it. The overlay runs
# with `-alpha`, i.e. as a layered window on Windows, which is what makes
# WS_EX_TRANSPARENT take effect for hit-testing.
#: The shared-owner overlay is created by posting to the control window's
#: mailbox. Wait this long for that callback before disabling the overlay.
OVERLAY_READY_TIMEOUT_SECONDS = 4.0

_GWL_EXSTYLE = -20
_WS_EX_NOACTIVATE = 0x08000000
_WS_EX_TRANSPARENT = 0x00000020


def _click_through_style(current: int) -> int:
    """The extended style with the click-through bits added, none cleared."""
    return current | _WS_EX_NOACTIVATE | _WS_EX_TRANSPARENT


def _apply_click_through(hwnd: int) -> bool:
    """Best-effort: make the overlay window click-through on Windows.

    Any failure -- non-Windows, no user32 -- leaves the overlay exactly as
    it was. Cosmetic window behaviour must never take the transcript
    display down with it. Only ever applied to the overlay; the control
    window must stay clickable.
    """
    if _SYSTEM != "windows" or not hwnd:
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        get_style = user32.GetWindowLongPtrW
        get_style.argtypes = [ctypes.c_void_p, ctypes.c_int]
        get_style.restype = ctypes.c_ssize_t
        set_style = user32.SetWindowLongPtrW
        set_style.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        set_style.restype = ctypes.c_ssize_t
        set_style(hwnd, _GWL_EXSTYLE, _click_through_style(int(get_style(hwnd, _GWL_EXSTYLE))))
        return True
    except Exception:  # noqa: BLE001 - best-effort only, see docstring
        log.debug("overlay click-through style not applied")
        return False


def _is_rtl(text: str) -> bool:
    if not text.strip():
        # Empty text has no direction; falling through to True right-aligned
        # the placeholder for no reason.
        return False
    for ch in text.strip():
        if (
            "\u0600" <= ch <= "\u06ff"
            or "\u0750" <= ch <= "\u077f"
            or "\ufb50" <= ch <= "\ufdff"
            or "\ufe70" <= ch <= "\ufeff"
        ):
            return True
        if ch.isascii() and ch.isalpha():
            return False
    return False




class TranscriptOverlay:
    def __init__(self, enabled: bool = True, offset_x: int = 20, offset_y: int = 24, wraplength: int = 380):
        self.enabled = enabled and _TK_AVAILABLE
        self.offset_x = offset_x
        self.offset_y = offset_y
        self.wraplength = wraplength
        self._root: Optional["tk.Tk | tk.Toplevel"] = None
        self._label: Optional["tk.Label"] = None
        self._status: Optional["tk.Label"] = None
        self._font_family: str = "Tahoma"
        self._ready = threading.Event()
        self._closed = False
        self._mailbox = dispatch.Mailbox()
        self._warning = ""
        self._last_geometry = ""

        if not self.enabled:
            log.info("Overlay GUI disabled or Tkinter unavailable")
            self._ready.set()
            return

        if dispatch.owner_mailbox is not None:
            dispatch.owner_mailbox.post("overlay-create", self._run)
            if not self._ready.wait(timeout=OVERLAY_READY_TIMEOUT_SECONDS):
                log.warning("Overlay UI startup timed out")
                self.enabled = False
            return
        self._thread = threading.Thread(target=self._run, name="overlay-ui", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=OVERLAY_READY_TIMEOUT_SECONDS):
            log.warning("Overlay UI startup timed out")
            self.enabled = False

    def _run(self) -> None:
        built = False
        try:
            # Standalone overlay mode creates its own interpreter; the shared
            # owner path already applied this on the control window's Tk.
            fonts.enable_windows_dpi_awareness()
            if self._closed:
                return
            shared = dispatch.owner_root
            self._root = tk.Toplevel(shared) if shared is not None else tk.Tk()
            fonts.apply_tk_scaling(self._root)
            self._root.title("Medical STT Overlay")
            self._root.overrideredirect(True)
            self._root.attributes("-topmost", True)
            self._font_family = fonts.family(self._root)

            try:
                self._root.attributes("-alpha", 0.94)
            except tk.TclError:
                pass

            border_frame = tk.Frame(self._root, bg=colors.SURFACE, padx=1, pady=1)
            border_frame.pack(fill="both", expand=True)

            inner_frame = tk.Frame(border_frame, bg=colors.BG, padx=12, pady=10)
            inner_frame.pack(fill="both", expand=True)

            self._status = tk.Label(
                inner_frame, text="\u25cf در حال شنیدن...", fg=colors.OK, bg=colors.BG,
                font=(self._font_family, 9, "bold"), anchor="e", justify="right",
            )
            self._status.pack(fill="x", pady=(0, 4))

            self._label = tk.Label(
                inner_frame, text="...", fg=colors.FG, bg=colors.BG,
                font=(self._font_family, 11), wraplength=self.wraplength,
                justify="right", anchor="ne",
            )
            self._label.pack(fill="both", expand=True)

            self._root.geometry("+100+100")
            # The overlay follows the cursor, so it can park itself under a
            # click meant for the EMR. Make it click-through where the OS
            # allows (Windows); everywhere else this is a no-op.
            try:
                _apply_click_through(self._root.winfo_id())
            except tk.TclError:  # pragma: no cover - window already gone
                pass
            self._ready.set()
            built = True
            self._tick_follow()
            self._pump()
            if shared is None:
                self._root.mainloop()
        except Exception as exc:
            # Widget construction touches fonts and OS window APIs, so a
            # non-Tcl failure is possible. Never leave `enabled=True` (and
            # never hold start callers hostage) for an overlay that has no
            # usable widgets. Log the type only: Tcl messages can carry text.
            log.warning("Failed to initialize overlay (%s)", type(exc).__name__)
            self.enabled = False
        finally:
            self._ready.set()
            if not built:
                self.enabled = False

    def _pump(self) -> None:
        self._mailbox.drain()
        if self._root is not None:
            self._root.after(25, self._pump)

    def set_warning(self, message: str) -> None:
        def render() -> None:
            self._warning = message
            if self._status:
                self._status.config(text=message, fg=colors.WARNING)
        self._ui(render, key="warning")

    def _tick_follow(self) -> None:
        if self._closed or self._root is None:
            return
        try:
            px = self._root.winfo_pointerx()
            py = self._root.winfo_pointery()
            left, top, sw, sh = self._screen_bounds()
            ww = self._root.winfo_reqwidth()
            wh = self._root.winfo_reqheight()

            x = px + self.offset_x
            y = py + self.offset_y
            if x + ww > left + sw - 12:
                x = px - ww - 10
            if y + wh > top + sh - 12:
                y = py - wh - 10
            geometry = f"{max(left + 6, x):+d}{max(top + 6, y):+d}"
            if geometry != self._last_geometry:
                self._root.geometry(geometry)
                self._last_geometry = geometry
        except tk.TclError:
            pass
        if self._root and not self._closed:
            self._root.after(40, self._tick_follow)

    def _screen_bounds(self) -> tuple[int, int, int, int]:
        if _SYSTEM == "windows":
            import ctypes
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            return tuple(int(user32.GetSystemMetrics(i)) for i in (76, 77, 78, 79))  # type: ignore[return-value]
        assert self._root is not None
        return (0, 0, self._root.winfo_screenwidth(), self._root.winfo_screenheight())

    def _ui(self, fn: Callable[[], None], *, force: bool = False, key: str = "render") -> None:
        if not force and (not self.enabled or self._closed):
            return
        self._mailbox.post("close" if force else key, fn)

    def _apply_text_alignment(self, text: str) -> None:
        if not self._label:
            return
        if _is_rtl(text):
            self._label.config(anchor="ne", justify="right")
        else:
            self._label.config(anchor="nw", justify="left")

    def set_partial(self, text: str) -> None:
        self._show(text, text_fg=colors.FG, status="\u25cf در حال شنیدن...", status_fg=colors.OK)

    def set_done(self, text: str) -> None:
        self._show(text, text_fg=colors.ACCENT, status="\u2713 ارسال شد؛ درج را بررسی کنید", status_fg=colors.ACCENT)

    def _show(self, text: str, *, text_fg: str, status: str, status_fg: str) -> None:
        """Render `text` plus its status line from the Tk thread.

        `set_partial` and `set_done` differ only in colours and status text;
        the body lives here once so the two paths cannot drift apart.
        """

        def _() -> None:
            display = to_display(text[:8000]) if text else "..."
            self._apply_text_alignment(text or "")
            if self._label:
                self._label.config(text=display, fg=text_fg)
            if self._status:
                self._status.config(text=self._warning or status, fg=colors.WARNING if self._warning else status_fg, anchor="e")

        self._ui(_)

    def set_idle(self) -> None:
        def _() -> None:
            if self._label:
                self._label.config(text="...", fg=colors.IDLE, anchor="ne")
            if self._status and not self._warning:
                self._status.config(text="\u25cf آماده", fg=colors.MUTED, anchor="e")

        self._ui(_)

    def close(self) -> None:
        """Schedule window destruction, then mark the overlay closed.

        Order matters: scheduling must happen while `_closed` is still
        False (via `force=True` for defense-in-depth), otherwise the Tk
        mainloop thread would never receive the destroy callback and could
        outlive the rest of the application.
        """

        def _destroy() -> None:
            if self._root is not None:
                try:
                    self._root.destroy()
                except tk.TclError:
                    pass
                self._root = None

        self._ui(_destroy, force=True)
        self._closed = True
