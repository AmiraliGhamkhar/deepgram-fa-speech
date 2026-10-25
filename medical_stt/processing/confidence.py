"""Conservative confidence warnings for high-risk medical tokens.

`find_low_confidence_medical_words` only reports metadata; it never changes
recognized text. `low_confidence_protected_ranges` maps those reported word
indices/timings back onto the normalized utterance so the terminology FST can
protect just the uncertain spans instead of disabling every rule in the
utterance. If that mapping cannot be established, it returns None and the
caller must fall back to preserving the whole utterance.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..stt.base import WordInfo
from .normalize import normalize

_NUMBER_RE = re.compile(r"\d")
_ABBREVIATION_RE = re.compile(r"^[A-Z][A-Z0-9&-]{1,9}$")
_DRUG_RE = re.compile(
    r"(?:cillin|cycline|mycin|azole|pril|sartan|olol|statin|mab|vir|پنی[‌ ]?سیلین)$",
    re.IGNORECASE,
)
_UNITS = frozenset({
    "%", "درصد", "mg", "mcg", "g", "kg", "ml", "l", "cc", "mm", "cm", "mmhg", "iu", "bpm",
})
_MEDICAL_ABBREVIATIONS = frozenset({
    "AFI", "BIRADS", "BP", "BPD", "CBC", "COPD", "CRL", "DVT", "ECG", "EFW", "ERCP", "HBA1C",
    "ICU", "IUGR", "IV", "MI", "PCNL", "PFNA", "TKA", "TURP",
})
_PROCEDURES_AND_DIAGNOSES = frozenset({
    "biopsy", "catheter", "hypertension", "hyperechoic", "hypoechoic", "infarction", "stenosis",
    "ultrasound", "آمبولی", "بیوپسی", "دیابت", "سکته", "کاتتر", "نارسایی", "هایپرتنشن",
})

#: Used only as a fallback when a provider word's punctuation was normalized
#: differently from the joined utterance. The exact normalized word is always
#: tried first, so clinical punctuation remains part of the protected span.
_PUNCTUATION_STRIP = ".,،؛:!?()[]{}%\"'«»…"
_WORD_JOINERS = "\u200c\u200d"


@dataclass(frozen=True)
class UncertainMedicalWord:
    word: WordInfo
    category: str
    #: Position in the provider's original `words` tuple. The tuple is the
    #: authoritative order; timings help re-order providers that emit words
    #: out of sequence.
    index: int


@dataclass(frozen=True)
class MedicalConfidenceWarning:
    threshold: float
    uncertain_words: Tuple[UncertainMedicalWord, ...]


def medical_risk_category(text: str) -> Optional[str]:
    token = text.strip(".,،؛:!?()[]{}%")
    lowered = token.casefold()
    if _NUMBER_RE.search(token):
        return "number"
    if lowered in _UNITS or text.strip().endswith("%"):
        return "measurement"
    if token.upper() in _MEDICAL_ABBREVIATIONS or _ABBREVIATION_RE.fullmatch(token):
        return "abbreviation"
    if _DRUG_RE.search(token):
        return "drug"
    if lowered in _PROCEDURES_AND_DIAGNOSES:
        return "procedure_or_diagnosis"
    return None


def find_low_confidence_medical_words(
    words: Tuple[WordInfo, ...], threshold: float
) -> Optional[MedicalConfidenceWarning]:
    """Return warning metadata without changing or guessing recognized text."""
    uncertain: List[UncertainMedicalWord] = []
    for index, word in enumerate(words):
        category = medical_risk_category(word.text)
        if category is not None and word.confidence is not None and word.confidence < threshold:
            uncertain.append(UncertainMedicalWord(word=word, category=category, index=index))
    if not uncertain:
        return None
    return MedicalConfidenceWarning(threshold=threshold, uncertain_words=tuple(uncertain))


def _is_word_char(ch: str) -> bool:
    return bool(ch) and (ch.isalnum() or ch in _WORD_JOINERS)


def _find_normalized_word(text: str, needle: str, start: int) -> Optional[Tuple[int, int]]:
    """Find `needle` in `text` at/after `start`, preferring a whole-word hit.

    Boundary-valid hits come first so a short token cannot claim a substring
    of a longer one. ASR also glues digits to letters/units, so an exact
    boundary-invalid hit is still better than losing the span entirely.
    """
    if not needle or start >= len(text):
        return None
    position = start
    while True:
        index = text.find(needle, position)
        if index < 0:
            break
        end = index + len(needle)
        before = text[index - 1] if index > 0 else ""
        after = text[end] if end < len(text) else ""
        if not _is_word_char(before) and not _is_word_char(after):
            return (index, end)
        position = index + 1

    trimmed = needle.strip(_PUNCTUATION_STRIP)
    if trimmed and trimmed != needle:
        index = text.find(trimmed, start)
        if index >= 0:
            return (index, index + len(trimmed))

    index = text.find(needle, start)
    return (index, index + len(needle)) if index >= 0 else None


def _align_word_spans(
    canonical: str,
    ordered: List[Tuple[int, WordInfo]],
) -> Tuple[dict[int, Tuple[int, int]], int]:
    """Map each provider word index to a normalized-text span, left to right."""
    spans: dict[int, Tuple[int, int]] = {}
    cursor = 0
    for index, word in ordered:
        needle = normalize(word.text).strip()
        if not needle:
            continue
        found = _find_normalized_word(canonical, needle, cursor)
        if found is None:
            continue
        spans[index] = found
        cursor = found[1]
    return spans, len(spans)


def low_confidence_protected_ranges(
    text: str,
    words: Tuple[WordInfo, ...],
    warning: Optional[MedicalConfidenceWarning],
) -> Optional[List[Tuple[int, int]]]:
    """Return character spans of uncertain words, or None if unsafe to map.

    `text` may be raw or normalized; `normalize()` is idempotent. Words are
    aligned in both tuple order and start-timing order; the alignment that
    finds more words and every uncertain token wins. Every reported index is
    checked against the supplied `words` tuple. A missing/ambiguous span for
    ANY uncertain token returns None: the caller must then preserve the whole
    utterance rather than risk rewriting PHI.
    """
    if warning is None or not warning.uncertain_words:
        return []
    canonical = normalize(text)
    if not canonical:
        return None

    uncertain_indices: set[int] = set()
    for item in warning.uncertain_words:
        if item.index < 0 or item.index >= len(words) or words[item.index] != item.word:
            return None
        uncertain_indices.add(item.index)

    # Try both plausible provider orders. The tuple order matches the joined
    # utterance for providers whose timings reset per segment; sorted timings
    # match providers that emit words out of sequence but keep stream-relative
    # timestamps. Choose the alignment that finds more words AND every
    # uncertain token, preferring tuple order on a tie.
    original_order = list(enumerate(words))
    timing_order = sorted(original_order, key=lambda item: (item[1].start, item[0]))
    candidates = [original_order]
    if timing_order != original_order:
        candidates.append(timing_order)

    best_spans: Optional[dict[int, Tuple[int, int]]] = None
    best_found = -1
    for ordered in candidates:
        spans, found = _align_word_spans(canonical, ordered)
        if not uncertain_indices.issubset(spans) or found <= best_found:
            continue
        best_spans, best_found = spans, found
    if best_spans is None:
        return None
    return [best_spans[index] for index in sorted(uncertain_indices)]
