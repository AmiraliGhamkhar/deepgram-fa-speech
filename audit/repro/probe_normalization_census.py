#!/usr/bin/env python3
"""Census non-normalized terminology sources/targets (P0-8).

Run from the repository root. The YAML is read as raw data, then every entry
is compared with its normalized form and with the parsed rule actually given
to the FST. This is a whole-file census, not a hand-written exception list.
Rule 776's Arabic-yeh source is verified through a synthetic opt-in engine.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from medical_stt.processing.normalize import normalize  # noqa: E402
from medical_stt.processing.terminology import (  # noqa: E402
    TerminologyEngine,
    parse_rules,
)

PATH = Path(__file__).resolve().parents[2] / "data" / "corrections.yaml"


def main() -> int:
    raw = yaml.safe_load(PATH.read_text(encoding="utf-8"))["rules"]
    raw_differences: list[str] = []
    for index, rule in enumerate(raw, 1):
        for side in ("from", "to"):
            value = str(rule.get(side, ""))
            canonical = normalize(value)
            if value != canonical:
                raw_differences.append(f"{index}:{side} {value!r} -> {canonical!r}")
    parsed = parse_rules(raw)
    parsed_differences = [
        f"{i}:{r.source!r}/{r.target!r}"
        for i, r in enumerate(parsed, 1)
        if r.source != normalize(r.source) or r.target != normalize(r.target)
    ]

    print(f"total rules: {len(raw)}, parsed rules: {len(parsed)}")
    print(f"raw non-normalized sides: {len(raw_differences)}")
    for line in raw_differences:
        print("  " + line)
    print(f"parsed non-normalized sides: {len(parsed_differences)}")
    for line in parsed_differences:
        print("  " + line)

    engine = TerminologyEngine(enable_context_dependent=True)
    engine.load([{
        "from": "پايدار",
        "to": "stable",
        "category": "context_dependent",
        "requires_context": True,
    }])
    fired = engine.apply("وضعیت پایدار است") == "وضعیت stable است"
    print(f"Arabic-yeh source fires on normalized Persian: {fired}")
    return 0 if not parsed_differences and fired else 1


if __name__ == "__main__":
    raise SystemExit(main())
