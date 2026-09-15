"""Detection of clinically meaningful numeric expressions.

The terminology engine (fst.py/terminology.py) must never replace text
inside a numeric clinical expression -- a blood pressure "120/80 mmHg", a
percentage "98%", a dose "500 mg IV" must survive byte-for-byte. This module
provides a single source of truth for "is this span a protected numeric
expression" so normalization and terminology rewriting agree.

This is pattern matching, not a units library: it protects the whole span
(number + unit + optional second number for ranges/ratios) so that no rule
can partially match inside it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator, List

_NUM = r"\d+(?:[.,]\d+)?"

# Ordered from most specific to least specific; matching stops at the first
# hit per start position (see find_numeric_spans).
_PATTERNS: List[re.Pattern[str]] = [
    # Blood pressure / ratio: 120/80, 10x5, 10×5 (with optional unit).
    re.compile(rf"\b{_NUM}\s*[x×/]\s*{_NUM}\s*(?:mmHg|mm|cm|ml|cc)?\b", re.IGNORECASE),
    # Range: 5-10 mg, 2 to 4 cm
    re.compile(
        rf"\b{_NUM}\s*(?:-|to|تا)\s*{_NUM}\s*"
        r"(?:mg|mcg|g|kg|ml|l|cc|mmHg|mmol/l|mEq/l|cm|mm|IU|units?|%)?\b",
        re.IGNORECASE,
    ),
    # Number + unit (dose, vitals, measurements). Unit list is intentionally
    # explicit rather than "any letters" so we don't accidentally swallow
    # unrelated words that happen to follow a number.
    re.compile(
        rf"\b{_NUM}\s*"
        r"(?:mg/kg/day|mg/kg|mcg/kg|mg|mcg|g|kg|ml|l|cc|mmHg|mmol/l|mEq/l|"
        r"cm|mm|km|IU|units?|bpm|C|F|%|درصد)\b",
        re.IGNORECASE,
    ),
    # Bare percentage / decimal lab value with a trailing % sign.
    re.compile(rf"\b{_NUM}\s*%"),
    # Date-like patterns: 2024-01-05, 1403/06/12, 5/1/2024
    re.compile(r"\b\d{1,4}[/-]\d{1,2}[/-]\d{1,4}\b"),
    # Time-like patterns: 14:30, 8:05
    re.compile(r"\b\d{1,2}:\d{2}\b"),
    # A bare decimal / plain number on its own (lowest priority: only
    # protects the digits themselves, e.g. lab values "7.2").
    re.compile(rf"\b{_NUM}\b"),
]


@dataclass(frozen=True)
class NumericSpan:
    start: int
    end: int
    text: str


def find_numeric_spans(text: str) -> List[NumericSpan]:
    """Return non-overlapping, longest-match spans of clinically meaningful
    numeric expressions in `text`, left to right."""
    spans: List[NumericSpan] = []
    occupied = [False] * len(text)

    for pattern in _PATTERNS:
        for m in pattern.finditer(text):
            start, end = m.start(), m.end()
            if any(occupied[start:end]):
                continue
            spans.append(NumericSpan(start, end, m.group(0)))
            for i in range(start, end):
                occupied[i] = True

    spans.sort(key=lambda s: s.start)
    return spans


def protected_ranges(text: str) -> List[tuple]:
    """Convenience wrapper returning plain (start, end) tuples."""
    return [(s.start, s.end) for s in find_numeric_spans(text)]


def is_inside_numeric_span(spans: List[NumericSpan], start: int, end: int) -> bool:
    """True if [start, end) overlaps any protected numeric span."""
    for span in spans:
        if start < span.end and end > span.start:
            return True
    return False


def iter_protected_mask(text: str) -> Iterator[bool]:
    """Yield one bool per character: True if that character belongs to a
    protected numeric expression."""
    mask = [False] * len(text)
    for span in find_numeric_spans(text):
        for i in range(span.start, span.end):
            mask[i] = True
    yield from mask
