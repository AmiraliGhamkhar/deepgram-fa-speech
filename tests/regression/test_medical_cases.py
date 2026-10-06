"""Medical regression corpus: realistic Persian-English dictation examples
checked for semantic preservation, not exact string equality (task section
9 & 18)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import List

import pytest
import yaml

from medical_stt.config import load_correction_rules_raw
from medical_stt.processing.negation import contains_negation
from medical_stt.processing.normalize import normalize
from medical_stt.processing.terminology import TerminologyEngine

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "medical_regression_corpus.yaml"


def _load_cases():
    data = yaml.safe_load(FIXTURES.read_text(encoding="utf-8"))
    return data["cases"]


@pytest.fixture(scope="module")
def engine() -> TerminologyEngine:
    eng = TerminologyEngine(enable_context_dependent=False)
    eng.load(load_correction_rules_raw())
    return eng


@pytest.fixture(scope="module")
def opt_in_engine() -> TerminologyEngine:
    """The configuration README.md offers a reviewed deployment.

    The invariant tests below run against both, because the shipped corpus is
    clean under the default engine and was *not* clean under this one: nearly
    every short rule source (`مش`, `پا`, `دست`, `امی`, `دما`, `تنفس`) is
    `context_dependent`, so enabling it is what turned `مشاهده شد` into
    `Meshاهده شد` in six of these fixtures.
    """
    eng = TerminologyEngine(enable_context_dependent=True)
    eng.load(load_correction_rules_raw())
    return eng


def _process(engine: TerminologyEngine, text: str) -> str:
    return engine.apply(normalize(text))


CASES = _load_cases()


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_regression_case(engine, case):
    result = _process(engine, case["input"])

    for expected in case.get("must_contain", []):
        assert expected in result, f"{case['id']}: expected {expected!r} in {result!r}"

    for forbidden in case.get("must_not_contain", []):
        assert forbidden not in result, f"{case['id']}: forbidden {forbidden!r} found in {result!r}"

    if case.get("preserves_negation"):
        assert contains_negation(case["input"])
        assert contains_negation(result)


# -- corpus-wide invariant ------------------------------------------------
#
# Every case above checks hand-written must_contain / must_not_contain lists.
# That is how a whole class of corruption survived: `مش` -> `Mesh` fired inside
# `مشاهده` ("observed"), so six fixtures in this corpus and in
# benchmarks/corpus.yaml came out as `Meshاهده شد`, and no hand-written list
# thought to forbid it. The invariant below needs no list: a rewrite may
# replace a whole word, but it may never weld Latin script onto the middle of
# a Persian one.

_PERSIAN_RE = re.compile(r"[\u0600-\u06ff]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def _mixed_script_tokens(text: str) -> List[str]:
    """Whitespace tokens carrying both Persian and Latin letters.

    Legitimate in principle -- an input could contain one -- so the assertion
    below is a subset relation, not "there are none".
    """
    return [
        token for token in text.split()
        if _PERSIAN_RE.search(token) and _LATIN_RE.search(token)
    ]


@pytest.mark.parametrize("opt_in", [False, True], ids=["default", "context_dependent"])
@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_no_rewrite_welds_latin_onto_a_persian_word(engine, opt_in_engine, case, opt_in):
    active = opt_in_engine if opt_in else engine
    original = normalize(case["input"])
    result = _process(active, case["input"])
    introduced = [t for t in _mixed_script_tokens(result) if t not in _mixed_script_tokens(original)]
    assert not introduced, (
        f"{case['id']}: a rule matched inside a word -- {introduced} in {result!r}"
    )


def _all_keyterm_phrases() -> List[str]:
    """Every phrase in data/keyterms/*.yaml, across all specialties.

    Read from the files rather than through `config.load_keyterms`, which
    loads general plus one specialty and caps the result -- the invariant is
    about the data, so it should see all of it.
    """
    phrases: List[str] = []
    for path in sorted((FIXTURES.parent.parent.parent / "data" / "keyterms").glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        phrases.extend(str(term) for term in data.get("keyterms", []))
    return phrases


@pytest.mark.parametrize("opt_in", [False, True], ids=["default", "context_dependent"])
def test_no_keyterm_welds_latin_onto_a_persian_word(engine, opt_in_engine, opt_in):
    """Same invariant over every phrase the shipped keyterm data declares.

    These are the phrases clinicians actually dictate, so a rule that fires
    inside one of them corrupts real reports rather than a synthetic case.
    """
    active = opt_in_engine if opt_in else engine
    phrases = _all_keyterm_phrases()
    assert phrases, "no keyterms were found -- the data layout changed"
    offenders = []
    for phrase in phrases:
        result = _process(active, phrase)
        introduced = [
            t for t in _mixed_script_tokens(result)
            if t not in _mixed_script_tokens(normalize(phrase))
        ]
        if introduced:
            offenders.append((phrase, result, introduced))
    assert not offenders, (
        f"{len(offenders)} of {len(phrases)} keyterms corrupted, e.g. {offenders[:3]}"
    )


def test_corpus_is_non_trivial():
    assert len(CASES) >= 15
