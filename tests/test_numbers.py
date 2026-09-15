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
    assert any(s.text.startswith("500 mg") for s in spans)


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
