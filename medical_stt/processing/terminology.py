"""Medical terminology rewriting with clinical-safety guardrails.

This module wraps the deterministic FST (fst.py) with the categorized rule
metadata from data/corrections.yaml so that:

* "unsafe"/"dangerous" rules are never applied.
* "context_dependent" rules are opt-in (disabled by default) because their
  source is a common word that is only unambiguous with extra context this
  system does not have.
* Numeric clinical expressions (processing/numbers.py) are never rewritten.
* Recognized negation phrases (processing/negation.py) are never rewritten,
  so a rule can't accidentally consume part of a negation and flip meaning.

Principle (see README): if uncertain, preserve the original transcript
rather than inventing or forcing a medical abbreviation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, List, Tuple

from .fst import DeterministicFST, LoadResult, Rule
from .negation import find_negation_spans
from .numbers import find_numeric_spans

log = logging.getLogger("medical_stt.processing.terminology")

SAFE_CATEGORIES = frozenset({"safe_lexical", "abbreviation_expansion", "specialty_terminology"})
OPT_IN_CATEGORIES = frozenset({"context_dependent"})
BLOCKED_CATEGORIES = frozenset({"unsafe"})

VALID_CATEGORIES = SAFE_CATEGORIES | OPT_IN_CATEGORIES | BLOCKED_CATEGORIES


@dataclass(frozen=True)
class TerminologyRule:
    source: str
    target: str
    category: str
    confidence: str = "high"
    requires_context: bool = False
    dangerous: bool = False
    specialty: str = ""

    def is_safe_default(self) -> bool:
        return not self.dangerous and self.category in SAFE_CATEGORIES


def parse_rules(raw_items: Iterable[dict]) -> List[TerminologyRule]:
    """Parse raw YAML rule dicts into TerminologyRule, tolerating the
    legacy flat {from, to} shape (treated as specialty_terminology/high
    confidence for backward compatibility, never as dangerous)."""
    rules: List[TerminologyRule] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        src = item.get("from") or item.get("source")
        tgt = item.get("to") or item.get("target")
        if not src or tgt is None:
            continue
        category = str(item.get("category", "specialty_terminology"))
        if category not in VALID_CATEGORIES:
            log.warning("unknown terminology category %r for %r; treating as context_dependent", category, src)
            category = "context_dependent"
        rules.append(
            TerminologyRule(
                source=str(src),
                target=str(tgt),
                category=category,
                confidence=str(item.get("confidence", "high")),
                requires_context=bool(item.get("requires_context", category in OPT_IN_CATEGORIES)),
                dangerous=bool(item.get("dangerous", category in BLOCKED_CATEGORIES)),
                specialty=str(item.get("specialty", "")),
            )
        )
    return rules


class TerminologyEngine:
    """Applies safe terminology rewriting to normalized transcripts."""

    def __init__(self, enable_context_dependent: bool = False) -> None:
        self.enable_context_dependent = enable_context_dependent
        self._fst = DeterministicFST()
        self._all_rules: List[TerminologyRule] = []
        self._active_rules: List[TerminologyRule] = []
        self._load_result: LoadResult = LoadResult(rules_loaded=0)

    def load(self, raw_items: Iterable[dict]) -> LoadResult:
        self._all_rules = parse_rules(raw_items)
        self._active_rules = [r for r in self._all_rules if self._is_active(r)]
        skipped_dangerous = sum(1 for r in self._all_rules if r.dangerous)
        skipped_context = sum(
            1 for r in self._all_rules if r.requires_context and not r.dangerous and not self.enable_context_dependent
        )
        # DeterministicFST.load() is documented as single-use, and this is why:
        # pyahocorasick accepts add_word() after make_automaton(), so reusing
        # the instance silently unions the previous load's rules into the
        # automaton while _all_rules/_active_rules -- and therefore rule_count
        # and total_rule_count -- describe only this one. apply() would then
        # rewrite text using rules the engine reports it does not have, which
        # is the worst failure mode for an auditable clinical rewriter.
        self._fst = DeterministicFST()
        self._load_result = self._fst.load(
            [Rule(r.source, r.target) for r in self._active_rules]
        )
        log.info(
            "terminology loaded: %d active rules (%d dangerous skipped, %d context-dependent skipped)",
            self._load_result.rules_loaded, skipped_dangerous, skipped_context,
        )
        return self._load_result

    def _is_active(self, rule: TerminologyRule) -> bool:
        if rule.dangerous or rule.category in BLOCKED_CATEGORIES:
            return False
        if rule.requires_context and not self.enable_context_dependent:
            return False
        return True

    @property
    def rule_count(self) -> int:
        return len(self._active_rules)

    @property
    def total_rule_count(self) -> int:
        return len(self._all_rules)

    def apply(self, text: str) -> str:
        """Rewrite `text`, protecting numeric expressions and negation
        markers from being touched by any rule.

        Word boundaries are required as well, and this is the single most
        important safety property in the pipeline. Aho-Corasick matches
        substrings, so without it a rule for a whole word fires inside a
        longer one. The shipped rule set contains `مش` -> `Mesh`, `پا` ->
        `Foot`, `دست` -> `Hand`, `امی` -> `MI`, `دما` -> `T` and `تنفس` ->
        `RR`, and Persian builds words by attachment, so substring matching
        turned ordinary dictation into this:

            مشکل تنفسی   ->  Meshکل RRی        ("respiratory problem")
            پاسخ دهید    ->  Footسخ دهید      ("please answer")
            امید به زندگی ->  MIد به زندگی    ("life expectancy")
            دستگاه تنفس  ->  Handگاه RR        ("ventilator")

        The third one is the dangerous shape: `MI` (myocardial infarction)
        injected into a sentence about life expectancy, then pasted into the
        clinician's record. Preserving the spoken word is always safe;
        rewriting part of one never is.
        """
        if not text:
            return text
        protected: List[Tuple[int, int]] = []
        protected.extend((s.start, s.end) for s in find_numeric_spans(text))
        protected.extend(find_negation_spans(text))
        return self._fst.apply(
            text, protected_ranges=protected, require_word_boundaries=True
        )
