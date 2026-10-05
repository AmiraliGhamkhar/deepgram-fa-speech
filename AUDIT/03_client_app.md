# 03 — Module review: `medical_stt/app.py` (client session lifecycle)

Read in full: medical_stt/app.py L1–652 (652 lines). Cross-read: ui/overlay.py
L1–40 + threading calls (module 13 pending), audio/queue.py API, stt/* modules.

## What it holds

`LatencyTracker` L65–119 (durations only, no transcript text);
`LiveMedicalSTT` L122–484 (session wiring: capture → queue → sender → provider
→ transcript callback → processing → injection); `SessionController`
L487–560 (Start/Stop lifecycle, strict on config errors); logging setup
L563–591 (1 MB rotating file, 3 backups — bounded logs); `main()` L610–652
(single-instance guard, ControlWindow).

## Findings

**F1 · Low · `provider.stop()` can raise an uncaught non-`ProviderError`
during a normal Stop.** FACT: medical_stt/app.py:422 calls
`self.provider.stop()` in the `finally` block of `_run_session`. FACT:
deepgram_provider.py:326–328 wraps `send_finalize()`/`send_close_stream()` in
`except (OSError, RuntimeError)` only. If the server disconnects at the same
moment the user presses Stop, the SDK raises a `websockets` exception
(inherits `Exception`, not OSError/RuntimeError) — HYPOTHESIS (SDK-internal
behavior not read): `send_finalize()` on a closing connection raises. That
exception escapes `_run_session`, is not caught by `run()`'s
`except ProviderError` (medical_stt/app.py:454), surfaces only via
`SessionController`'s broad handler (L537–540) → session reported FAILED for
a race the user did not cause. Fix (suggestion only): in `stop()`, catch
`Exception` for the close path — closing is best-effort by definition.

**F2 · Info · Cross-thread access to `LatencyTracker` without a lock.**
FACT: `mark_utterance_start()` is called from the sounddevice audio thread
(L276) while `mark_first_interim`/`mark_speech_final`/`mark_final` run on the
provider's transcript thread. Single-attribute reads/writes are GIL-atomic and
the tracker only records monotonic timestamps, so worst case is a slightly
misattributed latency sample — benign, but undocumented.

**F3 · Info · `time.sleep(0.08)` on the transcript callback thread (L260).**
FACT: `_finalize_utterance` sleeps after every injected utterance to pace
injection. This delays the next transcript event by up to 80 ms on the
provider thread (not the audio path — the sender thread is separate, so no
audio loss). Deliberate pacing; worth documenting as a latency budget item.

**F4 · Info · `last_error = str(exc)` (L539)** stores arbitrary exception
text for the UI. Audited error sources (config, host client — module 07) do
not embed credentials; ProviderError messages for send failures include raw
SDK exception text (deepgram_provider.py:316–317), which per SDK behavior
does not carry the bearer token. HYPOTHESIS: no secret can reach the UI;
flagged for the domain re-check.

**F5 · Info · `_errors` list is cleared at session start and the first error
wins.** FACT: L340 `self._errors.clear()`; L429 `raise self._errors[0]`.
Correct for the reconnect design; no growth (bounded state).

## Correct in this module (verified)

- Capture callback stays O(1): no I/O, no logging, flag-then-log pattern for
  overflow/device status (L268–284 → L286–303); drops flip a `degraded`
  health state surfaced at L188–199 rather than being silently absorbed.
- Shutdown ordering is explicit and lossless: mic released by `with` →
  sender drains queue → `provider.stop()` → provider thread joined → pending
  utterance flushed (L403–427, comments L405–417); a stale-queue burst is
  prevented by draining on reset (L305–318).
- Reconnect: `decide()` honors `retry_after` (L464–468) and backoff sleep is
  interruptible by Stop (L476–478); SHUTDOWN category never retries.
- Per-session isolation: a new `LiveMedicalSTT` is built per Start
  (L524–526), so terminology engine, injector, accumulator, and queue are
  never shared across sessions.
- Logging is bounded (L580–582) and the latency logger logs durations only
  (L105–119); no transcript text at any log site in this file.

## Summary (3 lines)

Lifecycle/threading design is careful: lightweight capture callback, drain-
then-finalize shutdown, per-session object isolation, bounded logs. One real
defect: `provider.stop()`'s narrow exception scope can turn a
Stop-vs-disconnect race into a spurious FAILED state. Minor cross-thread
access and pacing sleeps are benign but undocumented.
