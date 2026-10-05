# 18 — Domain review: Deepgram parameters, Persian handling, WER harness, cost

Sources: medical_stt/stt/deepgram_provider.py L189–260 (connection params),
medical_stt/config.py (defaults L150–160, `_LANGUAGE_MODEL_SUPPORT` L61–64,
`validate_model_language` L107–124), medical_stt/processing/normalize.py
L28–60 (digit/separator tables), benchmarks/metrics.py + corpus.yaml
(detail in module 16), Deepgram official docs
(developers.deepgram.com/docs/models-languages-overview, fetched 2026-10-05)
and the Nova-3 Persian launch announcement (deepgram.com/learn, 2026-02-12).

## The central domain question — is `nova-3` + `fa` real?

**Yes, verified against Deepgram's own model/language matrix:** `fa`
(Persian) is listed under `nova-3`/`nova-3-general` among the monolingual
languages, launched 2026-02-12 as a *production-grade monolingual model with
streaming and batch*. The repo hard-pins exactly this:
`_LANGUAGE_MODEL_SUPPORT = {"fa": frozenset({"nova-3"})}` (config.py:64) and
`validate_model_language` refuses any other pair with an actionable CONFIG
error naming the validated configuration. Two consequences worth recording:

1. **`nova-3-medical` and `nova-3-pharma` are English-only** (`en` +
   regional variants per the same matrix). A Persian medical dictation app
   *cannot* use Deepgram's medical model. The repo's architecture — general
   `nova-3` + Keyterm Prompting (`keyterm` param, phrases allowed, fed from
   `data/keyterms/*.yaml`) + a deterministic client-side FST terminology
   engine — is therefore not a workaround but the *correct* design for this
   language/model combination. `keyterm_parameter()` picks `keyterm` for
   nova-3 and `keywords` for older models, matching Deepgram's "Nova-3
   rejects `keywords`" behavior (provider docstring L10–12, L202–216).
2. The pin doubles as a safety net: if Deepgram changes model names or `fa`
   moves, the client fails at Start with a clear message instead of opening
   a socket that would be rejected mid-handshake.

## Streaming parameters (defaults, config.py:150–160 + provider L242–254)

`model=nova-3, language=fa, encoding=linear16, sample_rate=16000,
channels=1, interim_results=True, endpointing=400ms,
utterance_end_ms=1200ms, vad_events=True, smart_format=True,
punctuate=True, replace=<asr_replacements>, keyterm=[...]`.

All sane for dictation: 80 ms blocks (`block_duration=0.08`) → 12.5
messages/s per session, ×50 sessions ≈ 625 msg/s upstream — ordinary WS
load; 16 kHz mono linear16 = 32 kB/s per client uplink; endpointing 400 ms
is responsive for dictated phrases while `utterance_end_ms=1200` catches
endpoint-suppression cases (UtteranceEnd is handled at provider L256–261,
emitting an empty final event so the accumulator closes the segment).

## Findings

**F1 · Info · smart_format/punctuate behavior for `fa` is defensively
handled rather than assumed.** The event converter takes
`punctuated_word or word` (provider L113), so if Deepgram's smart-format
does not emit punctuated words for Persian, nothing breaks; the downstream
deterministic pipeline owns final formatting anyway
(`test_formatting_ownership.py` guards this boundary).

**F2 · Info · Digit normalization covers the real-world ASR variance.** ASR
may return Persian digits (۰–۹) *or* Arabic-Indic digits (٠–٩) and Arabic
separators (U+066B decimal, U+066C thousands, U+066A percent). normalize.py
maps both digit scripts to ASCII and the separators to `.`/`,`/`%`
(L44–60) *before* the numeric-protection stage — so clinical numbers are
protected and scored on ASCII regardless of which script Deepgram returns.
Verified present and ordered before `processing/numbers.py` in the pipeline.

**F3 · Info · Cost model is plan-dependent and the repo says so.** Token
grants (`/v1/auth/grant`) are not metered per call; the billable surface is
streamed audio minutes — 50 concurrent clinicians ≈ 50 audio-minutes per
wall-clock minute billed to the operator's Deepgram project. README's
capacity section explicitly scopes the claim: "Whether your Deepgram project
permits 50 simultaneous Nova-3 streams is a property of your plan" and the
live-load test separates `APPLICATION CAPACITY` from `PROVIDER CAPACITY`.
Honest; nothing to fix. (Reconnects re-mint a token but do not double-bill —
billing follows audio, and a reconnect re-sends only from the live mic.)

**F4 · Info · WER harness is regression-sized, not acceptance-sized — and
refuses to pretend otherwise.** 22 corpus cases across 11 clinically
targeted categories; metrics are defined to be hand-computable (WER/CER
post-NFC whitespace tokens; numeric accuracy requires *verbatim* presence of
pipeline-protected expressions, so 120/8 vs 120/80 cannot average away;
negation and medical-term recall; terminology rewrites applied to the
reference so deliberate `آی سی یو → ICU` rewrites don't count as errors).
No baseline WER is shipped anywhere — the harness docstring states this in
bold. For a clinical aid, adding recorded-audio baselines per specialty
would be the highest-value future contribution, but that requires audio the
repository deliberately does not contain.

## Correct in this module (verified)

- Fresh short-lived token per connection, requested at connect time — never
  a stale token on reconnect (provider L230–238 with explicit rationale).
- `keyterm` values come from curated YAML (general + 4 specialties), not
  free-text user input; `replace=` passes the audited `asr_replacements.yaml`.
- `_recognition_hint_parameters()` returns `{}` when no keyterms — no empty
  parameter noise to the API.
- Mixed Persian/English dictation is a first-class corpus category
  (`mixed_language`), and `english_term_recall` scores Latin-token survival
  (SpO2, ICU) separately from WER — the right lens for code-switching
  medical speech.

## Summary (3 lines)

The domain choices check out end-to-end against Deepgram's current matrix:
`fa` is a production-grade monolingual nova-3 language, `nova-3-medical` is
English-only so Keyterm Prompting plus the local FST engine is the correct
Persian medical architecture, and every streaming default is defensible for
50-session dictation. Persian-script digit/separator variance is normalized
before numeric protection, and the WER harness is honest about being a
regression tool rather than an accuracy claim.
