# Deepgram integration — multi-user, multi-request and concurrency analysis

Date: 2026-10-14. Analysed at commit `6491d64` (`main`). The review itself
was read-only; the findings were then fixed on the same branch -- see
[Remediation status](#remediation-status) at the end for what changed where.

**Method.** Every file on the Deepgram path was read in full (`host/core.py`,
`host/app.py`, `host/passenger_wsgi.py`, `medical_stt/stt/*`,
`medical_stt/host_client.py`, `medical_stt/app.py`, `medical_stt/audio/queue.py`,
`medical_stt/ui/control.py`, `medical_stt/config.py`). The pinned SDK
(`deepgram-sdk==7.9.0`) was downloaded and its WebSocket client inspected.
The repository's own evidence was executed: the full suite (776 passed) and
`scripts/measure_capacity.py` (all six criteria PASS). The behaviour was then
compared, item by item, with Deepgram's current documentation (sources in §9).
Where a number below was *computed* rather than read, the computation is shown.

---

## 1. Executive summary

The integration follows Deepgram's documented token-based pattern precisely:
the API key exists only on the host, every connection mints a fresh ~30 s JWT
via `POST /v1/auth/grant`, and the Windows client opens `wss://api.deepgram.com/v1/listen`
with `Authorization: Bearer <JWT>`. Audio never touches the host. The
per-session threading on the client and the async, bounded, identity-keyed
token path on the host are both well engineered and well tested.

The gaps are concentrated in what happens **after** the WebSocket is open and
in what the system **knows about the shared Deepgram concurrency pool**:

| # | Finding | Severity | Where |
|---|---|---|---|
| F1 | Deepgram close codes are never read. `1008 DATA-0000` (undecodable audio — a configuration fault) is classified `SERVER_DISCONNECT` and **retried forever**, and no close reason reaches the log, so the loop is undiagnosable from `app.log`. | High (robustness/diagnosability) | `stt/deepgram_provider.py:69-115, 380-394` |
| F2 | No `KeepAlive` and no "no audio captured" watchdog. A microphone that silently stops delivering frames produces Deepgram's `NET-0001` after 10 s, then an endless reconnect loop (~112 connections/hour) shown to the clinician as "اتصال مجدد…" instead of "microphone is silent". | High (UX/cost) | `app.py:455-636`, no `send_keep_alive` call anywhere |
| F3 | Nothing in the system knows Deepgram's **per-project** concurrent-stream cap (150 PAYG / 225 Growth-NA / 150 Growth-EU/AU/IN; **1** for secondary self-serve projects). User N+1 gets a 429 at the handshake, which the client treats as a generic rate limit and retries every ~22 s indefinitely with no distinct message. The README says "a property of your plan" but never states the numbers or the secondary-project trap. | High (capacity planning) | `host/app.py`, `README.md`, `host/README.md` |
| F4 | Per-client token-bucket burst of **120** is ~10× larger than needed (a client needs one token per Start). A single looping/compromised device can open 120 streams in seconds and exhaust the shared Deepgram pool for the whole clinic. The global burst (1200) is what protects the morning surge; the per-client burst should be small. | Medium | `host/core.py:122-131`, `host/app.py:140-150` |
| F5 | Regional endpoint is hard-coded to North America on both sides (`DEEPGRAM_GRANT_URL`, SDK default environment). Deepgram documents that keys *and tokens* work unchanged on `api.eu/.au/.in`. For a Persian-speaking deployment the EU region is likely closer, and data-residency may require it. | Medium | `host/core.py:44`, `stt/deepgram_provider.py:324` |
| F6 | No per-user attribution on the Deepgram side. All 50 clinicians share one key; `tag=`/`extra=` are not sent and `dg-request-id` is not captured, so a Deepgram request cannot be tied to a `client_id`/`session_id` for audit or cost allocation. | Medium | `stt/deepgram_provider.py:327-341` |
| F7 | Local validation is looser than Deepgram's contract: `utterance_end_ms` accepts 0–20000 (Deepgram: 1000–5000); keyterm cap is "100 terms" while Deepgram's hard limit is **500 tokens**; `UtteranceEnd.last_word_end == -1` is not ignored; empty audio frames are not guarded. None bites with today's data (33 keyterms ≈ 1.3 KB URL), all are latent. | Low | `config.py:454, 544`, `stt/deepgram_provider.py:369-372` |
| F8 | `RATE_LIMIT` retries can wait up to 45 s although `reconnect_backoff_max` is 30 s (positive jitter is capped by `max_retry_after_seconds=60`, not by `max_delay`). | Low | `stt/reconnect.py:106-108, 137-141` |

What is **correct and should be kept** is listed in §3 so the findings are not
read as a verdict on the whole design.

---

## 2. The system as implemented

### 2.1 Topology

```
 N × Windows client (one process per user session; local-namespace mutex)
 ┌───────────────────────────────────────────────────────────────┐
 │ Tk thread  │ stt-session │ stt-provider │ audio-sender │ transcript-worker │ PortAudio cb │
 └───────────────────────────────────────────────────────────────┘
        │ 1. POST /v1/session  (Bearer <device secret>, X-Client-Id)
        ▼
 ┌─────────────── host (ONE uvicorn worker, or Passenger via a2wsgi) ───────────────┐
 │ authenticate_client → client token bucket → global token bucket → body cap →      │
 │ inflight cap (min(100, pool 100)) → AsyncGrantClient.post(/v1/auth/grant, 7 s)    │
 └────────────────────────────────────────────────────────────────────────────────────┘
        │ 2. { access_token, expires_in, session_id, client_id }
        ▼
 client: DeepgramClient(access_token) → listen.v1.connect(...)   3. wss://api.deepgram.com/v1/listen
        │                                                            Authorization: Bearer <JWT>
        └── linear16 / 16 kHz / mono, 80 ms chunks ──────────────────────────────────▶ Deepgram
                                                               ◀── Results / UtteranceEnd / SpeechStarted
```

One Deepgram **project** (the host's key) is the concurrency domain for every
clinician. The host carries only token requests; it never sees audio.

### 2.2 Per-session timeline and time budgets

| Step | Where | Budget | Source of the budget |
|---|---|---|---|
| Token request to host | `host_client.py` | `host_timeout_seconds` = 10 s | client settings |
| ─ host reads body | `host/app.py` | 2 s | `HOST_REQUEST_BODY_TIMEOUT_SECONDS` |
| ─ host → Deepgram grant | `host/core.py` | 7 s whole-call deadline (5 s connect) | `HOST_GRANT_DEADLINE_SECONDS` |
| Token validity | Deepgram | 30 s from mint (max 3600) | Deepgram token docs |
| WebSocket handshake | SDK → `websockets.sync.connect` | **10 s** (`open_timeout`, library default — not configurable through the SDK call the repo makes) | websockets 17.2 |
| Readiness wait in app | `app.py:573-580` | `host_timeout_seconds + 12` = 22 s | code |
| First audio after open | mic opened *after* readiness, `app.py:587` | must be < **10 s** or Deepgram closes `1011 NET-0001` | Deepgram troubleshooting docs |
| Idle-connection liveness | websockets ping | 20 s interval / 20 s timeout | websockets defaults |
| Stop | `deepgram_provider.py:457-477` | `Finalize` + `CloseStream`, 1 s, then socket abort | code |

Worst-case token age at handshake completion ≈ 7 s (grant) + host→client
transfer + 10 s (`open_timeout`) ≈ 18–20 s, inside the 30 s TTL. On poor links
`session_ttl_seconds` can be raised (host clamps to 5…3600; Deepgram allows
1…3600).

### 2.3 Threads and ownership (client)

Per session: `stt-provider` blocks in the SDK's `start_listening()` (a sync
`for message in websocket` loop; the SDK emits `ERROR` then `CLOSE` in
`finally`), `audio-sender` drains the bounded queue and calls `send_media`
outside the connection lock, `transcript-worker` finalises/injects from a
bounded queue of 16, and the PortAudio callback only enqueues. A
`_session_generation` counter is stamped on every callback so a wedged thread
from session *N* cannot stop, error or feed session *N+1*. Up to three
abandoned workers are tolerated before Start is refused. Reconnects build a
**new** `DeepgramProvider` (`app.py:462-463`), so no socket state survives.

### 2.4 Concurrency controls (host)

| Control | Key | Default | Effect when exceeded |
|---|---|---|---|
| Admitted session requests | process | 128 | 503 `Retry-After: 1` (middleware) |
| Failed-auth lockout | peer IP (or client id without a peer) | 20 / 60 s fixed window | 429, charged **only** on failure |
| Per-client token bucket | `client_id` | burst 120, refill 60/60 s | 429 `Retry-After` |
| Global token bucket | `global` | burst 1200, refill 600/60 s | 429 `Retry-After` |
| Body cap | request | 4096 B, streamed | 413 |
| In-flight grants | process | min(100, pool 100) | 503 `Retry-After: 1` |
| Grant pool | process | 100 connections / 20 keep-alive, no redirects | — |
| Upstream errors | — | 400/401/403/404 → 502; 429/5xx → 503 (+`Retry-After`); timeout → 504 | client maps 5xx → retryable |

All of this state is per process; the docs say so and prescribe one worker.

---

## 3. Conformance with Deepgram's documentation

Legend: ✅ conforms · ⚠️ conforms today but fragile / under-specified · ❌ gap

### 3.1 Authentication and tokens

| Deepgram guidance | Repository | Status |
|---|---|---|
| Mint JWTs on a backend with `POST /v1/auth/grant`, `Authorization: Token <key>`; key needs **Member** or higher | `AsyncGrantClient.grant()` does exactly this; 403 is mapped to 502 with a "needs Member" note in `host/README.md` | ✅ |
| Default TTL 30 s, `ttl_seconds` up to 3600; keep it as short as the client allows | Default 30, host clamps 5…3600, body field name `ttl_seconds` (the documented copy-paste trap `ttl` is avoided) | ✅ |
| Generate at connection start, use immediately, handle expiry | Token requested inside `start()` immediately before `connect()`; a reconnect always mints a new one (`test_a_fresh_token_is_fetched_for_every_connection`) | ✅ |
| Token only needs to be valid for the handshake; the socket then lives on | Relied upon; documented in README | ✅ |
| Client sends `Authorization: Bearer <JWT>` | `DeepgramClient(access_token=…)`; the SDK overrides the header to `Bearer` (verified in `client.py` / `client_wrapper.py`) | ✅ |
| Temporary **API keys** are limited to 250/day; use token-based auth instead | The repo never creates temporary keys; `/auth/grant` has no published rate limit | ✅ |
| Usage of temporary tokens is attributed to the issuing key's accessor | True here — which is exactly why per-user attribution needs `tag`/`extra` (F6) | ⚠️ |

### 3.2 Concurrency and rate limits

| Deepgram guidance | Repository | Status |
|---|---|---|
| Limits are **per project**, not per key or account. Nova-3 streaming: PAYG **150** (all regions); Growth **225** NA / **150** EU, AU, IN; Enterprise negotiable. Multiple keys in one project share one pool | One key → one project → one pool for all clinicians: the right shape. The numbers appear nowhere in the repo; the README only says "a property of your plan" | ⚠️ (F3) |
| Secondary projects on a self-serve account are limited to **1 concurrent stream**; spreading traffic across projects violates ToS | Not mentioned. An operator who creates "a separate project for the clinic" on an existing self-serve account gets a 1-stream pool and every second clinician sees 429s | ❌ (F3) |
| Over the cap → `429 Too Many Requests`; use exponential backoff; PAYG/Growth limits cannot be raised | Handshake 429 → `ApiError(status_code=429)` → `RATE_LIMIT` → exponential, full-jitter backoff with a 5 s floor, unlimited attempts by default | ✅ mechanism / ❌ no admission control, no distinct user message (F3) |
| Rate-limiting middleware on your side to stay under the cap | Host limits *token requests*, not *streams*; it cannot know how many sockets are open | ⚠️ (F3/F4) |

### 3.3 Streaming protocol

| Deepgram guidance | Repository | Status |
|---|---|---|
| Send audio within **10 s** of opening or the socket closes `1011 NET-0001`; `KeepAlive` alone does not prevent this, at least one audio frame is required | Mic is opened right after readiness; first frame typically < 1 s. Nothing enforces or measures it | ⚠️ |
| Send `KeepAlive` every 3–5 s during silence; `NET-0002 no_audio_timeout` is reset by `KeepAlive` | Never sent. Acceptable while the mic streams silence frames; fails when the device stops delivering (F2) | ❌ (F2) |
| On an error Deepgram sends a Close frame with a code and payload: `1008 DATA-0000` (undecodable audio), `1011 NET-0000/0001/0002` | `ConnectionClosedError.rcvd.code/reason` are available (verified with websockets 17.2) but never read or logged; all map to `SERVER_DISCONNECT`, which is retryable | ❌ (F1) |
| Never send an empty byte frame (`b''`) — it causes unexpected closures | `send_audio()` does not guard `len(chunk) == 0`; PortAudio with a fixed blocksize never produces one, so latent | ⚠️ (F7) |
| Audio may be sent at most **1.25× realtime**; buffer audio during a reconnect and replay it | The queue (3.2 s) is **drained and discarded** on every session reset and the mic is closed during backoff. No replay, so 1.25× is never exceeded — but 2–30 s of dictation is lost per reconnect by design | ⚠️ documented trade-off |
| After a reconnect, word timestamps restart at 0; realign if you need continuity | Timestamps are used only for intra-session de-duplication; the accumulator is reset per session | ✅ n/a |
| `Finalize` flushes; `CloseStream` makes the server flush, send final results + metadata, then close | `stop()` sends `Finalize` then `CloseStream`, waits 1 s, and the provider thread keeps receiving for up to 4 s so the flushed finals are injected | ✅ |
| `utterance_end_ms`: 1000–5000, requires `interim_results=true`; ignore `UtteranceEnd` with `last_word_end: -1` | `interim_results=True` ✅; local validation accepts 0–20000 ❌; `-1` not checked (benign: the accumulator returns `None` when nothing is buffered) | ⚠️ (F7) |
| `dg-request-id` is returned on every handshake (and `dg-error` on failures) for support | Not captured; the SDK wraps failures in `ApiError` without response headers | ❌ (F6) |

### 3.4 Model, language and vocabulary

| Deepgram guidance | Repository | Status |
|---|---|---|
| Persian `fa` is a Nova-3 language; `nova-3-medical` is English-only | `model: nova-3`, `language: fa`, validated together; `nova-3-medical` is rejected for `fa` | ✅ |
| `keyterm` is Nova-3/Flux only; legacy models use `keywords`; repeat the parameter per term; no weights | `keyterm_parameter()` picks per model; the SDK encodes lists as repeated params (verified in `query_encoder.py`) | ✅ |
| Keyterm limit is **500 tokens** per request (not a term count); over-limit at connect returns an error | Cap is `MAX_KEYTERMS = 100` *terms*. Today general+specialty ≈ 29–34 terms, URL ≈ 1.0–1.3 KB — far under. Persian phrases tokenise to several tokens each, so 100 terms could exceed 500 tokens | ⚠️ (F7) |
| `smart_format` on non-English models applies punctuation + paragraphs; numerals only for select languages | Numbers/units are protected and normalised locally, so the design does not depend on Persian numeral formatting | ✅ |
| `replace` applies verbatim; keep find terms lowercase | One entry today, validated to carry no digits/units/Latin targets | ✅ |
| Regional endpoints accept the same keys **and tokens**; mid-stream `Configure keyterms` is NA-only | Region not configurable (NA hard-coded both sides); the repo does not use `Configure`, so no regional feature loss | ❌ (F5) |

---

## 4. Multi-user analysis (host)

**Identity.** Per-device `client_id` + 256-bit secret, SHA-256 at rest,
constant-time comparison, unknown ids pay a dummy compare. Legacy single-secret
mode maps to the `legacy` identity and is explicitly "not a 50-user
configuration". Correct.

**NAT-safe limiting.** Session limits are keyed on `client_id`; the only per-IP
limiter is for *failed* authentication and is charged only on failure
(`host/app.py:300-345`). `test_host_concurrency.py` proves 50 clients behind
one address with one abuser throttled alone. Correct.

**Blast radius of one device (F4).** With burst 120 / refill 1 s, a device in a
tight loop can mint 120 tokens in the first second and 3 600/hour after. Each
token is a potential Deepgram stream drawn from the *shared* pool. The
justification in `host/core.py:123-125` ("sized above the 50-clinician startup
surge") applies to the **global** bucket, not the per-client one: a clinician
needs one token per Start plus occasional reconnects. A per-client burst of
~10 with ~6/min refill still never 429s a legitimate user (the client's own
backoff saturates at 15–30 s between attempts) and limits one device to ~10
simultaneous streams instead of 120.

**Admission control (F3).** The host cannot see open streams, so it cannot
refuse user 151 (or user 2 on a secondary project) with a human message. Two
practical options, in order of cost:

1. *Lease accounting.* Treat each issued token as a lease of
   `HOST_SESSION_LEASE_SECONDS` (e.g. 90 s), let the client renew with a
   cheap `POST /v1/session/{id}/heartbeat` while recording and release it on
   Stop. Return 503 with `{"detail": "clinic at capacity (N/M streams)"}` and
   expose `active_leases` on `/metrics`. Approximate, but it turns an opaque
   provider 429 into an actionable operator signal and a clear client message.
2. *Configured ceiling only.* A `HOST_PROVIDER_STREAM_LIMIT` (150/225/1) that
   is purely informational: logged at startup, published as a gauge, and
   compared against `session_success_total` over the last minute to raise a
   warning. Zero protocol change.

**Revocation semantics.** Deleting a registry line blocks *new* tokens only;
an already-open WebSocket runs until it closes (documented). JWTs cannot be
revoked at Deepgram; the 30 s TTL is the mitigation. Correct and honest.

**Deployment topology.** One uvicorn worker, or Passenger with a single
dedicated loop thread and one `AsyncGrantClient` (`passenger_wsgi.py` avoids
the double-app/double-pool trap). Passenger may still spawn several
*processes* (`PassengerMinInstances`), each with its own limiter state; the
docs mention this for workers, and it applies equally to Passenger instances.

**Multi-user Windows (RDS/Citrix).** The single-instance mutex is in the
*local* (per-logon-session) namespace (`app_instance.py:20-24`), the DPAPI
blob is user-scoped and `settings.yaml` lives in `%APPDATA%`, so several
clinicians on one terminal server each get their own instance, identity and
secret. Correct; worth stating in the Windows docs because hospitals do this.

---

## 5. Multi-request analysis (one client)

**Serialisation.** `SessionController.start()` is a no-op while a session
thread is alive; the controller stops the old session fully before another
can start; recovery text blocks a new Start until reviewed. One socket per
user at a time, which is the right model for dictation.

**Token requests per client.** One per connection attempt plus one per
"connection check" button press (a real grant, no audio — fine, and useful).

**Reconnect schedule.** Computed with the shipped `ReconnectPolicy`
(base 2 s, max 30 s, jitter 0.5, unlimited attempts) and `decide()`:

| Scenario | Delays, attempts 1…8 (one random draw) | Steady state |
|---|---|---|
| Handshake 429 (Deepgram pool full) | 7.1, 5.6, 7.3, 18.4, 16.6, 33.5, 26.5, 29.5 s | ≈ **129 token+handshake attempts/hour**, forever |
| `NET-0001` close (mic silent) | 1.2, 2.8, 4.1, 9.8, 21.6, 22.4, 18.5, 18.5 s | ≈ **112 Deepgram connections/hour**, each holding a slot ~10 s |
| Host 429 with `Retry-After: 3` | 4.0, 3.9, 10.7, 15.6, 17.4, 32.5 s | honoured and floored by backoff |
| Host 503 with `Retry-After: 120` | 60, 60, 60, … s | clamped to `max_retry_after_seconds` |

Observations:

* `consecutive_failures` resets only after ≥ 30 s connected
  (`app.py:668-669`). A `NET-0001` loop reconnects every ~10 s, so backoff
  saturates — correct, the storm is bounded.
* The `RATE_LIMIT` row exceeds the configured `reconnect_backoff_max` (30 s)
  because `_positive_jitter` is capped by `max_retry_after_seconds` (60 s),
  giving up to 45 s (F8). Harmless, but it contradicts the setting's name.
* Ten clients waiting for Deepgram capacity cost the host ≈ 1 300
  tokens/hour — far under every host limit, and under Deepgram's (unpublished,
  apparently generous) grant limit, but each attempt is a wasted TLS +
  WebSocket handshake that a lease-aware host would avoid.

**Thundering herd at 08:00.** 50 Starts within a second: the global bucket
(1200) and in-flight cap (100) admit all 50; `test_token_issuance_does_not_block_the_event_loop`
and the harness (p95 ≈ 105 ms with a 50 ms mocked grant) show no
serialisation. Deepgram receives 50 grants + 50 handshakes, far below its
pool. Correct.

---

## 6. Concurrency analysis (streams)

**Capacity arithmetic.** Target 50 streams vs. a 150-stream PAYG pool (any
region) or 225 on Growth-NA. Headroom must cover transient double-occupancy:

* a client that reconnects after a *silent* network loss leaves its old
  stream open until Deepgram's 10 s no-data timer fires, so a correlated
  Wi-Fi blip across a ward can briefly hold up to 2× the active count;
* a clean Stop releases the slot as soon as `CloseStream` is processed, and
  the socket-abort fallback sends a TCP FIN that the server sees immediately.

50 active + 50 zombies = 100 < 150: fine on a primary project, **impossible on
a secondary self-serve project (limit 1)** — the single most likely way a
50-user pilot fails on day one, and the repo does not warn about it.

**What user N+1 experiences today.** Handshake 429 → `RATE_LIMIT` → status
label "● اتصال مجدد…" → retry every ~22 s until a colleague stops. No
distinct text, no operator metric (the host issued the token successfully and
counts it as `session_success_total`). The host's `/metrics` therefore cannot
distinguish "Deepgram is full" from "all is well" — see F3 option 2.

**What a configuration fault experiences today (F1).** `1008 DATA-0000` →
`SERVER_DISCONNECT` → reconnect ≈ every 20 s forever, each cycle minting a
token and opening a stream; `app.log` shows only "Deepgram connection closed".
The close payload is one attribute away (`exc.rcvd.reason`).

**Correctness of the per-session machinery.** Verified by reading and by the
suite: the send happens outside the connection lock; a send that races Stop is
reclassified `SHUTDOWN` via a generation counter; `prepare()` precedes any
Stop that could race startup; late callbacks are rejected for the whole
backoff window; 50 in-process sessions share no state
(`test_session_concurrency.py`); 50 simultaneous shutdowns release every
thread, queue and connection. These are the hard parts and they are done
properly.

**Host under Deepgram degradation.** If grants slow to the 7 s deadline,
in-flight saturates at 100 and further Starts get 503 `Retry-After: 1`, which
the client maps to `SERVER_DISCONNECT` + hint → retry. `inflight_grants`
pinned at capacity and `deepgram_connection_failures_total` rising are the
alerts; `/readyz` deliberately does **not** reflect this. Correct, documented.

---

## 7. Recommendations, prioritised

**P1 — do before the first multi-user deployment**

1. **Read and act on Deepgram close codes** (`stt/deepgram_provider.py`).
   In `classify_deepgram_exception`, when `isinstance(exc, ConnectionClosed)`
   and `exc.rcvd` is present: `1008` / `DATA-*` → `CONFIG` (not retried);
   `1011 NET-0001` and `NET-0002` → `SERVER_DISCONNECT` but carry the reason;
   log `code` and `reason` at WARNING in `_on_provider_error`/`_on_close`.
   Add a test for each payload (the websockets `Close` frame makes this a
   three-line fixture).
2. **Audio watchdog + `KeepAlive`** (`app.py`). In the recording loop, if no
   chunk has been *captured* for > 3 s (compare `put_total` between polls),
   call `connection.send_keep_alive()` every 3–5 s via a new
   `STTProvider.keep_alive()` hook and raise the warning "میکروفون صدایی
   ارسال نمی‌کند" instead of letting Deepgram close the socket. This turns a
   10 s silent failure plus an infinite reconnect loop into an immediate,
   correct diagnosis, and keeps the stream (and its slot) alive.
3. **Document the provider pool** in `README.md` → *Capacity* and
   `host/README.md` → *Load testing*: Nova-3 streaming 150 (PAYG, all regions),
   225 (Growth NA) / 150 (Growth EU, AU, IN), Enterprise by contract; limits
   are per **project**; all keys in a project share one pool; **secondary
   projects on self-serve accounts are capped at 1 stream**; only Enterprise
   can raise limits. Add a one-line pre-flight to the live load test that
   prints which endpoint and project the key belongs to.

**P2 — before scaling past a pilot**

4. **Per-client burst ≈ 10, refill ≈ 6/min** (keep global 1200 / 600).
   Update `host/README.md` sizing text, `host/.env.example`, and
   `test_host_concurrency.py` expectations (the 50-client surge is still
   admitted: each client spends one token).
5. **Lease-based admission control** on the host (option 1 in §4) with
   `active_leases`, `lease_capacity` gauges and a 503 whose `detail` the client
   shows verbatim. Alternatively start with option 2 (configured ceiling +
   warning) if the protocol must stay frozen.
6. **Regional endpoint setting.** `DEEPGRAM_API_BASE` on the host (for the
   grant URL) and `deepgram_region`/`deepgram_base_url` in client settings,
   passed as `DeepgramClient(environment=DeepgramClientEnvironment(base=…,
   production="wss://api.eu.deepgram.com", …))`. The host should return the
   region in the session payload so client and host cannot disagree.
7. **Attribution.** Send `tag=[client_id]` and `extra=["session_id=<id>"]`
   on `connect()`; read `dg-request-id` from the handshake response
   (`connection._websocket.response.headers` on the sync client) and log it
   beside `session_id`. Per-department cost allocation and Deepgram support
   tickets both become possible.

**P3 — hygiene**

8. Validate `utterance_end_ms` to 1000–5000; cap keyterms by an estimated
   token budget (e.g. refuse when `sum(len(t.split())*2 + 1)` > 400) rather
   than by count; skip `UtteranceEnd` with `last_word_end == -1`; drop empty
   chunks in `send_audio`.
9. Clamp `RATE_LIMIT` jitter at `max(max_delay, rate_limit_delay)` or rename
   the setting's documentation.
10. Not a concurrency item but relevant to a clinical multi-user rollout:
    decide explicitly on `mip_opt_out` (Deepgram Model Improvement Program)
    for PHI audio and document the choice.

---

## 8. What was executed for this review

```
pytest tests/ -q --ignore=tests/test_soak.py --ignore=tests/test_live_load.py \
       --ignore=tests/test_live_deepgram.py           # 776 passed, 38 s
python scripts/measure_capacity.py                   # 6/6 PASS (mocked grant)
```

Probes (not committed): reconnect-schedule simulation over the shipped
`decide()` (table in §5); close-code classification of websockets 17.2
`ConnectionClosedError(Close(1008, "DATA-0000"))` → `server_disconnect`,
retryable; generated `/v1/listen` URL length per specialty (1 039–1 335
bytes); SDK 7.9.0 source: sync `connect()` uses library-default
`open_timeout=10`, `ping_interval=20`, `ping_timeout=20`; `ApiError` is
raised only for `InvalidStatus` and carries *request* headers, so the
handshake's `Retry-After`/`dg-request-id` are not surfaced.

No live Deepgram call was made; nothing here measures provider latency or
accuracy.

---

## Remediation status

Applied after the review, deliberately without new infrastructure (no lease
service, no protocol change): every item is a small, local change with a
test.

| Finding | Change | Where | Test |
|---|---|---|---|
| F1 close codes | `ConnectionClosed.rcvd` is read: `1008`/`DATA-*` → `CONFIG` (never retried), everything else `SERVER_DISCONNECT`; code + reason in the error message and a WARNING log line | `stt/deepgram_provider.py` (`_close_code_and_reason`, `close_details`, `classify_deepgram_exception`, `_on_provider_error`) | `tests/test_deepgram_protocol.py` |
| F2 silent mic / KeepAlive | `STTProvider.keep_alive()` hook; `DeepgramProvider.keep_alive()` sends `KeepAlive`; the session loop's `_watch_capture` reports "میکروفون صدایی ارسال نمی‌کند" after 3 s without a captured frame, sends KeepAlive every 3 s, and withdraws both when frames resume | `stt/base.py`, `stt/deepgram_provider.py`, `app.py` (`AUDIO_SILENCE_SECONDS`, `KEEP_ALIVE_INTERVAL_SECONDS`, `_watch_capture`) | `tests/test_capture_watchdog.py` |
| F3 pool unknown / opaque 429 | A `RATE_LIMIT` reconnect shows "ظرفیت سرویس گفتار پر است" until a session records; the per-project numbers, the shared-pool rule and the secondary-project trap are documented | `app.py` (`CAPACITY_WARNING`), `host/README.md › Deepgram's concurrency limits`, `README.md › Capacity` | `tests/test_capture_watchdog.py` |
| F4 per-client burst | Defaults `HOST_CLIENT_BURST=10`, `HOST_CLIENT_RATE_LIMIT_REQUESTS=6` (global bucket unchanged); the 50-client surge test now runs on the defaults | `host/core.py`, `host/README.md`, `host/.env.example` | `tests/test_host_concurrency.py::test_default_per_device_burst_cannot_drain_the_shared_stream_pool` |
| F5 region | `DEEPGRAM_REGION` (host grant endpoint) and `deepgram_region` (client stream endpoint), `us`/`eu`/`au`/`in`, validated at startup; SDK `environment` built from it | `host/core.py`, `medical_stt/config.py`, `stt/deepgram_provider.py`, `config/settings.yaml` | `tests/test_host_service.py`, `tests/test_deepgram_protocol.py` |
| F6 attribution | `tag=<host_client_id>`, `extra=session_id:<host session id>` on every connection; `dg-request-id` logged with the session id | `stt/deepgram_provider.py` (`_attribution_parameters`, `_request_id`) | `tests/test_deepgram_protocol.py` |
| F7 validation gaps | `utterance_end_ms` 1000–5000; keyterms also budgeted to 1000 characters (≈500 tokens) in priority order; `UtteranceEnd` with `last_word_end == -1` ignored; empty frames never sent | `medical_stt/config.py`, `stt/deepgram_provider.py` | `tests/test_deepgram_protocol.py`, `tests/test_capture_watchdog.py` |
| F8 jitter ceiling | `RATE_LIMIT` backoff is capped at `max(reconnect_backoff_max, rate_limit_delay)` | `stt/reconnect.py` | `tests/test_capture_watchdog.py` |
| §7.10 MIP | `mip_opt_out` setting (default `false`, Deepgram's default) sent only when enabled; the trade-off is documented in `config/settings.yaml` | `medical_stt/config.py`, `stt/deepgram_provider.py` | `tests/test_deepgram_protocol.py` |

Not done, on purpose: lease-based admission control on the host (§4 option 1).
It needs a new endpoint, a heartbeat from the client and approximate
bookkeeping, for a signal the client already surfaces precisely at the
handshake. Revisit if the clinic grows past the published pool size.

Verification after the changes: `pytest tests/` (excluding the opt-in soak and
live suites) 810 passed; `ruff check .` and `mypy medical_stt host benchmarks`
clean; `scripts/measure_capacity.py` PASS on the new defaults.

---

## 9. Sources

Deepgram documentation (retrieved 2026-10-14):

* Token-based authentication guide — <https://developers.deepgram.com/guides/fundamentals/token-based-authentication>
* `POST /v1/auth/grant` reference — <https://developers.deepgram.com/reference/auth/tokens/grant>
* API rate limits (per-project, per-region tables; secondary-project rule) — <https://developers.deepgram.com/reference/api-rate-limits>
* Working with concurrency rate limits — <https://developers.deepgram.com/docs/working-with-concurrency-rate-limits>
* Errors (429 body, HTTP codes to handle) — <https://developers.deepgram.com/docs/errors>
* STT WebSocket, NET and DATA errors (close codes, 10 s rule, `dg-request-id`, empty-frame warning) — <https://developers.deepgram.com/docs/stt-troubleshooting-websocket-data-and-net-errors>
* KeepAlive — <https://developers.deepgram.com/docs/audio-keep-alive>
* Recovering from connection errors (buffer during reconnect, 1.25× realtime, timestamp realignment) — <https://developers.deepgram.com/docs/recovering-from-connection-errors-and-timeouts-when-live-streaming-audio>
* Finalize — <https://developers.deepgram.com/docs/finalize>; Close Stream — <https://developers.deepgram.com/docs/close-stream>
* Utterance End (1000–5000 ms, `last_word_end: -1`) — <https://developers.deepgram.com/docs/utterance-end>
* Keyterm prompting (500-token limit, Nova-3 only, repeated parameter) — <https://developers.deepgram.com/docs/keyterm>
* Models & languages (Persian `fa` on Nova-3; `nova-3-medical` English-only) — <https://developers.deepgram.com/docs/models-languages-overview>
* Smart Format on non-English models — <https://developers.deepgram.com/docs/smart-format>
* Regional endpoints (same keys and tokens; `Configure keyterms` NA-only) — <https://developers.deepgram.com/reference/regional-endpoints>
* Temporary API key limit (250/day; prefer token auth) — <https://developers.deepgram.com/docs/create-additional-api-keys>

Code: `deepgram-sdk==7.9.0` wheel (`deepgram/listen/v1/socket_client.py`,
`raw_client.py`, `core/websocket_compat.py`, `core/query_encoder.py`,
`environment.py`), `websockets==17.2`.
