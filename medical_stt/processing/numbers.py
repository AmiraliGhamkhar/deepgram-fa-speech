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
from typing import Iterator, List, Tuple

_NUM = r"\d+(?:[.,]\d+)?"

# `\b` is the wrong boundary for medical dictation: Persian and Latin letters
# are `\w`, so ASR output such as "BP120/80", "CO2" or "دکستروز5 درصد" has no
# boundary between the letters and the first/last digit. We only need to know
# that a match does not start or end *inside a longer number*. Letters, ZWNJ
# and other punctuation on either side are valid neighbours. The right guard
# also refuses to stop just before a decimal separator + digit, which would
# otherwise protect "7" instead of "7.2" when the regex backtracks.
_LEFT = r"(?<![\d.,٫٬])"
_RIGHT = r"(?!\d|[.,٫٬]\d)"

# One explicit list, shared by ratios, ranges and single values. Longer units
# come first so "mmHg" cannot be claimed as "mm" and "C" cannot claim the
# first letter of "cm". Persian compounds may be spelled with ZWNJ or a space
# between their parts; both forms denote the same clinical unit.
_UNITS = (
    r"mg/kg/day|mg/kg|mcg/kg|mmHg|mmol/l|mEq/l|mcg|mg|ml|mm|km|kg|cm|cc|"
    r"g|l|IU|units?|bpm|C|F|%|"
    r"درصد|درجه|کیلوگرم|میلی[ \u200c]?گرم|میلی[ \u200c]?لیتر|"
    r"میلی[ \u200c]?مول|سانتی[ \u200c]?متر|گرم|روز"
)

# Ordered from most specific to least specific; matching stops at the first
# hit per start position (see find_numeric_spans). Specificity here means
# "how much structure does the pattern commit to", not "how long is it".
_PATTERNS: List[re.Pattern[str]] = [
    # Date-like patterns first: 2024-01-05, 1403/06/12, 5/1/2024. A date
    # commits to TWO separators, which makes it more specific than a ratio
    # ("120/80") or a range ("5-10 mg") -- neither can match a date. If a
    # range matched first it would claim "2024-01" out of "2024-01-05" and
    # leave the second hyphen unprotected, so a rule could then rewrite
    # across the middle of a date.
    re.compile(rf"{_LEFT}\d{{1,4}}[/-]\d{{1,2}}[/-]\d{{1,4}}{_RIGHT}"),
    # Time-like patterns: 14:30, 8:05
    re.compile(rf"{_LEFT}\d{{1,2}}:\d{{2}}{_RIGHT}"),
    # Blood pressure / ratio: 120/80, 10x5, 10×5 (with optional unit).
    # The space belongs INSIDE the optional unit group: when no unit follows,
    # a range/ratio span must not end with a detached trailing space.
    re.compile(
        rf"{_LEFT}{_NUM}\s*[x×/]\s*{_NUM}(?:[ \t]*(?:{_UNITS}))?{_RIGHT}",
        re.IGNORECASE,
    ),
    # Range: 5-10 mg, 2 to 4 cm, 3-4 روز
    re.compile(
        rf"{_LEFT}{_NUM}\s*(?:-|to|تا)\s*{_NUM}(?:[ \t]*(?:{_UNITS}))?{_RIGHT}",
        re.IGNORECASE,
    ),
    # Number + unit (dose, vitals, measurements). Unit list is intentionally
    # explicit rather than "any letters" so we don't accidentally swallow
    # unrelated words that happen to follow a number.
    re.compile(
        rf"{_LEFT}{_NUM}[ \t]*(?:{_UNITS})(?:[ \t]+(?:IV|IM|SC|PO))?{_RIGHT}",
        re.IGNORECASE,
    ),
    # Bare percentage / decimal lab value with a trailing % sign.
    re.compile(rf"{_LEFT}{_NUM}[ \t]*%{_RIGHT}"),
    # A bare decimal / plain number on its own (lowest priority: only
    # protects the digits themselves, e.g. lab values "7.2").
    re.compile(rf"{_LEFT}{_NUM}{_RIGHT}"),
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


def protected_ranges(text: str) -> List[Tuple[int, int]]:
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
