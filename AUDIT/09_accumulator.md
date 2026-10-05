# 09 — Module review: `stt/accumulator.py` (finalized-segment collector)

Read in full: stt/accumulator.py L1–105. Cross-read: medical_stt/app.py
L201–240 (caller), deepgram_provider.py L261–268 (UtteranceEnd path).

## What it holds

`UtteranceAccumulator`: buffers `is_final` segments until `speech_final`
(or a synthetic UtteranceEnd event), then `flush()` joins them into one
utterance; duplicate suppression only when the same acoustic span is provably
re-sent (`_SPAN_TOLERANCE_SECONDS = 0.001`).

## Findings

**F1 · Info · Confidence averaging weights every segment equally** (L60–68):
the flushed utterance's `confidence` is the unweighted mean of segment
confidences; short segments count as much as long ones. Downstream
(medical_stt/app.py:213–225) only checks *word-level* confidences for the
medical-token gate, so the utterance-level mean is currently unused for any
safety decision — no impact, just dead-ish data.

**F2 · Info · Duplicate check compares only against the last segment**
(L88–105). A provider that re-sends an *older* segment after the endpoint
would duplicate it in the joined text. No evidence Deepgram does this (its
retry behavior re-sends the in-flight segment), so recorded as a documented
assumption rather than a defect.

## Correct in this module (verified)

- Legitimate repetition survives: identical text with different (or absent)
  word timings is kept (docstring L1–12, code L88–105) — the "بله twice"
  case is explicitly protected. This is the clinically important property:
  repeated dosages/measurement dictations must not be deduplicated away.
- Provably-identical acoustic spans are dropped: same word count requires
  per-word start/end match within 1 ms; different word counts compare first
  start and last end (L16–36).
- No timings at all → only the identical event *object* is treated as a
  repeat (L103–105), i.e. default is keep.
- `flush()` swaps out the segment list before building the result (L54–69),
  so re-entrant adds during processing cannot corrupt state; `reset()` clears
  both fields.
- Per-session lifecycle: a fresh accumulator per `LiveMedicalSTT`
  (app.py:160) and reset between sessions (app.py:313) — no cross-session
  bleed; the pending utterance is flushed at shutdown (app.py:424–426) so the
  last dictation is not lost.
- The UtteranceEnd synthetic event (provider L263–265) arrives as
  `is_final=True, speech_final=True, text=""` → `add()` skips the empty text
  (L26–29 `if text and ...`) and flushes — correct handling of the no-text
  endpoint marker.

## Summary (3 lines)

Small, conservative, and clinically safe: repetition is preserved unless the
provider provably re-sent the same acoustic span; state is swapped atomically
and reset per session. No defects; two documented assumptions (last-segment-
only duplicate check, unweighted confidence mean) with no current impact.
