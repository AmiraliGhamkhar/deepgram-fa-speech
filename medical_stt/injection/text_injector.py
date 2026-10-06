"""Injects Persian/RTL and Unicode text at the current cursor position.

Platform-specific work is delegated to an `InjectionBackend` (see
backend.py); this class only decides *what* to send: streaming delta
backspacing for partial hypotheses, and BiDi-aware clipboard paste for
final utterances.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

from ..processing.bidi import to_injected
from .backend import InjectionBackend, get_default_backend

log = logging.getLogger("medical_stt.injection")

# Characters that must never be split from their neighbor when computing a
# common-prefix backspace boundary: ZWNJ/ZWJ word joins and Arabic combining
# diacritics.
_COMBINING_MARKS = "\u200c\u200d\u064b\u064c\u064d\u064e\u064f\u0650\u0651\u0652\u0670"

# Tabs and other horizontal whitespace collapse inside a line; line breaks
# are structural and are never collapsed or converted. Windows-style
# CRLF and bare CR are normalized to LF so injection is platform-neutral.
_HORIZONTAL_WS_RE = re.compile(r"[^\S\n]+")


def normalize_injected_whitespace(text: str) -> str:
    """Collapse horizontal whitespace, preserve line structure.

    Runs of spaces/tabs become one space, trailing whitespace on each line
    is dropped, and empty leading/trailing lines are removed. `\\n` is kept
    exactly as it is.
    """
    if not text:
        return text
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_HORIZONTAL_WS_RE.sub(" ", line).strip() for line in normalized.split("\n")]
    return "\n".join(lines).strip("\n")


def _trailing_separator(text: str) -> str:
    """The word separator a caller appended after an utterance, if it did.

    `LiveMedicalSTT` pastes every finalized utterance followed by a single
    space so consecutive dictations cannot fuse into one token
    ("فشار خون120/80بیمار" instead of "فشار خون 120/80 بیمار"). But
    `normalize_injected_whitespace` strips trailing whitespace from every line
    *by design* -- that is what keeps pasted transcripts free of ragged
    margins, and tests/test_formatting_ownership.py pins it -- so the
    separator has to be re-attached after normalizing, not passed through it.

    Returns "" when the caller supplied no trailing whitespace, or when that
    whitespace was a line break: a newline is already a stronger separator
    than a space, and the normalizer preserves it.
    """
    trailing = text[len(text.rstrip()):]
    if not trailing or "\n" in trailing or "\r" in trailing:
        return ""
    return " "


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
        """Type text via synthetic Unicode key events.

        Every backend call below is wrapped in a broad `except Exception`, not
        `except OSError`. The backends delegate to third-party OS bindings that
        raise whatever suits them -- `pyperclip.PyperclipException` when no
        clipboard helper is installed, `pyautogui.FailSafeException` when the
        pointer hits a screen corner, `WinError` variants from ctypes -- and
        none of those are `OSError`. This method runs on the speech provider's
        callback thread, so an exception that escaped here would kill that
        thread and end dictation silently mid-consult. The contract callers
        rely on is "returns False and records `last_error`", so the boundary
        enforces it for any failure mode. The try blocks wrap *only* the
        backend delegation; our own text handling stays outside them, so a bug
        in this class is still loud.
        """
        if not text:
            return False
        try:
            ok = self.backend.send_unicode_text(text)
        except Exception as exc:  # noqa: BLE001 - see the docstring
            self.last_error = str(exc)
            log.warning("type_text failed: %s", exc)
            return False
        if not ok:
            self.last_error = "backend reported failure"
            log.warning("type_text: backend reported failure")
        return ok

    def paste_text(self, text: str, add_rtl_mark: bool = True) -> bool:
        """Paste `text` via the clipboard.

        Whitespace is normalized *within* each line (runs of spaces/tabs
        collapse to one space), but line breaks are preserved: structured
        medical dictation -- a diagnosis line, a vital-signs line -- must
        not be flattened into a single paragraph. `\\n` is the newline
        contract for the whole pipeline; a future "سر خط" command only has
        to emit it.

        `add_rtl_mark` (kept for backward compatibility) is now equivalent
        to always-on: the injected representation always gets exactly one
        leading directional mark matching its base direction, computed by
        processing.bidi.to_injected -- never a forced RLE/PDF embedding
        around the whole string (see processing/bidi.py for why).
        """
        separator = _trailing_separator(text)
        text = normalize_injected_whitespace(text)
        if not text:
            return False
        text += separator

        text = to_injected(text)

        try:
            ok = self.backend.paste_text(text, self.restore_clipboard, self.paste_settle_seconds)
        except Exception as exc:  # noqa: BLE001 - see the note on type_text
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
        except Exception as exc:  # noqa: BLE001 - see the note on type_text
            self.last_error = str(exc)
            log.warning("send_backspaces failed: %s", exc)
            return False
        if not ok:
            self.last_error = "backend reported failure"
        return ok
