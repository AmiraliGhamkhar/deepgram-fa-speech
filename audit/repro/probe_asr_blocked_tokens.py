"""Census: every uppercase Latin clinical token in corrections.yaml must be
blocked for provider-side ASR replacements (see medical_stt/config.py)."""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from medical_stt.config import _ASR_BLOCKED_TOKENS, _ASR_LATIN_TOKEN_RE

ROOT = Path(__file__).resolve().parents[2]
rules = yaml.safe_load((ROOT / "data/corrections.yaml").read_text(encoding="utf-8"))["rules"]
tokens = set()
for rule in rules:
    for side in ("from", "to"):
        text = str(rule.get(side, "")) if isinstance(rule, dict) else ""
        for match in _ASR_LATIN_TOKEN_RE.finditer(text):
            token = match.group()
            if token.isupper() and len(token) >= 2:
                tokens.add(token.casefold())
missing = sorted(tokens - _ASR_BLOCKED_TOKENS)
print(f"uppercase Latin tokens: {len(tokens)}, blocked: {len(tokens) - len(missing)}, missing: {len(missing)}")
if missing:
    print("missing:", ", ".join(missing))
raise SystemExit(1 if missing else 0)
