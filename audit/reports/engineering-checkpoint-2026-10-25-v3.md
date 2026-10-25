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
