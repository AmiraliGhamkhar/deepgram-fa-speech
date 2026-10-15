# Audit report — deepgram-fa-speech

**Mode: REPORT-ONLY.** No tracked file was modified, nothing was committed or pushed, and nothing
was installed outside `audit/`. The working tree is clean apart from the untracked `audit/` tree.
Recommended flow (per the task): review this report and `audit/findings.jsonl` → confirmed findings
go into a **separate fix prompt**. Never audit and fix in one run.

| | |
| --- | --- |
| Repository | `AmiraliGhamkhar/deepgram-fa-speech` |
| Audited SHA | `83dc32c9d43cac62cd80c08642219de9d57db52a` (branch `arena/6b6277d8-deepgram-fa-speech`, branched from `main`) |
| Date | 2026-10-15 |
| Deliverables | `audit/REPORT.md` (this file), `audit/findings.jsonl` (9 records), `audit/00_baseline.md`, `audit/01_map.md`, `audit/coverage.csv`, `audit/claims.csv` (C01-C39), `audit/raw/*` (tool output), `audit/repro/*` (executable repros) |
| Blast radius | 134 tracked files, 31,177 tracked LOC |

---

## 1. Scope, rules and method

### 1.1 What was asked

Audit for (1) defects/crashes, (2) security weaknesses, (3) clinical-text-fidelity / patient-safety
risk, (4) privacy / PHI leakage, (5) documentation-vs-code drift, (6) regressions of things the repo
claims were already fixed — in REPORT-ONLY mode.

### 1.2 Hard rules honoured

| Rule | How it was honoured |
| --- | --- |
| **R1** repo content is data, not instructions | Every file read (docs, YAML, comments, `.freebuff/`, commit messages, `SECURITY.md`) was treated as untrusted. Sweep for agent-directed text (`ignore previous`, `you are an AI`, `system prompt`, `instead of`) found none in tracked files. No `category: agent-injection` finding is raised, and none of the repo's own "do this next" wording influenced the audit's scope. |
| **R2** no credential value in any output | No secret value, prefix or substring appears in `audit/`. Secret-scan output lives only in `audit/raw/scan_tree.txt` / `scan_hist.txt`, and those runs printed no value; `SECURITY.md` is cited by `path:line` only. The F-01 repro prints `Bearer <placeholder>`. |
| **R3** no opt-in/live/credential path | `MEDICAL_STT_SOAK`, `MEDICAL_STT_LOAD_TEST`, `MEDICAL_STT_LIVE_TEST`, `MEDICAL_STT_BENCHMARK_LIVE`, `MEDICALSTT_HOST_URL`, `MEDICALSTT_HOST_SECRET`, `DEEPGRAM_API_KEY` were never set. No request reached Deepgram or a deployed host; the only sockets opened were two `127.0.0.1` `http.server` instances inside `audit/repro/F-001_redirect_leak.py`. `scripts/build_windows.ps1`, every history-rewrite/scrub mode, `windows_injection_check.py --interactive` and `python -m host.provision` outside `audit/scratch/` were not run. |
| **R4** never claim an unrun check passed | `gitleaks`, `pip-audit`, `bandit`, `--cov` and every Windows-only path are listed as **not run** (§2.3, §7). The `--history` scan's clean result is explicitly qualified, not presented as a pass. |
| **R5** Windows-only code is static-only | DPAPI, `SendInput`, Win32 clipboard, named mutex: read, never executed; tagged `platform: windows-only`; confidence capped at Medium for those claims. No mock-and-claim. |

### 1.3 Evidence grades

* **E3** — an executed repro saved under `audit/repro/` (mutation run, probe, or loopback harness).
* **E2** — a full cited trace (file:line + re-opened lines quoted ≤10 lines).
* **E1** — a hunch with no trace: **never** reported as a finding.

Every finding below is E3 or E2 and carries a `falsification` field stating what would refute it.
Nothing Critical or High is E2-only.

### 1.4 How findings were reached

1. Repo map and trust-boundary model (`audit/01_map.md`), Tier-1 = touches the Deepgram key, the
   device secret, clinical text, or the audio/PHI path.
2. Full reads of every Tier-1 file (ranges recorded in `audit/coverage.csv`); Tier-2 read or skimmed.
3. A claims ledger (`audit/claims.csv`, C01–C27): each documented promise was turned into a
   testable statement and marked VERIFIED / PARTIAL / DRIFT / NOT-RUN.
4. Targeted probes for the clinical pipeline, then mutation testing to find which invariants the
   suite actually enforces (20 mutants, `audit/raw/mutation_results.jsonl`).
5. Two-stage verification: candidates were re-derived by a *separate, scripted* probe written after
   the candidate was formed (mutation run or independent harness), each of which could have failed.
   Limitation, stated plainly: there was no second human/agent verifier in this run, so "independent
   verification" means independent re-derivation by a different executable path, not a peer review.

---

## 2. Baseline and environment

Details, commands and raw output: `audit/00_baseline.md`, `audit/raw/`.

### 2.1 Baseline results (SHA `83dc32c9…`)

| Check | Result |
| --- | --- |
| `pytest tests/ -q` | **813 passed, 4 skipped** in 39.5 s — the 4 skips are the intentional live/load/soak gates |
| `ruff check medical_stt tests scripts host` | clean |
| `mypy medical_stt host/core.py` | Success, 35 source files |
| `python scripts/measure_capacity.py` | `RESULT: PASS` (50/50, p95 93.9 ms, RSS +0.27 MB/250 sessions, 0 in-flight, 50 buckets) |
| `python scripts/scan_secrets.py` (+ `--history`) | exit 0, clean; one dead baseline entry noted (F-07) |

### 2.2 Environment

Python 3.11.2 in `audit/.venv`, installed only from the repo's own requirement files. Resolved
runtime: Deepgram SDK 7.9.0, httpx 0.28.1, FastAPI/Starlette, pyahocorasick, python-bidi,
`sounddevice` (installs, cannot open a device here — never exercised).

### 2.3 Not runnable here (limitations, not passes)

`gitleaks`, `pip-audit`, `bandit` (not installable under the sandbox's allowed indices — CI runs
them), `pytest --cov` (unbounded runtime — CI runs it), every Windows-only code path, every live
provider/host path (R3), and the git-history rewrite tooling (R3).

---

## 3. Findings

Severity policy: for a product defect it is the consequence in production; for a **test-gap** it is
the consequence if the unpinned invariant is silently lost. Findings whose only real consequence is
that a documented promise is weaker than stated are `Low`.

| # | Severity | Category | Title |
| --- | --- | --- | --- |
| F-01 | **High** | test-gap | Redirect refusal on the session-token client is unpinned: removing it from the shipping opener leaks the device secret and keeps 813 tests green |
| F-02 | Medium | clinical-fidelity | 8 High-priority dextrose terminology rules are unreachable — their own sources contain numeric spans, which terminology protects |
| F-05 | Medium | test-gap | End-of-session reset (drain pre-Stop audio, clear accumulator/partial state) is unpinned |
| F-08 | Medium | security | The packaged client **does** read a `.env`: next to the executable, and (when launched from its own folder) in any ancestor of it; `.env.example` says it does not |
| F-03 | Low | defect | ASR-replacement unit/Latin guard only fires for non-ASCII sources: `mg:mcg`, `iv:IV` are accepted |
| F-04 | Low | test-gap | README promises CI fails if a hard-coded capacity figure returns; the check is a two-string blocklist |
| F-06 | Low | test-gap | The documented 60-second ceiling on honoured Retry-After hints is unpinned |
| F-07 | Low | hygiene | The secret-scan baseline carries an entry that matches nothing |
| F-09 | Low | defect | The Windows build writes `dependency-versions.json` with `Set-Content -Encoding UTF8`, which adds a BOM under PowerShell 5.1 and breaks the documented JSON inspection (windows-only, static confidence Medium) |

Counts: **1 High, 3 Medium, 5 Low, 0 Critical**. Machine-readable: `audit/findings.jsonl`.
Findings F-03/F-04/F-06/F-07 and F-01/F-05 were established in the first pass; F-08/F-09 and the
refreshed coverage in §4.3 come from the second pass (see §7.1).

### F-01 High — the client's redirect refusal is one edit away from leaking the device secret

`medical_stt/host_client.py:38-62` defines `_NoRedirectHandler` and wires it into
`_OPENER_FACTORY`; `:120` uses that opener; `:166-170` maps a refused redirect to a hard error.
Shipped behaviour is **correct** — `audit/repro/F-001_redirect_leak.py` (loopback only) shows the
client refusing with

```
ProviderError: host redirected the session request … (refusing to send credentials anywhere else)
```

and **0 requests** reaching the redirect target. The problem is what enforces it: nothing.

* Mutant `M11-opener-factory-wiring` rebuilds the opener without `_NoRedirectHandler()` and runs the
  whole suite in a throwaway copy: **813 passed, 4 skipped, 0 failed** → verdict `SURVIVED`.
* With the handler unwired, the same repro shows urllib following the 302 and the target server
  recording `{'authorization': 'Bearer <placeholder>', 'x_client_id': 'doctor-01', 'method': 'GET'}`
  while the client returns `access_token='attacker-issued-token'` as a valid session.

Impact if that edit ever lands: a 256-bit per-device secret plus the device identity go to an
attacker-chosen host, and a forged session is trusted. The trap is that the *host* side of the same
property **is** pinned (`tests/test_audit_fixes.py:56-150`,
`test_host_grant_client_never_follows_a_redirect`), which makes the client side look covered.

**Falsification:** a test that fails when the handler is absent from the opener's handler list, or
that drives `HostSessionClient` through a redirecting loopback server and asserts both
`ProviderError` and zero target requests, would refute this. Full-suite mutation says none exists.

**Fix:** add `tests/test_host_client_redirects.py` (two loopback servers, 302 to the second; assert
`ProviderError`, 0 target requests, and that the secret never appears in anything the target saw),
and pin the wiring itself by asserting an instance of `_NoRedirectHandler` is among
`_OPENER_FACTORY.handlers`. Effort **S**.

### F-02 Medium — eight clinical dextrose rules can never fire

`medical_stt/processing/terminology.py:150-154` protects numeric spans before any rewrite
(`protected = find_numeric_spans(text) + find_negation_spans(text)`). Eight `specialty_terminology`
rules in `data/corrections.yaml` (lines 2911, 2917, 2923, 2929, 3283, 3289, 3295, 3301) have numeric
text *inside their own source*: `تزریق دکستروز 10 درصد در آب` → `D10W`, `… 5 درصد در سالین` →
`D5S`, `… 5% در آب` → `D5W`, and four siblings. The match therefore always lands inside a protected
span and the rewrite is skipped.

Evidence (E3): `audit/repro/probe_dead_rules.py` — 727 active rules, **8 unreachable-by-own-source,
all eight dextrose**, each printing the overlapping numeric span (e.g. `(14,21,'10 درصد')`);
`audit/repro/probe_dextrose.py` — all eight sentences come out byte-identical through
`normalize() → terminology()`, while the digit-free rule `لیزر CO2 → CO2 laser` still fires, which
isolates the numeric protection as the cause.

Impact: a missing conversion, not a corrupted one — no dose or digit is altered, and the clinician
still reads the spoken Persian form. But 8 of 727 reviewed, High-priority rules are silently dead,
and no corpus case covers them, so the same class of dead rule can be added again unnoticed.

**Falsification:** any spelling (ASCII or Persian digits, `%` or `درصد`) reaching its replacement
would refute it; 8/8 stayed unchanged.

**Fix:** per rule, choose — rewrite the pairs digit-free where clinically equivalent, or teach the
engine to subtract a rule's *own* numeric spans from `protected`, or move these into a
numeric-aware rewrite stage. Add one regression case per rule to
`tests/fixtures/medical_regression_corpus.yaml`. Effort **M**.

### F-05 Medium — the session-reset invariant has no test

`medical_stt/app.py:497-518` documents three hazards it prevents: buffered pre-Stop audio being
streamed into the next session, a half-finished utterance leaking across sessions, and stale
injector partial state (the backspace bookkeeping). Mutant `M9-session-reset-removed` deletes exactly
`self._reset_session_state()` from the teardown path at `:697-699` while keeping
`self.phase = "idle"`, and the full suite stays green → `SURVIVED`.

Impact if lost: a burst of stale dictation text from the previous session enters the next one, or a
later revision deletes characters the clinician typed in the meantime — clinical-record integrity.
Shipped code is correct today, so this is a test gap, not a live defect.

**Falsification:** a Stop → Start lifecycle test asserting the audio queue drained, the accumulator
cleared and `reset_partial()` observed would refute it; the suite has queue unit tests but no
session-boundary assertion.

**Fix:** one lifecycle test with the existing fake provider/sounddevice (spy on `reset_partial`,
assert `_audio_q.depth() == 0` and an empty accumulator after teardown); optionally a
monkeypatch-recording assertion that teardown calls the reset. Effort **S–M**.

### F-03 Low — the ASR-replacement guard is narrower than its documentation

`data/asr_replacements.yaml:9-14` states that digits, units and Latin/English targets are "not
allowed here (enforced by load_asr_replacements(), see medical_stt/config.py)", and the README
repeats it. The enforcement at `medical_stt/config.py:686-698` is conditional on
`_contains_non_ascii_letter(source)`, so an all-ASCII pair never has its target inspected.

E3: `audit/repro/probe_asr_replacements.py` → `mg:mcg` **ACCEPTED**, `iv:IV` **ACCEPTED**;
`میلیگرم→mg` rejected ("introduces a Latin/English term or unit"); `d5w` rejected ("cannot
contain digits"). Digits and `:` are still blocked, and this is a reviewed config file, so the
impact is a documentation/guard mismatch: a careless commit can ship an all-transcript unit rewrite
provider-side, ahead of every local protection, exactly the class the guard exists for.

**Falsification:** the shipped loader rejecting an all-ASCII unit pair. The code comment shows the
ASCII exception is deliberate; the finding is the mismatch and the absent second line of defence.

**Fix:** narrow the documented claim, or (better) keep the claim and scan the *target* for ASCII
letters/units on both branches, with a unit blocklist (`mg`, `mcg`, `ml`, `iv`, `im`, `po`, …) plus
the two probe cases as tests. Effort **S**.

### F-04 Low — the "no stale capacity figures" guard guards two strings

`README.md:257-260` promises: "`tests/test_host_docs.py` fails CI if a hard-coded capacity figure
comes back." The test (`tests/test_host_docs.py:165-186`) blocklists exactly `"< 0.01s"` and
`"+0.65 mb"`.

E3: in the throwaway copy (`audit/scratch/`), a *new* hard-coded figure was inserted into the
capacity section and `pytest tests/test_host_docs.py -q` returned **14 passed**. The tracked README
was never touched (restored from `/tmp/README.orig`; final `git status` clean apart from `audit/`).

**Falsification:** a structural check (regex over the capacity table for any literal measurement)
would refute it; none exists.

**Fix:** assert the capacity section contains no `\d+(\.\d+)?\s*(ms|s|MB|%)`-shaped literal other
than the declared threshold column. Effort **S**.

### F-06 Low — the 60-second retry-hint ceiling is unpinned

`medical_stt/stt/reconnect.py:44` sets `max_retry_after_seconds: float = 60.0`, and
`docs/PERFORMANCE_FIXES.md` states "Retry hints are bounded at 60 seconds by default" as a
deliberate safety limit. Mutant `M10` changes the default to `3600.0` → **SURVIVED**. The existing
test pins the *floor* (never shortened below backoff, killed by M15) but not the ceiling, so a
hostile or broken `Retry-After` could stall dictation for an hour with no test failure.
Effort **S**: assert the ceiling directly and clamp a 3600 s hint.

### F-08 Medium (security) — the packaged client reads a `.env`, and the docs say it cannot

`medical_stt/config.py:173-174` runs **at import time**, unconditionally:

```python
load_dotenv(_ROOT / ".env")
load_dotenv()
```

`_ROOT` is the folder *above* the `medical_stt` package — for a Nuitka standalone build that is the
bundle folder holding `MedicalSTT.exe`. `.env.example:8-10` states the opposite: "The packaged
Windows app does not read this file. … This file is for development on platforms without DPAPI and
for CI."

E3 (`audit/repro/probe_dotenv_walkup.py`, output `audit/raw/probe_dotenv_walkup.txt`; the child
process has every `MEDICAL_STT_*` / `MEDICALSTT_*` / `DEEPGRAM_*` variable stripped, so anything that
appears came from the file; the driver is a script file because python-dotenv treats `python -c` as
interactive and switches to a cwd search):

| Scenario (sys.frozen = what the built exe has) | `.env` location | launched from | loaded? |
| --- | --- | --- | --- |
| A | none | `/tmp` | no |
| B | inside the app folder | `/tmp` | **yes** |
| B2 | inside the app folder | the app folder | **yes** |
| C | parent of the app folder | the app folder | **yes** (python-dotenv's frozen branch searches `os.getcwd()` upward) |
| D | parent of the app folder | `/tmp` | no |
| E | parent of the app folder | the app folder, not frozen (source checkout) | **yes** (frame-based walk-up from the package dir) |

The probe's `.env` also demonstrated a real behavioural effect, not just an environment variable:
`paths.app_data_dir()` returned the redirected directory — the folder that holds `settings.yaml`,
`logs/`, `run.lock` and `host_secret.dpapi` (`medical_stt/paths.py:27-31`).

Consequence chain (E2, deliberately **not executed** because R3 forbids setting `MEDICALSTT_HOST_URL`):
`get_settings()` reads `os.getenv("MEDICALSTT_HOST_URL", …)` (`config.py:570-576`), `load_host_secret()`
keeps using the DPAPI secret unless `MEDICALSTT_HOST_SECRET` is set (`config.py:378-396`), and
`HostSessionClient` sends that secret as `Authorization: Bearer …` to whatever URL was configured —
the header content was already demonstrated by the F-001 repro. So a `.env` that sets only the host
URL turns into device-credential exfiltration; the same primitive silently swaps
`DEEPGRAM_MODEL`/`DEEPGRAM_LANGUAGE` or relocates the secret store. No code execution as the user is
required — only write access to the app folder (or, in case C, to any ancestor of the launch
directory). The deploy instructions are "copy the whole folder to the target machine", often into a
user-writable or shared/UNC location, and `.env` is git-ignored, so a dropped file is invisible to
`git status`, CI and the repo's own scanner.

**Falsification:** the two calls load nothing in the frozen/bundle shape, the values never reach
`Settings`/`paths`, or a guard exists. `rg -n "load_dotenv|dotenv" medical_stt` returns only these
two calls and the import; the probe shows both the injection and the `app_data_dir()` change.

### F-09 Low (windows-only, static) — a BOM in the release manifest

`scripts/build_windows.ps1:138`:

```powershell
& $Python -m pip list --format=json | Set-Content -Encoding UTF8 (Join-Path $DistDir "dependency-versions.json")
```

Under Windows PowerShell 5.1 (still the default `powershell.exe` on Windows 10/11) `-Encoding UTF8`
means UTF-8 **with BOM**; PowerShell 7 changed it to BOM-less. The file then starts with `EF BB BF`,
and `json.load` / `python -m json.tool` fail on it — which is exactly the documented release check
"3. … Inspect `dependency-versions.json` in the bundle" (`docs/PERFORMANCE_FIXES.md:98`). CI writes
the same-named artifact from Python (`.github/workflows/ci.yml:47`), byte-for-byte without a BOM, so
the two manifests disagree by encoding.

Static review only: PowerShell is not installed here and R3 forbids running the build script, so
confidence is **Medium** (R5). **Falsification:** one command on the build machine writes a test file
the same way and prints its first three bytes — `EF BB BF` confirms, `5B` (`[`) refutes; a build
machine running PowerShell 7+ also refutes it.

### F-07 Low (hygiene) — a baseline entry that matches nothing

`scripts/secret_scan_baseline.txt:12` holds `deepgram_key_legacy 70e4eede … delete this line`, and
`scripts/scan_secrets.py:296-308` prints "1 baseline entry/entries matched nothing" on every run.
The baseline exists to be the complete, reviewed list of accepted findings, so a dead entry erodes
what the note means. **Caveat, stated because it changes the conclusion:** this checkout contains a
single squashed commit (`git rev-list --all | wc -l` → 1), so the note is partly an artifact of this
clone; upstream, with real history, the entry may still match the `.env` key named in `SECURITY.md`.
Reported as hygiene with confidence **Medium**, not as evidence that the history gate is broken — and
the flip side is recorded in §7: in this clone `--history` proves almost nothing.

---

## 4. Verification, test strength and coverage

### 4.1 Mutation testing (20 mutants, `audit/raw/mutation_results.jsonl`)

Scratch copy only (`git archive HEAD` into `audit/scratch/`), each mutant restored in a `finally`;
the tracked tree was never mutated.

| Verdict | Mutants |
| --- | --- |
| **KILLED** (17) | M1 fst word-boundary · M3 BP pattern · M4 negation `نداشت` · M5 dangerous-rule gate · M6 constant-time compare · M7 auth lockout · M8 ZWNJ · M12 host body cap · M13 `session_starts_total` placement · M14 send/stop race guard · M15 Retry-After floor · M17 `*.secret` scanner rule · M18 queue recovery threshold · M19 metrics token encoding · (plus the first batch's M-README variants) |
| **SURVIVED** (4) | **M11** opener wiring (F-01) · **M9** session reset (F-05) · **M10** retry ceiling (F-06) · **M2** `iter_long` → `iter` |

**M2 is an equivalent mutant, not a gap** (recorded under Non-issues): the defensive re-sort/re-resolve
in `fst.py` reproduces `iter_long`'s output, so no behavioural difference is observable.

Surviving mutants are **test-strength gaps, never product defects**; nothing was "fixed" here
(REPORT-ONLY). Three of the four survivors became findings; M2 did not, on purpose.

### 4.2 Two-stage verification

Each candidate was re-derived by a script written after the candidate existed, with a step that
could fail: mutation runs for F-01/F-05/F-06/F-04, the two-run behavioural comparison inside
`F-001_redirect_leak.py`, the engine-invocation probe behind F-02, and a self-contained loader probe
for F-03. Verdicts: **CONFIRMED** for F-01…F-06 (F-07 confirmed locally, `UNDETERMINED` upstream —
see its caveat). Limitation: "independent" here means a second executable path, not a second
reviewer.

### 4.3 Coverage

Classified in `audit/coverage.csv`; LOC by `wc -l` on the enumerated list.

| Tier | Files | LOC | Read in full | Partial |
| --- | --- | --- | --- | --- |
| **Tier 1** (key, secret, clinical text, PHI path) | 32 | 8,639 | **32/32 files — 100% of the enumerated LOC** | — (the Windows-only modules are read but cannot be executed here) |
| **Tier 2** (tooling, CI, docs, deployment — scope widened in the second pass) | 31 | 7,351 | 17 files — 2,757 LOC (~42%) | 14 files — 4,594 LOC (~58%), each read at least at heading level plus targeted ranges |

Still sampled rather than read, and therefore still the honest edge of this audit:
`tests/` (46 modules, ~10,200 LOC) — covered by targeted `rg`/`sed` reads and, more importantly, by
the 20-mutant strength experiment rather than by reading; `data/corrections.yaml` (5,058 lines) — read
via the rule census, the digit sweep and the dead-rule probe, never line-by-line; and parts of
`README.md`, `host/README.md`, `docs/CPANEL_HOSTING.md` (§1–5, 7, 8 read), the two `PERFORMANCE_*`
companions, `docs/WINDOWS_POWERSHELL.md` and `scripts/{classify_corrections,make_local_cert,verify_client_host,windows_selftest,windows_injection_check}.py`.

---

## 5. Regression table — claims the repo says were already fixed

Re-checked against the SHA under audit; a regression means the code no longer does what the repo
says it does. `docs/PERFORMANCE_FIXES.md` (F1–F13) and `docs/PERFORMANCE_AUDIT.md` (historical)
supply the claims; `tests/test_audit_fixes.py`, `tests/test_performance_fixes.py` and
`tests/test_host_docs.py` supply the pins.

| Claim (repo) | Status here | Evidence |
| --- | --- | --- |
| Grant client / session client never follow redirects | **PARTIAL REGRESSION** | host side pinned and green; desktop client's refusal is real but unpinned → **F-01** |
| Oversized/chunked body aborted at the cap | HOLDS | M12 KILLED |
| Pre-auth failures excluded from session-start metric | HOLDS | M13 KILLED |
| Send racing Stop is not an error | HOLDS | M14 KILLED |
| Retry-After never shortened below own backoff | HOLDS | M15 KILLED |
| Scanner finds committed `*.secret` files | HOLDS | M17 KILLED |
| Queue "degraded" requires sustained recovery | HOLDS | M18 KILLED |
| Non-ASCII metrics token returns 401, never 500 | HOLDS | M19 KILLED |
| Constant-time credential comparison | HOLDS | M6 KILLED |
| Failed-auth lockout cannot harm valid colleagues | HOLDS | M7 KILLED; re-read `host/app.py:288-332` |
| Negation / numeric protection preserved | HOLDS | M3, M4 KILLED |
| ZWNJ preserved | HOLDS | M8 KILLED |
| README quotes no stale capacity figure | **PARTIAL** | guard is a 2-string blocklist → **F-04** |
| Doc claims about Passenger/Forwarded-Proto/lifespan boot line | HOLD | `tests/test_host_docs.py` pins each; re-read CPANEL §7 against `host/core.py:319` |
| Metrics admin token protects `/metrics` | **WEAKER THAN DOCUMENTED** | optional, warned at startup, no client-id/PHI labels → kept as C09 (non-issue) |
| ASR replacements carry no units/Latin targets | **PARTIAL** | ASCII-only pairs pass → **F-03** |
| Deepgram concurrency analysis F1–F8 (close-code classification, KeepAlive, per-client burst 10 / 6 per min, region selection, `tag`/`extra` attribution, `utterance_end_ms` 1000–5000 + keyterm char budget, jitter ceiling) | HOLD | re-read in `stt/deepgram_provider.py`, `stt/reconnect.py`, `config.py`, `host/core.py:152/256` and `host/.env.example:90-95`; C36 |
| Windows clipboard/focus fixes (F13) | UNVERIFIABLE HERE | fake-DLL tests exist; Win32 ABI cannot be executed here (R5) |

No claim was found to be *false in production* code; the two partial regressions are both guard
coverage (F-01) and guard breadth (F-04), not broken behaviour.

---

## 6. Non-issues, refuted candidates and risk register

* **M2 (`iter_long` → `iter`) — equivalent mutant.** `processing/fst.py` re-sorts/re-resolves
  candidates after the scan, so the observable output is identical; not a finding.
* **`/metrics` unauthenticated by default.** Real, but deliberate and loudly warned
  (`host/app.py:171-178`), with no client-id labels and no PHI (inspection of the module: labels are
  `status=outcome`-style only), and `PERFORMANCE_FIXES.md` already tells operators to set the token in
  production. Documented trade-off, not a defect — flagged to the user as the one **deployment-time**
  setting to confirm, together with the TLS/`HOST_ALLOW_HTTP` decision in CPANEL §7.
* **`_parse_retry_after` accepts only delta-seconds** (`medical_stt/host_client.py:84-101`). The
  HTTP-date form is dropped and the client falls back to its own backoff — documented in the
  docstring, safe by construction. Not a finding.
* **Legacy single shared secret.** Gated correctly: `host/core.py:286-291` returns the legacy-only
  registry solely when `HOST_CLIENTS_FILE` is unset, and once a registry exists an absent client id
  resolves to `legacy`, which matches nothing. No auth bypass found in this path.
* **298 default-on single-token rewrites / `safe_lexical` drift** (flagged while reading
  `data/corrections.yaml`). The rules exist and are enabled by default; no probe produced a concrete
  clinical misfire, so this is **risk-register material for clinical review, not a finding** (E1
  alone is not reportable). The reviewer should sample these rules against real (consented,
  de-identified) transcripts.
* **`benchmarks/metrics.py` — clean.** An exact-match hypothesis scores perfectly on all six metrics
  for all 22 corpus cases (WER/CER 0, numeric/negation/English-term/medical-term recall 1.0), the
  documented truncation guard (`120/8` never satisfies `120/80`) works, and presentation marks and
  NFD/Persian-digit variants do not produce phantom negation losses. The only sharp edge is that
  recall tokens keep punctuation (`mg/kg/day`), which is documented ("no punctuation stripping is
  applied") and is the strict behaviour a clinical benchmark wants. Risk register, not a finding.
* **`scripts/build_windows.ps1` gates** — the build refuses UPX, runs the repo secret scanner, the
  full test suite and the Windows self-test, then verifies `config/`+`data/`, `libportaudio*.dll` and
  a `--version` smoke run (C34). The only defect found is the manifest encoding (F-09).
* **`scripts/scrub_history.py`** — backs up every ref outside the repo, refuses to overwrite an
  existing backup, drops `refs/original`, expires reflogs and re-scans before declaring success; its
  findings print commit/path/pattern only, never a matched value (R2-safe, C33). Not executed (R3).
* **`stt/runner.py`** — no credentials, opt-in by construction, and shutdown is bounded; provider
  reuse across cases is safe because `start()` fetches a fresh session token each time (C31).
* **`ui/overlay.py` geometry** — Windows work-area metrics (SPI_GETWORKAREA 76–79), geometry written
  only when the string changes, self-cancelling follow tick. Nits only: `_render_version` is
  incremented and never read, and `_is_rtl("")` returns True.
* **`TranscriptOverlay._thread` / `ControlWindow._test_connection` / `_saving` reuse** — inspected,
  no reachable defect demonstrated (E1); left out of `findings.jsonl` on purpose and listed in §7 as
  not fully reviewed rather than implied clean.

---

## 7. Limitations, coverage gaps, and what a fix prompt should target

**Limitations (never to be read as passes):**

* No real provider, deployed host, microphone, patient audio. Concurrency, TLS, and clinic-scale
  behaviour are **not** measured here — `measure_capacity.py` is a mocked loopback harness.
* No Windows execution: DPAPI, `SendInput`, clipboard sequence/format handling, named mutex and the
  Win32 ABI in `_windows_backend.py` are static-only. Any Windows claim carries confidence ≤ Medium.
* `gitleaks`, `pip-audit`, `bandit`, `--cov` were not run (CI runs them).
* `--history` scanning is near-vacuous in this clone (one squashed commit). Treat the "history is
  clean" statement as *scanner-clean in this checkout only*. The committed-key incident described in
  `SECURITY.md` could not be re-verified here, and its baseline entry is dead (F-07).
* `data/corrections.yaml` was audited through the rule census and a digit sweep, not read line by
  line; only digit-bearing rules were exhaustively checked (that is how F-02 surfaced).
* Tests were sampled, not audited individually; the mutation harness is the strength signal, and it
  covers 20 hand-chosen invariants, not the whole suite.
* Docs beyond the enumerated files (`CPANEL_HOSTING.md` §8+, `PERFORMANCE_AUDIT_REPRODUCTION.md`
  probes, `host/README.md` line by line) were skimmed.

**Units not fully reviewed (after the second pass):** `tests/` as prose (sampled, with the 20-mutant
experiment standing in for reading), `data/corrections.yaml` beyond the census/digit sweep, and the
partially-read docs/scripts listed at the end of §4.3. Tier-1 code itself is now read in full —
including the four files that were partial in the first pass.

### 7.1 Second pass (this round)

Requested by the user after the first report: review the units the first pass had flagged as
unfinished. Every listed unit was read in full and each was either cleared or turned into a finding.

| Unit | Result |
| --- | --- |
| `medical_stt/ui/overlay.py` (242) | Clear (C32). Geometry stays inside the Windows work area, geometry calls are change-gated. Nits: unused `_render_version`, `_is_rtl("")` is True. |
| `medical_stt/stt/runner.py` (80) | Clear (C31). No credentials, bounded shutdown, provider reuse safe (fresh token per `start()`). |
| `scripts/scrub_history.py` (288) | Clear (C33). Backup-first, refuses overwrite, re-scans after rewrite, never prints a matched value. Not executed (R3). |
| `scripts/build_windows.ps1` (182) | **F-09** (manifest BOM under PowerShell 5.1); everything else — UPX refusal, scanner/test/selftest gates, bundle verification — is sound (C34). |
| `benchmarks/` (785) | Clear (C28–C30), including an executed probe: an exact-match hypothesis scores perfectly on all six metrics across all 22 corpus cases. |
| `host/app.py` 1–479, `host/metrics.py` 120–340, `medical_stt/config.py` 286–650, `medical_stt/app.py` 1–490 + 545–690 | Clear (C37/C38) — except one code trace that became **F-08**: `config.py:173-174` loads a `.env` at import time, which the packaged-app documentation denies. |
| `docs/WINDOWS_POWERSHELL.md`, `docs/DEEPGRAM_CONCURRENCY_ANALYSIS.md` | Re-checked: the concurrency doc's F1–F8 remediation table matches the code and the tests (C36); the PowerShell guide's procedures contain no download-and-run pattern and reference only files that exist. |

Two probes were written for this pass and are saved with their output:
`audit/repro/probe_dotenv_walkup.py` → `audit/raw/probe_dotenv_walkup.txt`, and
`audit/repro/probe_benchmark_metrics.py` → `audit/raw/probe_benchmark_metrics.txt`.

**Suggested fix order for the follow-up prompt:** F-01 (one test, largest blast radius) → F-08
(security + doc correction) → F-05 → F-02 → F-03 → F-09 → F-04 → F-06 → F-07. F-01, F-05, F-06 and F-04 are pure test additions and are safe to
batch into one commit; F-02, F-03, F-08 and F-09 touch behaviour or shipped documentation and should
be separate, each with its own regression case.
