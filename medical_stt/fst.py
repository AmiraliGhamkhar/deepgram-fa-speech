"""Lightweight character-level Finite State Transducer (pure Python)."""

from __future__ import annotations

from typing import Dict, List, Tuple


class SimpleFST:
    """Longest-match left-to-right string rewriter built as a trie."""

    def __init__(self) -> None:
        self.root: Dict = {}
        self._outputs: Dict[int, str] = {}

    def add_rule(self, source: str, target: str) -> None:
        if not source:
            return
        node = self.root
        for char in source:
            if char not in node:
                node[char] = {}
            node = node[char]
        self._outputs[id(node)] = target

    def apply(self, text: str) -> str:
        if not text:
            return text
        result: List[str] = []
        i = 0
        n = len(text)
        while i < n:
            node = self.root
            last_end = -1
            last_out = None
            j = i
            while j < n and text[j] in node:
                node = node[text[j]]
                nid = id(node)
                if nid in self._outputs:
                    last_end = j + 1
                    last_out = self._outputs[nid]
                j += 1
            if last_end != -1:
                result.append(last_out)  # type: ignore[arg-type]
                i = last_end
            else:
                result.append(text[i])
                i += 1
        return "".join(result)


def load_rules_from_pairs(pairs: List[Tuple[str, str]]) -> SimpleFST:
    fst = SimpleFST()
    for src, tgt in pairs:
        fst.add_rule(src, tgt)
    return fst
