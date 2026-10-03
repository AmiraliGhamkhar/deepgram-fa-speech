"""One owner per formatting concern (Phase 13 guardrails).

The pipeline has several text stages, and the failure mode to prevent is
two stages quietly owning the same concern (for example, two modules
collapsing spaces or two modules inserting bidi marks). These tests pin the
ownership map documented in README > "Formatting ownership":

    unicode/digits/whitespace/punctuation  -> processing/normalize.py
    numbers + units (protected spans)      -> processing/numbers.py
    terminology/abbreviations              -> processing/terminology.py (FST)
    bidi marks + display order             -> processing/bidi.py
    line breaks / injected whitespace      -> injection/text_injector.py
    provider-side spelling fixes           -> data/asr_replacements.yaml
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from medical_stt.config import load_correction_rules_raw
from medical_stt.injection.text_injector import normalize_injected_whitespace
from medical_stt.processing.bidi import LRM, RLM, to_injected
from medical_stt.processing.normalize import normalize
from medical_stt.processing.terminology import TerminologyEngine

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "medical_stt"


@pytest.fixture(scope="module")
def engine() -> TerminologyEngine:
    eng = TerminologyEngine()
    eng.load(load_correction_rules_raw())
    return eng


# -- exactly one owner for directional marks -------------------------------

DIRECTIONAL_MARKS = {"\u200e": "LRM", "\u200f": "RLM", "\u202a": "LRE", "\u202b": "RLE",
                     "\u202c": "PDF", "\u2066": "LRI", "\u2067": "RLI", "\u2069": "PDI"}


def test_only_bidi_module_contains_directional_control_characters():
    """A second module emitting bidi marks is how mixed text breaks."""
    offenders = []
    for path in PACKAGE.rglob("*.py"):
        if path.name == "bidi.py":
            continue
        source = path.read_text(encoding="utf-8")
        for char, name in DIRECTIONAL_MARKS.items():
            if char in source:
                offenders.append(f"{path.relative_to(ROOT)} contains {name}")
    assert not offenders, "; ".join(offenders)


def test_normalization_and_terminology_never_emit_directional_marks(engine):
    text = "فشار خون 120/80 mmHg و MI در ECG"
    assert not any(mark in normalize(text) for mark in (LRM, RLM))
    assert not any(mark in engine.apply(normalize(text)) for mark in (LRM, RLM))


def test_to_injected_adds_at_most_one_leading_mark():
    for text in ["گزارش", "MI در ECG", "120/80 mmHg", "HbA1c برابر 7.2 درصد"]:
        injected = to_injected(text)
        assert not injected.lstrip(LRM + RLM).startswith((LRM, RLM))
        assert injected.count(LRM) + injected.count(RLM) <= 1
        assert injected.lstrip(LRM + RLM) != ""


# -- line breaks have exactly one owner ------------------------------------


LINE_STRUCTURED = (
    "نام بیمار: علی رضایی\n"
    "تشخیص: هایپرتنشن\n"
    "فشار خون: 120/80 mmHg\n"
    "SpO2: 98%"
)


def test_content_normalization_preserves_line_structure():
    normalized = normalize(LINE_STRUCTURED)
    assert normalized.count("\n") == LINE_STRUCTURED.count("\n")
    assert "\r" not in normalized


def test_injection_whitespace_is_structural_and_idempotent():
    once = normalize_injected_whitespace(LINE_STRUCTURED)
    assert once == normalize_injected_whitespace(once)
    assert once.count("\n") == 3
    assert normalize_injected_whitespace("a   b\t\tc") == "a b c"
    assert normalize_injected_whitespace("a\r\nb\rc") == "a\nb\nc"  # CRLF/CR -> LF
    assert normalize_injected_whitespace("\n\nfirst\n\n") == "first"


def test_no_other_module_collapses_newlines_into_spaces():
    """`" ".join(text.split())` is the historical newline-destroying bug."""
    offenders = []
    for path in PACKAGE.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if re.search(r'" "\s*\.\s*join\s*\(\s*\w+\s*\.\s*split\s*\(\s*\)\s*\)', source):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"newline-collapsing join found in {offenders}"


# -- stage boundaries agree on clinical numbers ----------------------------


CLINICAL_NUMBERS = ["120/80", "98%", "7.2", "500 mg", "5-10 mg", "2 to 4 cm", "14:30"]


@pytest.mark.parametrize("number", CLINICAL_NUMBERS)
def test_numbers_survive_every_stage(number, engine):
    text = f"مقدار {number} ثبت شد"
    normalized = normalize(text)
    assert number in normalized
    assert number in engine.apply(normalized)
    assert number in to_injected(engine.apply(normalized))
    assert number in normalize_injected_whitespace(to_injected(engine.apply(normalized)))


def test_terminology_is_the_only_stage_that_expands_abbreviations(engine):
    spoken = "بیمار در آی سی یو بستری شد"
    assert "ICU" not in normalize(spoken), "normalize must not expand terminology"
    assert "ICU" in engine.apply(normalize(spoken))


def test_stage_composition_is_deterministic_and_idempotent(engine):
    def pipeline(text: str) -> str:
        return normalize_injected_whitespace(to_injected(engine.apply(normalize(text))))

    first = pipeline(LINE_STRUCTURED)
    assert pipeline(LINE_STRUCTURED) == first
    assert normalize(normalize(LINE_STRUCTURED)) == normalize(LINE_STRUCTURED)
    assert first.count("\n") == 3
    assert "120/80" in first and "98%" in first
