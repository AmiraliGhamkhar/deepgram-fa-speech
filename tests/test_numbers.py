"""Numeric clinical-expression protection tests (task section 11)."""
from __future__ import annotations

import pytest

from medical_stt.processing.numbers import find_numeric_spans


@pytest.mark.parametrize(
    "text,expected_substring",
    [
        ("فشار خون 120/80 mmHg", "120/80 mmHg"),
        ("SpO2 98%", "98%"),
        ("دمای بدن 37.2 C", "37.2 C"),
        ("HbA1c 7.2%", "7.2%"),
        ("دوز 5 mg", "5 mg"),
        ("500 mg IV تجویز شد", "500 mg"),
        ("توده 2 cm", "2 cm"),
        ("اندازه 10 × 5 mm", "10 × 5 mm"),
    ],
)
def test_protects_expected_span(text, expected_substring):
    spans = find_numeric_spans(text)
    texts = [s.text for s in spans]
    assert any(expected_substring in t or t in expected_substring for t in texts), texts


def test_spans_are_non_overlapping_and_sorted():
    text = "BP 120/80 mmHg, HR 88 bpm, T 37.2 C"
    spans = find_numeric_spans(text)
    for a, b in zip(spans, spans[1:], strict=False):
        assert a.end <= b.start
    assert spans == sorted(spans, key=lambda s: s.start)


def test_dose_expression_with_route():
    spans = find_numeric_spans("500 mg IV")
    assert any(s.text == "500 mg IV" for s in spans)


def test_range_expression_protected():
    spans = find_numeric_spans("دوز 5-10 mg")
    assert any("5-10" in s.text for s in spans)


def test_bare_decimal_protected():
    spans = find_numeric_spans("مقدار 7.2 است")
    assert any(s.text == "7.2" for s in spans)


def test_no_numbers_returns_empty():
    assert find_numeric_spans("بدون هیچ عددی") == []


def test_date_like_pattern_protected():
    spans = find_numeric_spans("تاریخ 1403/06/12")
    assert any("1403" in s.text for s in spans)


def test_time_like_pattern_protected():
    spans = find_numeric_spans("ساعت 14:30 مراجعه کرد")
    assert any(s.text == "14:30" for s in spans)


# -- whole-span protection ------------------------------------------------
#
# find_numeric_spans exists so no terminology rule can match *part* of a
# clinical numeric expression. A date has two separators and is therefore more
# specific than a ratio or a range, but it used to be tried last: the range
# pattern claimed "2024-01" out of "2024-01-05" and the bare-number pattern
# claimed "05", leaving the second hyphen unprotected.


@pytest.mark.parametrize(
    "text",
    [
        "2024-01-05",
        "1403/06/12",
        "5/1/2024",
        "تاریخ 2024-01-05 مراجعه کرد",
        "تاریخ 1403/06/12 مراجعه کرد",
    ],
)
def test_a_date_is_protected_as_one_contiguous_span(text):
    digits_and_separators = [
        (i, ch) for i, ch in enumerate(text) if ch.isdigit() or ch in "-/:"
    ]
    if not digits_and_separators:
        pytest.skip("no date in this sample")
    first, last = digits_and_separators[0][0], digits_and_separators[-1][0]
    spans = find_numeric_spans(text)
    covering = [s for s in spans if s.start <= first and s.end > last]
    assert len(covering) == 1, f"date split across spans: {[(s.start, s.end, s.text) for s in spans]}"
    # Every separator inside the date must be inside the protected span, not
    # just its digits -- that is what stops a rule rewriting across the middle.
    span = covering[0]
    for i, ch in digits_and_separators:
        assert span.start <= i < span.end, f"{ch!r} at {i} unprotected in {span.text!r}"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("120/80 mmHg", "120/80 mmHg"),
        ("5-10 mg", "5-10 mg"),
        ("10x5", "10x5"),
        ("14:30", "14:30"),
        ("2 to 4 cm", "2 to 4 cm"),
    ],
)
def test_moving_dates_first_did_not_steal_ratios_ranges_or_times(text, expected):
    """A date needs two separators, so single-separator forms are untouched."""
    assert any(s.text == expected for s in find_numeric_spans(text))
