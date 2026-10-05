# 17 — Inventory and review: `tests/` (coverage, gaps, harness quality)

Method: file inventory + per-file test counts (`grep -c "def test"`),
selected reads, live runs of the opt-in gating, grep for which units are
asserted directly. 37 test files, ~6,027 lines. Current totals (this
sandbox, default run): **454 passed, 4 skipped** (opt-in soak ×1,
live-load ×1, live×2). Exit 0.

## Inventory (by weight)

| File | # tests | What it proves |
|---|---|---|
| test_config.py | 41 | settings validation, env overrides, YAML rules, secret-in-config hard error |
| test_host_service.py | 38 | host core: auth, TTL clamping, rate limiting, registry, grant transport errors |
| test_streaming.py | 26 | Deepgram event parsing, connection lifecycle |
| test_normalize.py / test_injection.py | 19+19 | normalization; injection backends/delta |
| test_host_app.py | 15 | FastAPI surface: endpoints, headers, metrics, HTTPS middleware |
| test_secret_tooling.py | 15 | scan_secrets behavior on synthetic trees |
| test_host_concurrency.py | 12 | the 50-client proof suite (module 06 summary below) |
| test_session_concurrency.py | 11 | 50 concurrent LiveMedicalSTT sessions |
| test_benchmark.py | 16 | hand-computed WER/CER/numeric/negation values |
| conftest.py | — | strips `DEEPGRAM_API_KEY` from the env; sys.path setup |

Plus: audio queue, accumulator, FST, terminology, negation, numbers,
confidence, bidi, overlay, secret store, DPAPI (Windows-only job in CI),
host client, host docs (drift guards), data safety, edge paths,
formatting ownership, shutdown flow, windows selftest.

## The 50-client proof suite (what actually runs in CI)

`test_host_concurrency.py` covers, among its 12 tests: 50 simultaneous
requests all succeed with unique `session_id`s; token issuance does not
block the loop; p95 under target; 50 clients behind one NAT all allowed;
one abusive client throttled alone; failed auth limited per-IP; unknown id
cannot borrow another client's secret; client id mandatory once a registry
exists; no growth in host state across connect/disconnect cycles; limiter
state bounded under a flood of fabricated ids (`tracked_client_labels <= 50`,
directly asserting the metrics-module bound); oversized bodies refused.
`test_session_concurrency.py` proves the client side (isolation, no thread
leaks, no queue cross-talk, shutdown storms). Both are in CI's `host-test`
job on every push. The soak and live-load suites are opt-in and verified
skipped by default in this sandbox.

## Findings

**F1 · Low · `provision.py` coverage is functional, not CLI-shaped: the
flows with real failure modes are exactly the untested ones.** FACT: tests
exercise `generate_client_id`/`generate_secret`/`registry_line` as library
helpers (test_host_concurrency.py:64–68 builds registries from them), and
`ClientRegistry.from_file` is parsed directly (test_host_concurrency.py:69).
Not covered anywhere: the `_generate` dedup loop (module 14 F1's hang), the
`--client-id` path that stores the id verbatim (module 14 F3), append-mode
registry updates, `--count` capping, and `--secrets-dir` file creation. The
untested code is precisely where the two Medium findings live — coverage
follows library shape, not operator shape. Fix (suggestion only): one CLI
test invoking `main([...])` per flag combination would have caught both.

**F2 · Low · `metrics.py` has no direct unit file.** FACT: grep shows
`metrics` appears only through app-level assertions
(`app.state.metrics.tracked_client_labels <= 50`,
`test_host_app.py` `/metrics` endpoints) and indirect counting. Rendering
escaping (`_escape_label_value`), the `other`-overflow fold, `quantiles`
boundary behavior, `host_uptime_seconds`, and histogram `_sum`/`_count`
pairs are never asserted directly. All currently correct on inspection, but
the module that formats operator-facing text is the least directly tested
new module. Fix (suggestion only): a ~10-test `test_host_metrics.py`.

**F3 · Info · `/readyz` is state-dependent and untested at the endpoint
level.** `readyz` returns 503 until the grant client initializes; app tests
assert `/healthz` and `/metrics`, but no test flips grant-client state to see
both 200 and 503. Small.

**F4 · Info · Coverage measurement exists but has no threshold.** CI runs
`pytest --cov=medical_stt --cov-report=term-missing` and prints, but there
is no `fail_under`; drops would be silent. Deliberately minimal per repo
philosophy ("do not add unnecessary frameworks"); noted once.

**F5 · Info · Windows-only tests and CI are well-paired** (mutex, DPAPI,
SendInput + `windows_selftest.py` on a real runner with annotated failures)
— this is the correct pattern and worth keeping as the deployment surface
grows.

## Correct in this suite (verified)

- The suite proves claims, not implementation details: the README "Proving
  it" table maps 1:1 onto test files; capacity thresholds asserted in tests
  are the same numbers `measure_capacity.py` judges and README quotes.
- Opt-in discipline is real and re-verified here: soak/live/loadtest all
  skip cleanly with explanatory skip messages when their env vars are
  absent, so CI can never spend Deepgram credit; conftest additionally
  strips a developer's `DEEPGRAM_API_KEY` from the environment.
- Host tests stub the Deepgram grant at the async transport (no network,
  no key), then assert auth, limits, and 50-client concurrency genuinely —
  the concurrency tests would fail on real regressions (unique ids, NAT
  fairness, bounded limiter state), not on mocks matching mocks.
- No test depends on machine-local state (all `tmp_path`-based); the suite
  is hermetic and passes in this sandbox with exit 0.

## Summary (3 lines)

The test suite is a genuine proof suite: CI's host job re-proves the 50-client
claims on every push, opt-in tests cannot spend credit, and asserts target
behavior (NAT fairness, isolation, bounded state) rather than mocks. Gaps are
sharp and pointed: the provisioner CLI — where the two module-14 findings
live — has no CLI-level test, and the metrics module has no direct unit file.
A single test file could close both.
