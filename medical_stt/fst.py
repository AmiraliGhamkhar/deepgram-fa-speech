"""Aho-Corasick based longest-match rewriter."""
from __future__ import annotations
from typing import List, Tuple
import ahocorasick

class AhoFST:
    def __init__(self) -> None:
        self.automaton = ahocorasick.Automaton()
        self._ready = False

    def add_rule(self, source: str, target: str) -> None:
        if source:
            self.automaton.add_word(source, (source, target))

    def make(self) -> None:
        self.automaton.make_automaton()
        self._ready = True

    def apply(self, text: str) -> str:
        if not text or not self._ready:
            return text
        result = []
        last_end = 0
        for end, (original, replacement) in self.automaton.iter(text):
            start = end - len(original) + 1
            if start >= last_end:
                result.append(text[last_end:start])
                result.append(replacement)
                last_end = end + 1
        result.append(text[last_end:])
        return "".join(result)

    @property
    def _outputs(self):
        return self.automaton

def load_rules_from_pairs(pairs: List[Tuple[str, str]]) -> AhoFST:
    fst = AhoFST()
    for src, tgt in pairs:
        fst.add_rule(src, tgt)
    fst.make()
    return fst