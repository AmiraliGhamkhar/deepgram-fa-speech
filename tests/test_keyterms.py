import pytest

from medical_stt.config import ConfigError, load_asr_replacements, load_keyterms


def test_general_terms_load() -> None:
    terms = load_keyterms()
    assert "HbA1c" in terms
    assert "هایپرتمشن" in terms


def test_specialty_loads_after_general_and_deduplicates() -> None:
    terms = load_keyterms("ultrasound")
    assert "ICU" in terms
    assert "CRL" in terms
    assert terms.count("IUGR") == 1
    assert terms.index("ICU") < terms.index("CRL")


def test_maximum_keyterm_count_is_respected() -> None:
    assert len(load_keyterms("ultrasound", max_count=7)) == 7
    assert load_keyterms("general", max_count=0) == []


def test_unknown_specialty_fails_clearly() -> None:
    with pytest.raises(ConfigError):
        load_keyterms("not-a-specialty")


def test_asr_replacements_use_deepgram_parameter_shape() -> None:
    assert "هایپرتمشن:هایپرتنشن" in load_asr_replacements()
