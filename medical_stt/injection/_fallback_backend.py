"""Non-Windows fallback backend using pyautogui + pyperclip.

Only imported lazily on Linux/macOS. PyAutoGUI's `write()` cannot emit
non-ASCII on X11/macOS, so Persian text always goes through the clipboard
here (even for the "type" mode), matching the previous implementation's
`_type_fallback` behaviour.
"""
from __future__ import annotations

import logging
import platform
import time
from typing import Optional

from .backend import InjectionBackend

log = logging.getLogger("medical_stt.injection.fallback")

_SYSTEM = platform.system().lower()


class FallbackBackend(InjectionBackend):
    def send_unicode_text(self, text: str) -> bool:
        return self.paste_text(text, restore_clipboard=False, settle_seconds=0.0)

    def send_backspaces(self, count: int) -> bool:
        if count <= 0:
            return True
        try:
            import pyautogui
        except ImportError as exc:
            log.warning("pyautogui unavailable, cannot send backspaces: %s", exc)
            return False
        for _ in range(count):
            pyautogui.press("backspace")
        return True

    def get_clipboard_text(self) -> Optional[str]:
        try:
            import pyperclip

            return pyperclip.paste()
        except Exception as exc:  # pragma: no cover - depends on OS clipboard
            log.debug("clipboard read failed: %s", exc)
            return None

    def paste_text(self, text: str, restore_clipboard: bool, settle_seconds: float) -> bool:
        try:
            import pyautogui
            import pyperclip
        except ImportError as exc:
            log.warning("pyautogui/pyperclip unavailable, cannot paste: %s", exc)
            return False

        previous = None
        if restore_clipboard:
            previous = self.get_clipboard_text()

        pyperclip.copy(text)
        time.sleep(0.05)

        if _SYSTEM == "darwin":
            pyautogui.hotkey("command", "v")
        else:
            pyautogui.hotkey("ctrl", "v")

        if settle_seconds:
            time.sleep(settle_seconds)

        if restore_clipboard and previous is not None and previous != text:
            try:
                pyperclip.copy(previous)
            except Exception as exc:  # pragma: no cover
                log.debug("clipboard restore failed: %s", exc)

        return True
