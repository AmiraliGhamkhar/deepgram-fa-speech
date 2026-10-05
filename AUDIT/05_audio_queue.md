# 05 — Module review: `medical_stt/audio/queue.py` (bounded queue, drop accounting)

Read in full: medical_stt/audio/queue.py L1–129 (129 lines). Cross-read:
medical_stt/app.py L268–303 (producer/consumer wiring), audio/__init__.py.

## What it holds

`DEGRADED_AFTER_DROPS = 3` (L19); frozen `QueueStats` (L22–41) with
`consecutive_drops` and `degraded`; `BoundedAudioQueue` (L44–129) wrapping
`queue.Queue(maxsize)` with lock-guarded counters.

## Findings

**F1 · Low · Any single successful `get()` resets the degraded streak, so
sustained *partial* loss never trips the health flag.** FACT:
queue.py:75–82 — `get()` sets `consecutive_drops = 0` on every dequeue, and
`degraded` is `consecutive_drops >= DEGRADED_AFTER_DROPS` (L98). A sender
that is intermittently slow (e.g. drops every other chunk — 50% audio loss)
interleaves gets between drops, so `consecutive_drops` oscillates 0→1→2→0 and
`degraded` stays False while `dropped_total` climbs. The operator signal then
degrades to WARNING-level logs (medical_stt/app.py:294–303) instead of the
ERROR + `session_health.degraded=True` path that Phase 9 of the hardening
spec intended for persistent loss. The counter `dropped_total` is still
correct and observable, so this is a signal-fidelity defect, not data hiding.
Fix (suggestion only): trip `degraded` on a *rate* (drops / puts over a
sliding window) or on `dropped_total` growth per N sends, and let a success
only pause, not clear, the streak.

**F2 · Info · Comment/semantics drift in `get()`.** FACT: L79–80 comment says
"The sender has drained one chunk, so it is keeping up again" — a `get()` is
a dequeue, not a successful *send*; the chunk can still fail at
`send_media()` (app.py:389–395 exits the sender on ProviderError, leaving the
reset state behind). Cosmetic given F1's fix would replace this anyway.

**F3 · Info · `drain()` counts as `drained_total`, not `dropped_total`**
(L101–118) — correct distinction: drained chunks are deliberate shutdown
discards (app.py:305–318), not quality loss. Good design; documenting so the
next reviewer doesn't "fix" it.

## Correct in this module (verified)

- Bounded: `queue.Queue(maxsize=40)` (constructed at app.py:151); no growth
  path; counters are fixed-size ints.
- Producer path stays non-blocking: `put_nowait` never raises into the
  callback (L55–73); on overflow it returns False and the callback only sets
  a flag (app.py:283–284).
- Lock scope is correct: queue lock only guards counters; `self._q.put_nowait`
  is called outside it (L60), so the audio thread never holds the counter
  lock while blocking on the queue.
- `stats()` is a consistent snapshot under one lock (L87–99).
- `drain()` prevents stale-audio burst across sessions (L103–110 docstring
  matches app.py usage).

## Summary (3 lines)

Bounded, non-blocking, well-accounted queue with a deliberate health flag.
One real gap: the degraded streak resets on any dequeue, so intermittent-but-
persistent audio loss stays below the ERROR/degraded threshold. Everything
else (lock scope, drain semantics, bounded state) is correct.
