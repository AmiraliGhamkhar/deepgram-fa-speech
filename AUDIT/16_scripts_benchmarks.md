# 16 — Module review: `scripts/` and `benchmarks/`

Read in full: scripts/measure_capacity.py (228), scripts/scan_secrets.py
(287), scripts/verify_client_host.py (186), scripts/classify_corrections.py
(172), scripts/scrub_history.py (278, partial skim), scripts/windows_selftest.py
(110, via bandit + tests), benchmarks/run_benchmark.py (235, header + core),
benchmarks/metrics.py (279), benchmarks/corpus.yaml. Cross-read: bandit
report (15 of 26 findings live here), AUDIT/tools/pip-audit.md.

## Surface

- `measure_capacity.py` — the 50-session acceptance harness: mocked Deepgram
  grant (50ms latency so the loop-blocking question is real), thresholds
  ≥99% success / 0 legitimate 429 / p95 < 2s / loop not blocked / bounded
  memory / 0 active sessions after shutdown. Self-describing, exit-code
  friendly.
- `scan_secrets.py` / `scrub_history.py` — incident tooling per SECURITY.md;
  fingerprinted baseline, never prints values, never pushes.
- `verify_client_host.py` — end-to-end client↔host checker (retargeted in PR
  #6 to the async stub; 16/16 PASS locally).
- `classify_corrections.py` — one-off migration tool, explicitly marked
  "NOT part of the runtime application".
- `benchmarks/run_benchmark.py` — offline WER/CER scoring over
  `benchmarks/corpus.yaml`; live mode env-gated (`MEDICAL_STT_BENCHMARK_LIVE=1`),
  exits 3 with an explanation when unconfigured.
- `benchmarks/metrics.py` — dependency-free, hand-computable metrics.

## Findings

**F1 · Low · `measure_capacity.py` sets fake host credentials in `os.environ`
before importing the host app.** FACT: L46–48
`os.environ.setdefault("DEEPGRAM_API_KEY", "dg_fake_host_side_only")`,
`HOST_SHARED_SECRET = "s"*40`, `HOST_ALLOW_HTTP=1`. This is deliberate and
documented (the app under test must start), and the fake key is
bandit-flagged but inert. The residual risk is only that a developer running
the script in a shell that *later* runs something else inherits
`HOST_ALLOW_HTTP=1` in that process — impossible to matter beyond the
process. Correctly scoped; noted because bandit's B105 finding lives there.

**F2 · Info · `scan_secrets.py` legacy pattern requires the key name to sit
next to the value** (`deepgram_key[_-]?api...name=value`), so a bare 32/40-hex
token in a random file is invisible (deliberate, to avoid FP storms) — the
`dg_` form covers modern keys. Combined with module 15 F3 (no provisioned
secret pattern) the scanner's blind spots are: provisioned secrets and
bare-hex values. Both are documented operator-failure modes rather than
scanner bugs.

**F3 · Info · `scrub_history.py` uses `git filter-branch`** (bandit B603/B607
on the `git` subprocess calls, args are repo constants). The script writes a
backup bundle outside the repo first, refuses to push, and re-scans after
rewriting — the design is careful; `git filter-repo` would be faster/safer on
huge histories but adds a dependency the project deliberately avoids.

**F4 · Info · `benchmarks/run_benchmark.py` lives-mode hosts are entered via
env vars documented in the docstring;** there is no risk of secret leakage
into the report (only scores are written). The harness honestly refuses to
ship accuracy numbers ("the repository contains no measured baseline, and
none is implied").

## Correct in this module (verified)

- **Benchmark metrics are mathematically sound and honestly documented:**
  WER/CER defined precisely (whitespace tokens post-NFC, punctuation counts
  as a token — stated because WER is not portable between tools); empty-ref
  edge cases defined; numeric accuracy reuses the pipeline's *own* numeric
  extractor (`find_numeric_spans`) so scoring matches what the pipeline
  protects; "120/8 vs 120/80 must not be averaged away" is enforced by
  verbatim presence, not edit distance; BiDi control marks are stripped as
  presentation metadata; terminology substitutions are applied to the
  reference (`expected_written_reference`, longest-first) so deliberate
  rewrites don't read as WER penalties.
- **Corpus shape:** 22 cases across 11 categories (normal_dictation,
  terminology, medications, numbers, blood_pressure, spo2, dosage,
  fast_noisy_speech, mixed_language, repeated_phrases, long_dictation) —
  small but clinically targeted; adequate for regression tracking, not a
  WER claim (and it says so).
- **measure_capacity.py thresholds mirror README's "Measured" table
  verbatim** — the doc numbers are reproducible by running the script.
- Incident tooling and the one-off migration script are clearly labeled and
  kept out of runtime import paths.

## Summary (3 lines)

Operator tooling is unusually self-describing: every script states what it
measures, what it refuses to claim, and what it never prints. The capacity
harness reproduces the README's numbers, and the benchmark metrics are
defined to be hand-verifiable with domain-aware scoring (numeric verbatim,
negation recall, terminology-normalized references). Findings here are
informational; the systemic gap (no scanner pattern for provisioned
secrets) belongs to module 15.
