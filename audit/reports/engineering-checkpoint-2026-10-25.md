# Engineering checkpoint — 2026-10-25

## Scope: PARTIAL, not final acceptance

Completed Phase 1 P0-1 through P0-6 in priority order, one commit per fix.
Not implemented: P0-7 through P0-14, all P1/P2 items, and all Phase 2 GUI changes.
Next work item: P0-7 (numeric-span boundaries, Persian units and rule census).
No GUI widgets have changed, so there are no new screenshots/widget-tree changes.
No live Deepgram, microphone or native Windows input tests were run.

## Per-fix evidence

All “before” runs executed new regression tests against the original implementation
before editing production code; failures were observed, not inferred.

| Item | Commit | Before | After targeted suite |
|---|---|---|---|
| P0-1 SDK/UI exception isolation | c5840a6 | 4 failed: overload, interim, error, warning | 44 performance tests passed |
| P0-2 nonblocking keep-alive | cd56571 | 1 failed: stalled send blocked caller for 3s | 10 watchdog tests passed; caller bound <200ms; one worker while stalled |
| P0-3 modifier snapshot, including Ctrl | ce1a1d5 | 5 failed: Shift/Alt/left Win/right Win/Ctrl | 16 clipboard-policy tests passed |
| P0-4 failed clipboard writes | 48e3710 | 4 failed: allocate/lock/set failures and restore logging | 20 clipboard-policy tests passed |
| P0-5 multiline ZWNJ | 20ccf4c | 1 failed: newline preservation | 25 normalization tests passed |
| P0-6 normalization idempotence | 16efd55 | 1 failed: corpus/combinatorial property | 26 normalization tests passed |

P0-5's commit message mistakenly states 34 normalization tests; actual output was 25.
P0-6 checks all medical YAML corpus inputs and 216 punctuation/ZWNJ combinations.

## Deliberately updated pins

- `test_mailbox_overload_is_explicit` replaced with
  `test_mailbox_overload_drops_oldest_without_raising`.
- The two modifier async-state recheck pins were replaced with a five-case snapshot
  pin that actually applies synthetic keyups to the fake async state. Async state
  cannot distinguish physical release from synthetic input. The tests establish
  orchestration policy only, not native Win32 ABI/timing correctness.
- No security tests were weakened; no runtime dependencies or lockfiles changed.

## Remaining acceptance limitations

- Zero warnings is NOT met: the locked FastAPI/Starlette test client emits a
  deprecation warning. It is preserved, not filtered. No lockfile was edited.
- The secret scanner passes, but notes an unmatched pre-existing history baseline
  entry. Neither that baseline nor Git history was changed.
- Native Windows modifier/clipboard behavior still requires gated Windows
  selftests. This Linux checkout cannot establish Windows ABI correctness.
- Historical-key owner action from SECURITY.md remains: revoke the exposed key
  in the Deepgram console and follow the owner-managed history cleanup procedure.
  No history rewrite or key-management operation was attempted here.

## Full validation output

Commands used `.venv/bin/` tools installed from the existing hash-locked dev and
host requirements. No dependencies were added to the project.

### pytest tests/ -q

```text
........................................................................ [  6%]
........................................................................ [ 13%]
........................................................................ [ 20%]
........................................................................ [ 27%]
........................................................................ [ 33%]
........................................................................ [ 40%]
........................................................................ [ 47%]
........................................................................ [ 54%]
........................................................................ [ 61%]
........................................................................ [ 67%]
...ss.s................................................................. [ 74%]
........................................................................ [ 81%]
........................................................................ [ 88%]
.............................s.......................................... [ 95%]
...................................................                      [100%]
=============================== warnings summary ===============================
.venv/lib/python3.11/site-packages/fastapi/testclient.py:1
  /home/user/deepgram-fa-speech/.venv/lib/python3.11/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
1055 passed, 4 skipped, 1 warning in 46.34s
```

### ruff check .

```text
All checks passed!
```

### mypy medical_stt host benchmarks

```text
Success: no issues found in 43 source files
```

### python scripts/scan_secrets.py

```text
clean: no credentials found in the working tree
note: 1 baseline entry/entries matched nothing -- the history looks scrubbed. Remove them from secret_scan_baseline.txt so the check stays meaningful.
```

