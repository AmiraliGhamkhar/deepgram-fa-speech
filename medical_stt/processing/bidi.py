"""BiDi/RTL-LTR handling for mixed Persian/English medical text.

Three representations of the same utterance are kept explicitly distinct,
per the engineering rules ("do not use visual-order text as the canonical
transcript"):

* **logical** -- the transcript exactly as recognized/normalized, in
  logical (reading/typing) order. This is the only representation stored,
  logged (when enabled), or used for terminology processing.
* **display** -- a *visual-order* string suitable for rendering in a
  toolkit that does not itself implement the Unicode Bidirectional
  Algorithm (our Tkinter overlay). Never fed back into processing.
* **injected** -- the logical string plus minimal directional metadata
  (at most one leading mark) for pasting into applications that DO
  implement the UBA themselves (virtually all real text editors/EMR/browser
  fields). We deliberately do NOT wrap the whole string in RLE...PDF: most
  target applications already run the full Unicode Bidi Algorithm on the
  pasted text, and forcibly embedding it overrides that algorithm, which is
  exactly the bug this module fixes -- it previously broke mixed strings
  like "فشار خون 120/80 mmHg" by forcing every character (including the
  numbers and "mmHg") into the RTL embedding, corrupting their visual order
  in apps that handle BiDi correctly on their own.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Optional

try:
    from bidi.algorithm import get_display as _bidi_get_display
    _HAS_PYTHON_BIDI = True
except ImportError:  # pragma: no cover - exercised when dependency missing
    _HAS_PYTHON_BIDI = False

RLM = "\u200f"  # Right-to-Left Mark
LRM = "\u200e"  # Left-to-Right Mark


def _strong_direction(ch: str) -> Optional[str]:
    bidi_class = unicodedata.bidirectional(ch)
    if bidi_class in ("R", "AL"):
        return "rtl"
    if bidi_class == "L":
        return "ltr"
    return None


def base_direction(text: str) -> str:
    """Return 'rtl' or 'ltr' using the first-strong-character rule (the
    same heuristic browsers/UBA "auto" mode use), defaulting to 'ltr' for
    text with no strong characters (pure numbers/punctuation)."""
    for ch in text:
        direction = _strong_direction(ch)
        if direction:
            return direction
    return "ltr"


def has_rtl_content(text: str) -> bool:
    return any(_strong_direction(ch) == "rtl" for ch in text)


def has_ltr_content(text: str) -> bool:
    return any(_strong_direction(ch) == "ltr" for ch in text)


def is_mixed_direction(text: str) -> bool:
    return has_rtl_content(text) and has_ltr_content(text)


@dataclass(frozen=True)
class TranscriptView:
    """Explicit separation of logical/display/injected representations of
    the same utterance."""

    logical: str
    display: str
    injected: str


def to_display(text: str) -> str:
    """Visual-order string for a UBA-naive renderer (our Tkinter overlay).

    Falls back to the logical string unchanged if python-bidi is not
    installed; the overlay is a convenience UI, not the system of record.
    """
    if not text:
        return text
    if not _HAS_PYTHON_BIDI:
        return text
    return _bidi_get_display(text)


def to_injected(text: str) -> str:
    """Logical-order string plus at most one leading directional mark, for
    pasting into applications that run their own Unicode Bidi Algorithm.

    We do not add per-run isolates/embeddings: real-world editors (Word,
    browsers, EMR web forms) already apply UBA to pasted text, so adding a
    directional *mark* only resolves the paragraph's base direction for
    otherwise-neutral leading punctuation, while leaving the mixed content
    itself for the target application to reorder correctly.
    """
    if not text:
        return text
    direction = base_direction(text)
    mark = RLM if direction == "rtl" else LRM
    if text.startswith((RLM, LRM)):
        return text
    return mark + text


def build_transcript_view(logical_text: str) -> TranscriptView:
    return TranscriptView(
        logical=logical_text,
        display=to_display(logical_text),
        injected=to_injected(logical_text),
    )
