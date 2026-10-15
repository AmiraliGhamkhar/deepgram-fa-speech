"""Provider-side replacements never own units or Latin medical abbreviations."""
from __future__ import annotations

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
