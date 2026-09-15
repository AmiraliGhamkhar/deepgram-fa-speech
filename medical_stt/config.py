"""Load and validate settings and medical rules from environment + YAML.

Configuration is validated eagerly (see `validate_settings`) so invalid
values fail fast with a clear message instead of surfacing as a confusing
runtime error deep inside the audio or network stack.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml
from dotenv import load_dotenv

_PKG_DIR = Path(__file__).resolve().parent
_ROOT = _PKG_DIR.parent
_CONFIG_DIR = _ROOT / "config"
_DATA_DIR = _ROOT / "data"

_VALID_INJECT_MODES = frozenset({"paste", "type"})
_SUPPORTED_MODELS = frozenset({"nova-3", "nova-2", "nova", "enhanced", "base"})


def _valid_specialty_name(value: str) -> bool:
    return bool(value) and value.replace("_", "").replace("-", "").isalnum()


load_dotenv(_ROOT / ".env")
load_dotenv()


class ConfigError(ValueError):
    """Raised when settings.yaml / environment values fail validation."""


@dataclass
class Settings:
    model: str = "nova-3"
    language: str = "fa"
    sample_rate: int = 16000
    channels: int = 1
    block_duration: float = 0.08
    endpointing: int = 400
    utterance_end_ms: int = 1200
    inject_mode: str = "paste"
    paste_settle_seconds: float = 0.12
    restore_clipboard: bool = True
    overlay_enabled: bool = True
    reconnect_delay: float = 2.0
    max_reconnect_attempts: int = 0
    reconnect_backoff_max: float = 30.0
    reconnect_jitter: float = 0.5
    enable_context_dependent_terms: bool = False
    specialty: str = "general"
    medical_confidence_threshold: float = 0.65
    use_asr_replacements: bool = True
    api_key: str = field(default="", repr=False)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "language": self.language,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "block_duration": self.block_duration,
            "endpointing": self.endpointing,
            "utterance_end_ms": self.utterance_end_ms,
            "inject_mode": self.inject_mode,
            "paste_settle_seconds": self.paste_settle_seconds,
            "restore_clipboard": self.restore_clipboard,
            "overlay_enabled": self.overlay_enabled,
            "reconnect_delay": self.reconnect_delay,
            "max_reconnect_attempts": self.max_reconnect_attempts,
            "reconnect_backoff_max": self.reconnect_backoff_max,
            "reconnect_jitter": self.reconnect_jitter,
            "enable_context_dependent_terms": self.enable_context_dependent_terms,
            "specialty": self.specialty,
            "medical_confidence_threshold": self.medical_confidence_threshold,
            "use_asr_replacements": self.use_asr_replacements,
            "api_key": self.api_key,
        }

    def __getitem__(self, key: str) -> Any:
        # Dict-like access retained for backward compatibility with call
        # sites/tests written against the old plain-dict settings.
        return self.as_dict()[key]


def _load_yaml(path: Path) -> Any:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as f:
        try:
            return yaml.safe_load(f) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"Malformed YAML in {path}: {exc}") from exc


def validate_settings(settings: Settings) -> List[str]:
    """Return a list of validation error strings; empty list means valid."""
    errors: List[str] = []

    if settings.sample_rate not in (8000, 16000, 22050, 24000, 44100, 48000):
        errors.append(f"sample_rate must be a standard rate, got {settings.sample_rate}")
    if settings.channels not in (1, 2):
        errors.append(f"channels must be 1 or 2, got {settings.channels}")
    if not (0.01 <= settings.block_duration <= 1.0):
        errors.append(f"block_duration must be between 0.01 and 1.0 seconds, got {settings.block_duration}")
    if not (10 <= settings.endpointing <= 10000):
        errors.append(f"endpointing must be between 10 and 10000 ms, got {settings.endpointing}")
    if not (0 <= settings.utterance_end_ms <= 20000):
        errors.append(f"utterance_end_ms must be between 0 and 20000 ms, got {settings.utterance_end_ms}")
    if settings.inject_mode not in _VALID_INJECT_MODES:
        errors.append(f"inject_mode must be one of {sorted(_VALID_INJECT_MODES)}, got {settings.inject_mode!r}")
    if settings.reconnect_delay < 0:
        errors.append("reconnect_delay must be >= 0")
    if settings.max_reconnect_attempts < 0:
        errors.append("max_reconnect_attempts must be >= 0 (0 = unlimited)")
    if settings.reconnect_backoff_max < settings.reconnect_delay:
        errors.append("reconnect_backoff_max must be >= reconnect_delay")
    if not (0 <= settings.reconnect_jitter <= 1):
        errors.append("reconnect_jitter must be between 0 and 1 (fraction of delay)")
    if not settings.model:
        errors.append("model must not be empty")
    elif settings.model not in _SUPPORTED_MODELS:
        # Not fatal: Deepgram may add models we don't know about yet, but
        # warn loudly since this is the #1 cause of "silently wrong" runs.
        errors.append(
            f"model {settings.model!r} is not in the explicitly tested set "
            f"{sorted(_SUPPORTED_MODELS)}; proceeding is allowed but unverified"
        )
    if not settings.language:
        errors.append("language must not be empty")
    if not _valid_specialty_name(settings.specialty):
        errors.append("specialty must contain only letters, numbers, '_' or '-'")
    if not (0.0 <= settings.medical_confidence_threshold <= 1.0):
        errors.append("medical_confidence_threshold must be between 0 and 1")
    if not settings.api_key:
        errors.append("DEEPGRAM_API_KEY is not set (put it in .env or the environment)")

    return errors


def get_settings() -> Settings:
    cfg = _load_yaml(_CONFIG_DIR / "settings.yaml")
    if not isinstance(cfg, dict):
        raise ConfigError("config/settings.yaml must contain a mapping at the top level")

    return Settings(
        model=os.getenv("DEEPGRAM_MODEL", cfg.get("model", "nova-3")),
        language=os.getenv("DEEPGRAM_LANGUAGE", cfg.get("language", "fa")),
        sample_rate=int(cfg.get("sample_rate", 16000)),
        channels=int(cfg.get("channels", 1)),
        block_duration=float(cfg.get("block_duration", 0.08)),
        endpointing=int(cfg.get("endpointing", 400)),
        utterance_end_ms=int(cfg.get("utterance_end_ms", 1200)),
        inject_mode=cfg.get("inject_mode", "paste"),
        paste_settle_seconds=float(cfg.get("paste_settle_seconds", 0.12)),
        restore_clipboard=bool(cfg.get("restore_clipboard", True)),
        overlay_enabled=bool(cfg.get("overlay_enabled", True)),
        reconnect_delay=float(cfg.get("reconnect_delay", 2.0)),
        max_reconnect_attempts=int(cfg.get("max_reconnect_attempts", 0)),
        reconnect_backoff_max=float(cfg.get("reconnect_backoff_max", 30.0)),
        reconnect_jitter=float(cfg.get("reconnect_jitter", 0.5)),
        enable_context_dependent_terms=bool(cfg.get("enable_context_dependent_terms", False)),
        specialty=str(cfg.get("specialty", "general")),
        medical_confidence_threshold=float(cfg.get("medical_confidence_threshold", 0.65)),
        use_asr_replacements=bool(cfg.get("use_asr_replacements", True)),
        api_key=os.getenv("DEEPGRAM_API_KEY", ""),
    )


MAX_KEYTERMS = 100


def _terms_from_file(path: Path) -> List[str]:
    data = _load_yaml(path)
    terms = data.get("keyterms") if isinstance(data, dict) else data
    if terms is None:
        return []
    if not isinstance(terms, list):
        raise ConfigError(f"{path} must contain a 'keyterms' list")
    return [str(term).strip() for term in terms if term is not None and str(term).strip()]


def load_keyterms(specialty: str = "general", max_count: int = MAX_KEYTERMS) -> List[str]:
    """Load general plus selected specialty terms, preserving priority order."""
    if not _valid_specialty_name(specialty):
        raise ConfigError("invalid keyterm specialty name")
    if max_count < 0:
        raise ConfigError("maximum keyterm count must be non-negative")
    if max_count == 0:
        return []
    keyterm_dir = _DATA_DIR / "keyterms"
    if keyterm_dir.is_dir():
        paths = [keyterm_dir / "general.yaml"]
        if specialty != "general":
            selected = keyterm_dir / f"{specialty}.yaml"
            if not selected.is_file():
                raise ConfigError(f"unknown keyterm specialty: {specialty!r}")
            paths.append(selected)
    else:
        # Existing deployments with only the original file continue to work.
        paths = [_DATA_DIR / "keyterms.yaml"]

    result: List[str] = []
    seen = set()
    for path in paths:
        for term in _terms_from_file(path):
            if term not in seen:
                seen.add(term)
                result.append(term)
                if len(result) == max_count:
                    return result
    return result


def load_asr_replacements() -> List[str]:
    """Load safe Deepgram `from:to` replacement parameters."""
    data = _load_yaml(_DATA_DIR / "asr_replacements.yaml")
    replacements = data.get("replacements", []) if isinstance(data, dict) else []
    if not isinstance(replacements, list):
        raise ConfigError("data/asr_replacements.yaml 'replacements' must be a list")

    result: List[str] = []
    for item in replacements:
        if not isinstance(item, dict) or not item.get("from") or item.get("to") is None:
            continue
        source, target = str(item["from"]).strip(), str(item["to"]).strip()
        # Keep provider-side replacement away from clinical numbers; those
        # remain protected locally during all terminology operations.
        if any(ch.isdigit() for ch in source + target) or ":" in source + target:
            raise ConfigError("ASR replacements cannot contain digits or ':'")
        result.append(f"{source}:{target}")
    return result


def load_correction_rules_raw() -> List[Dict[str, Any]]:
    """Return the raw categorized rule dicts (see processing/terminology.py
    for the schema). Kept separate from the legacy pair-tuple loader so
    callers can access category metadata."""
    data = _load_yaml(_DATA_DIR / "corrections.yaml")
    if not isinstance(data, dict) or "rules" not in data:
        if data:
            raise ConfigError("data/corrections.yaml must contain a top-level 'rules' list")
        return []
    rules = data.get("rules")
    if rules is None:
        return []
    if not isinstance(rules, list):
        raise ConfigError("data/corrections.yaml 'rules' must be a list")
    return [r for r in rules if isinstance(r, dict)]


def load_correction_rules() -> List[Tuple[str, str]]:
    """Backward-compatible flat (source, target) pairs, ignoring category
    metadata. Prefer TerminologyEngine for anything safety-sensitive."""
    pairs: List[Tuple[str, str]] = []
    for item in load_correction_rules_raw():
        src = item.get("from") or item.get("source") or item.get("src")
        tgt = item.get("to") or item.get("target") or item.get("tgt")
        if src and tgt is not None:
            pairs.append((str(src), str(tgt)))
    return pairs
