# Audit of `medical_stt` + `host` — report only

Audited revision: `83dc32c9d43cac62cd80c08642219de9d57db52a`.
**No product code was changed by this audit** — everything here is evidence, analysis and
reproduction scripts. Fixes are a separate follow-up.

## Read this first

| File | What it is |
| --- | --- |
| `REPORT.md` | The audit: scope and rules (§1), baseline (§2), findings (§3), test-strength experiment and coverage (§4), regression table (§5), non-issues and risk register (§6), limitations (§7) |
| `findings.jsonl` | One JSON record per finding — `id`, `fingerprint`, `commit`, `severity`, `confidence`, `category`, `location[]`, `trigger`, `impact`, `reachability`, `platform`, `evidence{}`, `falsification`, `fix`, `effort`, `verifier` |
| `claims.csv` | The claims ledger: every documented promise turned into a testable statement (`id, claim, source, status, evidence`) |
| `coverage.csv` | Per-file review status — `read-full` vs `partial`/`skimmed`, plus everything deliberately not run |
| `raw/` | Tool output exactly as produced (pytest, capacity harness, secret scans, mutation verdicts, probe output) |
| `repro/` | Runnable probes. Each one is the evidence behind a finding or a cleared claim |
| `00_baseline.md`, `01_map.md` | Environment/baseline record, and the repository map with trust boundaries |

Findings with severity policy: F-01 **High**, F-02/F-05/F-08 **Medium**, F-03/F-04/F-06/F-07/F-09
**Low** (0 Critical). Nothing Critical or High rests on a static read alone.

## Reproducing

```bash
python -m venv audit/.venv
audit/.venv/bin/pip install -r requirements-dev.txt -r host/requirements-dev.txt

audit/.venv/bin/python audit/repro/probe_dead_rules.py          # F-02
audit/.venv/bin/python audit/repro/probe_dotenv_walkup.py       # F-08
audit/.venv/bin/python audit/repro/probe_benchmark_metrics.py   # benchmarks cleared
audit/.venv/bin/python audit/repro/F-001_redirect_leak.py       # F-01 (loopback only)

git archive HEAD | tar -x -C audit/scratch       # mutate.py needs this copy
audit/.venv/bin/python audit/repro/mutate.py M11-opener-factory-wiring
cat audit/raw/mutation_results.jsonl
```

`audit/.venv/` and `audit/scratch/` are git-ignored: the venv is a local toolchain, and `scratch/`
is a throwaway copy of the tree that the mutation harness mutates (it restores every file it
touches). Neither is part of the audit result.

## What was not run — do not read this audit as a clean bill of health

Every opt-in and live path was skipped on purpose, and the Windows-only code (DPAPI, `SendInput`,
Win32 clipboard, named mutex) was reviewed statically only. `gitleaks`, `pip-audit`, `bandit` and
`--cov` are not installed in the audit environment; CI runs them. No request reached Deepgram or any
deployed host, and no credential or opt-in variable was set. The full list, with the reason for each
gap and what it would take to close it, is in `REPORT.md` §7.
