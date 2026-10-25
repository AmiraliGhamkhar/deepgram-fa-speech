"""Medical terminology rewriting with clinical-safety guardrails.

This module wraps the deterministic FST (fst.py) with the categorized rule
metadata from data/corrections.yaml so that:

* "unsafe"/"dangerous" rules are never applied.
* "context_dependent" rules are opt-in (disabled by default) because their
  source is a common word that is only unambiguous with extra context this
  system does not have.
* Numeric expressions are protected except inside explicit digit-preserving rules.
* Recognized negation phrases (processing/negation.py) are never rewritten,
  so a rule can't accidentally consume part of a negation and flip meaning.

Principle (see README): if uncertain, preserve the original transcript
rather than inventing or forcing a medical abbreviation.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable, List

from ..config import ConfigError
from .fst import DeterministicFST, LoadResult, Rule
from .negation import find_negation_spans
from .normalize import normalize
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
    numeric_in_source: bool = False


def parse_rules(raw_items: Iterable[dict]) -> List[TerminologyRule]:
    """Parse raw YAML rule dicts into TerminologyRule, tolerating the
    legacy flat {from, to} shape (treated as specialty_terminology/high
    confidence for backward compatibility, never as dangerous).

    Both sides are normalized at parse time: every transcript reaching the
    FST has already been normalized, so a raw Arabic-yeh source would never
    match, and a raw target would inject non-normalized text back into the
    pipeline. Digit-run validation uses the same canonical representation on
    both sides.
    """
    rules: List[TerminologyRule] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        src = item.get("from") or item.get("source")
        tgt = item.get("to") or item.get("target")
        if not src or tgt is None:
            continue
        numeric_in_source = item.get("numeric_in_source", False)
        if not isinstance(numeric_in_source, bool):
            raise ConfigError("terminology numeric_in_source must be a boolean")
        normalized_source = normalize(str(src))
        normalized_target = normalize(str(tgt))
        if not normalized_source:
            raise ConfigError("terminology rule source normalizes to an empty string")
        source_digits = re.findall(r"\d+", normalized_source)
        target_digits = re.findall(r"\d+", normalized_target)
        if numeric_in_source:
            if not source_digits or source_digits != target_digits:
                raise ConfigError("numeric_in_source requires identical digit sequences in source and target")
        elif find_numeric_spans(normalized_source):
            raise ConfigError("terminology rule with a numeric span requires numeric_in_source: true")
        category = str(item.get("category", "specialty_terminology"))
        if category not in VALID_CATEGORIES:
            log.warning("unknown terminology category %r for %r; treating as context_dependent", category, normalized_source)
            category = "context_dependent"
        rules.append(
            TerminologyRule(
                source=normalized_source,
                target=normalized_target,
                category=category,
                confidence=str(item.get("confidence", "high")),
                requires_context=bool(item.get("requires_context", category in OPT_IN_CATEGORIES)),
                dangerous=bool(item.get("dangerous", category in BLOCKED_CATEGORIES)),
                specialty=str(item.get("specialty", "")),
                numeric_in_source=numeric_in_source,
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
            [Rule(r.source, r.target, numeric_in_source=r.numeric_in_source) for r in self._active_rules]
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
        """Rewrite `text`, protecting negation and numeric expressions.

        Only numeric-in-source rules may consume an entire numeric span.

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
        numeric = [(s.start, s.end) for s in find_numeric_spans(text)]
        # Negation is never eligible for the numeric-in-source exception.
        return self._fst.apply(
            text, protected_ranges=find_negation_spans(text),
            numeric_ranges=numeric, require_word_boundaries=True,
        )
