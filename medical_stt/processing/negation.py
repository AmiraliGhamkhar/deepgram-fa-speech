"""Conservative, deterministic negation/clinical-fidelity guard.

This is NOT clinical NLP and makes no attempt at full negation scope
detection. It provides one narrow guarantee: terminology rules must not be
applied in a way that could flip a negated finding into a positive one, and
common negation/fidelity phrases are never altered by normalization or
terminology rewriting.

The mechanism is simple and auditable: a fixed list of negation/fidelity
markers is treated as "hard boundaries" that:
  1. Are never rewritten by the terminology engine (matched fully, not
     partially, to avoid accidental corruption of the negation words
     themselves), and
  2. Split a sentence into "protected" spans so a downstream consumer that
     wants extra caution (e.g. suppressing overly aggressive rewriting) can
     detect that the current utterance contains a negation.
"""
from __future__ import annotations

import re
from typing import List, Tuple

# Order matters only for readability; matching is longest-match via regex
# alternation sorted by length below.
NEGATION_MARKERS: Tuple[str, ...] = (
    "وجود ندارد",
    "مشاهده نشد",
    "منفی است",
    "رد می‌شود",
    "رد میشود",
    "رد شد",
    "شواهدی از",
    "بدون",
    "ندارد",
    "نیست",
    "منفی",
)

_SORTED_MARKERS = sorted(NEGATION_MARKERS, key=len, reverse=True)
_NEGATION_RE = re.compile("|".join(re.escape(m) for m in _SORTED_MARKERS))


def contains_negation(text: str) -> bool:
    """True if `text` contains any recognized negation/fidelity marker."""
    return bool(_NEGATION_RE.search(text))


def find_negation_spans(text: str) -> List[Tuple[int, int]]:
    """Return non-overlapping (start, end) spans of negation markers,
    longest match first at each position."""
    spans: List[Tuple[int, int]] = []
    occupied = [False] * len(text)
    for m in _NEGATION_RE.finditer(text):
        start, end = m.start(), m.end()
        if any(occupied[start:end]):
            continue
        spans.append((start, end))
        for i in range(start, end):
            occupied[i] = True
    spans.sort()
    return spans
