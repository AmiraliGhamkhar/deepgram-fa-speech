"""Load settings and medical rules from environment + YAML files."""
from __future__ import annotations
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple
import yaml
from dotenv import load_dotenv

_PKG_DIR = Path(__file__).resolve().parent
_ROOT = _PKG_DIR.parent
_CONFIG_DIR = _ROOT / "config"
_DATA_DIR = _ROOT / "data"

load_dotenv(_ROOT / ".env")
load_dotenv()

def _load_yaml(path: Path) -> Any:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

def get_settings() -> Dict[str, Any]:
    cfg = _load_yaml(_CONFIG_DIR / "settings.yaml")
    return {
        "model": os.getenv("DEEPGRAM_MODEL", cfg.get("model", "nova-3")),
        "language": os.getenv("DEEPGRAM_LANGUAGE", cfg.get("language", "fa")),
        "sample_rate": int(cfg.get("sample_rate", 16000)),
        "channels": int(cfg.get("channels", 1)),
        "block_duration": float(cfg.get("block_duration", 0.08)),
        "endpointing": int(cfg.get("endpointing", 400)),
        "utterance_end_ms": int(cfg.get("utterance_end_ms", 1200)),
        "inject_mode": cfg.get("inject_mode", "paste"),
        "paste_settle_seconds": float(cfg.get("paste_settle_seconds", 0.12)),
        "restore_clipboard": bool(cfg.get("restore_clipboard", True)),
        "overlay_enabled": bool(cfg.get("overlay_enabled", True)),
        "reconnect_delay": float(cfg.get("reconnect_delay", 2.0)),
        "max_reconnect_attempts": int(cfg.get("max_reconnect_attempts", 0)),
        "api_key": os.getenv("DEEPGRAM_API_KEY", ""),
    }

def load_keyterms() -> List[str]:
    data = _load_yaml(_DATA_DIR / "keyterms.yaml")
    terms = data.get("keyterms") if isinstance(data, dict) else data
    return [str(t).strip() for t in (terms or []) if str(t).strip()]

def load_correction_rules() -> List[Tuple[str, str]]:
    data = _load_yaml(_DATA_DIR / "corrections.yaml")
    rules = data.get("rules") if isinstance(data, dict) else data
    pairs: List[Tuple[str, str]] = []
    for item in rules or []:
        if isinstance(item, dict):
            src = item.get("from") or item.get("source") or item.get("src")
            tgt = item.get("to") or item.get("target") or item.get("tgt")
            if src and tgt is not None:
                pairs.append((str(src), str(tgt)))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            pairs.append((str(item[0]), str(item[1])))
    return pairs