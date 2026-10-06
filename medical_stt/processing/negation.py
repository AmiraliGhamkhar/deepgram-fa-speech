"""Conservative, deterministic negation/clinical-fidelity guard.

This is NOT clinical NLP and makes no attempt at full negation scope
detection. It provides one narrow guarantee: terminology rules must not be
applied in a way that could flip a negated finding into a positive one, and
common negation/fidelity phrases are never altered by normalization or
terminology rewriting.

The mechanism is simple and auditable: a fixed list of negation/fidelity
markers is treated as "hard boundaries" that:
  1. Are never rewritten by the terminology engine (matched fully, not
     partially, to avoid accidental corruption of the negation words
     themselves), and
  2. Split a sentence into "protected" spans so a downstream consumer that
     wants extra caution (e.g. suppressing overly aggressive rewriting) can
     detect that the current utterance contains a negation.
"""
from __future__ import annotations

import re
from typing import List, Tuple

# Order matters only for readability; matching is longest-match via regex
# alternation sorted by length below.
NEGATION_MARKERS: Tuple[str, ...] = (
    "وجود ندارد",
    # Past tense of the above. Clinical dictation reports what *was* found,
    # so the past forms are at least as common as the present ones, and a
    # list that only covered the present tense silently stopped protecting
    # exactly the sentences it existed for:
    #     بیمار شکایتی نداشت      ("the patient had no complaint")
    #     تب نداشت                  ("there was no fever")
    #     هیچ توده‌ای دیده نشد    ("no mass was seen")
    #     آنژیوگرافی انجام نشد      ("the angiography was not performed")
    "وجود نداشت",
    "مشاهده نشد",
    "منفی است",
    "رد می‌شود",
    "رد میشود",
    "رد شد",
    "رد گردید",
    "شواهدی از",
    "بدون",
    "فاقد",
    "هیچ",
    "ندارد",
    "نداشت",
    "نیست",
    "نبود",
    "نشد",
    "نشود",
    "نمی‌شود",
    "نمیشود",
    "منفی",
    # "Denies" -- `نفی کرد`, `نفی شد`, and the noun-phrase `نفی سابقه` that
    # Persian notes use for "denies a history of". The bare stem covers all of
    # them; the verb forms would be redundant entries.
    "نفی",
)

# Every marker above is matched as a substring, so each was checked against
# the 945 distinct Persian tokens in this repository's data, fixtures and
# documentation for words it would match inside. One listed marker has a known
# collision, discussed below, and one tempting marker was left out because of
# one.
#
# `رد` on its own was deliberately NOT added, though it looks tempting: it
# occurs inside `درصد` ("percent") and `مرداد` (a month name), so it would flag
# an ordinary measurement as a negated finding. Only the complete verb forms
# `رد شد` and `رد گردید` are listed.
#
# `نفی` is the one listed marker with a known collision -- it matches inside
# `نفیس` ("exquisite"). It stays, because the collision errs in the only safe
# direction for a guard like this: a false positive protects three more
# characters from rewriting and tells a consumer to be more careful, whereas
# the alternative (listing only `نفی کرد` / `نفی شد`) would miss the
# noun-phrase `نفی سابقه` that Persian clinical notes actually use.

_SORTED_MARKERS = sorted(NEGATION_MARKERS, key=len, reverse=True)
_NEGATION_RE = re.compile("|".join(re.escape(m) for m in _SORTED_MARKERS))


def contains_negation(text: str) -> bool:
    """True if `text` contains any recognized negation/fidelity marker."""
    return bool(_NEGATION_RE.search(text))


def find_negation_spans(text: str) -> List[Tuple[int, int]]:
    """Return non-overlapping (start, end) spans of negation markers,
    longest match first at each position."""
    spans: List[Tuple[int, int]] = []
    occupied = [False] * len(text)
    for m in _NEGATION_RE.finditer(text):
        start, end = m.start(), m.end()
        if any(occupied[start:end]):
            continue
        spans.append((start, end))
        for i in range(start, end):
            occupied[i] = True
    spans.sort()
    return spans
