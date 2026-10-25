"""Shared typography and Windows DPI setup for both Tk windows (GUI R2).

One font helper so the control window and the overlay cannot drift apart, and
process DPI awareness applied before any ``tk.Tk()`` interpreter exists --
Windows otherwise bitmap-scales the whole UI at 125%/150% and the result is
blurry and wrongly sized.

Everything here is best-effort by design: a font lookup or a DPI call that
fails must never take the UI down with it. Tk itself is imported lazily so
headless test runs (where tkinter is not even installed) can exercise the
selection logic through the small seams below.
"""
from __future__ import annotations

import logging
import platform
import threading
from typing import Any, Collection, Optional, Tuple

log = logging.getLogger("medical_stt.ui.fonts")

_SYSTEM = platform.system().lower()

#: Preferred families, most preferred first. Persian UI text needs a font
#: with proper Arabic-script shaping before anything else; Segoe UI/Tahoma/
#: Arial are the portable fallbacks.
PREFERRED_FAMILIES = (
    "Vazirmatn",
    "Vazir",
    "IRANSans",
    "B Yekan",
    "B Nazanin",
    "Segoe UI",
    "Tahoma",
    "Arial",
)
DEFAULT_FAMILY = "Tahoma" if _SYSTEM == "windows" else "Arial"

#: `PROCESS_PER_MONITOR_DPI_AWARE` for `SetProcessDpiAwareness`.
_PROCESS_PER_MONITOR_DPI_AWARE = 2

_lock = threading.Lock()
_family_cache: Optional[str] = None
_dpi_attempted = False
_dpi_applied = False


def reset_cache() -> None:
    """Forget cached decisions (tests)."""
    global _family_cache, _dpi_attempted, _dpi_applied
    with _lock:
        _family_cache = None
        _dpi_attempted = False
        _dpi_applied = False


def choose_family(available: Collection[str]) -> str:
    """Pick the best family from `available`, or the platform default."""
    for name in PREFERRED_FAMILIES:
        if name in available:
            return name
    return DEFAULT_FAMILY


def _available_families(root: Any) -> "set[str]":
    import tkinter.font as tkfont

    return set(tkfont.families(root) if root is not None else tkfont.families())


def family(root: Any = None) -> str:
    """Best available UI font family for `root` (cached for the process)."""
    global _family_cache
    with _lock:
        if _family_cache is not None:
            return _family_cache
    try:
        available = _available_families(root)
    except Exception as exc:  # noqa: BLE001 - a font query must never break the UI
        log.debug("font family lookup failed (%s)", type(exc).__name__)
        available = set()
    resolved = choose_family(available)
    with _lock:
        if _family_cache is None:
            _family_cache = resolved
        return _family_cache


def font(root: Any = None, size: int = 9, weight: Optional[str] = None) -> Tuple[Any, ...]:
    """A Tk font tuple built on the shared family."""
    base = (family(root), size)
    return base + (weight,) if weight else base


def _win32_enable_dpi_awareness() -> None:
    """Per-monitor DPI awareness, with the pre-1607 system-aware fallback."""
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(_PROCESS_PER_MONITOR_DPI_AWARE)  # type: ignore[attr-defined]
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]


def enable_windows_dpi_awareness(loader: Optional[Any] = None) -> bool:
    """Make the process DPI-aware before the first Tk interpreter exists.

    Idempotent: the loader runs at most once per process. Off Windows this is
    a no-op. Returns True only when awareness was actually applied.
    """
    global _dpi_attempted, _dpi_applied
    with _lock:
        if _dpi_attempted:
            return _dpi_applied
        _dpi_attempted = True
    applied = False
    try:
        if _SYSTEM == "windows":
            (loader or _win32_enable_dpi_awareness)()
            applied = True
    except Exception as exc:  # noqa: BLE001 - best effort, see docstring
        log.debug("DPI awareness not applied (%s)", type(exc).__name__)
    with _lock:
        _dpi_applied = applied
    return applied


def _window_dpi(root: Any) -> float:
    import ctypes

    try:
        return float(ctypes.windll.user32.GetDpiForWindow(root.winfo_id()))  # type: ignore[attr-defined]
    except Exception:
        return float(root.winfo_fpixels("1i"))


def apply_tk_scaling(root: Any, dpi_loader: Optional[Any] = None) -> None:
    """Set points-to-pixels from actual window DPI, on its Tk thread only.

    Process awareness must already have been attempted before Tk creation.
    Other platforms retain their native Tk scaling.
    """
    if _SYSTEM != "windows":
        return
    try:
        dpi = float((dpi_loader or _window_dpi)(root))
        if 48 <= dpi <= 768:
            root.tk.call("tk", "scaling", dpi / 72.0)
    except Exception as exc:
        log.debug("Tk DPI scaling not applied (%s)", type(exc).__name__)
