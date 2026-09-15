from medical_stt.processing.confidence import find_low_confidence_medical_words
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
