# Engineering checkpoint — 2026-10-25 (full-prompt reconciliation)

## Scope and actual baseline

The supplied prompt references `887a1f8`, but this session starts at `0544e35`
(PR #27 merged). P0-1…P0-14 already exist, with regressions and historical
before/after evidence in the v1/v2 checkpoints. They are preserved, not
reimplemented or recommitted. The v2 P1/P2 batch used an inferred list, not
this full prompt; its blanket completion statement does not cover all the
requirements supplied here. This checkpoint records the remaining deltas.

Observed baseline: `1162 passed, 4 skipped, 1 warning in 58.42s`.
Tools installed from unchanged `requirements/dev.lock` in ignored `.venv`.

## Per-fix regression evidence

| Scope | Before | After |
|---|---|---|
| P1-2/5/7/8/9/10 remaining client gaps | New `test_remaining_client.py`: 21 failures; dedupe fixture corrected to use an unendpointed first segment, then separately verified failing against HEAD | 138 passing across remaining-client, accumulator, ASR, performance and provider-protocol suites |

- P1-2: deliberately sticky lost-audio warning documented and pinned: recovering
  queue throughput cannot recover missing clinical words. No RT work added.
- P1-5: both increment and read of connection count take the connection lock.
- P1-7: one bounded `_display_interim` normalization helper on both paths.
- P1-8: rotate the utterance tracker before a possibly failing queue post.
- P1-9: unequal word counts no longer dedupe on outer boundaries alone.
- P1-10: Persian unit guard handles joined, ZWNJ and spaced compound forms
  on both source and target sides, retaining the standalone Latin guard.
- P1-15: enqueue-streak comment corrected; settings document Retry-After cap.

No existing pins changed in this batch. Runtime dependencies unchanged.

### P1 host/deployment batch

| Scope | Before | After |
|---|---|---|
| P1-11 bind validation/settings reuse, P1-12 exclusive secrets/count policy, P1-13 terminal logs | host-app/audit suites: 11 failed, 68 passed | 79 passed, accepted warning |
| P1-14 deployment defaults and Markdown table | host-docs: 2 failed, 19 passed | host-docs + docs-polish: 26 passed |

Registry replacement already fsyncs the file before replace (preserved);
now also fsyncs the parent directory on POSIX. Secret files use O_EXCL and
O_NOFOLLOW where available, mode 0600, fsync; newly-created secret dirs 0700.
Existing secret files are refused, never truncated. Registry replacement is
crash-atomic, not a multi-process transaction: provision from one writer.
Bind accepts IPs/hostnames, rejects blank/URL/whitespace forms without logging
raw values. Main reuses `app.state.settings`, so config warnings occur once.
Every post-start refusal now emits fixed reason/status with its session id;
body cancellation logs terminal failure before re-raising.

Deliberately updated pin: `test_main_says_so_when_plaintext_is_deliberate`
now changes the already-parsed settings instead of changing the environment
after import (main no longer reparses it). Existing production guards remain.

### P2 explicit list (one batch)

Before: remaining-polish + provider-protocol suites **9 failed, 37 passed**.
After (including updated metric pin): full Phase 1 validation below.

- Uptime emits TYPE gauge; status-labelled HTTP outcomes no longer have an
  unlabelled zero series. Cancellation uses numeric conventional `499` (not an
  actual response) rather than `status="cancelled"`; authenticated per-client
  outcome `cancelled` remains unchanged.
- Clamp warnings identify TTL/body setting names only; obsolete rate setting
  marked deprecated. Retry-After's delta-seconds-only parser documented.
- Passenger limitation explicitly documented (allowed alternative to a new
  watchdog): startup smoke check only; an exited/stalled loop needs external
  health timeout + worker restart; daemon shutdown has no join/loop.close.
- NFKC superscript/compatibility folding documented; fraction slash mapped.
- Legacy streaming-backspace API marked public-API-only. App sends unmarked
  text to injection; type mode opts into a single mark at its boundary, paste
  keeps its existing boundary. Raw public type/delta callers are unchanged.
- Positive jitter accepts an injectable RNG. Stereo uses documented
  `multichannel`, not ignored `channels`.
- Host-client socket timeout vs 25 s session-readiness deadline documented;
  a dribbling fetch can outlive the session on its fenced daemon thread.
- Existing keep-alive cross-reconnect worker bound/comment retained: a
  stalled worker prevents another keep-alive until it dies. No P0-2 changes.

Updated pin: `test_new_metrics_preseeds_counters_and_gauges_but_not_histograms`
now explicitly excludes the status-labelled counter from unlabelled zeros.

## Phase 1 full validation (observed)

```
pytest tests/ -q: 1205 passed, 4 skipped, 1 warning in 59.56s
ruff check .: All checks passed!
mypy medical_stt host benchmarks: Success: no issues found in 44 source files
python scripts/scan_secrets.py: clean: no credentials found in the working tree
note: 1 baseline entry matched nothing (existing scanner advisory)
```

Warning unchanged: Starlette/httpx TestClient deprecation; no filters or locks
changed. Live/load/soak gates untouched. Native Windows/Tk visual acceptance
is not claimed in this headless Linux environment.

## Phase 2 R2 completion: actual-DPI Tk scaling

Shared font preference resolution and before-Tk process awareness were already
implemented in PR #27; preserved. Added one best-effort scaling helper called
on each window's owning thread after creation, before widget build, querying
GetDpiForWindow with Tk's pixels-per-inch fallback, scaling = DPI/72.
Before: two new scaling tests failed with AttributeError (missing helper).
After: fonts/control/overlay suites 57 passed. Fake-DLL awareness pin also
passes; existing before-Tk ordering test preserved. No existing pins changed.

### Phase 2 R1 completion: uniform circle mechanics

Before new regressions: control suite **3 failed, 35 passed**. After:
**38 passed**; ruff/mypy clean.

All five secondary actions (including recovery copy/clear) now use the same
52px Canvas helper, with a glyph above an 8pt caption. Primary remains 72px,
quit 20px. One reusable painter owns hover, .8 press-darken/release restore,
2px outer state ring (BORDER / recording REC), independent dashed keyboard
focus ring and 50% palette-derived disabled colors with arrow cursor.
Destroyed circles release their Python bookkeeping. Click tags, Return/Space,
takefocus and no-stretch geometry preserved. Start/Stop is now green/red,
not green/blue. Disabled clicks post feedback, not commands.

Updated pins in this commit:
- `test_set_button_repaints_the_circle`: Stop color REC, not ACCENT.
- `test_recovery_popup_is_themed_modal_and_confirms_copy`: circle copy control,
  no automatic copy, explicit click still copies and confirms; modal/read-only
  behavior unchanged.
- Added exact requested `test_secondary_buttons_share_the_circle_style` across
  main and popup controls. Existing RTL row and drag pins preserved.

### Phase 2 R3 completion: alignment, busy affordances, drag coverage

Before new regressions: control suite **7 failed, 38 passed**. After:
**45 passed**, with all original drag/A3 guard pins still green.

- Client id right-justified; URL/secret remain LTR, masked/write-only secret.
- Settings labels and separator added to drag handles. Existing press-origin
  math unchanged; toplevel bindtag explicitly ignores events from entries or
  circles (otherwise Tk bubbles descendant clicks to the root drag binding).
- Host fields and Save/Test circles visually disable on starting/running/
  stopping/saving; restore on idle. Test-in-flight disables Test only; Save
  remains independent (A3.4). Disabled clicks run existing refusal guards to
  explain why. Recovery stays available for explicit review.
- Popup uses shared font and a full-range right-justification Text tag (Tk
  Text has no widget-level justify option), retaining read-only/modal/copy
  behavior. Single STATUS_DOT constant; Persian status text unchanged.
- Opaque control and overlay translucency preserved from PR #27.

No existing pins weakened or deleted; new tests cover each remaining delta.

### Final widget-tree acceptance description (headless, not a screenshot)

```
Tk [opaque, borderless, topmost, shared Persian font, actual-DPI scaling]
  Frame [BG, padded]
    Header [draggable]
      Canvas quit [20x20, ×, hover REC, keyboard focus]
      Title [10pt bold, right]
    Status [10pt, right, STATUS_DOT + unchanged phase text]
    Canvas Start/Stop [72x72, centered, green/red, ON_LIGHT 12pt bold]
      oval [2px BORDER / REC ring; hover / press fill]
      text [same click tag]
      focus oval [dashed, shown only on keyboard focus]
    Detail [8pt, right]
    Separator [draggable]
    Label/Entry host [draggable label; LTR entry]
    Label/Entry secret [draggable label; masked write-only LTR entry]
    Label/Entry client id [draggable label; right-justified entry]
    RTL action row
      Circle connection [52x52, ↗ + 8pt caption]
      Circle recovery [52x52, ≡ + 8pt caption]
      Circle save [52x52, ✓ + 8pt caption]
Recovery Toplevel [themed, transient, grabbed, Escape closes]
  Text [shared Persian 9pt, right-justified full-range tag, read-only]
  Feedback [8pt, right; copy confirmation]
  RTL row: Circle copy [52x52, ⧉ + caption], Circle clear [52x52, × + caption]
```

All secondary circles share SURFACE/BORDER hover, .8 dark press, dashed focus,
50% palette-derived disabled ink/fill and guarded feedback. Canvas geometry is
square, never stretched; interactive widgets are excluded from dragging.

### Phase 2 full validation (observed)

```
ruff check .: All checks passed!
mypy medical_stt host benchmarks: Success: no issues found in 44 source files
python scripts/scan_secrets.py: clean: no credentials found in the working tree
note: 1 baseline entry matched nothing (existing scanner advisory)
```

Full pytest console output:
........................................................................ [  5%]
........................................................................ [ 11%]
........................................................................ [ 17%]
........................................................................ [ 23%]
........................................................................ [ 29%]
........................................................................ [ 35%]
........................................................................ [ 41%]
........................................................................ [ 47%]
........................................................................ [ 53%]
........................................................................ [ 58%]
........................................................................ [ 64%]
..............ss.s...................................................... [ 70%]
........................................................................ [ 76%]
........................................................................ [ 82%]
........................................................................ [ 88%]
..........................s............................................. [ 94%]
......................................................................   [100%]
=============================== warnings summary ===============================
.venv/lib/python3.11/site-packages/fastapi/testclient.py:1
  /home/user/deepgram-fa-speech/.venv/lib/python3.11/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
1218 passed, 4 skipped, 1 warning in 61.65s (0:01:01)

P0 census rerun: **835 rules, 9 digit-bearing/numeric-span sources, loader OK,
0 rejected**; **1 raw non-normalized side, 0 parsed non-normalized sides**,
Arabic-yeh source fires on normalized Persian. Historical P0 before/after
per-fix evidence remains in the v2 checkpoint; no duplicate P0 work needed.
