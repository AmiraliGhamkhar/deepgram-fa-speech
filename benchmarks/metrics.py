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
  requires each one to appear verbatim in the hypothesis. This is stricter
  than WER on purpose: "120/8" versus "120/80" must not be averaged away.
* Medical-term recall and English-term recognition are recall-only: they
  ask whether the terms the speaker said survived, not whether the model
  invented extra ones (precision is visible through WER/CER).
"""
from __future__ import annotations

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


def medical_term_recall(reference: str, hypothesis: str, terms: Iterable[str]) -> float:
    wanted = [t for t in terms if t and t in reference]
    if not wanted:
        return 1.0
    hits = sum(1 for term in wanted if term in hypothesis)
    return hits / len(wanted)


def english_term_recall(reference: str, hypothesis: str) -> float:
    """Recall over Latin-script tokens of the reference (e.g. `SpO2`, `ICU`)."""
    latin = [token for token in tokens(reference) if _has_latin(token)]
    if not latin:
        return 1.0
    hyp_text = _normalize_for_scoring(hypothesis)
    hits = 0
    remaining = hyp_text
    for token in latin:
        if token in remaining:
            hits += 1
            remaining = remaining.replace(token, "", 1)
    return hits / len(latin)


def _has_latin(token: str) -> bool:
    return any("a" <= ch.lower() <= "z" for ch in token)


def numeric_accuracy(reference: str, hypothesis: str) -> float:
    spans = [span.text.strip() for span in find_numeric_spans(_normalize_for_scoring(reference))]
    if not spans:
        return 1.0
    return sum(1 for span in spans if span in hypothesis) / len(spans)


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
