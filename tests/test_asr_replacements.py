"""Provider-side replacements never own units or Latin medical abbreviations."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from medical_stt import config
from medical_stt.config import ConfigError


def _load_pair(tmp_path, monkeypatch, source: str, target: str) -> list[str]:
    (tmp_path / "asr_replacements.yaml").write_text(
        yaml.safe_dump({"replacements": [{"from": source, "to": target}]}, allow_unicode=True),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "_DATA_DIR", tmp_path)
    return config.load_asr_replacements()


@pytest.mark.parametrize("source,target", [
    ("mg", "mcg"),  # both ends are units, regardless of source script
    ("iv", "IV"),  # same clinical abbreviation in both cases
    ("placeholder", "MCG"),  # target inspected even for an ASCII source
    ("placeholder", "ICU"),  # Latin medical abbreviation in corrections.yaml
    ("µg", "placeholder"),
    ("placeholder", "μg"),  # Greek mu spelling of microgram
    ("placeholder", "%"),
])
def test_ascii_or_unit_bearing_pairs_are_rejected(tmp_path, monkeypatch, source, target):
    with pytest.raises(ConfigError, match="Latin/English|unit"):
        _load_pair(tmp_path, monkeypatch, source, target)


@pytest.mark.parametrize("source,target", [
    ("میلیگرم", "mg"),  # already rejected: non-ASCII source -> Latin unit
    ("d5w", "dextrose"),  # already rejected: digits
])
def test_existing_forbidden_pairs_stay_rejected(tmp_path, monkeypatch, source, target):
    with pytest.raises(ConfigError):
        _load_pair(tmp_path, monkeypatch, source, target)


@pytest.mark.parametrize("source,target", [("x", "y"), ("imagine", "region")])
def test_benign_ascii_pairs_are_not_rejected_as_substring_units(tmp_path, monkeypatch, source, target):
    assert _load_pair(tmp_path, monkeypatch, source, target) == [f"{source}:{target}"]


def test_shipped_persian_spelling_correction_still_loads():
    assert config.load_asr_replacements() == ["هایپرتمشن:هایپرتنشن"]


def _uppercase_latin_tokens_in_corrections() -> set[str]:
    root = Path(__file__).resolve().parent.parent
    rules = yaml.safe_load((root / "data" / "corrections.yaml").read_text(encoding="utf-8"))["rules"]
    tokens: set[str] = set()
    for rule in rules:
        for side in ("from", "to"):
            text = str(rule.get(side, "")) if isinstance(rule, dict) else ""
            for match in config._ASR_LATIN_TOKEN_RE.finditer(text):  # noqa: SLF001
                token = match.group()
                if token.isupper() and len(token) >= 2:
                    tokens.add(token.casefold())
    return tokens


def test_every_uppercase_latin_clinical_token_is_blocked_for_asr_replacements():
    tokens = _uppercase_latin_tokens_in_corrections()
    missing = tokens - config._ASR_BLOCKED_TOKENS  # noqa: SLF001
    assert not missing, f"unblocked clinical tokens: {sorted(missing)}"


@pytest.mark.parametrize("token", ["cbc", "tsh", "crp", "bmi", "cxr", "wbc"])
def test_census_derived_clinical_abbreviations_are_rejected(tmp_path, monkeypatch, token):
    with pytest.raises(ConfigError, match="Latin/English|unit"):
        _load_pair(tmp_path, monkeypatch, "placeholder", token.upper())
