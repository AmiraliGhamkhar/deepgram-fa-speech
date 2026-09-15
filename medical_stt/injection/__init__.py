"""Text injection: platform abstraction + the injector that decides what
to send (delta backspacing, clipboard paste, BiDi-aware prefixing).

`InjectionBackend` isolates every platform-specific call (Win32 SendInput/
clipboard, or the pyautogui/pyperclip fallback) behind a small interface so
`TextInjector`'s logic -- and its tests -- do not depend on a real Windows
GUI, X11 display, or clipboard manager.
"""
from .backend import DryRunBackend, InjectionBackend, get_default_backend
from .text_injector import TextInjector

__all__ = [
    "InjectionBackend",
    "DryRunBackend",
    "get_default_backend",
    "TextInjector",
]
