# 04 — Module review: `medical_stt/stt/deepgram_provider.py` (Deepgram WS)

Read in full: medical_stt/stt/deepgram_provider.py L1–329 (329 lines).
Cross-read: stt/base.py, stt/reconnect.py (module 08), config.py parameters
(module 11).

## What it holds

`classify_deepgram_exception` L39–94 (status → ErrorCategory taxonomy);
`transcript_event_from_result` L100–144; `DeepgramProvider` L148–329: fresh
short-lived token per connection (L189–213), `listen.v1.connect` parameters
L241–254, event handlers L261–281, `send_audio` L308–317, `stop` L319–328.

## Findings

**F1 · Medium · `send_audio()` sends without holding the connection lock —
untracked race with `stop()`.** FACT: deepgram_provider.py:308–314 copies
`self._connection` under `_connection_lock`, then calls
`connection.send_media(chunk)` at L314 outside the lock, while `stop()`
(L319–328) concurrently finalizes/closes the same object. HYPOTHESIS (SDK
internals not read): the SDK send on a mid-close socket raises
`websockets.ConnectionClosed`; the exception *is* correctly classified at
L316–317 into a `ProviderError`, which the sender thread turns into session
stop (medical_stt/app.py:389–395). So impact is bounded to a spurious
error-recorded stop when Stop races a chunk send. The lock-free read is
deliberate (holding a lock across network send would block `stop()`), so the
right fix is a generation/stopped check rather than re-locking.

**F2 · Low · `stop()`'s exception scope too narrow (see module 03 F1).**
FACT: L326–328 `except (OSError, RuntimeError)`. A
`websockets.exceptions.WebSocketException` from the close path propagates
into `LiveMedicalSTT._run_session`'s `finally` (app.py:422) and defeats the
clean shutdown accounting. Closing is best-effort; catching `Exception` here
cannot hide a real problem (the connection is being discarded either way).

**F3 · Low · Silent drop of malformed words.** FACT: L123–125 — words whose
`start`/`end` are missing or non-numeric are skipped with `continue`. The
utterance text still flows (text-based processing is unaffected), but
confidence gating (module 10) sees fewer words than were spoken. No log line
marks this, so a systematic provider-format change would be invisible.

**F4 · Info · No application-level keepalive/ping.** FACT: no
`KeepAlive`/ping message anywhere in the module (grep: no "keep"). Deepgram
closes idle streams (~10 s silence without endpointing configured otherwise);
this deployment always sends endpointing/utterance_end and real audio, so
keepalive absence is benign for dictation use. Noted because a muted
microphone + no keepalive would produce a SERVER_DISCONNECT reconnect cycle.

**F5 · Info · `_connections_opened` is instance-local by design** (L152–156)
— correct for per-session isolation; the property (L300–307) exists for tests.

## Correct in this module (verified)

- Fresh short-lived token per connection, requested *inside* `start()` so a
  reconnect cannot reuse a stale token (L191–213); token never logged — the
  only log line records `session_id` + `expires_in` (L207–211) — and the
  local reference is cleared in `finally` (L293–297).
- Parameter set matches the intended ASR: Nova-3 via `s.model`, `language`
  from config, `encoding=linear16`, explicit `sample_rate`/`channels`,
  `interim_results=True`, `endpointing`, `utterance_end_ms`, `vad_events`,
  `smart_format`, `punctuate`, server-side `replace` (L241–254). Exact
  config values are checked in module 11; model/parameter mismatch is
  pre-validated by `validate_config` (L216–231).
- Error taxonomy: 401/403→AUTH, 429→RATE_LIMIT, other 4xx→CONFIG,
  5xx→SERVER_DISCONNECT (L44–55), websockets closure types, socket timeouts
  (L57–91) — matches base.py categories consumed by the reconnect policy.
- `_on_close` reports SERVER_DISCONNECT only when the stop was not requested
  (L274–277) — a clean Stop does not fabricate an error.
- Per-connection state is instance-local; no module-level mutable provider
  state exists (module grep).

## Summary (3 lines)

Provider is disciplined: fresh bearer token per connection, clean error
taxonomy, no state shared across sessions, secrets never logged. Two defects
around the send/close race: `stop()` catches too few exception types (escapes
as raw exception), and `send_audio` sends outside the lock (bounded impact,
spurious stop on race). Malformed word metadata is dropped silently.
