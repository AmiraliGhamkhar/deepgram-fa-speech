#!/usr/bin/env python3
"""Census correction rules whose validation verdict depends on P0-7.

Run from the repository root. Before P0-7, `find_numeric_spans` used `\\b`
boundaries, so a digit glued to a Latin/Persian letter ("CO2") was invisible
at load time and silently dead at rewrite time. This probe lists every rule
whose normalized source contains a digit or a protected numeric span, together
with the loader's verdict under the current implementation. An unflagged rule
with a numeric span is a hard ConfigError; a flagged rule must keep the same
digit runs in source and target.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from medical_stt.processing.normalize import normalize  # noqa: E402
from medical_stt.processing.numbers import find_numeric_spans  # noqa: E402
from medical_stt.processing.terminology import parse_rules  # noqa: E402

PATH = Path(__file__).resolve().parents[2] / "data" / "corrections.yaml"


def main() -> int:
    raw = yaml.safe_load(PATH.read_text(encoding="utf-8"))["rules"]
    digit_rules = 0
    span_rules = 0
    verdicts: list[str] = []
    for index, rule in enumerate(raw, 1):
        source = normalize(str(rule.get("from", "")))
        target = str(rule.get("to", ""))
        spans = find_numeric_spans(source)
        if re.search(r"\d", source):
            digit_rules += 1
        if not spans:
            continue
        span_rules += 1
        flagged = bool(rule.get("numeric_in_source", False))
        source_digits = re.findall(r"\d+", source)
        target_digits = re.findall(r"\d+", target)
        if not flagged:
            verdict = "REJECT: numeric span requires numeric_in_source: true"
        elif not source_digits or source_digits != target_digits:
            verdict = "REJECT: digit runs differ"
        else:
            verdict = "OK: reviewed numeric opt-in"
        verdicts.append(verdict)
        print(
            f"rule {index}: {source!r} -> {target!r} "
            f"spans={[(s.start, s.end, s.text) for s in spans]} {verdict}"
        )

    # Whole-file validation: no census entry may be left in a rejecting state.
    parse_rules(raw)
    print(f"\ntotal rules: {len(raw)}, digit-bearing sources: {digit_rules}, "
          f"numeric-span sources: {span_rules}, loader: OK")
    bad = [v for v in verdicts if v.startswith("REJECT")]
    print(f"rejected by census: {len(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
