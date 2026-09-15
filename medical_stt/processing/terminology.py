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
from typing import Iterable, List, Sequence

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
        markers from being touched by any rule."""
        if not text:
            return text
        protected: List[tuple] = []
        protected.extend((s.start, s.end) for s in find_numeric_spans(text))
        protected.extend(find_negation_spans(text))
        return self._fst.apply(text, protected_ranges=protected)


def load_rules_from_yaml_data(data) -> Sequence[dict]:
    """Extract the `rules` list from parsed YAML, tolerating both the
    categorized dict-list shape and a bare list."""
    if isinstance(data, dict):
        return data.get("rules") or []
    if isinstance(data, list):
        return data
    return []
