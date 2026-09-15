"""Conservative confidence warnings for high-risk medical tokens."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple

from ..stt.base import WordInfo

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


@dataclass(frozen=True)
class UncertainMedicalWord:
    word: WordInfo
    category: str


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
    uncertain = []
    for word in words:
        category = medical_risk_category(word.text)
        if category is not None and word.confidence is not None and word.confidence < threshold:
            uncertain.append(UncertainMedicalWord(word=word, category=category))
    if not uncertain:
        return None
    return MedicalConfidenceWarning(threshold=threshold, uncertain_words=tuple(uncertain))
