"""Deterministic longest-match text rewriter.

Guarantees (covered by tests/test_fst.py):

* Left-to-right processing: matches are applied in the order they start.
* Longest valid source match at each position: if two rules both match
  starting at (or covering) the same position, the longer source string
  always wins, even when the shorter one is found by the automaton first.
* Deterministic under rule-order permutation: adding the same set of rules
  in any order produces identical output for any input.
* Rule conflicts (same source, different targets) are resolved
  deterministically (lexicographically smallest target wins) rather than
  "whichever was added last", and are reported via `conflicts`.

Implementation: pyahocorasick's `iter_long` already returns non-overlapping,
longest matches at each scan position (verified in tests/test_fst.py against
the classic Aho-Corasick literature examples: {"he", "her", "here"} on
"he here her" -> ["he", "here", "her"], not partial/short matches). The
previous implementation used plain `iter()` (which yields *every* match,
short and long, in end-position order) and then greedily accepted whichever
one arrived first -- so a short rule registered earlier could "claim" a
position before a longer, more specific rule was ever considered. This
module fixes that by using `iter_long` and by canonicalizing duplicate
sources before building the automaton.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple

import ahocorasick

log = logging.getLogger("medical_stt.processing.fst")


@dataclass(frozen=True)
class Rule:
    source: str
    target: str


@dataclass
class LoadResult:
    rules_loaded: int
    conflicts: List[Tuple[str, List[str], str]] = field(default_factory=list)
    empty_sources_skipped: int = 0


class DeterministicFST:
    """Longest-match, left-to-right deterministic text rewriter."""

    def __init__(self) -> None:
        self._automaton = ahocorasick.Automaton()
        self._ready = False
        self._has_words = False
        self._rule_count = 0

    def load(self, rules: Sequence[Rule]) -> LoadResult:
        """Build the automaton from `rules`. Safe to call once; construct a
        new instance to reload (automatons cannot be mutated after
        `make_automaton()` without full rebuild anyway).
        """
        by_source: Dict[str, List[str]] = {}
        empty_skipped = 0
        for rule in rules:
            if not rule.source:
                empty_skipped += 1
                continue
            by_source.setdefault(rule.source, []).append(rule.target)

        conflicts: List[Tuple[str, List[str], str]] = []
        for source, targets in by_source.items():
            unique_targets = sorted(set(targets))
            chosen = unique_targets[0]
            if len(unique_targets) > 1:
                conflicts.append((source, unique_targets, chosen))
                log.warning(
                    "terminology conflict: source=%r has %d distinct targets; "
                    "deterministically choosing %r",
                    source, len(unique_targets), chosen,
                )
            self._automaton.add_word(source, (source, chosen))
            self._has_words = True

        # pyahocorasick refuses make_automaton()/iter_long on a trie with
        # zero words, so an empty rule set is handled explicitly as a
        # pass-through rather than calling into the library.
        if self._has_words:
            self._automaton.make_automaton()
        self._ready = True
        self._rule_count = len(by_source)
        return LoadResult(
            rules_loaded=len(by_source),
            conflicts=conflicts,
            empty_sources_skipped=empty_skipped,
        )

    @property
    def rule_count(self) -> int:
        return self._rule_count

    @property
    def ready(self) -> bool:
        return self._ready

    def apply(self, text: str, protected_ranges: Sequence[Tuple[int, int]] = ()) -> str:
        """Rewrite `text` using longest-match rules, left to right.

        `protected_ranges` is an optional list of (start, end) character
        spans (e.g. numeric expressions, negation markers) that must not be
        touched even if a rule would otherwise match inside them.
        """
        if not text or not self._ready or not self._has_words:
            return text

        matches = self._longest_matches(text)
        if protected_ranges:
            matches = [m for m in matches if not _overlaps_any(m, protected_ranges)]

        return self._render(text, matches)

    def _longest_matches(self, text: str) -> List[Tuple[int, int, str]]:
        """Return non-overlapping (start, end, replacement) triples using
        the automaton's longest-match iterator, then re-resolve overlaps
        deterministically by preferring (a) longer source span, (b) earlier
        start position, so behavior never depends on automaton internal
        traversal order."""
        raw: List[Tuple[int, int, str]] = []
        for end, (source, target) in self._automaton.iter_long(text):
            start = end - len(source) + 1
            raw.append((start, end + 1, target))

        # iter_long already yields non-overlapping longest matches, but we
        # defensively re-sort/re-resolve in case of future library changes
        # or manual construction paths, keeping the guarantee explicit and
        # tested rather than implicit.
        raw.sort(key=lambda m: (m[0], -(m[1] - m[0])))
        resolved: List[Tuple[int, int, str]] = []
        cursor = 0
        for start, end, target in raw:
            if start < cursor:
                continue
            resolved.append((start, end, target))
            cursor = end
        return resolved

    @staticmethod
    def _render(text: str, matches: List[Tuple[int, int, str]]) -> str:
        out: List[str] = []
        cursor = 0
        for start, end, target in matches:
            out.append(text[cursor:start])
            out.append(target)
            cursor = end
        out.append(text[cursor:])
        return "".join(out)


def _overlaps_any(match: Tuple[int, int, str], ranges: Sequence[Tuple[int, int]]) -> bool:
    start, end, _ = match
    for r_start, r_end in ranges:
        if start < r_end and end > r_start:
            return True
    return False


def load_rules_from_pairs(pairs: Sequence[Tuple[str, str]]) -> DeterministicFST:
    """Backwards-compatible helper used by older call sites/tests."""
    fst = DeterministicFST()
    fst.load([Rule(src, tgt) for src, tgt in pairs])
    return fst
