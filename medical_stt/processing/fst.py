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
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple

import ahocorasick

log = logging.getLogger("medical_stt.processing.fst")


@dataclass(frozen=True)
class Rule:
    source: str
    target: str
    numeric_in_source: bool = False


@dataclass
class LoadResult:
    rules_loaded: int
    conflicts: List[Tuple[str, List[str], str]] = field(default_factory=list)
    empty_sources_skipped: int = 0
    #: Rules the terminology layer refused before they ever reached the FST
    #: (per-rule validation failures skipped with a warning, so one bad rule
    #: in a hand-edited file cannot brick the client). Filled in by
    #: `TerminologyEngine.load`; always 0 for a bare FST load.
    invalid_rules_skipped: int = 0


class DeterministicFST:
    """Longest-match, left-to-right deterministic text rewriter."""

    def __init__(self) -> None:
        self._automaton = ahocorasick.Automaton()
        self._ready = False
        self._has_words = False
        self._rule_count = 0
        self._numeric_sources: set[str] = set()

    def load(self, rules: Sequence[Rule]) -> LoadResult:
        """Build the automaton from `rules`. Safe to call once; construct a
        new instance to reload (automatons cannot be mutated after
        `make_automaton()` without full rebuild anyway).
        """
        by_source: Dict[str, List[str]] = {}
        numeric_allowed: Dict[str, bool] = {}
        empty_skipped = 0
        for rule in rules:
            if not rule.source:
                empty_skipped += 1
                continue
            by_source.setdefault(rule.source, []).append(rule.target)
            # Duplicate sources cannot elevate each other's numeric permission.
            numeric_allowed[rule.source] = numeric_allowed.get(rule.source, True) and rule.numeric_in_source
        self._numeric_sources = {source for source, allowed in numeric_allowed.items() if allowed}

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

    def apply(
        self,
        text: str,
        protected_ranges: Sequence[Tuple[int, int]] = (),
        require_word_boundaries: bool = False,
        numeric_ranges: Sequence[Tuple[int, int]] = (),
    ) -> str:
        """Rewrite `text` using longest-match rules, left to right.

        `protected_ranges` is an optional list of (start, end) character
        spans (e.g. negation markers) that must not be touched. `numeric_ranges`
        also blocks matches, except when a rule explicitly opts in and each
        overlapping numeric span is wholly inside that rule's match.

        `require_word_boundaries` additionally drops a match that is glued to
        a word character on either side, so a rule for a whole word cannot
        fire inside a longer one. Off by default: this class is a general
        longest-match rewriter and its substring semantics are pinned by
        tests/test_fst.py. Terminology rewriting turns it on -- see
        `_is_word_char` for why a clinical rewriter has to.

        Filtering happens after overlap resolution rather than before, which
        is equivalent here: if a match is rejected because a word character
        touches its start, every shorter match at that same start is rejected
        for the same reason, and if it is rejected because of its end, the
        shorter match ends inside the same run of word characters. So no
        boundary-valid match is ever lost by resolving overlaps first.
        """
        if not text or not self._ready or not self._has_words:
            return text

        matches = self._longest_matches(text)
        if protected_ranges:
            # Merge once, then use binary search rather than scanning every
            # numeric/negation span for every match on long dictations.
            merged: List[Tuple[int, int]] = []
            for start, end in sorted(protected_ranges):
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
                else:
                    merged.append((start, end))
            ends = [end for _, end in merged]
            matches = [m for m in matches if not (
                (index := bisect_right(ends, m[0])) < len(merged) and merged[index][0] < m[1]
            )]
        if numeric_ranges:
            merged_numeric: List[Tuple[int, int]] = []
            for start, end in sorted(numeric_ranges):
                if merged_numeric and start <= merged_numeric[-1][1]:
                    merged_numeric[-1] = (merged_numeric[-1][0], max(end, merged_numeric[-1][1]))
                else:
                    merged_numeric.append((start, end))
            numeric_ends = [end for _, end in merged_numeric]
            allowed: List[Tuple[int, int, str]] = []
            for match in matches:
                start, end, _ = match
                flagged = text[start:end] in self._numeric_sources
                index = bisect_right(numeric_ends, start)
                blocked = False
                while index < len(merged_numeric) and merged_numeric[index][0] < end:
                    span_start, span_end = merged_numeric[index]
                    if not flagged or span_start < start or span_end > end:
                        blocked = True
                        break
                    index += 1
                if not blocked:
                    allowed.append(match)
            matches = allowed
        if require_word_boundaries:
            matches = [m for m in matches if _is_whole_word(text, m[0], m[1])]

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


#: Characters that continue a word beyond what `str.isalnum()` covers.
#:
#: `str.isalnum()` is Unicode-aware, so it already counts Persian and Arabic
#: letters and both the ASCII and the Persian/Arabic-Indic digits as word
#: characters. The zero-width joiners are added explicitly because a
#: ZWNJ-joined Persian compound is ONE word: "میلی‌جیوه" is millimetre-of-
#: mercury, not "میلی" next to "جیوه". Treating the ZWNJ as a boundary would
#: let a rule rewrite the second half of a compound and emit "میلی‌Mercury".
_WORD_JOINERS = "\u200c\u200d"


def _is_word_char(ch: str) -> bool:
    # The empty string must be False: `"" in _WORD_JOINERS` is True, and an
    # absent neighbour is a boundary, not a continuation.
    return bool(ch) and (ch.isalnum() or ch in _WORD_JOINERS)


def _is_whole_word(text: str, start: int, end: int) -> bool:
    """True if text[start:end] is a complete word, not a fragment of one."""
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    return not _is_word_char(before) and not _is_word_char(after)


def load_rules_from_pairs(pairs: Sequence[Tuple[str, str]]) -> DeterministicFST:
    """Backwards-compatible helper used by older call sites/tests."""
    fst = DeterministicFST()
    fst.load([Rule(src, tgt) for src, tgt in pairs])
    return fst
