"""
Always-on-top floating transcript overlay optimized for mixed Persian & English medical text.
- Supports Persian fonts (Vazirmatn, IRANSans, B Yekan, Tahoma, Segoe UI).
- Automatic RTL / LTR text alignment & right-justified multi-line wrapping.
- Auto-clamps to screen borders so it never goes off-screen.
- Smooth Catppuccin dark theme with real-time status indicators.
- Graceful headless fallback when GUI / Tkinter is unavailable.
"""
from __future__ import annotations

import logging
import platform
import threading
from typing import Optional

log = logging.getLogger("medical-stt.overlay")
_SYSTEM = platform.system().lower()

# Check Tkinter availability
_TK_AVAILABLE = False
try:
    import tkinter as tk
    import tkinter.font as tkfont
    _TK_AVAILABLE = True
except Exception:
    _TK_AVAILABLE = False


def _is_rtl(text: str) -> bool:
    """Check if the text starts with or predominantly contains RTL (Persian/Arabic) characters."""
    for ch in text.strip():
        if (
            "\u0600" <= ch <= "\u06ff"
            or "\u0750" <= ch <= "\u077f"
            or "\ufb50" <= ch <= "\ufdff"
            or "\ufe70" <= ch <= "\ufeff"
        ):
            return True
        elif ch.isascii() and ch.isalpha():
            return False
    return True


def _get_best_persian_font(root: tk.Tk) -> str:
    """Find the best available Persian font on the host machine."""
    preferred_fonts = [
        "Vazirmatn",
        "Vazir",
        "IRANSans",
        "B Yekan",
        "B Nazanin",
        "Segoe UI",
        "Tahoma",
        "Arial",
    ]
    try:
        available = set(tkfont.families(root))
        for font in preferred_fonts:
            if font in available:
                return font
    except Exception:
        pass
    return "Tahoma" if _SYSTEM == "windows" else "Arial"


class TranscriptOverlay:
    """
    Floating always-on-top window displaying real-time ASR transcripts
    and injection status.
    """

    def __init__(
        self,
        enabled: bool = True,
        offset_x: int = 20,
        offset_y: int = 24,
        wraplength: int = 380,
    ):
        self.enabled = enabled and _TK_AVAILABLE
        self.offset_x = offset_x
        self.offset_y = offset_y
        self.wraplength = wraplength

        self._root: Optional[tk.Tk] = None
        self._label: Optional[tk.Label] = None
        self._status: Optional[tk.Label] = None
        self._font_family: str = "Tahoma"

        self._ready = threading.Event()
        self._closed = False

        if not self.enabled:
            log.info("Overlay GUI disabled or Tkinter unavailable — running in console-only mode")
            self._ready.set()
            return

        self._thread = threading.Thread(target=self._run, name="overlay-ui", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=4.0):
            log.warning("Overlay UI startup timed out — falling back to console mode")
            self.enabled = False

    def _run(self) -> None:
        try:
            self._root = tk.Tk()
            self._root.title("Medical STT Overlay")
            self._root.overrideredirect(True)
            self._root.attributes("-topmost", True)

            # Detect best available Persian font
            self._font_family = _get_best_persian_font(self._root)

            # Transparency
            try:
                self._root.attributes("-alpha", 0.94)
            except tk.TclError:
                pass

            # Outer border frame (Catppuccin Surface0 #313244)
            border_frame = tk.Frame(self._root, bg="#313244", padx=1, pady=1)
            border_frame.pack(fill="both", expand=True)

            # Inner container frame (Catppuccin Base #1e1e2e)
            inner_frame = tk.Frame(border_frame, bg="#1e1e2e", padx=12, pady=10)
            inner_frame.pack(fill="both", expand=True)

            # Status Label
            self._status = tk.Label(
                inner_frame,
                text="● در حال شنیدن...",
                fg="#a6e3a1",
                bg="#1e1e2e",
                font=(self._font_family, 9, "bold"),
                anchor="e",
                justify="right",
            )
            self._status.pack(fill="x", pady=(0, 4))

            # Main Transcript Label
            self._label = tk.Label(
                inner_frame,
                text="...",
                fg="#cdd6f4",
                bg="#1e1e2e",
                font=(self._font_family, 11),
                wraplength=self.wraplength,
                justify="right",
                anchor="ne",
            )
            self._label.pack(fill="both", expand=True)

            self._root.geometry("+100+100")
            self._ready.set()
            self._tick_follow()
            self._root.mainloop()
        except Exception as e:
            log.warning("Failed to initialize overlay window: %s", e)
            self.enabled = False
            self._ready.set()

    def _tick_follow(self) -> None:
        """Keep the overlay hovering near the mouse pointer with screen edge clamping."""
        if self._closed or self._root is None:
            return
        try:
            pointer_x = self._root.winfo_pointerx()
            pointer_y = self._root.winfo_pointery()
            screen_w = self._root.winfo_screenwidth()
            screen_h = self._root.winfo_screenheight()
            win_w = self._root.winfo_reqwidth()
            win_h = self._root.winfo_reqheight()

            x = pointer_x + self.offset_x
            y = pointer_y + self.offset_y

            # Clamp to screen right/bottom edges so overlay remains visible
            if x + win_w > screen_w - 12:
                x = pointer_x - win_w - 10
            if y + win_h > screen_h - 12:
                y = pointer_y - win_h - 10

            self._root.geometry(f"+{max(6, x)}+{max(6, y)}")
        except Exception:
            pass
        if self._root is not None and not self._closed:
            self._root.after(40, self._tick_follow)

    def _ui(self, fn) -> None:
        if not self.enabled or self._root is None or self._closed:
            return
        try:
            self._root.after(0, fn)
        except Exception:
            pass

    def _apply_text_alignment(self, text: str) -> None:
        """Dynamically switch alignment if the text is Persian (RTL) or English (LTR)."""
        if not self._label:
            return
        if _is_rtl(text):
            self._label.config(anchor="ne", justify="right")
        else:
            self._label.config(anchor="nw", justify="left")

    def set_partial(self, text: str) -> None:
        """Real-time streaming hypothesis update."""
        def _():
            display_text = text or "..."
            self._apply_text_alignment(display_text)
            if self._label:
                self._label.config(text=display_text, fg="#cdd6f4")
            if self._status:
                self._status.config(text="● در حال شنیدن...", fg="#a6e3a1", anchor="e")
        self._ui(_)

    def set_done(self, text: str) -> None:
        """Final transcript successfully injected into cursor position."""
        def _():
            display_text = text or "..."
            self._apply_text_alignment(display_text)
            if self._label:
                self._label.config(text=display_text, fg="#89b4fa")
            if self._status:
                self._status.config(text="✓ تایپ شد", fg="#89b4fa", anchor="e")
        self._ui(_)

    def set_idle(self) -> None:
        """Idle waiting state."""
        def _():
            if self._label:
                self._label.config(text="...", fg="#6c7086", anchor="ne")
            if self._status:
                self._status.config(text="● آماده", fg="#a6adc8", anchor="e")
        self._ui(_)

    def close(self) -> None:
        """Cleanly close overlay window."""
        self._closed = True
        def _():
            if self._root is not None:
                try:
                    self._root.destroy()
                except Exception:
                    pass
                self._root = None
        self._ui(_)
