from medical_stt.processing.confidence import (
    find_low_confidence_medical_words,
    low_confidence_protected_ranges,
)
from medical_stt.processing.normalize import normalize
from medical_stt.stt.base import WordInfo


def test_low_confidence_number_is_marked_without_repair() -> None:
    word = WordInfo("500", 0.2, 0.5, 0.4)
    warning = find_low_confidence_medical_words((word,), 0.65)
    assert warning is not None
    assert warning.uncertain_words[0].word.text == "500"
    assert warning.uncertain_words[0].category == "number"


def test_low_confidence_medical_abbreviation_is_marked() -> None:
    warning = find_low_confidence_medical_words((WordInfo("DVT", 0, 1, 0.5),), 0.65)
    assert warning is not None


def test_ordinary_or_missing_confidence_word_is_not_marked() -> None:
    words = (WordInfo("بیمار", 0, 1, 0.2), WordInfo("MI", 1, 2, None))
    assert find_low_confidence_medical_words(words, 0.65) is None


def test_high_confidence_medical_word_is_not_marked() -> None:
    assert find_low_confidence_medical_words((WordInfo("HbA1c", 0, 1, 0.9),), 0.65) is None


# -- P0-10: low-confidence spans are located by word index/timing -----------

def test_low_confidence_spans_follow_word_indices_and_timings() -> None:
    text = normalize("500 آی سی یو")
    words = (
        WordInfo("آی", 1.0, 1.2, 0.9),
        WordInfo("500", 0.0, 0.4, 0.3),
        WordInfo("سی", 1.2, 1.4, 0.9),
        WordInfo("یو", 1.4, 1.6, 0.9),
    )
    warning = find_low_confidence_medical_words(words, 0.65)
    assert warning is not None
    assert warning.uncertain_words[0].index == 1
    assert low_confidence_protected_ranges(text, words, warning) == [(0, 3)]


def test_low_confidence_spans_distinguish_repeated_words_by_timing() -> None:
    text = "DVT سپس DVT"
    words = (
        WordInfo("DVT", 0.0, 0.3, 0.9),
        WordInfo("سپس", 0.3, 0.6, 0.9),
        WordInfo("DVT", 0.6, 0.9, 0.3),
    )
    warning = find_low_confidence_medical_words(words, 0.65)
    assert warning is not None
    start = text.rindex("DVT")
    assert low_confidence_protected_ranges(text, words, warning) == [(start, start + 3)]


def test_low_confidence_span_includes_normalized_punctuation() -> None:
    text = normalize("HbA1c، 7.2")
    words = (WordInfo("HbA1c،", 0.0, 0.5, 0.3), WordInfo("7.2", 0.5, 1.0, 0.9))
    warning = find_low_confidence_medical_words(words, 0.65)
    assert warning is not None
    start = text.index("HbA1c")
    assert low_confidence_protected_ranges(text, words, warning) == [(start, start + len("HbA1c،"))]


def test_no_warning_needs_no_protected_ranges() -> None:
    words = (WordInfo("بیمار", 0.0, 1.0, 0.9),)
    assert low_confidence_protected_ranges("بیمار", words, None) == []


def test_unmappable_low_confidence_token_requires_conservative_fallback() -> None:
    words = (WordInfo("DVT", 0.0, 1.0, 0.3),)
    warning = find_low_confidence_medical_words(words, 0.65)
    assert warning is not None
    assert low_confidence_protected_ranges(normalize("بیمار پایدار است"), words, warning) is None
    mismatched = (WordInfo("ECG", 0.0, 1.0, 0.3),)
    assert low_confidence_protected_ranges("DVT", mismatched, warning) is None


def test_reset_segment_timings_use_tuple_alignment() -> None:
    text = normalize("500 آی سی یو")
    words = (
        WordInfo("500", 1.0, 1.4, 0.3),
        WordInfo("آی", 0.0, 0.2, 0.9),  # timing reset in the next segment
        WordInfo("سی", 0.2, 0.4, 0.9),
        WordInfo("یو", 0.4, 0.6, 0.9),
    )
    warning = find_low_confidence_medical_words(words, 0.65)
    assert warning is not None
    assert low_confidence_protected_ranges(text, words, warning) == [(0, 3)]
