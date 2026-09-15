"""Configuration loading & validation tests (task sections 9 & 13)."""
from __future__ import annotations


import pytest

from medical_stt.config import (
    ConfigError,
    Settings,
    load_correction_rules,
    load_correction_rules_raw,
    load_keyterms,
    validate_settings,
)


def _valid_settings(**overrides) -> Settings:
    base = dict(
        model="nova-3",
        language="fa",
        sample_rate=16000,
        channels=1,
        block_duration=0.08,
        endpointing=400,
        utterance_end_ms=1200,
        inject_mode="paste",
        reconnect_delay=2.0,
        max_reconnect_attempts=0,
        reconnect_backoff_max=30.0,
        reconnect_jitter=0.5,
        api_key="fake-key-for-tests",
    )
    base.update(overrides)
    return Settings(**base)


def test_valid_settings_pass_validation():
    assert validate_settings(_valid_settings()) == []


def test_missing_api_key_is_reported():
    errors = validate_settings(_valid_settings(api_key=""))
    assert any("DEEPGRAM_API_KEY" in e for e in errors)


def test_invalid_sample_rate_rejected():
    errors = validate_settings(_valid_settings(sample_rate=12345))
    assert any("sample_rate" in e for e in errors)


def test_invalid_channels_rejected():
    errors = validate_settings(_valid_settings(channels=5))
    assert any("channels" in e for e in errors)


def test_invalid_block_duration_rejected():
    errors = validate_settings(_valid_settings(block_duration=5.0))
    assert any("block_duration" in e for e in errors)


def test_invalid_endpointing_rejected():
    errors = validate_settings(_valid_settings(endpointing=1))
    assert any("endpointing" in e for e in errors)


def test_invalid_inject_mode_rejected():
    errors = validate_settings(_valid_settings(inject_mode="voodoo"))
    assert any("inject_mode" in e for e in errors)


def test_negative_reconnect_delay_rejected():
    errors = validate_settings(_valid_settings(reconnect_delay=-1))
    assert any("reconnect_delay" in e for e in errors)


def test_backoff_max_less_than_delay_rejected():
    errors = validate_settings(_valid_settings(reconnect_delay=10, reconnect_backoff_max=5))
    assert any("reconnect_backoff_max" in e for e in errors)


def test_empty_model_rejected():
    errors = validate_settings(_valid_settings(model=""))
    assert any("model" in e for e in errors)


def test_empty_language_rejected():
    errors = validate_settings(_valid_settings(language=""))
    assert any("language" in e for e in errors)


def test_invalid_medical_confidence_threshold_rejected():
    errors = validate_settings(_valid_settings(medical_confidence_threshold=1.1))
    assert any("medical_confidence_threshold" in error for error in errors)


def test_new_settings_defaults_are_backward_compatible():
    settings = Settings()
    assert settings.specialty == "general"
    assert settings.medical_confidence_threshold == 0.65
    assert settings.use_asr_replacements is True


def test_settings_dict_like_access_backward_compatible():
    s = _valid_settings()
    assert s["model"] == "nova-3"
    assert s["sample_rate"] == 16000


def test_load_correction_rules_raw_from_real_file():
    raw = load_correction_rules_raw()
    assert len(raw) > 0
    assert all("from" in r and "to" in r for r in raw)


def test_load_correction_rules_flat_pairs():
    pairs = load_correction_rules()
    assert len(pairs) > 0
    assert all(isinstance(p, tuple) and len(p) == 2 for p in pairs)


def test_load_keyterms_from_real_file():
    terms = load_keyterms()
    assert "ICU" in terms


def test_malformed_yaml_raises_config_error(tmp_path, monkeypatch):
    bad_yaml = tmp_path / "corrections.yaml"
    bad_yaml.write_text("rules: [this is: not: valid: yaml", encoding="utf-8")

    import medical_stt.config as config_module

    monkeypatch.setattr(config_module, "_DATA_DIR", tmp_path)
    with pytest.raises(ConfigError):
        config_module.load_correction_rules_raw()


def test_corrections_yaml_without_rules_key_raises(tmp_path, monkeypatch):
    weird = tmp_path / "corrections.yaml"
    weird.write_text("not_rules: []\n", encoding="utf-8")

    import medical_stt.config as config_module

    monkeypatch.setattr(config_module, "_DATA_DIR", tmp_path)
    with pytest.raises(ConfigError):
        config_module.load_correction_rules_raw()


def test_empty_corrections_yaml_returns_empty_list(tmp_path, monkeypatch):
    empty = tmp_path / "corrections.yaml"
    empty.write_text("", encoding="utf-8")

    import medical_stt.config as config_module

    monkeypatch.setattr(config_module, "_DATA_DIR", tmp_path)
    assert config_module.load_correction_rules_raw() == []


def test_corrections_yaml_rules_not_a_list_raises(tmp_path, monkeypatch):
    bad = tmp_path / "corrections.yaml"
    bad.write_text("rules: \"not a list\"\n", encoding="utf-8")

    import medical_stt.config as config_module

    monkeypatch.setattr(config_module, "_DATA_DIR", tmp_path)
    with pytest.raises(ConfigError):
        config_module.load_correction_rules_raw()
