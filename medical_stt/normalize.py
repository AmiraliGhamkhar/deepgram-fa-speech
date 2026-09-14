"""Light deterministic text normalization for Persian medical transcripts."""

from __future__ import annotations

import re

# Zero-width non-joiner
ZWNJ = "\u200c"

# Common Arabic presentation forms → standard Persian letters
_ARABIC_TO_PERSIAN = str.maketrans(
    {
        "ي": "ی",
        "ك": "ک",
        "ة": "ه",
        "ؤ": "و",
        "إ": "ا",
        "أ": "ا",
        "ٱ": "ا",
    }
)

# Collapse runs of spaces / ZWNJ noise
_SPACE_RE = re.compile(r"[ \t\u200c]{2,}")
_LEADING_TRAILING = re.compile(r"^[\s\u200c]+|[\s\u200c]+$")


def normalize(text: str) -> str:
    """Basic cleanup before FST correction."""
    if not text:
        return text
    text = text.translate(_ARABIC_TO_PERSIAN)
    text = text.replace("\u200d", "")  # ZWJ rarely wanted
    text = _SPACE_RE.sub(" ", text)
    text = _LEADING_TRAILING.sub("", text)
    return text
