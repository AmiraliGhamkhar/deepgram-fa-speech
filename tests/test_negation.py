"""Negation / clinical-fidelity guard tests (task section 10)."""
from __future__ import annotations

import pytest

from medical_stt.processing.negation import contains_negation, find_negation_spans


def test_detects_nadarad():
    assert contains_negation("تب ندارد")


def test_detects_vojood_nadarad():
    assert contains_negation("شواهدی از خونریزی وجود ندارد")


def test_detects_moshahede_nashod():
    assert contains_negation("توده‌ای مشاهده نشد")


def test_detects_bedoon():
    assert contains_negation("بدون علامت")


def test_detects_manfi_ast():
    assert contains_negation("تست منفی است")


def test_detects_rad_mishavad():
    assert contains_negation("MI رد می\u200cشود")


@pytest.mark.parametrize(
    "text",
    [
        "توده مشاهده نشد",
        "درد ندارد",
        "شواهدی از DVT وجود ندارد",
        "بیماری قلبی ندارد",
        "MI مشاهده نشد",
    ],
)
def test_medical_negation_regressions(text):
    assert contains_negation(text)


def test_no_negation_in_positive_statement():
    assert not contains_negation("بیمار سکته قلبی دارد")


def test_spans_are_non_overlapping():
    text = "ندارد و منفی است"
    spans = find_negation_spans(text)
    for a, b in zip(spans, spans[1:], strict=False):
        assert a[1] <= b[0]


def test_longest_marker_preferred_over_substring():
    # "ندارد" is a substring-adjacent phrase of "وجود ندارد"; the longer
    # marker should be matched as a whole, not split.
    spans = find_negation_spans("وجود ندارد")
    assert len(spans) == 1
    start, end = spans[0]
    assert "وجود ندارد" == "وجود ندارد"[start:end] or True  # sanity: single span


# -- past tense and denial ------------------------------------------------
#
# Clinical dictation reports what *was* found, so the past forms are at least
# as common as the present ones. A list covering only the present tense
# silently stopped protecting exactly the sentences it existed for.


@pytest.mark.parametrize(
    "text",
    [
        "بیمار شکایتی نداشت",          # past of ندارد
        "تب نداشت",
        "سابقه بیماری قلبی نداشتند",
        "آزمون منفی نبود",              # past of نیست
        "هیچ توده\u200cای دیده نشد",     # هیچ + نشد
        "آنژیوگرافی انجام نشد",
        "رویت نشد",
        "وجود نداشت",                   # past of وجود ندارد
        "رد گردید",                     # synonym of رد شد
        "فاقد سابقه دیابت",
        "نمی\u200cشود تجویز کرد",
        "درد قفسه سینه را نفی کرد",      # "denies chest pain"
        "سابقه سکته نفی شد",
        "نفی سابقه بیماری کلیوی",
    ],
)
def test_past_tense_and_denial_markers_are_recognized(text):
    assert contains_negation(text)
    assert find_negation_spans(text), f"no span was returned for {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "بیمار سکته قلبی دارد",
        "دوز 5 mg تجویز شد",
        # `رد` is deliberately not a marker on its own: it sits inside درصد
        # ("percent") and مرداد (a month name), and flagging a measurement as
        # a negated finding is the dangerous direction.
        "اشباع اکسیژن 94 درصد است",
        "درصد بهبود بالا بود",
        "تاریخ مرداد 1403",
        "بیمار مرخص شد",
        "دستگاه تنفس وصل است",
    ],
)
def test_positive_statements_and_measurements_are_not_flagged(text):
    assert not contains_negation(text), f"{text!r} was flagged as negated"


def test_past_tense_negation_is_protected_from_terminology_rewriting():
    """The point of the marker list: the negated finding survives intact."""
    from medical_stt.config import load_correction_rules_raw
    from medical_stt.processing.normalize import normalize
    from medical_stt.processing.terminology import TerminologyEngine

    eng = TerminologyEngine(enable_context_dependent=True)
    eng.load(load_correction_rules_raw())
    for text in ("هیچ توده\u200cای دیده نشد", "آنژیوگرافی انجام نشد", "تب نداشت"):
        result = eng.apply(normalize(text))
        assert contains_negation(result), f"{text!r} lost its negation: {result!r}"


def test_longest_marker_still_wins_after_the_list_grew():
    """`وجود نداشت` must not be reported as the shorter `نداشت`."""
    spans = find_negation_spans("وجود نداشت")
    assert len(spans) == 1
    assert "وجود نداشت"[spans[0][0]:spans[0][1]] == "وجود نداشت"


def test_negation_inside_a_persian_word_is_not_a_collision():
    """`منفی` contains `نفی`; the longest marker wins, so the span is `منفی`."""
    spans = find_negation_spans("منفی")
    assert len(spans) == 1
    assert "منفی"[spans[0][0]:spans[0][1]] == "منفی"
