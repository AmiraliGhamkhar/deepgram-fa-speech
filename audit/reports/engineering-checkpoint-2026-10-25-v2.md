# Engineering checkpoint — 2026-10-25 (remaining fixes, v2)

## Scope

Continuation from HEAD `887a1f8` / merged PR #26. P0-1 through P0-6 are
preserved and not reimplemented. This checkpoint records the remaining
Phase 1 bug fixes, the Phase 2 GUI restyle, deliberately updated pins and
full validation output. The file is appended as each item lands; partial
sections explicitly say what is still outstanding.

Baseline before any change in this round: `pytest tests/ -q` → **1055 passed,
4 skipped, 1 warning** (the accepted locked Starlette/httpx test-client
deprecation), `ruff check .` clean, `mypy medical_stt host benchmarks` clean
(43 files), `python scripts/scan_secrets.py` clean. Local tools were installed
from the existing hash-locked dev requirements; no lockfile was changed.

## Per-fix evidence

Every “before” row was produced by running the new/updated regression test
against the original implementation before editing the corresponding
production code. Failures below were observed, not inferred.

| Item | Commit | Before (targeted regression) | After (targeted regression) |
|---|---|---|---|
| P0-7 letter-glued numeric spans + Persian units | `783e7f7` | `17 failed, 50 passed` in `tests/test_numbers.py tests/test_terminology.py` | `67 passed` in the same targeted suite; census: `835 rules, 9 digit-bearing sources, 9 numeric-span sources, loader: OK, rejected: 0` |
| P0-8 normalized terminology rule storage | `13c8f25` | `5 failed, 132 passed` in `tests/test_terminology.py tests/test_config.py tests/test_data_safety.py` | `137 passed` in the same targeted suite; census: `835 raw rules, 1 raw non-normalized side, 0 parsed non-normalized sides, Arabic-yeh source fires: True` |
| P0-9 majority-strong BiDi direction | `65aa762` | `10 failed, 69 passed` in `tests/test_bidi.py tests/test_formatting_ownership.py tests/test_injection.py` | `79 passed` in the same targeted suite |
| P0-10 span-level low-confidence protection | `69568a0` | `2 failed, 7 passed` in `tests/test_app.py` | `97 passed` in `tests/test_app.py tests/test_confidence.py tests/test_terminology.py tests/test_performance_fixes.py` |
| P0-11 Start/Stop controller snapshot race | `6179094` | `1 failed, 1 passed` in the two controller race tests with the fix temporarily reverted | `2 passed` in the same tests; `14 passed` in `tests/test_session_lifecycle.py` |
| P0-12 silent provider exit | `7c183dd` | `2 failed, 14 passed` in `tests/test_shutdown_flow.py` | `80 passed` in shutdown/app/performance/concurrency targeted suites |
| P0-13 fallback clipboard PHI leak | `2f8b6be` | `4 failed, 44 passed` in `tests/test_injection.py tests/test_edge_paths.py` | `68 passed` in injection/edge/Windows-policy targeted suites |
| P0-14 utterance-limit recovery text | `8c56329` | `3 failed, 62 passed` in accumulator/performance/app targeted suites | `65 passed` in the same suites |
| P1 inferred follow-up batch | `313687b` | `20 failed, 167 passed` in nine targeted suites | `187 passed` in the same suites |
| P2 docs/metrics polish | `571dfa5` | `5 failed` in `tests/test_docs_polish.py` | `5 passed` in the same file |
| GUI R2 shared fonts + Windows DPI | `6ea4264` | `ImportError: cannot import name 'fonts'` collecting `tests/test_ui_fonts.py`; `2 failed` in `tests/test_control.py` | `47 passed` in `tests/test_ui_fonts.py tests/test_control.py tests/test_overlay.py` |
| GUI R1 circular controls | `2c4a73f` | `3 failed, 27 passed` in `tests/test_control.py` | `30 passed` in the same file |
| GUI R3 RTL/persistent drag/crisp | (this commit) | `4 failed, 31 passed` in `tests/test_control.py` | `54 passed` in `tests/test_control.py tests/test_ui_fonts.py tests/test_overlay.py` |

### P0-7 details

- New boundaries distinguish “inside a longer number” from “letter-adjacent
  to a number”: `BP120/80`, `فشارخون120/80mmHg`, `دکستروز5 درصد`, `CO2` and
  `دوز5گرم` now protect the intended whole span.
- One shared unit list covers ratios, ranges and single values, including
  `گرم`, `میلی\u200cگرم`, `میلی لیتر`, `کیلوگرم`, `سانتی\u200cمتر`,
  `میلی مول`, `درجه`, `درصد` and `روز`. Optional-unit groups own their
  leading space, so an unrecognised unit no longer leaves a detached trailing
  space in the protected range.
- `audit/repro/probe_numeric_rule_census.py` re-runs the whole `corrections.yaml`
  census. Rule 682, `لیزر CO2 → CO2 laser`, was the only previously
  digit-bearing unflagged source; its validation verdict flipped from
  accepted (but dead) to numeric opt-in. It was reviewed, given
  `numeric_in_source: true` (both sides preserve digit run `2`), and still
  fires. All eight dextrose rules remain valid; no rule semantics changed.
- A terminology rule that would rewrite `گرم` is now blocked by the FST's
  whole-numeric-span containment check for `دکستروز5 گرم`.

Before (excerpt from `/tmp/p0_7_before.txt`):

```text
FAILED tests/test_numbers.py::test_letter_glued_digit_starts_a_protected_span[فشارخون120/80mmHg-120/80mmHg]
FAILED tests/test_numbers.py::test_letter_glued_digit_starts_a_protected_span[دکستروز5 درصد-5 درصد]
FAILED tests/test_numbers.py::test_letter_glued_digit_starts_a_protected_span[BP120/80-120/80]
FAILED tests/test_numbers.py::test_persian_clinical_units_are_part_of_numeric_span[5 گرم-گرم]
FAILED tests/test_numbers.py::test_range_span_does_not_end_with_a_detached_space[3-4 روز]
FAILED tests/test_terminology.py::test_letter_glued_digit_requires_numeric_opt_in
FAILED tests/test_terminology.py::test_numeric_span_covers_persian_unit_against_terminology_rewrite
17 failed, 50 passed in 0.44s
```

After `python audit/repro/probe_numeric_rule_census.py`:

```text
rule 682: 'لیزر CO2' -> 'CO2 laser' spans=[(7, 8, '2')] OK: reviewed numeric opt-in

total rules: 835, digit-bearing sources: 9, numeric-span sources: 9, loader: OK
rejected by census: 0
```

Full-suite validation after P0-7: `1071 passed, 4 skipped, 1 warning`;
`ruff check .`: `All checks passed!`; `mypy medical_stt host benchmarks`:
`Success: no issues found in 43 source files`; secret scan clean.

## Deliberately updated pins

- `test_digit_in_latin_term_without_numeric_span_needs_no_opt_in` was replaced
  by `test_letter_glued_digit_requires_numeric_opt_in`. A digit glued to a
  Latin letter is now a protected numeric span, so a synthetic unflagged
  `لیزر CO2` rule must fail validation while its reviewed opt-in still fires.
- `test_numeric_accuracy_requires_the_exact_clinical_expression` now expects
  `2/3`, not `1/2`: the benchmark reference contains `HbA1c`, whose
  letter-glued `1` is deliberately part of the numeric census after P0-7.
- No security, threading, clipboard, reconnect, drag or GUI pins were
  weakened.

## Remaining acceptance limitations

- This checkpoint is appended incrementally; the latest per-fix section is the
  authoritative progress marker. All Phase 1 P0/P1/P2 fixes and the Phase 2
  GUI restyle (R2 fonts/DPI, R1 circular controls, R3 RTL/persistent
  drag/crisp) are complete at this revision.
- The accepted Starlette test-client warning is preserved; no warning filters
  or lockfile edits were added.
- Native Windows injection, live microphone and live Deepgram behaviors
  remain gated. This Linux checkout validates orchestration and offline
  policies only.
- The historical-key owner action from `SECURITY.md` remains: revoke the
  exposed key in the Deepgram console and follow the owner-managed history
  cleanup procedure. No rewrite was attempted here.

### P0-8 details

- `parse_rules` now normalizes both source and target once, stores the
  canonical forms in `TerminologyRule`, validates the numeric opt-in flag
  against digit runs from both normalized sides, and rejects a source that
  normalizes to empty with a loud `ConfigError`.
- The legacy flat pair loader also returns normalized pairs, so no caller can
  build a dead automaton from raw Arabic-yeh sources.
- `audit/repro/probe_normalization_census.py` swept all 835 shipped rules by
  comparing each raw `from`/`to` with `normalize()` and with the parsed rule.
  Exactly one raw difference remains, rule 776 `پايدار -> پایدار`; the parsed
  rule set has zero non-normalized sides and the synthetic opt-in engine fires
  on `پایدار`. The raw legacy spelling is intentionally retained in YAML as a
  regression fixture; no rule semantics changed. A census test asserts that
  no future parsed rule regresses.
- Targets written with Persian digits and Arabic decimal separators are now
  canonicalized before digit-run comparison and before injection.
- `data/corrections.yaml` header documents the normalization contract.

Before (excerpt from `/tmp/p0_8_before.txt`):

```text
FAILED tests/test_terminology.py::test_arabic_yeh_source_is_not_a_dead_rule_after_parse_time_normalization
FAILED tests/test_terminology.py::test_non_normalized_target_is_normalized_before_injection
FAILED tests/test_terminology.py::test_digit_run_validation_normalizes_both_source_and_target
FAILED tests/test_terminology.py::test_shipped_rule_census_has_no_non_normalized_parsed_entries
FAILED tests/test_terminology.py::test_flat_correction_pairs_use_normalized_rules
5 failed, 132 passed in 0.61s
```

After `python audit/repro/probe_normalization_census.py`:

```text
total rules: 835, parsed rules: 835
raw non-normalized sides: 1
  776:from 'پايدار' -> 'پایدار'
parsed non-normalized sides: 0
Arabic-yeh source fires on normalized Persian: True
```

Full-suite validation after P0-8: `1076 passed, 4 skipped, 1 warning`;
`ruff check .`, `mypy medical_stt host benchmarks` and secret scan clean.
No existing test pin was changed for P0-8; five new regression tests were
added.

### P0-9 details

- `base_direction` now counts RTL/LTR strong characters, chooses the
  majority, falls back to first-strong only on a tie, and keeps the LTR
  default for pure-neutral text. `to_injected` still prefixes at most one
  LRM/RLM, never RLE/PDF, and remains idempotent on already-marked text.
- New medical examples cover Latin-leading Persian-dominant utterances,
  Persian-leading Latin-dominant utterances, pure Persian/English, pure
  numbers (`120/80`, `98%`), punctuation-only text and strong-count ties.
- The comments in `to_injected` and the multiline injection test were updated
  so the documented policy matches the implementation.

Before (excerpt from `/tmp/p0_9_before.txt`):

```text
FAILED tests/test_bidi.py::test_base_direction_uses_persian_majority_when_latin_leads
FAILED tests/test_bidi.py::test_base_direction_counts_strong_characters[MI در ECG مشاهده شد-rtl]
FAILED tests/test_bidi.py::test_base_direction_counts_strong_characters[بیمار CT scan normal-ltr]
FAILED tests/test_bidi.py::test_injected_mark_follows_majority_without_embeddings[MI در ECG مشاهده شد-\u200f]
FAILED tests/test_bidi.py::test_injected_mark_follows_majority_without_embeddings[بیمار CT scan normal-\u200e]
10 failed, 69 passed in 0.41s
```

After: targeted suite `79 passed`; full suite `1096 passed, 4 skipped,
1 warning`; ruff, mypy and secret scan clean.

Deliberately updated pin: `test_base_direction_ltr_when_latin_leads` →
`test_base_direction_uses_persian_majority_when_latin_leads`, now asserting
`rtl` for `"MI در ECG مشاهده شد"`. No other existing pin was weakened; the
single-leading-mark and idempotence pins are re-asserted with the new
majority-heuristic examples.

### P0-10 details

- `UncertainMedicalWord` now carries the provider word tuple index.
  `low_confidence_protected_ranges` aligns normalized word texts to the
  normalized utterance in both tuple order and start-timing order, chooses the
  alignment that finds more words and every uncertain token, and returns their
  character spans. It never logs transcript text.
- `TerminologyEngine.apply` accepts extra `protected_ranges`, merged with
  negation spans before the numeric opt-in pass. A low-confidence token's span
  cannot be rewritten even by a `numeric_in_source` rule; reviewed rules
  elsewhere in the same utterance still fire.
- If a flagged token cannot be aligned (word/timing mismatch, empty canonical
  text, malformed warning), the app logs only the failure type and preserves
  the whole utterance — the old conservative behavior is the explicit fallback.
- `data/corrections.yaml` and `config/settings.yaml` now document the new
  low-confidence semantics.

Before (excerpt from `/tmp/p0_10_before.txt`, `pytest tests/test_app.py -q`):

```text
FAILED tests/test_app.py::test_low_confidence_medical_term_is_preserved - Ass...
FAILED tests/test_app.py::test_low_confidence_numeric_token_keeps_other_corrections
2 failed, 7 passed in 0.55s
```

After: `97 passed` across the app, confidence, terminology and processing-
failure targeted suites. New unit cases pin scrambled timings, reset segment
timings, repeated words, normalized punctuation, missing alignments, and FST
protection that is not bypassed by numeric opt-in. Full suite:
`1106 passed, 4 skipped, 1 warning`; ruff, mypy and secret scan clean.

Deliberately updated pin: `test_low_confidence_medical_term_is_preserved` now
contains a second reviewed term and asserts that `ICU` is still injected while
the low-confidence `هایپرتنشن` token remains unchanged. Two new app-level
regressions cover a sub-threshold number and the conservative alignment
fallback. No security, threading, numeric or GUI pins were weakened.

### P0-11 details

- `SessionController.stop()` now takes a consistent `(stt, thread)` snapshot
  under `_lock`, the same lock `start()` holds while swapping those fields.
  The lock is released before `request_stop()` and `thread.join()`.
- A 50-iteration Start→Stop race test gates the factory while `start()` holds
  the lock, calls `stop()` concurrently, and records every session-thread
  `join()`. It asserts the joined thread is the thread created by that same
  `start()`, using a fake STT that cannot finish until `stop()`'s own
  `request_stop()` runs. A second test probes `join()` and proves `_lock` is
  not held while waiting.
- No existing pins were changed. `cancel_start()` remains nonblocking so a
  cancellation during a slow factory still latches immediately.

Before (the fix was temporarily reverted after writing the tests):

```text
FAILED tests/test_session_lifecycle.py::test_stop_snapshots_the_started_thread_under_the_lock
1 failed, 1 passed in 0.07s
```

After: both race tests `2 passed`; whole lifecycle suite `14 passed`; full
suite `1108 passed, 4 skipped, 1 warning`; ruff, mypy and secret scan clean.

### P0-12 details

- `_run_session()` now records whether any provider error was emitted for the
  current generation. In `run_provider`'s `finally`, a current-generation
  thread that returns with no error, no stop flag and no shutdown request calls
  the generation-fenced `on_error` with
  `ProviderError(ErrorCategory.UNKNOWN, "provider exited without error or stop")`
  before setting `_stop`.
- A silent provider therefore raises through `_run_session()` and makes
  `run()` retry/classify the dead dictation instead of returning exit code 0.
  Deliberate Stop, an already-reported error, and superseded generations are
  explicitly excluded from the synthetic error.
- Four regressions use a `SilentProvider`, a provider that reports an error
  before returning, and a stoppable provider. The `run()` test pins a bounded
  retry configuration and asserts exit code 1 and two attempts (before the
  fix it returned 0 after one silent attempt).

Before (excerpt from `/tmp/p0_12_before.txt`):

```text
FAILED tests/test_shutdown_flow.py::test_silent_provider_exit_raises_unknown_error_before_stop
FAILED tests/test_shutdown_flow.py::test_silent_provider_exit_does_not_report_a_successful_run
2 failed, 14 passed in 4.44s
```

After: targeted shutdown/app/performance/concurrency suites `80 passed`;
full suite `1112 passed, 4 skipped, 1 warning`; ruff, mypy and secret scan
clean. No existing pins were changed.

### P0-13 details

- `FallbackBackend.send_unicode_text` now always calls `paste_text` with
  `restore_clipboard=True`; a dev-only "type" path must not leave dictated PHI
  on the clipboard even when paste-mode restoration is disabled.
- `paste_text` refuses an unreadable prior clipboard before writing, verifies
  that `pyperclip.copy` actually took the text, sends the paste only after
  that verification, and restores the previous text in a `finally` after the
  configured settle. Restoration uses a content guard so a newer user copy is
  not overwritten; if the guard read itself fails, a best-effort restore is
  still attempted to remove our text.
- All new failure logs use exception types only, never clipboard contents.
  The module docstring and README explicitly document the remaining gaps:
  pyperclip cannot enumerate non-plain formats, expose an OS sequence number
  (an identical-text newer copy is indistinguishable), or confirm that the
  target consumed the timed paste. Windows SendInput remains production.
- `backend.py`'s `get_default_backend` docstring now says the fallback type
  path restores the prior clipboard unconditionally.

Before (the fixed backend was temporarily replaced with the HEAD version):

```text
FAILED tests/test_injection.py::test_fallback_typed_path_respects_the_configured_settle
FAILED tests/test_injection.py::test_fallback_type_mode_restores_previous_clipboard
FAILED tests/test_injection.py::test_fallback_type_mode_refuses_an_unreadable_clipboard
FAILED tests/test_injection.py::test_fallback_type_mode_restores_after_a_failed_write_verification
4 failed, 44 passed in 0.29s
```

After: `48 passed` in `tests/test_injection.py tests/test_edge_paths.py`;
`68 passed` when the Windows clipboard-policy suite is included. Full suite
`1116 passed, 4 skipped, 1 warning`; ruff, mypy and secret scan clean.

Deliberately updated pin: `test_fallback_typed_path_respects_the_configured_settle`
now also asserts that `send_unicode_text` passes `restore_clipboard=True`.
Four new regressions cover prior-copy restoration, unreadable refusal,
verification-failure restore, and the newer-copy content guard. No Windows
clipboard or security pins were weakened.

### P0-14 details

- `UtteranceLimitError` now carries `accumulated_text`: all retained finalized
  segments joined with the segment that tripped the budget.
  `UtteranceAccumulator.pending_text()` exposes the current joined segments,
  and `add()` calls `reset()` at the instant the limit trips.
- `LiveMedicalSTT._on_transcript` retains `exc.accumulated_text` in a single
  `_retain_failed()` call (falling back to `event.text` only for an empty
  exception payload). Because the accumulator is already empty, shutdown's
  `_flush_pending_utterance()` cannot append the earlier segments again.
- Recovery remains bounded by the existing 8000-character/16-entry policy;
  the fix guarantees that all accumulated segments reach that buffer once,
  in order, rather than only the final segment.

Before (the fixed accumulator and app were temporarily replaced with HEAD):

```text
FAILED tests/test_utterance_accumulator.py::test_utterance_limit_exposes_all_segments_and_resets
FAILED tests/test_performance_fixes.py::test_accumulator_has_a_size_budget
FAILED tests/test_performance_fixes.py::test_utterance_limit_retains_the_whole_utterance_once
3 failed, 62 passed in 2.86s
```

After: `65 passed` in those three targeted suites; an app-level test asserts
`recovery_text() == "بخش اول بخش دوم بخش سوم"` even after
`_flush_pending_utterance()`. Full suite `1118 passed, 4 skipped, 1 warning`;
ruff, mypy and secret scan clean.

Deliberately updated pin: `test_accumulator_has_a_size_budget` now asserts the
exception's full accumulated text and that a tripped limit resets the
accumulator, instead of expecting the old segments to survive `flush()`.
No security, threading, or GUI pins were weakened.

### P1 batch details (inferred list, confirmed by the user)

The original P1/P2 prompt was available only as a condensed category list in
this session. The user confirmed proceeding from those categories. Each item
below received a regression that failed against the previous implementation;
no existing security, threading, behavior, or GUI pin was weakened.

- **MICROPHONE reconnect/status:** `run()` counts consecutive
  `ErrorCategory.MICROPHONE` failures and stops after
  `MAX_MICROPHONE_FAILURES = 2` with exit code 1, phase `idle`, and the
  distinct warning `MICROPHONE_UNAVAILABLE_WARNING`. Other retryable categories
  and a stable 30 s connection reset the counter.
- **Overlay readiness:** the shared-owner path now waits for the posted
  `overlay-create` callback using `OVERLAY_READY_TIMEOUT_SECONDS` and disables
  itself on timeout. `_run()` catches any build exception (not just
  `tk.TclError`), disables the overlay, and always signals `_ready`; exception
  logs use types only. Pure-neutral text is no longer forced RTL by `_is_rtl`.
- **Deepgram shutdown-send:** a late `send_audio()`/`keep_alive()` after `stop()`
  has cleared the connection is `SHUTDOWN`, not a retryable `NETWORK` error.
- **Secret decode errors:** an invalid UTF-8 DPAPI result becomes a
  `SecretStoreError` with a byte-free message. `load_host_secret()` logs the
  exception type, not its message.
- **Latency tracker ownership:** finals pass their per-utterance
  `LatencyTracker` explicitly through `_on_transcript`, `_finalize_utterance`,
  `_render_and_inject`, and `_flush_pending_utterance` instead of swapping the
  shared `self.latency` on the transcript worker.
- **Accumulator dedupe:** retained source events are kept parallel to segments;
  a re-sent event is recognized even after an intervening segment. Conservative
  evidence (identical word timings or the exact same event object) is still
  required, so legitimate repetition survives.
- **ASR blocked tokens:** `audit/repro/probe_asr_blocked_tokens.py` sweeps every
  standalone uppercase Latin token of length >= 2 in `data/corrections.yaml`.
  Census: `uppercase Latin tokens: 131, blocked: 131, missing: 0`; 121 tokens
  were added to `_ASR_BLOCKED_TOKENS`. Single-letter placeholders (`x:y`)
  remain legal. A test pins the census and six representative rejections.
- **Audio counters:** `BoundedAudioQueue.drain()` now counts discarded pre-Stop
  chunks in `dropped_total` as well as `dropped_bytes`, while not marking the
  next session degraded.
- **Host settings/logging/registry:** invalid numeric environment values are
  reported by setting name only, never by raw value. `host.provision` builds
  all secrets/lines first and atomically replaces the registry via a temporary
  file + `os.replace`; a failed replace leaves the original registry and no
  temporary files. Existing append/duplicate-warning behavior is preserved.
- **Config comment:** `reconnect_jitter` now documents the implemented full
  negative jitter ("below the computed ceiling"), not a symmetric +/- range.

Before (excerpt from `/tmp/p1_before.txt`):

```text
FAILED tests/test_performance_fixes.py::test_microphone_reconnects_are_capped
FAILED tests/test_performance_fixes.py::test_final_transcript_uses_the_event_latency_tracker
FAILED tests/test_overlay.py::test_shared_owner_overlay_disables_itself_on_ready_timeout
FAILED tests/test_overlay.py::test_non_tcl_overlay_build_failure_disables_and_signals_ready
FAILED tests/test_overlay.py::test_neutral_overlay_text_is_not_forced_rtl
FAILED tests/test_deepgram_protocol.py::test_late_send_after_stop_is_classified_as_shutdown[send_audio]
FAILED tests/test_secret_store.py::test_invalid_utf8_blob_is_a_secret_store_error
FAILED tests/test_utterance_accumulator.py::test_resent_segment_is_deduplicated_after_an_intervening_segment
FAILED tests/test_asr_replacements.py::test_every_uppercase_latin_clinical_token_is_blocked_for_asr_replacements
FAILED tests/test_audio_queue.py::test_drain_counts_discarded_chunks_as_lost_audio_without_degrading_health
FAILED tests/test_config_docs.py::test_jitter_comment_describes_the_actual_full_jitter_policy
FAILED tests/test_audit_fixes.py::test_invalid_numeric_host_setting_value_is_not_written_to_logs
FAILED tests/test_audit_fixes.py::test_registry_update_is_atomic_when_the_final_replace_fails
20 failed, 167 passed, 1 warning in 3.80s
```

After: targeted nine-suite run `187 passed, 1 warning`; full suite
`1141 passed, 4 skipped, 1 warning in 54.35s`; ruff, mypy and secret scan
clean. Deliberately updated pins: none.

### P2 batch details (inferred list, docs/metrics polish)

The remaining inferred P2 category was documentation and metrics polish; it
does not change runtime behavior. `tests/test_docs_polish.py` was added first
and failed `5 failed` against the pre-update documents and `host/metrics.py`
module docstring. It now pins:

- `README.md`'s new "Priority follow-ups — 2026-10-25" section, which covers
  two consecutive `MICROPHONE` failures, per-utterance latency trackers,
  neutral overlay text not forced RTL, atomic registry updates, and drained
  audio accounting.
- `host/README.md` provisioning documentation for the temporary file +
  `os.replace()` atomic registry replacement.
- Every declared host counter/gauge/histogram plus `host_uptime_seconds`
  appears in the host README metrics table and in `host/metrics.py`'s module
  docstring. Missing series were `session_http_requests_total`,
  `session_http_outcomes_total`, `grant_capacity_rejections_total`,
  `active_session_requests`, `session_http_latency`, and `host_uptime_seconds`.
- `docs/PERFORMANCE_FIXES.md` links this checkpoint under "2026-10-25
  follow-ups" while preserving the historical 2026-10-14 audit narrative.

After: `5 passed` targeted; full suite `1146 passed, 4 skipped, 1 warning in
53.96s`; ruff, mypy and secret scan clean. Deliberately updated pins: none.

## Phase 2 — GUI restyle

Offline-only evidence: this Linux sandbox has no display, so per-change
acceptance is widget-tree descriptions extracted from the fake-Tk harness
(`tests/test_control.py` builds the real widget tree against recorded
stand-ins), not screenshots. No GUI threading, mailbox, pump, escape-to-close,
drag-math, secret-write or status-string behavior was changed.

### GUI R2 details — shared fonts + Windows DPI (`ui/fonts.py`)

Widget tree after `_build` (font annotations only; structure unchanged):

```text
Tk (Medical STT, overrideredirect, topmost, opaque)
└── Frame (inner, bg=BG)
    ├── Frame (header, bg=BG)
    │   ├── Label "Medical STT"           font=(family, 10, "bold")
    │   └── Label "✕"                     (quit)
    ├── Label  "● آماده"                  font=(family, 10)
    ├── Canvas toggle circle              text font=(family, 12, "bold")
    ├── Label  (detail)                   font=(family, 8)
    ├── Frame (separator)
    ├── Label  "میزبان (https)"           font=(family, 8)
    ├── Entry  (host url)                 font=(family, 9)
    ├── Label  "کلید مشترک"               font=(family, 8)
    ├── Entry  (secret, write-only)       font=(family, 9)
    ├── Label  "شناسه دستگاه"             font=(family, 8)
    ├── Entry  (client id)                font=(family, 9)
    └── Button ×3 (flat)                  font=(family, 9)
```

`family` resolves once per process from
`Vazirmatn/Vazir/IRANSans/B Yekan/B Nazanin/Segoe UI/Tahoma/Arial`
with a platform default fallback; the overlay uses the same helper (its
private `_get_best_persian_font` was removed). `fonts.enable_windows_dpi_awareness()`
runs at the top of `ControlWindow._run` and `TranscriptOverlay._run`, before
`tk.Tk()`: per-monitor awareness via `SetProcessDpiAwareness(2)` with the
`SetProcessDPIAware` fallback, idempotent, no-op off Windows, failures logged
by type only. Eight pure selection/DPI tests plus one before-Tk ordering test
cover it.

Before (excerpt): `tests/test_ui_fonts.py` could not be collected
(`ImportError: cannot import name 'fonts'`), and `tests/test_control.py` gave
`2 failed, 26 passed` (font pin ImportError + DPI-ordering ImportError).
After: `47 passed` across the three UI test modules; full suite
`1155 passed, 4 skipped, 1 warning`; mypy now covers 44 source files.

Deliberately updated pin: `test_client_id_row_matches_the_other_settings_rows`
now compares the label font with `fonts.font(window._root, 8)` instead of the
hardcoded `("Segoe UI", 8)`.

### GUI R1 details — circular 72/52/20px controls

Widget tree after `_build` (geometry annotations; colors unchanged):

```text
Tk (Medical STT, overrideredirect, topmost, opaque)
└── Frame (inner, bg=BG)
    ├── Frame (header, bg=BG)
    │   ├── Label "Medical STT"                 (title, pack side=left)
    │   └── Canvas quit circle 20px "✕"         (tag "quit", pack side=right)
    ├── Label  "● آماده"                        (status)
    ├── Canvas primary circle 72px "شروع"       (tag "toggle", pack anchor=center)
    ├── Label  (detail)
    ├── Frame (separator)
    ├── Label  "میزبان (https)" / Entry (host url)
    ├── Label  "کلید مشترک" / Entry (secret, write-only, masked)
    ├── Label  "شناسه دستگاه" / Entry (client id)
    └── Frame action row (bg=BG, pack fill=x)
        ├── Canvas 52px "ذخیره\nتنظیمات"        (created third,  pack side=right)
        ├── Canvas 52px "متن\nبازیابی"          (created second, pack side=right)
        └── Canvas 52px "بررسی\nاتصال"          (created first,  pack side=right)
```

- The primary toggle is `_TOGGLE_DIAMETER = 72` (was 64); its `_paint_toggle`
  painter, `شروع/توقف` text swap, keyboard activation and hover-darken
  behavior are unchanged.
- The three settings actions are 52px Canvas circles (`_SECONDARY_DIAMETER`)
  in one row packed right-to-left, so the first action is rightmost (Persian
  reading order). Two-line labels keep the previous wording. Hover recolors
  SURFACE -> BORDER, the same pair the flat buttons used.
- Quit is a 20px Canvas circle (`_QUIT_DIAMETER`) with the same `✕` glyph and
  the same close command; no `WM_DELETE_WINDOW` protocol, Escape still closes.
- The recovery dialog keeps the flat `_flat_button` factory (its long labels
  do not fit a circle). No color token was added or changed.

Before (excerpt from `/tmp/r1_before.txt`): `assert 64 == 72`,
`AttributeError: 'ControlWindow' object has no attribute '_action_row'`,
`AttributeError: 'ControlWindow' object has no attribute '_quit'`
(`3 failed, 27 passed`). After: `30 passed` in `tests/test_control.py`;
full suite `1157 passed, 4 skipped, 1 warning`; ruff, mypy and secret scan
clean.

Deliberately updated pin: `test_secondary_buttons_share_the_flat_style` was
replaced by `test_secondary_actions_are_52px_circles_in_rtl_order` (the main
window's secondary controls are circles now, not flat buttons). The flat
factory remains pinned by `test_recovery_popup_is_themed_modal_and_confirms_copy`.

### GUI R3 details — RTL layout, persistent drag, crisp surface

Widget tree after `_build` (final Phase 2 shape):

```text
Tk (Medical STT, overrideredirect, topmost, OPAQUE — no -alpha)
└── Frame (inner, bg=BG, padx=16, pady=12)
    ├── Frame (header, bg=BG)
    │   ├── Canvas quit circle 20px "✕"          (tag "quit",  pack side=left)
    │   └── Label "Medical STT"                  (title,      pack side=right)
    ├── Label  "● آماده"                         (status, anchor=e / justify=right)
    ├── Canvas primary circle 72px "شروع"        (tag "toggle", pack anchor=center)
    ├── Label  (detail, anchor=e / justify=right, wraplength=320)
    ├── Frame (separator, pady=10)
    ├── Label  "میزبان (https)" / Entry (host url, LTR)
    ├── Label  "کلید مشترک" / Entry (secret, write-only, masked)
    ├── Label  "شناسه دستگاه" / Entry (client id, LTR)
    └── Frame action row (bg=BG)
        ├── Canvas 52px "ذخیره\nتنظیمات"        (pack side=right, padx=4)
        ├── Canvas 52px "متن\nبازیابی"          (pack side=right, padx=4)
        └── Canvas 52px "بررسی\nاتصال"          (pack side=right, padx=4)
```

- **Crisp:** `root.attributes("-alpha", 0.97)` is gone — the control surface is
  a fully opaque normal window (layered windows blur on Windows). The overlay
  keeps its own `-alpha` (required for its click-through extended styles);
  overlay behavior is otherwise untouched.
- **RTL:** the header mirrors — title at the right edge, quit circle at the
  left — matching the right-anchored status/detail column and the
  right-to-left action row. Persian status strings keep their exact wording
  and logical text (pinned); only layout sides changed.
- **Persistent drag:** the window remembers its dragged position across runs
  in `ui_state.json` (paths.ui_state_path: `{x, y}` only, never secrets),
  written atomically on drag release and on close, restored on build. A
  release without a drag does not write; corrupt/absurd state falls back to
  `+40+40` instead of blocking startup. `_on_drag_start`/`_on_drag_move` are
  byte-for-byte unchanged (A3.2 drag-math pins intact).
- **Polish:** vertical rhythm tightened (inner 16/12, status and detail
  `pady=(8, 0)`, separator `pady=10`). No color token added or changed; no
  widget protocol, binding or threading behavior changed.

Before (excerpt from `/tmp/r3_before.txt`): the opacity pin failed on the
present `("-alpha", 0.97)` call, the header pin on `side="left"` title,
`AttributeError: ... no attribute '_on_drag_end'`, and the close-persistence
pin on a missing `ui_state.json` (`4 failed, 31 passed`). After: `54 passed`
across the three UI test modules; full suite `1162 passed, 4 skipped,
1 warning`; ruff, mypy and secret scan clean. Deliberately updated pins: none.

## Final validation (end of Phase 2)

Commands used `.venv/bin/` tools installed from the existing hash-locked dev
and host requirements. No dependency or lockfile changed.

### pytest tests/ -q

```text
1162 passed, 4 skipped, 1 warning in 53.19s
```

The single warning is the accepted locked Starlette/httpx test-client
deprecation, preserved unfiltered.

### ruff check .

```text
All checks passed!
```

### mypy medical_stt host benchmarks

```text
Success: no issues found in 44 source files
```

### python scripts/scan_secrets.py

```text
clean: no credentials found in the working tree
note: 1 baseline entry/entries matched nothing -- the history looks scrubbed. Remove them from secret_scan_baseline.txt so the check stays meaningful.
```

## Final deliverable index

- Per-bug before/after test evidence: every table row and detail section above
  (P0-7..P0-14, the P1 batch, the P2 batch). Commits `783e7f7`, `13c8f25`,
  `65aa762`, `69568a0`, `6179094`, `7c183dd`, `2f8b6be`, `8c56329`, `313687b`,
  `571dfa5`.
- Per-GUI-change evidence: widget-tree descriptions for R2 `6ea4264`, R1
  `2c4a73f` and R3 `b85c435` in the Phase 2 sections above. This Linux
  sandbox has no display, so screenshots could not be captured; the fake-Tk
  harness extracts the real widget tree (kinds, sizes, pack sides, canvas
  items, bindings, fonts) on every run instead. Windows visual verification
  remains in the release checklist (`docs/PERFORMANCE_FIXES.md`).
- Deliberately updated pins (complete list for this round):
  `test_digit_in_latin_term_without_numeric_span_needs_no_opt_in` replaced by
  `test_letter_glued_digit_requires_numeric_opt_in`;
  `test_numeric_accuracy_requires_the_exact_clinical_expression` (1/2 -> 2/3);
  renamed `test_base_direction_uses_persian_majority_when_latin_leads`;
  `test_low_confidence_medical_term_is_preserved` (span-level ICU injection);
  `test_accumulator_has_a_size_budget` (limit resets, full-text exception);
  `test_fallback_typed_path_respects_the_configured_settle` (restore=True);
  `test_client_id_row_matches_the_other_settings_rows` (shared font helper);
  `test_secondary_buttons_share_the_flat_style` replaced by
  `test_secondary_actions_are_52px_circles_in_rtl_order`. No security,
  threading, clipboard, reconnect, drag-math, status-string or A3.x guard pin
  was weakened.
