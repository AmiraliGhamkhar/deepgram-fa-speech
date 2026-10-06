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


# -- a rule for a whole word must not fire inside a longer one ------------
#
# Same policy as above, different mechanism. Aho-Corasick matches substrings,
# and Persian builds words by attachment, so a short rule source -- `مش`,
# `پا`, `دست`, `امی`, `دما`, `تنفس` are all in the shipped set -- used to fire
# inside ordinary words. The output went straight into the clinician's record.
#
#     مشکل تنفسی    ->  Meshکل RRی       ("respiratory problem")
#     پاسخ دهید     ->  Footسخ دهید     ("please answer")
#     دستگاه تنفس   ->  Handگاه RR       ("ventilator")
#     امید به زندگی ->  MIد به زندگی    ("life expectancy")
#
# The last one is the dangerous shape: a clinical abbreviation that was never
# spoken, injected into a sentence about something else.


@pytest.mark.parametrize(
    "text",
    [
        "مشخص شد",              # contains مش -> Mesh
        "مشکل تنفسی دارد",      # contains مش and تنفس
        "مشاوره پزشکی انجام شد",
        "منافق",                # contains ناف -> Umbilicus
        "پاسخ دهید",            # contains پا -> Foot
        "پایان جلسه",
        "پاره شد",
        "دستور دارو صادر شد",   # contains دست -> Hand
        "دستی بررسی شد",
        "امید به زندگی",        # contains امی -> MI
        "بیمار امیدوار است",
        "نافه",
        "خالی از درد",          # contains خال -> Nevus
        "دماسنج",               # contains دما -> T
        "هیپوگلیسمی",           # contains هیپ -> Hip
        "سنگریزه",
        "رحمت",
        "گچی",
        "پینس",
    ],
)
def test_a_word_containing_a_short_rule_source_is_preserved(opt_in_engine, text):
    """Even with every opt-in rule enabled, a fragment is not a word."""
    assert process(opt_in_engine, text) == normalize(text)


def test_no_clinical_abbreviation_is_injected_into_an_unrelated_word(opt_in_engine):
    """The failure mode that matters clinically.

    `امی` is the spoken form of "MI", and `امید` (hope) contains it. Injecting
    a diagnosis that was never spoken into a sentence about life expectancy is
    not a formatting problem.
    """
    for text in ("امید به زندگی", "بیمار امیدوار است", "پایداری همودینامیک"):
        result = process(opt_in_engine, text)
        assert "MI" not in result, f"{text!r} became {result!r}"
        assert "Mesh" not in result and "Foot" not in result and "Hand" not in result


@pytest.mark.parametrize(
    "text,expected_fragment",
    [
        ("بیمار در آی سی یو بستری است", "ICU"),
        ("سکته قلبی داشته است", "MI"),
        ("نوار قلب گرفته شد", "ECG"),
        ("بیوپسی انجام شد", "Biopsy"),
        ("کبد چرب دارد", "Fatty liver"),  # longest match wins over کبد -> Liver
    ],
)
def test_whole_word_terminology_rewrites_still_fire(opt_in_engine, text, expected_fragment):
    """Requiring a boundary must not stop the rewrites that were the point."""
    assert expected_fragment in process(opt_in_engine, text)


def test_a_zwnj_compound_is_one_word_and_is_not_rewritten_inside(opt_in_engine):
    """میلی‌جیوه is millimetre-of-mercury, not میلی next to جیوه."""
    text = "فشار خون 120/80 میلی\u200cجیوه است"
    result = process(opt_in_engine, text)
    assert "میلی\u200cجیوه" in result, f"the compound was split: {result!r}"


def test_numeric_and_negation_protection_still_hold_with_boundaries(opt_in_engine):
    """The two older protections are independent of the new one."""
    result = process(opt_in_engine, "فشار خون 120/80 میلی\u200cجیوه و تب ندارد")
    assert "120/80" in result
    assert "ندارد" in result


def test_dangerous_rules_are_still_blocked_after_the_boundary_change(opt_in_engine):
    """The boundary fix must not become a reason to re-enable them."""
    for text in ("بیمار ناشتا است", "کاهش وزن داشته است", "کم شدن درد"):
        result = process(opt_in_engine, text)
        assert "NPO" not in result
        assert "DC" not in result
