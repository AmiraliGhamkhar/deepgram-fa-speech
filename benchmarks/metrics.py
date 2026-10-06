"""Deterministic, dependency-free metrics for the STT benchmark.

Nothing here talks to a network or a model: the metrics score a *reference*
string against a *hypothesis* string, so they can be unit-tested with
hand-computed values and never produce invented numbers.

Definitions (documented because "WER" is not portable between tools):

* Tokens are whitespace-separated strings after Unicode normalization; no
  punctuation stripping is applied, so a missing comma is a substitution.
* Word error rate = (substitutions + deletions + insertions) / reference
  tokens, computed with Levenshtein alignment. Empty reference with a
  non-empty hypothesis is 1.0 (undefined otherwise); both empty is 0.0.
* Character error rate uses the same alignment over code points.
* Numeric accuracy extracts clinical numeric expressions from the reference
  (processing/numbers.py, the same extractor the pipeline protects) and
  requires each one to appear in the hypothesis as a *complete* expression.
  This is stricter than WER on purpose: "120/8" versus "120/80" must not be
  averaged away.
* Medical-term recall and English-term recognition are recall-only: they
  ask whether the terms the speaker said survived, not whether the model
  invented extra ones (precision is visible through WER/CER). A term counts
  as present only when it is not embedded in a longer word, so the
  abbreviation "IV" is not credited to "DRIVE" and "MI" is not credited to
  "ADMINISTRATION".

All three of those use boundary-aware containment (`_contains_*` below)
rather than `str.__contains__`, because a substring test scores a truncated
digit or an abbreviation swallowed by a longer Latin word as a perfect hit --
the exact false positives a clinical benchmark exists to catch.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Sequence, Tuple

from medical_stt.processing.negation import find_negation_spans
from medical_stt.processing.numbers import find_numeric_spans

__all__ = [
    "CaseScore",
    "CorpusScore",
    "character_error_rate",
    "edit_distance",
    "english_term_recall",
    "expected_written_reference",
    "medical_term_recall",
    "negation_preservation",
    "numeric_accuracy",
    "score_case",
    "word_error_rate",
]


#: Bidi/formatting control characters that the injection layer may add.
#: They are presentation metadata, not transcription content, so scoring
#: strips them; otherwise every utterance would show a phantom error.
_PRESENTATION_MARKS = {
    "\u200e", "\u200f", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
    "\u2066", "\u2067", "\u2068", "\u2069",
}


def _normalize_for_scoring(text: str) -> str:
    stripped = "".join(ch for ch in (text or "") if ch not in _PRESENTATION_MARKS)
    return unicodedata.normalize("NFC", stripped).strip()


def tokens(text: str) -> List[str]:
    return _normalize_for_scoring(text).split()


def edit_distance(reference: Sequence[str], hypothesis: Sequence[str]) -> int:
    """Levenshtein distance between two sequences."""
    if not reference:
        return len(hypothesis)
    if not hypothesis:
        return len(reference)
    previous = list(range(len(hypothesis) + 1))
    for i, ref_item in enumerate(reference, start=1):
        current = [i]
        for j, hyp_item in enumerate(hypothesis, start=1):
            cost = 0 if ref_item == hyp_item else 1
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            )
        previous = current
    return previous[-1]


def word_error_rate(reference: str, hypothesis: str) -> float:
    ref_tokens = tokens(reference)
    hyp_tokens = tokens(hypothesis)
    if not ref_tokens:
        return 0.0 if not hyp_tokens else 1.0
    return edit_distance(ref_tokens, hyp_tokens) / len(ref_tokens)


def character_error_rate(reference: str, hypothesis: str) -> float:
    ref_chars = list(_normalize_for_scoring(reference))
    hyp_chars = list(_normalize_for_scoring(hypothesis))
    if not ref_chars:
        return 0.0 if not hyp_chars else 1.0
    return edit_distance(ref_chars, hyp_chars) / len(ref_chars)


#: Characters that can extend a numeric expression into a longer one: digits
#: and the separators processing/numbers.py matches between them. Only a
#: *digit* end of a span needs checking -- a span ending in a unit ("mmHg",
#: "درصد") is already delimited by the unit itself, and demanding a boundary
#: after it would reject ordinary sentence punctuation ("120/80 mmHg.").
_NUMERIC_EXTENSION_CHARS = frozenset("0123456789.,:/%-x\u00d7")

#: A term counts as present only when no ASCII alphanumeric touches either
#: side of the match. Persian characters are deliberately outside this class:
#: Persian terms keep exactly the containment semantics they always had, and
#: only Latin abbreviations -- where "IV" hides inside "DRIVE" -- get stricter.
_ALNUM_CLASS = "0-9A-Za-z"


def _term_pattern(term: str) -> "re.Pattern[str]":
    return re.compile(rf"(?<![{_ALNUM_CLASS}]){re.escape(term)}(?![{_ALNUM_CLASS}])")


def _contains_term(haystack: str, term: str) -> bool:
    """True if `term` occurs in `haystack` as a whole word/abbreviation."""
    return bool(term) and _term_pattern(term).search(haystack) is not None


def _contains_numeric_expression(haystack: str, needle: str) -> bool:
    """True if the numeric expression `needle` occurs whole in `haystack`.

    Every occurrence is tested, not just the first: a hypothesis can mention
    the truncated form before the real one ("120/8 یعنی 120/80").
    """
    if not needle:
        return False
    start = 0
    while True:
        index = haystack.find(needle, start)
        if index < 0:
            return False
        before = haystack[index - 1] if index > 0 else ""
        after = haystack[index + len(needle)] if index + len(needle) < len(haystack) else ""
        left_open = needle[0].isdigit() and before in _NUMERIC_EXTENSION_CHARS
        right_open = needle[-1].isdigit() and after in _NUMERIC_EXTENSION_CHARS
        if not left_open and not right_open:
            return True
        start = index + 1


def medical_term_recall(reference: str, hypothesis: str, terms: Iterable[str]) -> float:
    wanted = [t for t in terms if _contains_term(reference, t)]
    if not wanted:
        return 1.0
    hits = sum(1 for term in wanted if _contains_term(hypothesis, term))
    return hits / len(wanted)


def english_term_recall(reference: str, hypothesis: str) -> float:
    """Recall over Latin-script tokens of the reference (e.g. `SpO2`, `ICU`)."""
    latin = [token for token in tokens(reference) if _has_latin(token)]
    if not latin:
        return 1.0
    remaining = _normalize_for_scoring(hypothesis)
    hits = 0
    for token in latin:
        match = _term_pattern(token).search(remaining)
        if match is None:
            continue
        hits += 1
        # Consume one occurrence, as before, so a reference that repeats a
        # token requires the hypothesis to repeat it too.
        remaining = remaining[: match.start()] + remaining[match.end():]
    return hits / len(latin)


def _has_latin(token: str) -> bool:
    return any("a" <= ch.lower() <= "z" for ch in token)


def numeric_accuracy(reference: str, hypothesis: str) -> float:
    spans = [span.text.strip() for span in find_numeric_spans(_normalize_for_scoring(reference))]
    if not spans:
        return 1.0
    return sum(1 for span in spans if _contains_numeric_expression(hypothesis, span)) / len(spans)


def negation_preservation(reference: str, hypothesis: str) -> float:
    spans = find_negation_spans(_normalize_for_scoring(reference))
    if not spans:
        return 1.0
    ref_text = _normalize_for_scoring(reference)
    markers = [ref_text[start:end] for start, end in spans]
    return sum(1 for marker in markers if marker in hypothesis) / len(markers)


@dataclass
class CaseScore:
    case_id: str
    category: str
    wer: float
    cer: float
    numeric_accuracy: float
    negation_preservation: float
    english_term_recall: float
    medical_term_recall: float
    extra: Dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, object]:
        return {
            "id": self.case_id,
            "category": self.category,
            "wer": round(self.wer, 4),
            "cer": round(self.cer, 4),
            "numeric_accuracy": round(self.numeric_accuracy, 4),
            "negation_preservation": round(self.negation_preservation, 4),
            "english_term_recall": round(self.english_term_recall, 4),
            "medical_term_recall": round(self.medical_term_recall, 4),
        }


@dataclass
class CorpusScore:
    cases: List[CaseScore]

    @property
    def mean_wer(self) -> float:
        return _mean(case.wer for case in self.cases)

    @property
    def mean_cer(self) -> float:
        return _mean(case.cer for case in self.cases)

    def aggregate(self) -> Dict[str, float]:
        return {
            "cases": float(len(self.cases)),
            "mean_wer": round(self.mean_wer, 4),
            "mean_cer": round(self.mean_cer, 4),
            "mean_numeric_accuracy": round(_mean(c.numeric_accuracy for c in self.cases), 4),
            "mean_negation_preservation": round(_mean(c.negation_preservation for c in self.cases), 4),
            "mean_english_term_recall": round(_mean(c.english_term_recall for c in self.cases), 4),
            "mean_medical_term_recall": round(_mean(c.medical_term_recall for c in self.cases), 4),
        }

    def by_category(self) -> Dict[str, Dict[str, float]]:
        grouped: Dict[str, List[CaseScore]] = {}
        for case in self.cases:
            grouped.setdefault(case.category, []).append(case)
        out: Dict[str, Dict[str, float]] = {}
        for category, cases in sorted(grouped.items()):
            out[category] = {
                "cases": float(len(cases)),
                "mean_wer": round(_mean(c.wer for c in cases), 4),
                "mean_cer": round(_mean(c.cer for c in cases), 4),
                "mean_numeric_accuracy": round(_mean(c.numeric_accuracy for c in cases), 4),
                "mean_negation_preservation": round(_mean(c.negation_preservation for c in cases), 4),
            }
        return out


def _mean(values: Iterable[float]) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def expected_written_reference(reference: str, substitutions: Dict[str, str]) -> str:
    """Apply the *expected* terminology rewrites to a spoken reference.

    Terminology cases intentionally change words (`آی سی یو` -> `ICU`), so a
    post-processed hypothesis scored against the spoken reference shows a
    WER penalty that is not an accuracy loss. The corpus declares those
    expected rewrites explicitly; this turns them into the reference the
    post-processed run is measured against. Substitutions are applied
    longest-first so a phrase mapping wins over a substring of itself.
    """
    out = reference
    for spoken, written in sorted(substitutions.items(), key=lambda kv: -len(kv[0])):
        out = out.replace(spoken, written)
    return out


def score_case(
    case: Dict[str, object],
    hypothesis: str,
    *,
    medical_terms: Sequence[str] = (),
    reference: str | None = None,
) -> CaseScore:
    reference = reference if reference is not None else str(case.get("reference", ""))
    category = str(case.get("category", "uncategorized"))
    raw_terms = case.get("medical_terms") or []
    terms = [str(term) for term in raw_terms] if isinstance(raw_terms, (list, tuple)) else []
    terms += [str(term) for term in medical_terms]
    return CaseScore(
        case_id=str(case.get("id", "<unknown>")),
        category=category,
        wer=word_error_rate(reference, hypothesis),
        cer=character_error_rate(reference, hypothesis),
        numeric_accuracy=numeric_accuracy(reference, hypothesis),
        negation_preservation=negation_preservation(reference, hypothesis),
        english_term_recall=english_term_recall(reference, hypothesis),
        medical_term_recall=medical_term_recall(reference, hypothesis, terms),
    )


def score_corpus(
    cases: Sequence[Dict[str, object]],
    hypotheses: Dict[str, str],
    *,
    medical_terms: Sequence[str] = (),
    expect_written_terms: bool = False,
) -> Tuple[CorpusScore, List[str]]:
    """Score every case that has a hypothesis; return (scores, missing_ids).

    With `expect_written_terms=True` each case is scored against its
    reference *after* the corpus's declared terminology substitutions, which
    is the right comparison for locally post-processed text.
    """
    scores: List[CaseScore] = []
    missing: List[str] = []
    for case in cases:
        case_id = str(case.get("id", ""))
        if case_id not in hypotheses:
            missing.append(case_id)
            continue
        reference = None
        if expect_written_terms:
            substitutions = case.get("expected_substitutions") or {}
            if isinstance(substitutions, dict) and substitutions:
                reference = expected_written_reference(str(case.get("reference", "")), substitutions)
        scores.append(
            score_case(case, hypotheses[case_id], medical_terms=medical_terms, reference=reference)
        )
    return CorpusScore(cases=scores), missing
