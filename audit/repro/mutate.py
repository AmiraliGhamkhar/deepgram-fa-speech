#!/usr/bin/env python3
"""Mutation harness for the audit (section 6 test-strength check).

Copies nothing itself: it mutates files inside audit/scratch/ (a `git archive
HEAD` copy) only. Never touches the tracked tree.

Usage:
    python audit/repro/mutate.py <mutant-id>
Each mutant is a small, safety-relevant edit. The harness applies it, runs
`pytest tests/ -q -x` in the scratch tree, records KILLED/SURVIVED, and
restores the file from the pristine archive copy.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SCRATCH = ROOT / "audit" / "scratch"
VENV_PY = ROOT / "audit" / ".venv" / "bin" / "python"
OUT = ROOT / "audit" / "raw" / "mutation_results.jsonl"

MUTANTS = {
    "M1-fst-word-boundary": (
        "medical_stt/processing/fst.py",
        "        if require_word_boundaries:\n"
        "            matches = [m for m in matches if _is_whole_word(text, m[0], m[1])]\n",
        "",
    ),
    "M2-fst-iter-long": (
        "medical_stt/processing/fst.py",
        "for end, (source, target) in self._automaton.iter_long(text):",
        "for end, (source, target) in self._automaton.iter(text):",
    ),
    "M3-numbers-bp-pattern": (
        "medical_stt/processing/numbers.py",
        '    re.compile(rf"\\b{_NUM}\\s*[x×/]\\s*{_NUM}\\s*(?:mmHg|mm|cm|ml|cc)?\\b", re.IGNORECASE),\n',
        "",
    ),
    "M4-negation-past-tense": (
        "medical_stt/processing/negation.py",
        '    "نداشت",\n',
        "",
    ),
    "M5-terminology-active": (
        "medical_stt/processing/terminology.py",
        "        if rule.dangerous or rule.category in BLOCKED_CATEGORIES:\n            return False\n",
        "",
    ),
    "M6-registry-verify-always-true": (
        "host/core.py",
        "        if not hmac.compare_digest(presented, expected):\n            return None\n",
        "",
    ),
    "M7-host-lockout-removed": (
        "host/app.py",
        "            locked, lock_retry_after = auth_failure_limiter.is_locked(auth_key)\n",
        "            locked, lock_retry_after = False, 0\n",
    ),
    "M8-normalize-drop-zwnj": (
        "medical_stt/processing/normalize.py",
        "    text = _ZWNJ_DUPLICATED_RE.sub(ZWNJ, text)\n",
        "    text = text.replace(ZWNJ, \"\")\n",
    ),
    "M9-session-reset-removed": (
        "medical_stt/app.py",
        "            self._reset_session_state()\n            self.phase = \"idle\"\n",
        "            self.phase = \"idle\"\n",
    ),
    "M10-retry-after-clamp-removed": (
        "medical_stt/stt/reconnect.py",
        "    max_retry_after_seconds: float = 60.0",
        "    max_retry_after_seconds: float = 3600.0",
    ),

    "M11-opener-factory-wiring": (
        "medical_stt/host_client.py",
        "    urllib.request.HTTPSHandler(),\n    _NoRedirectHandler(),\n)",
        "    urllib.request.HTTPSHandler(),\n)",
    ),
    "M12-host-body-cap-removed": (
        "host/app.py",
        "                if len(raw) + len(chunk) > resolved.max_request_body_bytes:\n"
        "                    raise OverflowError(\"body cap\")\n",
        "",
    ),
    "M13-session-starts-before-auth": (
        "host/app.py",
        "        # -- abuse guard: a locked-out identity is refused without any work ---\n",
        "        metrics.increment(\"session_starts_total\")\n"
        "        # -- abuse guard: a locked-out identity is refused without any work ---\n",
    ),
    "M15-retry-after-floor-removed": (
        "medical_stt/stt/reconnect.py",
        "        honored = min(\n            max(float(retry_after), exponential_backoff_delay(policy, attempt)),\n            policy.max_retry_after_seconds,\n        )",
        "        honored = min(float(retry_after), policy.max_retry_after_seconds)",
    ),
    "M17-secret-filename-rule-removed": (
        "scripts/scan_secrets.py",
        'SECRET_FILENAME_SUFFIXES = (".secret",)',
        "SECRET_FILENAME_SUFFIXES = ()",
    ),
    "M18-queue-recovery-threshold": (
        "medical_stt/audio/queue.py",
        "            if self._consecutive_sends >= RECOVERED_AFTER_SENDS:",
        "            if self._consecutive_sends >= 1:",
    ),
    "M14-send-race-guard-removed": (
        "medical_stt/stt/deepgram_provider.py",
        "            if raced_stop:\n                raise ProviderError(ErrorCategory.SHUTDOWN, \"Audio send cancelled during stop\") from exc\n",
        "",
    ),
    "M19-metrics-token-encoding": (
        "host/app.py",
        "    return hmac.compare_digest(\n        presented.strip().encode(\"utf-8\"), expected.encode(\"utf-8\")\n    )",
        "    return hmac.compare_digest(presented.strip(), expected)",
    ),
}



def run(selection: str = "tests/") -> tuple[int, str]:
    proc = subprocess.run(
        [str(VENV_PY), "-m", "pytest", selection, "-q", "-x", "-p", "no:cacheprovider"],
        cwd=str(SCRATCH),
        capture_output=True,
        text=True,
        timeout=900,
    )
    tail = "\n".join(proc.stdout.strip().splitlines()[-6:])
    return proc.returncode, tail


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in MUTANTS:
        print(f"usage: {sys.argv[0]} <{'|'.join(MUTANTS)}>")
        return 2
    name = sys.argv[1]
    rel, old, new = MUTANTS[name]
    path = SCRATCH / rel
    original = path.read_text(encoding="utf-8")
    if old not in original:
        print(f"ERROR: anchor not found in {rel} for {name}")
        return 3
    path.write_text(original.replace(old, new, 1), encoding="utf-8")
    try:
        code, tail = run()
    finally:
        path.write_text(original, encoding="utf-8")
    verdict = "KILLED" if code != 0 else "SURVIVED"
    record = {"mutant": name, "file": rel, "verdict": verdict, "exit": code, "tail": tail}
    with OUT.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    print(f"{name}: {verdict} (pytest exit {code})")
    print(tail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
