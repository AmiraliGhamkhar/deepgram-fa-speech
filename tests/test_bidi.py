"""Mixed RTL/LTR handling tests (task section 5)."""
from __future__ import annotations

import pytest

from medical_stt.processing.bidi import (
    LRM,
    RLM,
    base_direction,
    build_transcript_view,
    has_ltr_content,
    has_rtl_content,
    is_mixed_direction,
    to_injected,
)


def test_base_direction_rtl_when_persian_leads():
    assert base_direction("فشار خون 120/80 mmHg") == "rtl"


def test_base_direction_uses_persian_majority_when_latin_leads():
    assert base_direction("MI در ECG مشاهده شد") == "rtl"


def test_base_direction_ltr_for_pure_numbers():
    assert base_direction("120/80") == "ltr"


def test_is_mixed_direction_detects_combination():
    assert is_mixed_direction("HbA1c برابر 7.2 درصد است")
    assert is_mixed_direction("TKA سمت راست")


def test_pure_persian_is_not_mixed():
    assert not is_mixed_direction("بیمار سابقه سکته قلبی دارد")


def test_pure_english_is_not_mixed():
    assert not is_mixed_direction("patient has stable vitals")


def test_logical_text_is_never_reordered():
    """The canonical/logical representation must be byte-identical to the
    input; only `display`/`injected` may add non-content marks."""
    for text in [
        "فشار خون 120/80 mmHg",
        "MI در ECG مشاهده شد",
        "HbA1c برابر 7.2 درصد است",
        "TKA سمت راست",
    ]:
        view = build_transcript_view(text)
        assert view.logical == text


def test_injected_preserves_logical_order_content():
    """to_injected must not reorder characters -- only prefix at most one
    directional mark -- so numbers/units/latin abbreviations inside a
    mixed sentence keep their original left-to-right sub-order."""
    text = "فشار خون 120/80 mmHg"
    injected = to_injected(text)
    # Strip an optional leading mark, then the remaining content must be
    # exactly the original text (logical order preserved).
    stripped = injected.lstrip("\u200e\u200f")
    assert stripped == text
    assert "120/80" in injected
    assert "mmHg" in injected


def test_injected_does_not_wrap_whole_string_in_embedding():
    text = "فشار خون 120/80 mmHg"
    injected = to_injected(text)
    assert "\u202b" not in injected  # RLE
    assert "\u202c" not in injected  # PDF


def test_injected_adds_single_leading_mark_only_once():
    text = "فشار خون 120/80 mmHg"
    injected = to_injected(text)
    marks = sum(1 for ch in injected if ch in "\u200e\u200f")
    assert marks == 1


def test_injected_idempotent_on_already_marked_text():
    text = "\u200fفشار خون"
    assert to_injected(text) == text


def test_has_rtl_and_ltr_content_flags():
    text = "MI در ECG مشاهده شد"
    assert has_rtl_content(text)
    assert has_ltr_content(text)


def test_empty_text():
    view = build_transcript_view("")
    assert view.logical == ""
    assert view.display == ""
    assert view.injected == ""


# -- P0-9: majority-strong paragraph direction ------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        # Persian-dominant medical dictation often begins with a Latin drug
        # name, abbreviation or lab marker. First-strong would flip the EMR
        # paragraph to LTR and move trailing punctuation to the wrong side.
        ("MI در ECG مشاهده شد", "rtl"),
        ("TKA سمت راست انجام شد", "rtl"),
        ("Aspirin برای بیمار تجویز شد", "rtl"),
        ("HbA1c برابر 7.2 درصد است", "rtl"),
        # A Persian lead does not force RTL when the sentence is Latin-dominant.
        ("بیمار CT scan normal", "ltr"),
        ("فشار خون 120/80 mmHg", "rtl"),
        ("بیمار سابقه سکته قلبی دارد", "rtl"),
        ("patient has stable vitals", "ltr"),
        # Pure-neutral text keeps the existing LTR default.
        ("120/80", "ltr"),
        ("98%", "ltr"),
        ("...", "ltr"),
        # Ties fall back to the old first-strong rule.
        ("AB حس", "ltr"),
        ("حس AB", "rtl"),
    ],
)
def test_base_direction_counts_strong_characters(text, expected):
    assert base_direction(text) == expected


@pytest.mark.parametrize(
    "text,expected_mark",
    [
        ("MI در ECG مشاهده شد", RLM),
        ("TKA سمت راست انجام شد", RLM),
        ("HbA1c برابر 7.2 درصد است", RLM),
        ("بیمار CT scan normal", LRM),
        ("patient has stable vitals", LRM),
    ],
)
def test_injected_mark_follows_majority_without_embeddings(text, expected_mark):
    injected = to_injected(text)
    assert injected.startswith(expected_mark)
    assert injected.count(LRM) + injected.count(RLM) == 1
    assert "\u202b" not in injected and "\u202c" not in injected  # RLE/PDF
    assert injected.lstrip(LRM + RLM) == text
    assert to_injected(injected) == injected


@pytest.mark.parametrize("text", ["120/80", "98%", "...", "7.2"])
def test_pure_neutral_text_gets_no_directional_mark(text):
    """Numbers-only dictation has no strong character, so a leading LRM
    would be a stray invisible mark carried into the EMR. `base_direction`
    still reports the LTR default; only the mark is withheld."""
    assert base_direction(text) == "ltr"
    assert to_injected(text) == text
