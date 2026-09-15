"""Medical regression corpus: realistic Persian-English dictation examples
checked for semantic preservation, not exact string equality (task section
9 & 18)."""
from __future__ import annotations

from pathlib import Path

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


def test_corpus_is_non_trivial():
    assert len(CASES) >= 15
