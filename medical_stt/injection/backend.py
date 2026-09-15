"""Platform injection backends.

`InjectionBackend` is the abstraction `TextInjector` depends on. This keeps
every OS-specific call (Win32 ctypes, pyautogui/pyperclip) isolated so the
higher-level injection logic (delta backspacing, BiDi-aware prefixing,
clipboard-restore policy) can be unit tested on any platform via
`DryRunBackend`, and so a future backend (e.g. a Linux/Wayland
implementation) only needs to implement this interface.

Correctness notes carried over from the original single-file implementation
(these were real, previously-shipped injection bugs on Windows):

1. ctypes defaults EVERY function's restype to a 32-bit C ``int``. On 64-bit
   Windows ``GlobalAlloc``/``GlobalLock``/``SetClipboardData`` return 64-bit
   handles/pointers, so the high 32 bits were silently truncated. ``memmove``
   then wrote the UTF-16 text to a bogus address and ``SetClipboardData``
   received a garbage handle -> the paste inserted stale/empty/corrupt text
   even though the transcript itself was perfect. Every prototype below is
   declared explicitly.
2. ``SetClipboardData`` transfers OWNERSHIP of the HGLOBAL to the system. The
   block must not be freed on success, and must not be locked when handed
   over.
3. ``OpenClipboard`` routinely fails with ERROR_ACCESS_DENIED because another
   process (Office, browsers, clipboard managers) holds the clipboard open
   for a few milliseconds. A single attempt turned into a dropped utterance,
   so it is retried with a short backoff.
4. Ctrl+V was synthesized while the user's real modifier keys (Ctrl/Shift/
   Alt/Win) could still be physically or logically down. Ctrl+Shift+V /
   Ctrl+Alt+V are completely different commands in most editors and EMR web
   forms. Stray modifiers are now released first and restored afterwards.
5. The clipboard was overwritten by the next utterance before the target app
   had finished reading it (paste is asynchronous), which produced
   duplicated or missing sentences during fast dictation. Injection now
   waits for the paste to be consumed before returning/restoring.
6. Backspacing counted Python code points while Windows deletes UTF-16 code
   units, so any non-BMP character erased too little.
"""
from __future__ import annotations

import logging
import platform
from abc import ABC, abstractmethod
from typing import List, Optional

log = logging.getLogger("medical_stt.injection")

_SYSTEM = platform.system().lower()

# Tag our own synthetic events so we can tell them apart from real typing.
_INJECT_SIGNATURE = 0x53545449  # "STTI"


class InjectionBackend(ABC):
    """Everything that touches the OS input/clipboard APIs."""

    @abstractmethod
    def send_unicode_text(self, text: str) -> bool:
        """Type `text` as synthetic keystrokes, one Unicode code unit at a
        time (surrogate-pair safe)."""

    @abstractmethod
    def send_backspaces(self, count: int) -> bool:
        """Send `count` backspace keystrokes."""

    @abstractmethod
    def paste_text(self, text: str, restore_clipboard: bool, settle_seconds: float) -> bool:
        """Put `text` on the clipboard and synthesize a paste command,
        optionally restoring the previous clipboard contents afterwards."""

    def get_clipboard_text(self) -> Optional[str]:  # pragma: no cover - optional
        return None


class DryRunBackend(InjectionBackend):
    """No-op backend used by tests and `--dry-run`: records calls instead of
    touching any real input device or clipboard."""

    def __init__(self) -> None:
        self.typed: List[str] = []
        self.backspace_count = 0
        self.pasted: List[str] = []
        self._clipboard: Optional[str] = None

    def send_unicode_text(self, text: str) -> bool:
        self.typed.append(text)
        return True

    def send_backspaces(self, count: int) -> bool:
        self.backspace_count += count
        return True

    def paste_text(self, text: str, restore_clipboard: bool, settle_seconds: float) -> bool:
        previous = self._clipboard
        self._clipboard = text
        self.pasted.append(text)
        if restore_clipboard and previous is not None:
            self._clipboard = previous
        return True

    def get_clipboard_text(self) -> Optional[str]:
        return self._clipboard


class FailingBackend(InjectionBackend):
    """Backend that always fails, for testing failure-handling paths."""

    def send_unicode_text(self, text: str) -> bool:
        return False

    def send_backspaces(self, count: int) -> bool:
        return False

    def paste_text(self, text: str, restore_clipboard: bool, settle_seconds: float) -> bool:
        return False


def _build_windows_backend() -> InjectionBackend:
    from ._windows_backend import WindowsBackend

    return WindowsBackend()


def _build_fallback_backend() -> InjectionBackend:
    from ._fallback_backend import FallbackBackend

    return FallbackBackend()


def get_default_backend() -> InjectionBackend:
    """Return the appropriate real backend for the current OS."""
    if _SYSTEM == "windows":
        return _build_windows_backend()
    return _build_fallback_backend()
