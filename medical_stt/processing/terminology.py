"""Medical terminology rewriting with clinical-safety guardrails.

This module wraps the deterministic FST (fst.py) with the categorized rule
metadata from data/corrections.yaml so that:

* "unsafe"/"dangerous" rules are never applied.
* "context_dependent" rules are opt-in (disabled by default) because their
  source is a common word that is only unambiguous with extra context this
  system does not have. Rules carrying `confidence: low` take the same
  opt-in in any category: the metadata is the engine's stated trust in the
  mapping, so a low-trust mapping must not fire by default either.
* Numeric expressions are protected except inside explicit digit-preserving rules.
* Recognized negation phrases (processing/negation.py) are never rewritten,
  so a rule can't accidentally consume part of a negation and flip meaning.

Principle (see README): if uncertain, preserve the original transcript
rather than inventing or forcing a medical abbreviation.

Loading principle: ONE malformed rule must not brick the client. Structural
failures of the file (no top-level `rules` list) are `ConfigError`s raised
by the config loader, but a *per-rule* validation failure -- a digit-run
violation, an empty side, a non-boolean flag -- only skips that rule, with
a WARNING naming its index and the reason (never the rule text, which at
worst could be dictated vocabulary pasted into the file). A 5,000-line
hand-edited YAML with one typo must still start the app with every other
rule active.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable, List, Sequence, Tuple

from .fst import DeterministicFST, LoadResult, Rule
from .negation import census_fully_negated_sources, find_negation_spans
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


@dataclass(frozen=True)
class ParseResult:
    """Outcome of parsing raw rule dicts: the usable rules, plus how many
    were skipped (and why, by index) so the skip is auditable at startup."""

    rules: List[TerminologyRule]
    skipped_invalid: int = 0
    skipped_reasons: Tuple[Tuple[int, str], ...] = field(default_factory=tuple)


def parse_rules_detailed(raw_items: Iterable[dict]) -> ParseResult:
    """Parse raw YAML rule dicts, skipping (never raising on) a bad rule.

    Legacy flat {from, to} shapes are tolerated (treated as
    specialty_terminology/high confidence for backward compatibility,
    never as dangerous).

    Both sides are normalized at parse time: every transcript reaching the
    FST has already been normalized, so a raw Arabic-yeh source would never
    match, and a raw target would inject non-normalized text back into the
    pipeline. Digit-run validation uses the same canonical representation on
    both sides.

    Per-rule validation failures -- empty/blank sides, a non-boolean
    `numeric_in_source`, digit-run violations, an unflagged numeric span --
    skip just that rule and log a WARNING with its index and the reason.
    Unknown categories keep their established downgrade to
    `context_dependent` rather than being skipped outright.
    """
    rules: List[TerminologyRule] = []
    skipped: List[Tuple[int, str]] = []

    def skip(index: int, reason: str) -> None:
        skipped.append((index, reason))
        log.warning("skipping terminology rule %d: %s", index, reason)

    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            skip(index, "rule entry is not a mapping")
            continue
        src = item.get("from") or item.get("source")
        tgt = item.get("to")
        if tgt is None:
            tgt = item.get("target")
        if src is None or tgt is None:
            skip(index, "rule is missing a source or a target")
            continue
        numeric_in_source = item.get("numeric_in_source", False)
        if not isinstance(numeric_in_source, bool):
            skip(index, "numeric_in_source must be a boolean")
            continue
        normalized_source = normalize(str(src))
        normalized_target = normalize(str(tgt))
        if not normalized_source or not normalized_target:
            skip(index, "rule source or target is empty")
            continue
        source_digits = re.findall(r"\d+", normalized_source)
        target_digits = re.findall(r"\d+", normalized_target)
        if numeric_in_source:
            if not source_digits or source_digits != target_digits:
                skip(index, "numeric_in_source requires identical digit sequences in source and target")
                continue
        elif find_numeric_spans(normalized_source):
            skip(index, "rule with a numeric span requires numeric_in_source: true")
            continue
        category = str(item.get("category", "specialty_terminology"))
        if category not in VALID_CATEGORIES:
            log.warning(
                "unknown terminology category %r in rule %d; treating as context_dependent",
                category, index,
            )
            category = "context_dependent"
        rules.append(
            TerminologyRule(
                source=normalized_source,
                target=normalized_target,
                category=category,
                confidence=str(item.get("confidence", "high")).strip().lower(),
                requires_context=bool(item.get("requires_context", category in OPT_IN_CATEGORIES)),
                dangerous=bool(item.get("dangerous", category in BLOCKED_CATEGORIES)),
                specialty=str(item.get("specialty", "")),
                numeric_in_source=numeric_in_source,
            )
        )
    return ParseResult(
        rules=rules, skipped_invalid=len(skipped), skipped_reasons=tuple(skipped)
    )


def parse_rules(raw_items: Iterable[dict]) -> List[TerminologyRule]:
    """Parse raw YAML rule dicts into the usable rules (see
    `parse_rules_detailed` for the skip-and-warn policy and the count)."""
    return parse_rules_detailed(raw_items).rules


class TerminologyEngine:
    """Applies safe terminology rewriting to normalized transcripts."""

    def __init__(self, enable_context_dependent: bool = False) -> None:
        self.enable_context_dependent = enable_context_dependent
        self._fst = DeterministicFST()
        self._all_rules: List[TerminologyRule] = []
        self._active_rules: List[TerminologyRule] = []
        self._skipped_invalid = 0
        self._load_result: LoadResult = LoadResult(rules_loaded=0)

    @staticmethod
    def _needs_opt_in(rule: TerminologyRule) -> bool:
        """Opt-in gate shared by context-dependent and low-confidence rules.

        `confidence: low` is the file's own statement that the mapping is
        not fully trusted; treating it exactly like `requires_context`
        gives that metadata the one safe meaning it can have.
        """
        return rule.requires_context or rule.confidence == "low"

    def load(self, raw_items: Iterable[dict]) -> LoadResult:
        parsed = parse_rules_detailed(raw_items)
        self._all_rules = parsed.rules
        self._skipped_invalid = parsed.skipped_invalid
        self._active_rules = [r for r in self._all_rules if self._is_active(r)]
        skipped_dangerous = sum(1 for r in self._all_rules if r.dangerous)
        skipped_context = sum(
            1
            for r in self._all_rules
            if self._needs_opt_in(r) and not r.dangerous and not self.enable_context_dependent
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
        self._load_result.invalid_rules_skipped = self._skipped_invalid
        # Startup-only census: an active source that is itself a negation
        # marker can never fire (negation spans are hard-protected in
        # `apply`); surface the count instead of leaving it silently dead.
        census_fully_negated_sources(r.source for r in self._active_rules)
        log.info(
            "terminology loaded: %d active rules (%d dangerous skipped, "
            "%d context-dependent skipped, %d invalid skipped)",
            self._load_result.rules_loaded, skipped_dangerous, skipped_context,
            self._skipped_invalid,
        )
        return self._load_result

    def _is_active(self, rule: TerminologyRule) -> bool:
        if rule.dangerous or rule.category in BLOCKED_CATEGORIES:
            return False
        if self._needs_opt_in(rule) and not self.enable_context_dependent:
            return False
        return True

    @property
    def rule_count(self) -> int:
        return len(self._active_rules)

    @property
    def skipped_rule_count(self) -> int:
        """Rules skipped at parse time as invalid (see `parse_rules_detailed`)."""
        return self._skipped_invalid

    @property
    def total_rule_count(self) -> int:
        # Every rule entry in the file, including the invalid ones that were
        # skipped while parsing: "X / Y total" at startup must account for
        # the whole file, not just the slice that parsed.
        return len(self._all_rules) + self._skipped_invalid

    def apply(
        self,
        text: str,
        protected_ranges: Sequence[Tuple[int, int]] = (),
    ) -> str:
        """Rewrite `text`, protecting negation, numeric and caller-provided spans.

        `protected_ranges` are extra character spans (e.g. low-confidence
        medical tokens) that no rule may touch, including rules explicitly
        flagged `numeric_in_source`. They are merged with negation spans,
        which have the same hard-protection semantics. Only numeric-in-source
        rules may consume an entire numeric span when it is NOT in a protected
        range.

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
        # Negation and caller-provided low-confidence spans are never eligible
        # for the numeric-in-source exception.
        protected = list(find_negation_spans(text)) + list(protected_ranges)
        return self._fst.apply(
            text, protected_ranges=protected,
            numeric_ranges=numeric, require_word_boundaries=True,
        )
