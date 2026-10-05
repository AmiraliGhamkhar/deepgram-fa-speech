# 08 — Module review: `stt/reconnect.py` + `stt/base.py` (policy + taxonomy)

Read in full: stt/reconnect.py L1–129, stt/base.py L1–121. Cross-read:
medical_stt/app.py L430–484 (the caller), host_client.py mapping (module 07).

## What it holds

`ReconnectPolicy` (base 2.0 s, cap 30 s, jitter 0.5, `rate_limit_delay` 5 s,
`max_retry_after_seconds` 60) and `decide()` (reconnect.py:30–120);
`ErrorCategory` + `is_retryable`, `ProviderError(retry_after)`,
`TranscriptEvent`, `STTProvider` ABC (base.py:15–121).

## Findings

**F1 · Low · Retry-After fully bypasses the RATE_LIMIT floor.** FACT:
reconnect.py:99–108 — when a hint is present, the decision returns
`honored = min(retry_after, 60)` immediately, before the
`max(delay, policy.rate_limit_delay)` floor at L111–113. A host that answers
429 with `Retry-After: 1` gets a 1-second retry cadence even though
`rate_limit_delay=5.0` was chosen to keep RATE_LIMIT retries slow. Honoring
the server is the right default for a *correct* server; the floor was meant
as a guard for a wrong one. Whether this is a defect is a judgment call —
documented here because the two mechanisms contradict each other and only
one can be intended. Caller always passes `error.retry_after`
(app.py:464–468), so the hint path is live whenever the host sends the
header (the host does — host/app.py:429 headers, module 01).

**F2 · Info · `_default_rng` is the `random` module, shared process-wide**
(reconnect.py:24, 116–127). Fine for its purpose (decorrelation across
sessions in separate processes); within one process two concurrent sessions
share the RNG but still draw independent samples. No lock needed for
`random.uniform` correctness in CPython (GIL-atomic enough for jitter).

**F3 · Info · `ConnectionClosed(ProviderError)` (base.py:63–65) is exported
(stt/__init__.py:11,26) but never raised anywhere** (repo grep). Dead code
in the taxonomy module.

## Correct in this module (verified)

- Non-retryable categories (AUTH/CONFIG/SHUTDOWN) short-circuit regardless of
  attempt count (reconnect.py:83–85; base.py:41–43) — matches the
  SessionController's strict-start design.
- Full-jitter bounded backoff: delay drawn from
  `[capped*(1-jitter), capped]` (L116–127), so clients failing together
  spread across a window instead of clustering (docstring L4–17 is accurate
  to the code).
- `max_attempts` enforced before delay computation (L87–89); attempt count is
  1-indexed and the caller increments per reconnect (app.py:460–462).
- Jitter clamped to [0,1] (L121) — a config typo cannot produce a negative
  floor.
- The storm-resistance design (per-process RNG, no shared retry schedule) is
  real: nothing in the module coordinates clients; `retry_after` is the only
  cross-client coupling and it comes from the host, which rate-limits
  per-client (modules 01–02).
- `TranscriptEvent`/`WordInfo` are frozen dataclasses — no mutable shared
  state between sessions.

## Summary (3 lines)

Backoff policy is correctly built for storm resistance (bounded full jitter,
per-session randomness, hard stops for AUTH/CONFIG/SHUTDOWN, clamped
Retry-After). One internal contradiction: a present Retry-After skips the
RATE_LIMIT minimum delay the same policy defines. `ConnectionClosed` is dead
code.
