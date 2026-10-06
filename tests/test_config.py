"""Configuration loading & validation tests (task sections 9 & 13)."""
from __future__ import annotations


import pytest

from medical_stt.config import (
    ConfigError,
    Settings,
    keyterm_parameter,
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
        host_url="https://stt.example.com",
        host_secret="shared-secret-for-tests-not-real",
    )
    base.update(overrides)
    return Settings(**base)


def test_valid_settings_pass_validation():
    assert validate_settings(_valid_settings()) == []


def test_missing_host_secret_is_reported():
    errors = validate_settings(_valid_settings(host_secret=""))
    assert any("shared secret" in e for e in errors)


def test_missing_host_url_is_reported():
    errors = validate_settings(_valid_settings(host_url=""))
    assert any("host_url" in e for e in errors)


def test_plain_http_host_is_rejected():
    errors = validate_settings(_valid_settings(host_url="http://stt.example.com"))
    assert any("https" in e for e in errors)


def test_http_is_allowed_for_localhost_development():
    assert validate_settings(_valid_settings(host_url="http://localhost:8443")) == []


def test_invalid_session_ttl_is_rejected():
    errors = validate_settings(_valid_settings(session_ttl_seconds=100_000))
    assert any("session_ttl_seconds" in e for e in errors)


def test_plaintext_secret_in_settings_yaml_is_rejected(tmp_path):
    from medical_stt import config as config_module

    settings_file = tmp_path / "settings.yaml"
    settings_file.write_text("host_url: https://x.example.com\nhost_secret: oops\n", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        config_module._reject_plaintext_secrets(
            config_module._load_yaml(settings_file), settings_file
        )
    assert "host_secret" in str(excinfo.value)


def test_api_key_in_settings_yaml_is_rejected(tmp_path):
    from medical_stt import config as config_module

    settings_file = tmp_path / "settings.yaml"
    settings_file.write_text("api_key: dg_abc123\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        config_module._reject_plaintext_secrets(
            config_module._load_yaml(settings_file), settings_file
        )


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


# -- model / language / keyterm compatibility ----------------------------


def test_nova_3_with_persian_is_valid():
    assert validate_settings(_valid_settings(model="nova-3", language="fa")) == []
    assert keyterm_parameter("nova-3") == "keyterm"


def test_persian_is_rejected_on_models_that_do_not_support_it():
    errors = validate_settings(_valid_settings(model="nova-2", language="fa"))
    assert any("nova-3" in e and "language" in e for e in errors), errors
    assert keyterm_parameter("nova-2") == "keywords"


def test_unknown_model_is_rejected():
    errors = validate_settings(_valid_settings(model="nova-9"))
    assert any("nova-9" in e and "not supported" in e for e in errors)


def test_invalid_language_is_rejected():
    for bad in ("fa_IR", "!!", "Farsi", "fa-"):
        errors = validate_settings(_valid_settings(language=bad))
        assert any("language" in e for e in errors), (bad, errors)


def test_legacy_models_keep_the_legacy_mechanism():
    # A language we do not make a support claim about is allowed on any
    # known model; the mechanism, not the model, is what changes.
    assert validate_settings(_valid_settings(model="nova-2", language="en")) == []
    assert keyterm_parameter("nova-2") == "keywords"
    assert keyterm_parameter("enhanced") == "keywords"
    assert keyterm_parameter("base") == "keywords"
    assert keyterm_parameter("unknown-model") == ""


def test_empty_keyterms_are_allowed():
    assert load_keyterms("general", max_count=0) == []
    assert validate_settings(_valid_settings()) == []


def test_keyterms_are_non_empty_and_deduplicated():
    terms = load_keyterms("cardiology")
    assert terms, "the cardiology keyterm file should not be empty"
    assert all(term.strip() for term in terms)
    assert len(terms) == len(set(terms))


def test_new_settings_defaults_are_backward_compatible():
    settings = Settings()
    assert settings.specialty == "general"
    assert settings.medical_confidence_threshold == 0.65
    assert settings.use_asr_replacements is True
    assert settings.session_ttl_seconds == 30


def test_settings_never_expose_a_deepgram_key():
    """The client must have no provider credential at all."""
    settings = _valid_settings()
    assert not hasattr(settings, "api_key")
    assert "api_key" not in settings.as_dict()
    # repr must not leak the shared secret either.
    assert settings.host_secret not in repr(settings)


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


# -- provider-side `replace` parameters (data/asr_replacements.yaml) -------


def _write_replacements(tmp_path, monkeypatch, body: str):
    import medical_stt.config as config_module

    (tmp_path / "asr_replacements.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setattr(config_module, "_DATA_DIR", tmp_path)
    return config_module


def test_real_asr_replacements_are_valid_and_lowercase():
    from medical_stt.config import load_asr_replacements

    replacements = load_asr_replacements()
    assert replacements, "the shipped replacement list should not be empty"
    for entry in replacements:
        source, _, target = entry.partition(":")
        assert source == source.lower()
        assert target
        assert not any(ch.isdigit() for ch in entry)


def test_asr_replacement_rejects_uppercase_source(tmp_path, monkeypatch):
    module = _write_replacements(
        tmp_path, monkeypatch, "replacements:\n  - from: 'MI'\n    to: 'myocardial'\n"
    )
    with pytest.raises(ConfigError, match="lowercase"):
        module.load_asr_replacements()


def test_asr_replacement_rejects_digits_and_colons(tmp_path, monkeypatch):
    module = _write_replacements(
        tmp_path, monkeypatch, "replacements:\n  - from: 'bp'\n    to: '120:80'\n"
    )
    with pytest.raises(ConfigError, match="digits"):
        module.load_asr_replacements()


def test_asr_replacement_rejects_conflicting_targets(tmp_path, monkeypatch):
    module = _write_replacements(
        tmp_path,
        monkeypatch,
        "replacements:\n  - from: 'x'\n    to: 'y'\n  - from: 'x'\n    to: 'z'\n",
    )
    with pytest.raises(ConfigError, match="conflict"):
        module.load_asr_replacements()


def test_asr_replacement_deduplicates_identical_entries(tmp_path, monkeypatch):
    module = _write_replacements(
        tmp_path,
        monkeypatch,
        "replacements:\n  - from: 'x'\n    to: 'y'\n  - from: 'x'\n    to: 'y'\n",
    )
    assert module.load_asr_replacements() == ["x:y"]


def test_asr_replacement_rejects_empty_side(tmp_path, monkeypatch):
    module = _write_replacements(
        tmp_path, monkeypatch, "replacements:\n  - from: 'x'\n    to: '   '\n"
    )
    with pytest.raises(ConfigError, match="non-empty"):
        module.load_asr_replacements()


def test_asr_replacement_rejects_latin_target_for_persian_source(tmp_path, monkeypatch):
    """A Persian spelling fix must not introduce an English term.

    The target flows through the *provider* verbatim, bypassing the local
    terminology engine's category/context/dangerous guards; an English term
    belongs in data/corrections.yaml instead (documented contract of
    load_asr_replacements and data/asr_replacements.yaml).
    """
    module = _write_replacements(
        tmp_path, monkeypatch, "replacements:\n  - from: '\u0622\u06cc \u0633\u06cc \u06cc\u0648'\n    to: 'ICU'\n"
    )
    with pytest.raises(ConfigError, match="Latin/English"):
        module.load_asr_replacements()


def test_asr_replacement_rejects_unit_target_for_persian_source(tmp_path, monkeypatch):
    """A unit target (e.g. 'mg') must be rejected for a Persian source."""
    module = _write_replacements(
        tmp_path, monkeypatch, "replacements:\n  - from: '\u0645\u06cc\u0644\u06cc \u06af\u0631\u0645'\n    to: 'mg'\n"
    )
    with pytest.raises(ConfigError, match="Latin/English"):
        module.load_asr_replacements()


def test_asr_replacement_allows_a_persian_spelling_fix(tmp_path, monkeypatch):
    module = _write_replacements(
        tmp_path, monkeypatch, "replacements:\n  - from: '\u0647\u0627\u06cc\u067e\u0631\u062a\u0645\u0634\u0646'\n    to: '\u0647\u0627\u06cc\u067e\u0631\u062a\u0646\u0634\u0646'\n"
    )
    assert module.load_asr_replacements() == ["\u0647\u0627\u06cc\u067e\u0631\u062a\u0645\u0634\u0646:\u0647\u0627\u06cc\u067e\u0631\u062a\u0646\u0634\u0646"]


def test_asr_replacement_ascii_placeholders_stay_allowed(tmp_path, monkeypatch):
    """Purely-ASCII pairs (no non-ASCII source) are not a Persian fix."""
    module = _write_replacements(
        tmp_path, monkeypatch, "replacements:\n  - from: 'x'\n    to: 'y'\n"
    )
    assert module.load_asr_replacements() == ["x:y"]
