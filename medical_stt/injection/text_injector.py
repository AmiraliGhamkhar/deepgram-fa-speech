"""Injects Persian/RTL and Unicode text at the current cursor position.

Platform-specific work is delegated to an `InjectionBackend` (see
backend.py); this class only decides *what* to send: streaming delta
backspacing for partial hypotheses, and BiDi-aware clipboard paste for
final utterances.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from ..processing.bidi import to_injected
from .backend import InjectionBackend, get_default_backend

log = logging.getLogger("medical_stt.injection")

# Characters that must never be split from their neighbor when computing a
# common-prefix backspace boundary: ZWNJ/ZWJ word joins and Arabic combining
# diacritics.
_COMBINING_MARKS = "\u200c\u200d\u064b\u064c\u064d\u064e\u064f\u0650\u0651\u0652\u0670"


class InjectionError(Exception):
    """Raised when a caller opts into strict-mode injection and the
    underlying backend reports failure."""


class TextInjector:
    """Decides what to type/paste; delegates the "how" to an InjectionBackend."""

    def __init__(
        self,
        backend: Optional[InjectionBackend] = None,
        dry_run: bool = False,
        enable_smart_rewrite: bool = True,
        restore_clipboard: bool = True,
        paste_settle_seconds: float = 0.12,
    ):
        if dry_run and backend is None:
            from .backend import DryRunBackend

            backend = DryRunBackend()
        self.backend = backend or get_default_backend()
        self.dry_run = dry_run
        self.enable_smart_rewrite = enable_smart_rewrite
        self.restore_clipboard = restore_clipboard
        #: How long to let the target app consume the clipboard after Ctrl+V.
        self.paste_settle_seconds = max(0.0, paste_settle_seconds)
        self._last_partial: str = ""
        self.last_error: Optional[str] = None

    def reset_partial(self) -> None:
        """Reset the streaming state (call when a sentence is finalized)."""
        self._last_partial = ""

    def type_delta_from_partial(self, partial: str) -> None:
        """Handles real-time speech streaming: if the STT revises a
        previous hypothesis, send backspaces for the changed suffix and
        type the new text."""
        partial = partial or ""
        if partial == self._last_partial:
            return

        if not self.enable_smart_rewrite:
            if partial.startswith(self._last_partial):
                delta = partial[len(self._last_partial):]
                self._last_partial = partial
                if delta:
                    self.type_text(delta)
            return

        common_len = 0
        min_len = min(len(self._last_partial), len(partial))
        while common_len < min_len and self._last_partial[common_len] == partial[common_len]:
            common_len += 1

        common_len = self._safe_split_point(self._last_partial, partial, common_len)

        removed = self._last_partial[common_len:]
        delta = partial[common_len:]

        # Windows deletes UTF-16 code units, not Python code points.
        backspaces_needed = self._utf16_len(removed)

        self._last_partial = partial

        if backspaces_needed > 0:
            self.send_backspaces(backspaces_needed)
            time.sleep(0.01)

        if delta:
            self.type_text(delta)

    @staticmethod
    def _utf16_len(text: str) -> int:
        """Number of UTF-16 code units, i.e. how many backspaces Windows
        needs."""
        return len(text.encode("utf-16-le")) // 2

    @staticmethod
    def _safe_split_point(old: str, new: str, index: int) -> int:
        """Back the common-prefix boundary off any position that would cut
        a grapheme in half (ZWNJ joins, combining marks, surrogate
        halves)."""

        def joins(ch: str) -> bool:
            # NB: `"" in _COMBINING_MARKS` is True in Python, so the empty
            # end-of-string case must be excluded explicitly or a plain
            # append would be treated as a mid-grapheme cut and emit a
            # bogus backspace.
            return bool(ch) and ch in _COMBINING_MARKS

        while index > 0:
            prev = old[index - 1]
            nxt_old = old[index] if index < len(old) else ""
            nxt_new = new[index] if index < len(new) else ""
            if joins(prev) or joins(nxt_old) or joins(nxt_new):
                index -= 1
                continue
            if "\ud800" <= prev <= "\udbff":  # dangling high surrogate
                index -= 1
                continue
            break
        return index

    def type_text(self, text: str) -> bool:
        """Type text via synthetic Unicode key events."""
        if not text:
            return False
        try:
            ok = self.backend.send_unicode_text(text)
        except OSError as exc:
            self.last_error = str(exc)
            log.warning("type_text failed: %s", exc)
            return False
        if not ok:
            self.last_error = "backend reported failure"
            log.warning("type_text: backend reported failure")
        return ok

    def paste_text(self, text: str, add_rtl_mark: bool = True) -> bool:
        """Paste `text` via the clipboard.

        `add_rtl_mark` (kept for backward compatibility) is now equivalent
        to always-on: the injected representation always gets exactly one
        leading directional mark matching its base direction, computed by
        processing.bidi.to_injected -- never a forced RLE/PDF embedding
        around the whole string (see processing/bidi.py for why).
        """
        text = " ".join(text.split())
        if not text:
            return False

        text = to_injected(text)

        try:
            ok = self.backend.paste_text(text, self.restore_clipboard, self.paste_settle_seconds)
        except OSError as exc:
            self.last_error = str(exc)
            log.warning("paste_text failed: %s", exc)
            return False
        if not ok:
            self.last_error = "backend reported failure"
            log.warning("paste_text: backend reported failure")
        return ok

    def send_backspaces(self, count: int) -> bool:
        """Send N backspace keystrokes to erase revised text."""
        if count <= 0:
            return True
        try:
            ok = self.backend.send_backspaces(count)
        except OSError as exc:
            self.last_error = str(exc)
            log.warning("send_backspaces failed: %s", exc)
            return False
        if not ok:
            self.last_error = "backend reported failure"
        return ok
