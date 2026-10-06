"""Normalization regression tests (task sections 4 & 9)."""
from __future__ import annotations

from medical_stt.processing.normalize import normalize


def test_arabic_letterforms_canonicalized_to_persian():
    assert normalize("كيست") == "کیست"


def test_persian_digits_converted_to_ascii():
    assert normalize("۱۲۰") == "120"


def test_arabic_indic_digits_converted_to_ascii():
    assert normalize("١٢٠") == "120"


def test_whitespace_collapsed():
    assert normalize("سلام    دنیا") == "سلام دنیا"


def test_leading_trailing_whitespace_trimmed():
    assert normalize("   سلام   ") == "سلام"


def test_zwnj_preserved_in_compound_word():
    text = "می\u200cخواهم"
    assert "\u200c" in normalize(text)
    assert normalize(text) == text


def test_stray_zwnj_next_to_space_removed():
    text = "کلمه \u200c دیگر"
    result = normalize(text)
    assert "\u200c" not in result


def test_duplicated_zwnj_collapsed():
    text = "خانه\u200c\u200cی"
    assert normalize(text) == "خانه\u200cی"


def test_zwj_always_removed():
    text = "ک\u200dلمه"
    assert "\u200d" not in normalize(text)


def test_numeric_value_unchanged_blood_pressure():
    assert "120/80" in normalize("فشار خون 120/80 mmHg")


def test_numeric_value_unchanged_percentage():
    assert normalize("7.2%") == "7.2%"


def test_numeric_value_unchanged_decimal():
    assert normalize("HbA1c 7.2") == "HbA1c 7.2"


def test_arabic_decimal_separator_converted_without_changing_value():
    # Arabic decimal separator glyph -> ASCII '.', value unchanged.
    assert normalize("7\u066b2") == "7.2"


def test_arabic_percent_sign_converted():
    assert normalize("98\u066a") == "98%"


def test_mixed_persian_latin_text_preserved():
    text = "بیمار MI در ECG دارد"
    result = normalize(text)
    assert "MI" in result and "ECG" in result
    assert "بیمار" in result


def test_idempotent():
    text = "بیمار   با   فشار خون ۱۲۰/۸۰ mmHg مراجعه کرد"
    once = normalize(text)
    twice = normalize(once)
    assert once == twice


def test_empty_string():
    assert normalize("") == ""


def test_punctuation_spacing_persian_comma():
    result = normalize("سردرد،تهوع")
    assert result == "سردرد، تهوع"


def test_punctuation_spacing_does_not_touch_numeric_colon():
    # 'time' style colon must not gain a space (numeric-adjacent).
    result = normalize("ساعت 14:30 مراجعه کرد")
    assert "14:30" in result


# -- spaced-out punctuation inside numeric expressions --------------------


def test_spaced_time_colon_is_left_alone():
    """ASR often spaces around punctuation; a time must survive it intact.

    The old immediate-neighbour check saw a space on both sides of ':' and
    decided it was prose, collapsing "10 : 00" to "10: 00" -- neither a valid
    time nor valid prose, and a numeric value corrupted on the way into a
    clinical record.
    """
    assert normalize("ساعت 10 : 00 مراجعه کرد") == "ساعت 10 : 00 مراجعه کرد"


def test_spaced_range_hyphen_is_left_alone():
    assert normalize("دوز 5 - 10 mg") == "دوز 5 - 10 mg"


def test_prose_colon_before_a_number_is_still_tightened():
    """Only a digit on BOTH sides counts as a numeric expression.

    One-sided adjacency through a space is ordinary prose, and the
    typographic fix there is wanted.
    """
    assert normalize("تعداد : 3 عدد") == "تعداد: 3 عدد"


def test_prose_colon_between_words_is_still_tightened():
    assert normalize("یادداشت : مهم") == "یادداشت: مهم"


def test_punctuation_tables_have_no_duplicate_entries():
    from medical_stt.processing.normalize import _PUNCT_NO_SPACE_BEFORE, _PUNCT_SPACE_AFTER

    for table in (_PUNCT_NO_SPACE_BEFORE, _PUNCT_SPACE_AFTER):
        assert len(table) == len(set(table)), f"duplicated marks in {table!r}"
