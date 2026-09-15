"""Always-on-top floating transcript overlay for mixed Persian & English.

Lifecycle note (previously a real bug): `close()` used to set `self._closed
= True` *before* scheduling the Tk `destroy()` callback via `self._ui(...)`,
but `_ui()` refuses to schedule anything once `_closed` is set -- so the
window was never actually destroyed and the Tk mainloop thread could hang
around after `close()` returned. `close()` now schedules destruction first
and only marks the overlay closed once that is done, and `_ui()` accepts an
explicit override for the shutdown path.
"""
from __future__ import annotations

import logging
import platform
import threading
from typing import Callable, Optional

from ..processing.bidi import to_display

log = logging.getLogger("medical_stt.ui.overlay")
_SYSTEM = platform.system().lower()

_TK_AVAILABLE = False
try:
    import tkinter as tk
    import tkinter.font as tkfont
    _TK_AVAILABLE = True
except ImportError:
    _TK_AVAILABLE = False


def _is_rtl(text: str) -> bool:
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
    return True


def _get_best_persian_font(root: "tk.Tk") -> str:
    preferred = ["Vazirmatn", "Vazir", "IRANSans", "B Yekan", "B Nazanin", "Segoe UI", "Tahoma", "Arial"]
    try:
        available = set(tkfont.families(root))
        for font in preferred:
            if font in available:
                return font
    except tk.TclError:
        pass
    return "Tahoma" if _SYSTEM == "windows" else "Arial"


class TranscriptOverlay:
    def __init__(self, enabled: bool = True, offset_x: int = 20, offset_y: int = 24, wraplength: int = 380):
        self.enabled = enabled and _TK_AVAILABLE
        self.offset_x = offset_x
        self.offset_y = offset_y
        self.wraplength = wraplength
        self._root: Optional["tk.Tk"] = None
        self._label: Optional["tk.Label"] = None
        self._status: Optional["tk.Label"] = None
        self._font_family: str = "Tahoma"
        self._ready = threading.Event()
        self._closed = False

        if not self.enabled:
            log.info("Overlay GUI disabled or Tkinter unavailable")
            self._ready.set()
            return

        self._thread = threading.Thread(target=self._run, name="overlay-ui", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=4.0):
            log.warning("Overlay UI startup timed out")
            self.enabled = False

    def _run(self) -> None:
        try:
            self._root = tk.Tk()
            self._root.title("Medical STT Overlay")
            self._root.overrideredirect(True)
            self._root.attributes("-topmost", True)
            self._font_family = _get_best_persian_font(self._root)

            try:
                self._root.attributes("-alpha", 0.94)
            except tk.TclError:
                pass

            border_frame = tk.Frame(self._root, bg="#313244", padx=1, pady=1)
            border_frame.pack(fill="both", expand=True)

            inner_frame = tk.Frame(border_frame, bg="#1e1e2e", padx=12, pady=10)
            inner_frame.pack(fill="both", expand=True)

            self._status = tk.Label(
                inner_frame, text="\u25cf در حال شنیدن...", fg="#a6e3a1", bg="#1e1e2e",
                font=(self._font_family, 9, "bold"), anchor="e", justify="right",
            )
            self._status.pack(fill="x", pady=(0, 4))

            self._label = tk.Label(
                inner_frame, text="...", fg="#cdd6f4", bg="#1e1e2e",
                font=(self._font_family, 11), wraplength=self.wraplength,
                justify="right", anchor="ne",
            )
            self._label.pack(fill="both", expand=True)

            self._root.geometry("+100+100")
            self._ready.set()
            self._tick_follow()
            self._root.mainloop()
        except tk.TclError as exc:
            log.warning("Failed to initialize overlay: %s", exc)
            self.enabled = False
            self._ready.set()

    def _tick_follow(self) -> None:
        if self._closed or self._root is None:
            return
        try:
            px = self._root.winfo_pointerx()
            py = self._root.winfo_pointery()
            sw = self._root.winfo_screenwidth()
            sh = self._root.winfo_screenheight()
            ww = self._root.winfo_reqwidth()
            wh = self._root.winfo_reqheight()

            x = px + self.offset_x
            y = py + self.offset_y
            if x + ww > sw - 12:
                x = px - ww - 10
            if y + wh > sh - 12:
                y = py - wh - 10
            self._root.geometry(f"+{max(6, x)}+{max(6, y)}")
        except tk.TclError:
            pass
        if self._root and not self._closed:
            self._root.after(40, self._tick_follow)

    def _ui(self, fn: Callable[[], None], *, force: bool = False) -> None:
        """Schedule `fn` to run on the Tk thread.

        `force=True` bypasses the `_closed` guard: this is required for the
        shutdown path itself (see `close()`), otherwise the destroy
        callback could never be scheduled once `_closed` was set.
        """
        if self._root is None:
            return
        if not force and (not self.enabled or self._closed):
            return
        try:
            self._root.after(0, fn)
        except RuntimeError:
            pass

    def _apply_text_alignment(self, text: str) -> None:
        if not self._label:
            return
        if _is_rtl(text):
            self._label.config(anchor="ne", justify="right")
        else:
            self._label.config(anchor="nw", justify="left")

    def set_partial(self, text: str) -> None:
        def _() -> None:
            display = to_display(text) if text else "..."
            self._apply_text_alignment(text or "")
            if self._label:
                self._label.config(text=display, fg="#cdd6f4")
            if self._status:
                self._status.config(text="\u25cf در حال شنیدن...", fg="#a6e3a1", anchor="e")

        self._ui(_)

    def set_done(self, text: str) -> None:
        def _() -> None:
            display = to_display(text) if text else "..."
            self._apply_text_alignment(text or "")
            if self._label:
                self._label.config(text=display, fg="#89b4fa")
            if self._status:
                self._status.config(text="\u2713 تایپ شد", fg="#89b4fa", anchor="e")

        self._ui(_)

    def set_idle(self) -> None:
        def _() -> None:
            if self._label:
                self._label.config(text="...", fg="#6c7086", anchor="ne")
            if self._status:
                self._status.config(text="\u25cf آماده", fg="#a6adc8", anchor="e")

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
