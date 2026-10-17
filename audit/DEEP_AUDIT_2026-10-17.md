# Deep audit — deepgram-fa-speech

| | |
| --- | --- |
| Mode | **REPORT-ONLY.** No product file was modified. The only writes are this report and the `.venv/` toolchain directory (git-ignored). `git status` is clean apart from this untracked report. |
| Repository | `AmiraliGhamkhar/deepgram-fa-speech` |
| Audited SHA | `895110b822be38e1935762eea03473072fd8a510` (branch `arena/0516f8f0-deepgram-fa-speech`, single squashed commit) |
| Date | 2026-10-17 |
| Prior audit | `audit/REPORT.md` (2026-10-15, SHA `83dc32c9…`, 9 findings F-01…F-09) — fully re-verified below |
| Baseline | **964 passed, 4 skipped** · ruff clean · mypy clean (42 files) · secret scan clean (tree + history) · `measure_capacity.py` PASS (50/50) |
| Verdict | **No Critical, no High.** 1 Medium + 3 Low + 3 Info-level new items. All 9 prior-audit findings are fixed and pinned by tests (F-07 is history-dependent and behaves as expected in this squashed clone). The security architecture (key-on-host-only, DPAPI secret, redirect refusal, per-client rate limits, bounded request path) holds up under re-examination. |

New items at a glance:

| # | Severity | Title |
| --- | --- | --- |
| N-01 | **Medium** | Documented Windows setup + build gate silently skip 158 host tests (no `host/requirements-dev.txt`, no skip-guard) |
| N-02 | Low | Control-window drag handler parks the window at the drag *delta* (`+80+80`) instead of following the cursor |
| N-03 | Low | Docs describe a PyInstaller fallback via an unversioned, git-ignored `MedicalSTT.spec` — a second, ungated build path |
| N-04 | Low | `WINDOWS_POWERSHELL.md` staleness: "478 tests" (actual 964), superseded ruff/mypy invocations |
| N-05 | Low | Out-of-range `ttl_seconds` → 422 CONFIG (never retried) instead of clamping; `normalize_ttl`'s clamp is unreachable from the route |
| N-06 | Low | `WindowsBackend._last_written_sequence` never initialized in `__init__` (windows-only, static) |
| N-07 | Info | No lockfile — compatibility ranges (already acknowledged in `PERFORMANCE_FIXES.md`); Starlette deprecation warning is a drift symptom |

---

## 1. Scope, method, and coverage

### 1.1 Scope

(1) defects/crashes, (2) security weaknesses, (3) clinical-text-fidelity / patient-safety risk,
(4) privacy/PHI leakage, (5) documentation-vs-code drift, (6) regressions of previously fixed issues,
(7) supply-chain and build integrity. The prior audit's 9 findings were each re-derived, not trusted.

### 1.2 What was read in full (Tier 1 — key, secret, clinical text, audio/PHI path)

Every file below was read end-to-end at the audited SHA (~10,600 LOC across product + scripts):

- `host/core.py` (1128), `host/app.py` (645), `host/metrics.py` (357), `host/provision.py` (275), `host/passenger_wsgi.py` (154)
- `medical_stt/app.py` (961), `medical_stt/config.py` (834), `medical_stt/host_client.py` (277), `medical_stt/paths.py`, `medical_stt/app_instance.py`
- `medical_stt/stt/{base,reconnect,accumulator,runner,deepgram_provider}.py`, `medical_stt/audio/queue.py`
- `medical_stt/processing/{normalize,numbers,negation,fst,terminology,bidi,confidence}.py`
- `medical_stt/injection/{backend,text_injector,_windows_backend,_fallback_backend}.py`
- `medical_stt/security/{dpapi,secret_store}.py`, `medical_stt/ui/{control,overlay,dispatch}.py`
- `scripts/{build_windows.ps1,scan_secrets,scrub_history,measure_capacity,verify_client_host,windows_selftest,windows_injection_check,make_local_cert,classify_corrections}.py`
- `config/settings.yaml`, `data/{asr_replacements,keyterms}.yaml`, `data/keyterms/*.yaml`, `host/Dockerfile`, `host/.env.example`, `.env.example`, `.gitleaks.toml`, `.gitignore`, `.github/workflows/ci.yml`, `pyproject.toml`
- `benchmarks/run_benchmark.py` (head + live-mode paths); `benchmarks/metrics.py` verdict carried from the prior audit's executed probe (unchanged file)

### 1.3 What was sampled, not read line-by-line

- `data/corrections.yaml` (5,068 lines): census by category/confidence/dangerous flags (835 rules: 486 specialty + 197 abbreviation + 105 context-dependent + 44 safe-lexical + 3 unsafe; 3 `dangerous: true`; 8 `numeric_in_source: true`), plus targeted reads of the dextrose rules and header policy. Same method that surfaced F-02 previously.
- `tests/` (46 modules): targeted reads of the pinning tests for each finding; strength assessed by execution (964 green) plus the skip-demonstration probe, not by reading every test as prose.
- `docs/`: `WINDOWS_POWERSHELL.md` §1–5 + install/test/build commands, `CPANEL_HOSTING.md` env table + forwarded-header §7, `host/README.md` settings table + TTL + proxy sections, `README.md` capacity/architecture/signing sections. `PERFORMANCE_AUDIT*.md` skimmed for drift against current code.

### 1.4 What was executed

- `pytest tests/ -q` → **964 passed, 4 skipped** (the 4 intentional live/load/soak gates), 51.6 s
- `ruff check .` → clean; `mypy medical_stt host benchmarks` → clean, 42 files
- `scripts/scan_secrets.py` and `--history` → clean + the known dead-baseline note (see N-08/F-07)
- `scripts/measure_capacity.py` → `RESULT: PASS` (50/50, p95 99.8 ms, +0.27 MB/250 sessions, 0 in-flight, 50 buckets)
- 4 purpose-built probes (TTL behavior, drag arithmetic, import-skip demonstration, dextrose re-verification) — outputs inline in §4–5

### 1.5 What was NOT run (limitations, not passes)

- No live Deepgram traffic, no deployed host, no microphone, no patient audio. `MEDICAL_STT_LIVE_TEST`, `MEDICAL_STT_LOAD_TEST`, `MEDICAL_STT_SOAK`, `MEDICAL_STT_BENCHMARK_LIVE`, real `DEEPGRAM_API_KEY`/`MEDICALSTT_HOST_*` were never set. Loopback-only probes excepted (TTL probe used `TestClient`, in-process).
- No Windows execution: DPAPI, `SendInput`, Win32 clipboard, named mutex reviewed statically only (N-06 capped accordingly).
- `pip-audit`, `bandit`, `gitleaks` binaries, `pytest --cov` not run here (CI runs them); history rewrite/scrub, `build_windows.ps1`, `--interactive` injection check not run.
- This clone has one squashed commit, so `--history` proves little (same caveat as the prior audit's F-07).

Rules honored: repo content treated as data (no agent-directed text acted on); no secret value in any output (probes used synthetic `s×40` / `dg_probe_only` placeholders); every finding is E3 (executed probe) or E2 (cited trace) with a stated falsification.

---

## 2. Baseline results (SHA `895110b…`)

| Check | Result |
| --- | --- |
| `pytest tests/ -q` | **964 passed, 4 skipped** (live ×2, load ×1, soak ×1 — all intentional gates) |
| `ruff check .` | clean |
| `mypy medical_stt host benchmarks` | Success, 42 source files |
| `scripts/scan_secrets.py` (+ `--history`) | exit 0, clean; 1 dead baseline entry noted (expected — §6) |
| `scripts/measure_capacity.py` | `RESULT: PASS` — 50/50 ok, 0 × 429, p95 99.8 ms, loop free (0.100 s vs 2.50 s serialized), +0.27 MB, 0 in-flight, 50 buckets |
| Environment | Python 3.11.2 venv, repo's own requirement files; resolved: deepgram-sdk 7.9.0, httpx 0.28.1, fastapi 0.143.3, starlette 1.8.0, pyahocorasick 2.3.1, python-bidi 0.6.11 |

One warning is emitted and is already documented in `docs/PERFORMANCE_FIXES.md:56` (Starlette `TestClient`/httpx deprecation) — not a finding.

---

## 3. Prior-audit findings re-verification (F-01…F-09)

Each was re-derived independently. **8 of 9 are fixed and test-pinned; the 9th (F-07) behaves exactly as predicted** for a squashed clone.

| Finding | Status | Evidence at this SHA |
| --- | --- | --- |
| F-01 High — client redirect refusal unpinned | **FIXED + pinned** | `tests/test_host_client_redirects.py` exists: loopback 302 harness asserts `ProviderError`/`CONFIG`, **0 target requests**, secret absent from target-visible data; plus opener-wiring assertion on `_NoRedirectHandler`. Suite green. |
| F-02 Medium — 8 dead dextrose rules | **FIXED, verified E3** | `numeric_in_source: true` added to all 8 rules; `terminology.parse_rules` *refuses* numeric-span sources without the flag (`ConfigError`); `fst.py` allows only flagged whole-span matches. Probe: **8/8 fire** (`تزریق دکستروز 10 درصد در آب → D10W`, …), 727 active rules, digits preserved in targets. |
| F-03 Low — ASR guard ASCII loophole | **FIXED + pinned** | `config.py:785-786` now scans units/abbreviations on **both** sides regardless of script; `tests/test_asr_replacements.py` pins `mg:mcg`, `iv:IV`, `MCG`, `ICU`, `μg`, `%` rejections and benign-pair acceptance. |
| F-04 Low — capacity-figure blocklist | **FIXED** | `test_host_docs.py:189-233` replaced the 2-string blocklist with an allowlist (`_ALLOWED_CAPACITY_FIGURES`) plus a detects-a-new-figure test. README capacity section quotes thresholds only, no measurements. |
| F-05 Medium — session reset unpinned | **FIXED + pinned** | `tests/test_shutdown_flow.py:258-316`: `test_stop_clears_queued_audio_and_partial_injector_state` (queue drained, accumulator cleared, `reset_partial` observed with `["", "previous-session"]`) and `test_session_reset_is_called_once_at_start_and_once_at_stop` (`["connecting", "stopping"]`). |
| F-06 Low — Retry-After ceiling unpinned | **FIXED + pinned** | `test_audit_fixes.py:460-466` asserts `max_retry_after_seconds == 60.0` and clamps 3600 s / 600 s hints; `test_session_concurrency.py:297` pins the cap. |
| F-07 Low — dead baseline entry | **Expected / unchanged** | `scripts/secret_scan_baseline.txt:12` still lists `deepgram_key_legacy 70e4eede`; both scans print "1 baseline entry matched nothing". Correct in this squashed clone; the entry must stay until the rewritten history is force-pushed upstream (see `SECURITY.md`). Not a regression. |
| F-08 Medium — packaged client read `.env` | **FIXED + pinned** | `config.py:202-205` now loads `.env` **only when not frozen** (`if not getattr(sys, "frozen", False)`, explicit `_ROOT/.env`, no walk-up); `tests/test_config_dotenv.py` pins frozen-ignore in 3 scenarios and source-checkout loading in 2. |
| F-09 Low — manifest BOM (PS 5.1) | **FIXED + pinned** | `build_windows.ps1:140` uses `[IO.File]::WriteAllText(..., [Text.UTF8Encoding]::new($false))`; CI `windows-build` validates with `python -m json.tool`. |

No prior fix regressed. The PR #20 backend-defect fixes (grant error mapping, 3xx-grant refusal, registry loading, id grammar, metric accounting) were also re-read in `host/core.py` / `host/app.py` and hold.

---

## 4. New findings

### N-01 Medium — the documented Windows setup and the build gate silently skip 158 host tests

**Category:** test-gap / build integrity. **Evidence:** E3 (executed).

`docs/WINDOWS_POWERSHELL.md:46` (the only setup instruction for a Windows build machine) installs
only `requirements-dev.txt`, which does **not** include `host/requirements-dev.txt` (fastapi/httpx/uvicorn/a2wsgi).
Five test modules gate their entire contents on those imports:

- `tests/test_host_app.py`, `test_audit_fixes.py`, `test_backend_fixes.py`, `test_host_concurrency.py`, `test_passenger_wsgi.py` — **158 tests** between them.

CI knows about this trap and guards it explicitly (`.github/workflows/ci.yml`: install both files, then
`grep "could not import" pytest.log → fail`). But neither the doc's `pytest tests/ -q` (§3) nor
`scripts/build_windows.ps1`'s `pytest tests -q` build gate has that guard — so a build machine set up
exactly per the docs runs a green suite that never exercised the host at all, then ships the EXE.

Demonstration (import blocker standing in for "host deps not installed"):

```
PYTHONPATH=/tmp/skipdemo pytest test_host_app.py test_audit_fixes.py
    test_backend_fixes.py test_host_concurrency.py test_passenger_wsgi.py -q
→ 5 skipped ... exit 0   ("could not import 'fastapi'")
```

With deps: 158 tests collected. Without: 5 whole-module skips, **exit code 0**.

Impact: the modules that pin the host security posture (auth, redirect refusal, rate limits,
concurrency, WSGI) are precisely the ones skipped. A host regression could ride a locally-built EXE
out the door while CI stays green on a different machine — the build gate cannot catch what it never runs.

**Falsification:** a host-deps install step in the doc, or a skip-guard in `build_windows.ps1`, would refute this.
Neither exists (`grep -n "host/requirements" docs/WINDOWS_POWERSHELL.md scripts/build_windows.ps1` → no hits).

**Fix (S):** (1) doc §2: `pip install -r requirements-dev.txt -r host/requirements-dev.txt`;
(2) mirror CI's guard in `build_windows.ps1` (tee pytest output, fail on `could not import`).
Verifier: rerun the probe with the guard — build must fail loudly instead of skipping.

### N-02 Low — control-window drag parks the window at the drag delta, not under the cursor

**Category:** defect (UX). **Evidence:** E3 (no Tk needed — pure arithmetic on fakes).

`medical_stt/ui/control.py:255-267`:

```python
def _on_drag_start(self, event):                       # grab at (520, 320)
    self._drag_origin = (event.x_root, event.y_root)
def _on_drag_move(self, event):                        # drag to (600, 320+80)
    x = event.x_root - origin[0]                       # x = 80  (a DELTA)
    y = event.y_root - origin[1]
    self._root.geometry(f"+{x}+{y}")                   # absolute position := delta
```

`geometry("+x+y")` sets an **absolute** screen position, but `x`/`y` are the drag *deltas*.
Probe output: `geometry calls: ['+80+80']` — a window sitting at `+500+300` jumps to near the
screen origin on the first drag instead of following the cursor. No test covers drag
(`grep -i "drag\|geometry" tests/test_control.py` → nothing).

Impact: Low — cosmetic/UX only (borderless always-on-top window is hard to reposition), no safety
or security consequence. But it is a genuine functional bug in the primary UI.

**Falsification:** a drag that lands the window at origin-position + delta would refute it; the probe shows otherwise.

**Fix (S):** offset from the window's current position, e.g.
`self._root.geometry(f"+{self._root.winfo_x()+dx}+{self._root.winfo_y()+dy}")`, plus a fake-root
test like the probe. Verifier: the probe's expectation (`+580+380`).

### N-03 Low — docs describe a PyInstaller fallback via an unversioned, git-ignored spec

**Category:** build integrity / docs drift. **Evidence:** E2.

`docs/WINDOWS_POWERSHELL.md` ("No-C-compiler fallback… via a local, git-ignored `MedicalSTT.spec`
kept on the build machine") blesses a second build path whose definition file is **absent from the
repo and unversionable** (`.gitignore` ignores `MedicalSTT.spec`; `ls` confirms it is not here).
Consequences:

- CI calls `scripts/build_windows.ps1` "the single build definition" — the doc contradicts that.
- The fallback path has none of the script's gates (secret scan, full suite, `windows_selftest`,
  portaudio-DLL check, `--version` smoke run) unless the operator runs them by hand.
- The doc itself admits the Nuitka build is "harder to unpack"; a PyInstaller fallback silently
  trades that away for any machine whose operator takes the easier road.

Impact: Low — requires an operator to choose the fallback; but build integrity for a clinical tool
should not depend on which paragraph the operator reads.

**Fix (S):** either version a reviewed `MedicalSTT.spec` (+ gates) in the repo, or delete the fallback
section and require Nuitka. Verifier: `rg -i pyinstaller docs/ .gitignore` → no blessed-but-absent path.

### N-04 Low — `WINDOWS_POWERSHELL.md` staleness: test count and check commands

**Category:** docs drift. **Evidence:** E2.

- §3 says "**478 tests**, ~30 s". The suite is **964 tests** (~52 s here). An operator who sees 964
  pass may reasonably wonder whether 486 unexpected tests ran — stale counts erode trust in the gate.
- §3's "deeper checks" still show `ruff check medical_stt tests scripts host` (CI: `ruff check .`)
  and `mypy medical_stt host/core.py` — the exact invocation CI's comment calls out as checking
  *nothing* for host ("naming the files individually makes mypy reject the package-relative imports
  and check nothing"; CI: `mypy medical_stt host benchmarks`).

Impact: Low — weaker local checks than CI; an operator following the doc gets less assurance, silently.

**Fix (S):** sync the three commands + count with CI, or (better) have the doc invoke the same
commands by reference so they cannot drift again. `test_host_docs.py`-style pinning exists for host
env vars; nothing pins these doc commands.

### N-05 Low — out-of-range `ttl_seconds` fails closed with 422 instead of clamping

**Category:** robustness (availability). **Evidence:** E3. **Documented behavior** (`host/README.md:529-531`) — reported as a design observation, not a defect.

`host/app.py` validates `ttl_seconds` strictly (`type(requested) is not int or not 5 <= … <= max` → 422),
*then* calls `normalize_ttl`, whose clamp is therefore unreachable for any present value. Probe:

```
ttl=30 → 200      ttl=99999 → 422      ttl=1 → 422      ttl='bad' → 422
normalize_ttl(99999) = 3600   (the helper WOULD clamp)
```

The client maps 422 → `CONFIG`, which is **never retried**: if an operator ever lowers
`HOST_MAX_TTL_SECONDS` below a deployed client's `session_ttl_seconds` (or a future client default
exceeds an old host's max), every Start fails terminally instead of degrading to a shorter TTL.
Defaults are compatible today (30 ≤ 3600), so this needs a config change to bite.

**Fix (S, optional):** clamp out-of-range integers (keep rejecting non-integers/non-objects with 422),
or keep 422 and document the coupling next to `HOST_MAX_TTL_SECONDS` ("must stay ≥ every deployed
client's `session_ttl_seconds`"). Verifier: probe `ttl=99999` → 200 with `expires_in == max`.

### N-06 Low — `WindowsBackend._last_written_sequence` never initialized in `__init__` (windows-only, static)

**Category:** defect-robustness. **Evidence:** E2, static only (module refuses import off-Windows; confidence Medium).

`medical_stt/injection/_windows_backend.py`: `__init__` sets only `_allow_own_process`/`_expected_target`.
`_last_written_sequence` is first assigned at `:353` (inside `_set_clipboard_text`) and read at `:393`
(inside `paste_text`, after a success check — so the shipping path cannot hit `AttributeError` today).
It is instance-mutable state with no lock and no default: any future caller of `_set_clipboard_text`
that reads the attribute on a failure path, or any concurrent paste, breaks. The success-path guard
makes this latent, not live.

**Fix (S):** initialize `self._last_written_sequence: Optional[int] = None` in `__init__` and handle
`None` at the read site. Verifier: `grep -n "_last_written_sequence" _windows_backend.py` shows the init.

### N-07 Info — no lockfile; compatibility ranges (acknowledged) + drift symptom

**Category:** supply chain. **Evidence:** E2.

`requirements.txt` / `host/requirements.txt` use compatibility ranges (`fastapi>=0.115,<1.0`, …);
only `deepgram-sdk==7.9.0` is pinned. Consequences are visible, not theoretical: today's resolve
pulled `fastapi 0.143.3` / `starlette 1.8.0`, whose `TestClient` emits the httpx deprecation warning
now documented in `PERFORMANCE_FIXES.md:56`. The repo **already acknowledges** this
("compatibility ranges, not a cross-platform lockfile; resolved release manifests improve
traceability but do not replace… advisory scanning"), and CI runs `pip-audit` + records
`dependency-versions.json` — so this is recorded as accepted risk with a recommendation, not a finding:
adopt a compiled lock (`pip-compile` / `pip freeze` per supported Python) for reproducible,
auditable builds; until then, treat any `pip-audit` failure as release-blocking (it already is: `--strict`).

---

## 5. What was verified and holds (no finding)

**Security architecture.**
- Deepgram key exists only in host env; client has no `api_key` field, `Settings.host_secret` is `repr=False` and excluded from `as_dict()`; plaintext secrets in YAML are a hard `ConfigError` (`_reject_plaintext_secrets`).
- Redirect refusal on both hops: client `_NoRedirectHandler` (pinned, F-01) and host `follow_redirects=False` + `response_is_a_grant` (2xx-only, bodyless-2xx and all-3xx refused).
- Auth: constant-time compare (`hmac.compare_digest`), unknown-id dummy compare, legacy-mode gating correct, failed-auth budget keyed per-IP (`auth_failure_key`, with documented unattributable-fallback that still enforces auth).
- HTTPS enforcement: `request_is_secure` trusts `X-Forwarded-Proto` only from `HOST_FORWARDED_ALLOW_IPS`, last-hop semantics; uvicorn `proxy_headers` + `forwarded_allow_ips` aligned; plaintext refused without `HOST_ALLOW_HTTP`; startup warnings for no-cert/no-proxy ambiguity.
- Bounded request path: declared + streamed body cap (chunked-safe), body timeout, TTL type/range validation, inflight + active limiters with 503+`Retry-After`, per-client + global token buckets, bounded limiter tables with LRU eviction.
- Metrics reconcile: every post-auth outcome goes through `record_failure`/success (`session_starts == success + failures`); per-client series use the *authenticated* id with cardinality folding; `/metrics` token-gated when configured, loudly warned otherwise (accepted, documented).
- DPAPI: user scope, `CRYPTPROTECT_UI_FORBIDDEN`, explicit ctypes prototypes, magic-prefixed atomic secret file, owner-only chmod, no secret in errors/logs. Secret never in YAML, never in errors (401/403 → `AUTH`, never retried; messages carry host/status only).
- Secret scanning: tree + history clean; `dg_` + legacy + private-key + host-credential-assignment patterns; `*.secret` filename rule; baseline prints-but-accepts; gitleaks allowlist narrowly anchored to the two templates. `.gitignore` covers `.env*`, `*.secret`, `clients.txt`, keys.

**Correctness / reliability (spot-verified by reading + suite).**
- `SessionController` start/stop race: the `_stop_requested` latch covers stop-during-construction (traced both interleavings); `stop()` is join-bounded; recovery-text gate blocks restart until reviewed.
- Session teardown: provider/sender/processor joins bounded, generation fencing for stale callbacks, `_injection_failed` fail-stop (CONFIG is non-retryable, so the latch cannot wedge reconnects — traced).
- Audio path: non-blocking callback (flags only), bounded queue with degraded/recovery hysteresis, 3.2 s buffer sizing, capture watchdog + KeepAlive, mic-silence warning lifecycle.
- Reconnect: full jitter, `Retry-After` honored *and* floored by backoff *and* capped at 60 s (both bounds pinned), rate-limit floor, unlimited-by-default attempts with 30 s-connected reset.
- Grant path: sync/async transports kept in step (2xx-only, Retry-After, every failure mapped, no credential/body in messages); `_retry_after` clamped to 60 s; API-key shape checked at startup without echoing anything.
- Client id grammar mirrored in all three places (`host/core.py`, `host/provision.py`, `medical_stt/config.py`); homoglyph/`..` cases handled; provision refuses bad ids/prefixes/counts, warns on re-provisioning, writes secrets 0600.
- YAML cache keyed on (path, mtime_ns, size), deep-copied on hit, capped at 64 docs.
- Numbers/negation/normalize/FST/BiDi/confidence: re-read fully; no ReDoS shape (bounded patterns, no nested quantifiers), no O(n²) after whitespace collapsing, word-boundary + ZWNJ-join handling intact, numeric/negation protection layering intact, `numeric_in_source` gate enforced at load (`ConfigError`) and at match time (whole-span containment).
- Injection: 64-bit ctypes prototypes, clipboard ownership discipline, OpenClipboard retry, modifier hygiene, paste-settle + sequence-verified restore, non-plaintext-clipboard refusal, focus-change cancellation, own-process targeting refusal, UTF-16 backspace accounting, grapheme-safe split points. Fallback backend is dev-only and degrades loudly.
- UI threading: workers never enter Tcl (bounded mailboxes, Tk-thread pump), mailbox overflow raises instead of growing, render failures logged without transcript content, `_test_connection` spends one real token by explicit user action only, recovery popup is explicit-review-only with user-confirmed copy.

**Privacy / PHI.**
- Log sweep over every `log.*` call touching text/transcript/secret/token/clipboard: **no transcript text, secret, token, or clipboard content is ever logged**; confidence warnings log counts/categories only; recovery text is memory-only (16 × 8000 chars, truncation-flagged) and never logged or persisted.
- `MIP_OPT_OUT` plumbed (omitted-when-false, so Deepgram's default applies unless the operator opts out and accepts the billing note); region selectable with token-valid-everywhere semantics; `tag` carries the stable device id (respects the 500-tags/day/project budget), `extra` the session id.
- Overlay inherently displays transcript text on screen (product purpose) — see risk register R-3 for operating guidance.

**Docs-vs-code.** Host env-var table ↔ template ↔ code is CI-pinned (`test_host_docs.py`); client env vars ↔ template and `settings.yaml` keys ↔ `Settings` are pinned (`test_config_docs.py`); capacity section quotes no measurements; TTL-422, proxy/forwarded-header, Passenger-lifespan, no-audio-proxying, and readiness claims all match the code read.

---

## 6. Risk register and non-issues (deliberately not findings)

- **R-1. `/metrics` open by default** (carried): deliberate, startup-warned, no credentials/transcripts/traffic-content, documented operator action. Confirm `HOST_METRICS_ADMIN_TOKEN` in production.
- **R-2. Committed-key incident** (carried from `SECURITY.md`): revocation is a repo-owner console action; history rewrite + force-push still pending upstream. The F-07 baseline entry must stay until then. Nothing in this audit revokes or replaces the key.
- **R-3. Overlay/shoulder-surfing**: the overlay shows live transcript (PHI) on screen by design; screen-share, screenshots, and shoulder-surfing capture it. Consider one operating-guidance paragraph (disable overlay for sensitive sessions, beware screen-share) — `overlay_enabled` already exists.
- **R-4. Default-on single-token rewrites** (carried): 727 active rules include short-source mappings; no probe produced a clinical misfire, but this needs sampling against real consented, de-identified transcripts before clinical use. The word-boundary + numeric/negation guards are the backstop, and they hold.
- **R-5. `save_host_credentials` with an empty secret keeps the previous secret** (observed behavior): convenient for URL-only updates, but there is no UI path to *clear* a compromised secret short of deleting `host_secret.dpapi` by hand. Consider documenting that, or an explicit clear control.
- **R-6. `scan_history` covers reachable blobs only** (reflog/stash/unreachable objects unscanned): acceptable alongside `scrub_history.py`'s expire+prune, but a scrub that skips pruning would pass the scan while blobs linger. The script prunes by default — keep it that way.
- **R-7. Provision concurrency**: concurrent `provision --out` appends to one registry without file locking could interleave lines (each line is one `write()` call, so CPython-level interleave is unlikely but not guaranteed). Single-operator tool — acceptable; note if ever automated.
- **R-8. Nits carried**: overlay `_render_version` write-only, `_is_rtl("") == True`, `make_local_cert._secure_dir` redundant branch. Cosmetic; bundle with any overlay/UI touch.
- **Reviewed and cleared (no action):** `_parse_retry_after` delta-only (documented, safe fallback); legacy single-secret gating; `X-Forwarded-Proto` multi-hop last-value; `parse_forwarded_allow_ips` fail-closed on garbage; Docker healthcheck vs overridden allow-IPs (documented: retain loopback or use native TLS); import-time `SystemExit`/eager app construction (documented fail-fast); provision stdout secrets (documented one-time operator transfer); `_show_recovery` clipboard copy (explicit user confirmation); `session_token = ""` (reference drop, best-effort); `_retry_after` clamp; bool-TTL rejection (`type(x) is int`).

---

## 7. Recommended fix order

1. **N-01** (Medium) — doc install line + build-script skip-guard; the only item where the *gate itself* is blind.
2. **N-03 + N-04** (Low, docs) — one docs pass: remove or version the PyInstaller fallback, sync test count and check commands. Same files, same reviewer.
3. **N-02** (Low) — drag arithmetic + fake-root test; tiny, safe, user-visible.
4. **N-05** (Low) — clamp-or-document TTL decision; needs an operator-behavior call, so separate from the mechanical fixes.
5. **N-06** (Low) — one-line init + `None` handling; bundle with any Windows-backend touch (verify on a Windows runner).
6. **N-07** (Info) — lockfile adoption; process change, own track. R-3/R-5 one-paragraph docs can ride along.

N-01…N-04 and N-06 are safe to batch; N-05 needs the behavior decision first.

---

## 8. Appendix — probes and commands

Toolchain: `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt -r host/requirements-dev.txt`
(git-ignored; removed from consideration for the diff).

```bash
.venv/bin/python -m pytest tests/ -q                    # 964 passed, 4 skipped
.venv/bin/ruff check .                                  # clean
.venv/bin/mypy medical_stt host benchmarks              # clean, 42 files
.venv/bin/python scripts/scan_secrets.py{, --history}   # clean + dead-baseline note
.venv/bin/python scripts/measure_capacity.py            # RESULT: PASS
```

- **TTL probe** (`TestClient`, stubbed async grant): `30→200`, `99999→422`, `1→422`, `"bad"→422`; `normalize_ttl(99999)→3600`. → N-05.
- **Drag probe** (fake root/events, no Tk): grab `(520,320)` → move `(600,400)` ⇒ `geometry('+80+80')`. → N-02.
- **Skip probe** (import blocker for fastapi/httpx/a2wsgi/starlette/uvicorn): 5 modules skip whole, exit 0; with deps, 158 tests collected. → N-01.
- **Dextrose probe** (engine + normalize): 8/8 fire with digit-preserving targets; 727 active rules. → F-02 stays fixed.
- **Log sweep**: every `log.*` call mentioning text/transcript/secret/token/clipboard reviewed — no PHI, secret, token, or clipboard content logged.
- **Data census**: 835 rules (486/197/105/44/3), 3 dangerous, 109 requires-context, 8 numeric-in-source; keyterms per specialty 24–34 terms / 166–271 chars (budgets 100 / 1000).

---

*End of report. Fixes intentionally left for a separate follow-up — never audit and fix in one run.*

---

## 9. Fix log (2026-10-17, same branch — applied after the report above)

All items resolved except N-07 (deferred as a process decision — see below).
Each fix was test-pinned first: the 9 new tests failed on the pre-fix tree
and pass after. Post-fix gates: **974 passed, 4 skipped** · ruff clean ·
mypy clean (42 files) · secret scans clean · `measure_capacity.py` PASS.

| Item | Resolution | Files |
| --- | --- | --- |
| N-01 Medium | Doc §2 installs both requirement files; doc pytest uses `-q -rs`; `build_windows.ps1` runs `pytest tests -q -rs`, tees output, and throws on `could not import '(fastapi\|httpx\|uvicorn\|a2wsgi)'`. The pattern is deliberately narrower than CI's: `tests/test_soak.py` always skips on Unix-only `resource` on Windows, so a broad match would fail every Windows build. Simulated a client-only setup: the pattern matches exactly the 5 host-module skips. | `docs/WINDOWS_POWERSHELL.md`, `scripts/build_windows.ps1`, `tests/test_build_docs.py` (new, 7 tests) |
| N-02 Low | `_on_drag_move` offsets the window's current `winfo_x()/winfo_y()` by the drag delta instead of passing the delta as an absolute position. | `medical_stt/ui/control.py`, `tests/test_control.py` (+2 tests) |
| N-03 Low | PyInstaller fallback removed: README paragraph + `either path` reword, PS-doc prereq rows + §5 fallback block removed (Nuitka script declared the only supported path), stale `.gitignore` spec section removed. The README sentence explaining *why* Nuitka (PyInstaller contrast) is kept deliberately. | `README.md`, `docs/WINDOWS_POWERSHELL.md`, `.gitignore`, pinned by `tests/test_build_docs.py` |
| N-04 Low | Hard `478 tests` count dropped (no quoted count to go stale); doc check commands synced to CI's (`ruff check .`, `mypy medical_stt host benchmarks`); `tests/test_build_docs.py` extracts CI's commands and requires them verbatim in the doc, and bans `\d{3,} tests`. | `docs/WINDOWS_POWERSHELL.md`, `tests/test_build_docs.py` |
| N-05 Low | **Kept strict 422 (deliberate, pinned in `test_backend_fixes.py` + `test_performance_fixes.py`)** — clamping would silently issue a different TTL than requested and mask config errors. Fix = documented coupling where operators set it. | `host/README.md` (settings-table row), `host/.env.example` (TTL comment) |
| N-06 Low | `self._last_written_sequence: Optional[int] = None` initialized in `__init__` with a contract comment. | `medical_stt/injection/_windows_backend.py`, `tests/test_windows_clipboard_policy.py` (+1 test, fake-DLL fixture) |
| N-07 Info | **Deferred, not fixed.** A lockfile needs decisions this run should not make unilaterally: tool (`pip-compile` vs `uv`), per-platform/per-Python locks (Windows vs Linux wheels differ), and CI rewiring (`--require-hashes`). Proposed: `pip-compile` a `requirements/client.lock` + `requirements/host.lock` with hashes on the next dependency touch, wire CI to them, keep `pip-audit --strict` blocking. | — (proposal only) |
| R-8 nits | Removed write-only `overlay._render_version` (init + increment); collapsed the redundant `if/else` in `make_local_cert._secure_dir`. `_is_rtl("")` left as is (behavioral, immaterial). | `medical_stt/ui/overlay.py`, `scripts/make_local_cert.py` |
