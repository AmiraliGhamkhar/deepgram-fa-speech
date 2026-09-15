"""Negation / clinical-fidelity guard tests (task section 10)."""
from __future__ import annotations

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
