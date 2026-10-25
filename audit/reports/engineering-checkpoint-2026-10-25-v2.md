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
| P0-8 normalized terminology rule storage | (this commit) | `5 failed, 132 passed` in `tests/test_terminology.py tests/test_config.py tests/test_data_safety.py` | `137 passed` in the same targeted suite; census: `835 raw rules, 1 raw non-normalized side, 0 parsed non-normalized sides, Arabic-yeh source fires: True` |

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
  authoritative progress marker. P0-9 through P0-14, all P1/P2 items and all
  GUI changes remain outstanding at this revision.
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
