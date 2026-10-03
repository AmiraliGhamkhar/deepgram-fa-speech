"""Data-safety tests for data/corrections.yaml.

The terminology engine is deterministic and longest-match; these tests pin
the *data* policy on top of it:

* an ordinary Persian word must not be replaced with a specific medical
  concept merely because a medical reading exists (`کلیه` = "all",
  `نمونه` = "sample", `جفت` = "pair", ...);
* unambiguous medical mappings must still fire by default;
* every ambiguous mapping is preserved as opt-in `context_dependent`, so
  a reviewed deployment can enable it without editing the data file.
"""
from __future__ import annotations

import pytest

from medical_stt.config import load_correction_rules_raw
from medical_stt.processing.normalize import normalize
from medical_stt.processing.terminology import TerminologyEngine


@pytest.fixture(scope="module")
def engine() -> TerminologyEngine:
    eng = TerminologyEngine(enable_context_dependent=False)
    eng.load(load_correction_rules_raw())
    return eng


@pytest.fixture(scope="module")
def opt_in_engine() -> TerminologyEngine:
    eng = TerminologyEngine(enable_context_dependent=True)
    eng.load(load_correction_rules_raw())
    return eng


def process(engine: TerminologyEngine, text: str) -> str:
    return engine.apply(normalize(text))


# -- ordinary language must survive ---------------------------------------

AMBIGUOUS_CASES = [
    ("کلیه بیماران ویزیت شدند", "Kidney"),
    ("نمونه را به آزمایشگاه فرستادیم", "Specimen"),
    ("جفت کفشش را برداشت", "Placenta"),
    ("کشت گندم در این منطقه", "Culture"),
    ("موهایش را شانه کرد", "Shoulder"),
    ("رحم کن و کمکش کن", "Uterus"),
    ("تراشه رایانه را عوض کرد", "Trachea"),
    ("شستشوی ظرف‌ها را انجام داد", "Irrigation"),
    ("آپاچی یک سرور است", "APACHE"),
    ("کلسترول بالا دارد؟", "Chol"),  # full word preserved by default
    ("تنگی کوچه زیاد است", "Stricture"),
    ("وضعیت بیمار مشکوک است", "Suspicious"),
    ("همه چیز پایدار است", "stable"),
]


@pytest.mark.parametrize("text,forbidden", AMBIGUOUS_CASES)
def test_ordinary_sentence_is_not_converted_to_a_medical_term(engine, text, forbidden):
    result = process(engine, text)
    assert forbidden not in result, f"{text!r} was rewritten to {result!r}"


def test_ambiguous_phrases_are_not_rewritten_by_default(engine):
    assert "QS" not in process(engine, "به مقدار کافی مایعات بنوشد")
    assert "DS" not in process(engine, "بیمار عدم پیگیری داشت")
    assert "OB" not in process(engine, "خون در مدفوع مشاهده شد")


# -- unambiguous medical mappings still apply -----------------------------


MEDICAL_CASES = [
    ("بیمار در آی سی یو بستری شد", "ICU"),
    ("سابقه سکته قلبی دارد", "MI"),
    ("دیابت دارد", "diabetes"),
    ("سوند فولی گذاشته شد", "Foley"),
    ("نرمال سالین دریافت کرد", "Normal Saline"),
    ("نوار قلب گرفته شد", "ECG"),
    ("هایپرتنشن کنترل شده است", "hypertension"),
]


@pytest.mark.parametrize("text,expected", MEDICAL_CASES)
def test_unambiguous_medical_mapping_still_applies(engine, text, expected):
    assert expected in process(engine, text)


def test_ambiguous_rules_are_disabled_not_deleted(opt_in_engine):
    """Opting in must restore them, which proves they were not deleted."""
    assert "Kidney" in process(opt_in_engine, "کلیه بیمار")
    assert "Specimen" in process(opt_in_engine, "نمونه فرستاده شد")
    assert "QS" in process(opt_in_engine, "به مقدار کافی")


def test_no_rule_source_is_empty_or_duplicated():
    rules = load_correction_rules_raw()
    sources = [str(rule["from"]).strip() for rule in rules]
    assert all(sources), "a rule with an empty source would match everywhere"
    assert len(sources) == len(set(sources)), "duplicate sources resolve ambiguously"


def test_every_rule_declares_a_known_category():
    from medical_stt.processing.terminology import VALID_CATEGORIES

    for rule in load_correction_rules_raw():
        assert rule.get("category") in VALID_CATEGORIES, rule
        assert rule.get("to") not in (None, ""), rule
