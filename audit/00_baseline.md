# 00 — Baseline (audit/00_baseline.md)

**Mode: report-only.** Nothing outside `audit/` was created, modified, or installed.
No commit, no push, no `git add`. No opt-in or credential environment variable was set.

## Ground truth

| Item | Value |
| --- | --- |
| `git rev-parse HEAD` | `83dc32c9d43cac62cd80c08642219de9d57db52a` |
| Branch in this session | `arena/6b6277d8-deepgram-fa-speech` (branched from `main` @ same SHA) |
| `git status --porcelain` (start and end) | empty — only untracked `audit/` exists |
| Tracked files | 134 |
| Total tracked LOC | 31,177 (Python 21,246) |
| Top-level LOC | tests 10,220 · data 5,108 · host 2,835 · medical_stt 2,181 · docs 1,873 · scripts 1,708 · README 978 · benchmarks 785 · CI 214 |
| Last 30 commits | `83dc32c Merge pull request #16 ...` (most recent; full log not reproduced here) |

Every finding is valid for that SHA only.

## Environment

`python -m venv audit/.venv` with the system Python 3.11.2 (the system interpreter was never used).

```
pip install -r requirements-dev.txt -r host/requirements-dev.txt   -> exit 0
```

Installed without error on Linux. `deepgram-sdk==7.9.0`, `pyahocorasick`, `python-bidi`,
`sounddevice`, `fastapi`, `httpx`, `aiohttp`(via a2wsgi), `pytest`, `pytest-cov`, `mypy`, `ruff`.
`sounddevice` installs on Linux but cannot open a device here (not exercised).
Windows-only modules (`_windows_backend.py`, `dpapi.py` Win32 path, named mutex) cannot run here.

## Baseline commands (exact, with exit codes)

| # | Command | Exit | Result |
| --- | --- | --- | --- |
| B1 | `pytest tests/ -q` | 0 | **813 passed, 4 skipped** in 39.5 s (`audit/raw/pytest.txt`) |
| B2 | `pytest tests/ -q -rs` (skip reasons) | 0 | 4 skips: 2 × live Deepgram (`tests/test_live_deepgram.py:43`), 1 × load test (`tests/test_live_load.py:61`), 1 × soak (`tests/test_soak.py:138`) — all environment-gated by design, none "could not import" (`audit/raw/pytest_skips.txt`) |
| B3 | `ruff check medical_stt tests scripts host` | 0 | "All checks passed!" |
| B4 | `mypy medical_stt host/core.py` | 0 | "Success: no issues found in 35 source files" |
| B5 | `python scripts/measure_capacity.py` | 0 | mocked 50-client harness: 50/50 ok, 0 × 429, p95 93.9 ms, RSS +0.27 MB, 0 in-flight after, 50 buckets for 50 clients → `RESULT: PASS` (`audit/raw/measure_capacity.txt`) |
| B6 | `python scripts/scan_secrets.py` | 0 | "clean: no credentials found in the working tree"; **note:** 1 baseline entry matched nothing (`audit/raw/scan_tree.txt`) |
| B7 | `python scripts/scan_secrets.py --history --strict` (read-only; R2 — output redirected, only line counts inspected) | 0 | "clean: no credentials found in the history" — 1 line of output, no value printed (`audit/raw/scan_hist.txt`) |
| B8 | `gitleaks detect --redact` | — | **not run** — not installable in this sandbox (no network access to its release host); CI runs `gitleaks/gitleaks-action@v2` |
| B9 | `pip-audit -r requirements.txt -r host/requirements.txt` | — | **not run** — `pip-audit` not installed and not fetchable from the allowed indices without installing outside `audit/`; CI job `dependency-audit` runs it |
| B10 | `bandit -r medical_stt host -ll` | — | **not run** — not installed |
| B11 | `pytest --cov=medical_stt --cov=host --cov-branch` | — | **not run** (unbounded runtime); CI runs `--cov=medical_stt --cov-report=term-missing` |

Tool output was used as a candidate generator only. No finding rests on a tool hit alone.

## Mutation testing (section 6 test-strength check)

`audit/scratch/` = `git archive HEAD | tar -x -C audit/scratch` (never the tracked tree).
`audit/repro/mutate.py <mutant>` applies one small edit to the scratch copy, runs
`pytest tests/ -q -x` there, restores the file, and appends a JSON record.

20 mutants were defined; 20 were executed. Records: `audit/raw/mutation_results.jsonl`.

| Mutant | Target | Verdict | Killed by |
| --- | --- | --- | --- |
| M1-fst-word-boundary | `processing/fst.py` boundary filter removed | KILLED | `tests/regression/test_medical_cases.py` |
| M2-fst-iter-long | `iter_long` → `iter` | **SURVIVED** | — (equivalent mutant: the defensive re-sort/re-resolve reproduces `iter_long`'s output; see REPORT > Non-issues) |
| M3-numbers-bp-pattern | blood-pressure/ratio pattern removed | KILLED | `tests/test_benchmark.py` |
| M4-negation-past-tense | `نداشت` removed from markers | KILLED | `tests/test_negation.py` |
| M5-terminology-active | `dangerous`/`unsafe` gate removed | KILLED | `tests/test_data_safety.py` |
| M6-registry-verify-always-true | constant-time secret compare removed | KILLED | `tests/test_audit_fixes.py` |
| M7-host-lockout-removed | failed-auth lockout disabled | KILLED | `tests/test_host_app.py` |
| M8-normalize-drop-zwnj | all ZWNJ deleted in `normalize()` | KILLED | `tests/regression/test_medical_cases.py` |
| M9-session-reset-removed | post-Stop `_reset_session_state()` removed | **SURVIVED** | — (F-005) |
| M10-retry-after-clamp-removed | `max_retry_after_seconds` 60 → 3600 | **SURVIVED** | — (F-002) |
| M11-opener-factory-wiring | `_NoRedirectHandler()` dropped from the shipping opener | **SURVIVED** | — (F-001) |
| M12-host-body-cap-removed | streaming body cap removed | KILLED | `tests/test_audit_fixes.py` |
| M13-session-starts-before-auth | counter incremented pre-auth | KILLED | `tests/test_audit_fixes.py` |
| M14-send-race-guard-removed | stop-race guard in `_send()` removed | KILLED | `tests/test_audit_fixes.py` |
| M15-retry-after-floor-removed | Retry-After floor against backoff removed | KILLED | `tests/test_audit_fixes.py` |
| M17-secret-filename-rule-removed | `*.secret` rule removed from the scanner | KILLED | `tests/test_audit_fixes.py` |
| M18-queue-recovery-threshold | degraded recovery 3 sends → 1 | KILLED | `tests/test_audit_fixes.py` |
| M19-metrics-token-encoding | UTF-8 encode removed from the metrics token compare | KILLED | `tests/test_audit_fixes.py` |
| M-README-hardcoded-figure | a new hard-coded capacity figure added to the README (scratch) | **SURVIVED** | — (F-006; `tests/test_host_docs.py` 14 passed) |

Four mutants survived; each is written up (F-001, F-002, F-005, F-006) with an assessment of
whether it is a real gap or an equivalent mutant (M2 is equivalent).

## Skipped / not runnable here (limitations, never successes)

* Any Windows-only path: DPAPI round trip, named mutex, `SendInput`, Win32 clipboard,
  UIPI/elevated targets, real target-app grapheme deletion — `platform: windows-only`,
  reviewed statically, confidence capped at Medium.
* Any real-provider path: Deepgram WebSocket, `/v1/auth/grant`, real concurrency caps.
* `scripts/build_windows.ps1` (R3: must not run), `windows_injection_check.py --interactive` (R3),
  `python -m host.provision` outside `audit/scratch/` (R3 — not needed; provision logic was
  exercised only through the repo's own tests).
* `MEDICAL_STT_SOAK` / `MEDICAL_STT_LOAD_TEST` / `MEDICAL_STT_LIVE_TEST` / `MEDICALSTT_HOST_URL` /
  `MEDICALSTT_HOST_SECRET` / `DEEPGRAM_API_KEY` were **never** set (R3).
* No request to Deepgram or any deployed host was made. The only network calls were to
  `127.0.0.1` (the F-001 repro's two loopback HTTP servers).
