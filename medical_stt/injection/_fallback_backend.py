"""Non-Windows fallback backend using pyautogui + pyperclip.

Only imported lazily on Linux/macOS. PyAutoGUI's `write()` cannot emit
non-ASCII on X11/macOS, so Persian text always goes through the clipboard
here (even for the "type" mode), matching the previous implementation's
`_type_fallback` behaviour.

Privacy contract (P0-13): because "type" mode is secretly a paste, it MUST
NOT leave dictated text on the clipboard or silently destroy the user's
previous copy. It therefore always requests restoration, regardless of the
paste-mode `restore_clipboard` setting. We refuse to paste when the prior
clipboard cannot be read, verify our own write, wait the configured settle,
and then restore the prior text. A content check acts as a best-effort
sequence guard: if the clipboard no longer holds exactly what we wrote, a
newer user copy is left alone.

Remaining gaps, intrinsic to pyperclip (development-only path): there is no
format enumeration (so image/file/rich clipboards cannot be refused and may
lose formats on restore), no OS clipboard sequence number (so a newer copy
with identical text is indistinguishable), and no confirmation that the
target consumed the paste (the settle is timed). Windows SendInput remains
the production path; see `_windows_backend.py` for the full policy.
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
    def __init__(self, paste_settle_seconds: float = 0.12) -> None:
        #: The configured settle for the typed path below. It used to be
        #: hardcoded to 0 there, so on Linux/macOS "type" mode ignored
        #: `paste_settle_seconds` entirely while "paste" mode honoured it.
        self.paste_settle_seconds = max(0.0, paste_settle_seconds)

    def send_unicode_text(self, text: str) -> bool:
        # Type mode must clean up its internal clipboard use even when paste
        # mode is configured not to restore. Never pass restore_clipboard=False.
        return self.paste_text(text, restore_clipboard=True, settle_seconds=self.paste_settle_seconds)

    def send_backspaces(self, count: int) -> bool:
        if count <= 0:
            return True
        try:
            import pyautogui
        except ImportError as exc:
            log.warning("pyautogui unavailable, cannot send backspaces: %s", type(exc).__name__)
            return False
        for _ in range(count):
            pyautogui.press("backspace")
        return True

    def get_clipboard_text(self) -> Optional[str]:
        try:
            import pyperclip

            return pyperclip.paste()
        except Exception as exc:  # pragma: no cover - depends on OS clipboard
            # Never log the exception message: clipboard helpers can include
            # clipboard content in their diagnostics.
            log.debug("clipboard read failed (%s)", type(exc).__name__)
            return None

    def _restore_clipboard(self, previous: str, written_text: str) -> None:
        """Best-effort restore, without overwriting a newer copy.

        A successful read that differs from `written_text` means the user (or
        another app) copied something during the paste settle. pyperclip has
        no sequence-number API, so content equality is the only guard. If the
        read itself fails, prefer removing our PHI over preserving an unknown
        newer clipboard, but log only the exception type.
        """
        try:
            import pyperclip

            try:
                current = pyperclip.paste()
            except Exception as exc:
                log.warning("clipboard state unknown during restore (%s)", type(exc).__name__)
            else:
                if current != written_text:
                    log.warning("clipboard changed during paste; not overwriting a newer copy")
                    return
            pyperclip.copy(previous)
        except Exception as exc:  # pragma: no cover - best effort by design
            log.warning("clipboard restore failed (%s)", type(exc).__name__)

    def paste_text(self, text: str, restore_clipboard: bool, settle_seconds: float) -> bool:
        try:
            import pyautogui
            import pyperclip
        except ImportError as exc:
            log.warning("pyautogui/pyperclip unavailable, cannot paste: %s", type(exc).__name__)
            return False

        previous: Optional[str] = None
        if restore_clipboard:
            previous = self.get_clipboard_text()
            if previous is None:
                # An unreadable clipboard could contain a non-text format or a
                # newer user copy. Do not overwrite it with PHI we cannot undo.
                log.warning("clipboard read failed; refusing a paste that cannot be restored")
                return False

        wrote = False
        try:
            pyperclip.copy(text)
            wrote = True
            time.sleep(0.05)

            # Mirror the Windows backend's write verification: a clipboard
            # helper that did not actually take our text must not receive a
            # paste keystroke, and the prior text is restored in `finally`.
            if self.get_clipboard_text() != text:
                log.warning("clipboard verification failed; aborting paste")
                return False

            if _SYSTEM == "darwin":
                pyautogui.hotkey("command", "v")
            else:
                pyautogui.hotkey("ctrl", "v")

            if settle_seconds:
                time.sleep(settle_seconds)
            return True
        except Exception as exc:
            log.warning("fallback paste failed (%s)", type(exc).__name__)
            return False
        finally:
            if restore_clipboard and wrote and previous is not None:
                self._restore_clipboard(previous, text)
