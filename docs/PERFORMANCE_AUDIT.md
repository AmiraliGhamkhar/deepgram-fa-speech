# Architecture and performance audit

> Historical pre-change audit. See [PERFORMANCE_FIXES.md](PERFORMANCE_FIXES.md) for implemented remediation, current checks, and remaining real-system validation.

**Date:** 2026-10-14
**Reviewed revision:** `477424a9d94caf842770a5938896fff4daee7757`
**Scope:** Windows desktop client, both Tk windows, audio capture, streaming, text processing, native injection, credentials/configuration, host APIs, deployment adapters, benchmarks and integration tests.
**Change policy:** Analysis only; no application behavior was changed.

## Executive assessment

The token-service architecture is appropriate for a small clinic: audio travels directly from each desktop to Deepgram, not through the host. The host reuses an asynchronous HTTP pool, bounds concurrent grants, and separates authenticated client rate limits from the global budget. Local terminology processing is not the main latency bottleneck for ordinary utterances.

**The highest-priority problems are desktop lifecycle and truthful feedback, not backend throughput.** There are reproducible paths that silently discard startup audio, open a connection after Stop, and display a successful injection when injection failed. Slow clipboard operations run on the provider receive path. The live validation tools also have defects that prevent them from substantiating production-performance claims.

The existing suite is valuable and passes, but **green tests do not establish reliable Windows dictation or real-provider capacity**.

### Evidence terminology

- **Measured:** executed locally with the stated workload.
- **Reproduced:** a deterministic local probe demonstrated the behavior, using fakes where necessary.
- **Code finding:** follows from inspected control flow; its frequency or user-visible magnitude has not been measured on Windows or a deployed host.

## 1. Architecture and ownership

```text
ControlWindow (Tk thread)
  -> SessionController
     -> LiveMedicalSTT session worker
        + microphone callback -> bounded PCM queue -> sender worker
        + provider worker -> host session token -> Deepgram WebSocket
        + transcript callback -> accumulator -> confidence checks
            -> normalization -> terminology -> logical BiDi formatting
            -> Windows clipboard / SendInput
            -> TranscriptOverlay (separate Tk thread)

Desktop HostSessionClient -- HTTPS --> FastAPI POST /v1/session
  -> TLS check -> auth -> per-client/global rate limits -> body parsing
  -> in-flight grant cap -> pooled AsyncGrantClient -> Deepgram grant API

Deployment: Uvicorn ASGI, or Passenger WSGI -> a2wsgi -> dedicated event loop
```

There is no browser frontend or transcription API in this repository. The GUI consists of the control window and cursor-following transcript overlay. The backend is a credential broker; its “sessions” and timings must not be confused with active audio streams.

| Section | Existing strengths | Main concern |
|---|---|---|
| Control window | Start/Stop abstraction; Stop uses a worker rather than blocking Tk | Construction still happens on Tk; recording state does not mean connection-ready |
| Transcript overlay | Widget updates scheduled on its owning interpreter | Uncoalesced updates, unconditional 25 Hz movement, incorrect success feedback |
| Audio | Bounded queue; no logging in capture callback; overflow counters | Startup sends disappear; up to 3.2 s of queued audio at defaults; health reporting can be delayed |
| Provider | Adapter boundary, classified failures, fresh token per connect | Cancellation and connection ownership are incomplete |
| Processing | Deterministic rules, numeric/negation protection, logical/display separation | Unbounded utterance accumulation; long-text overlap checks scale poorly |
| Windows injection | Explicit native signatures, batched SendInput, clipboard retries | Blocking callback work, clipboard restoration races, no target-consumption acknowledgement |
| Configuration/security | YAML cache, user-scope DPAPI, atomic secret writes, no client provider key | Synchronous UI writes, non-atomic settings write, missing client-ID editor |
| Host API | Async pool, body size cap, authentication, load shedding | No body-read deadline; no overall grant deadline; nonfinite TTL causes 500 |
| Deployment | Single-loop WSGI adapter; explicit single-worker support | Per-process limits, startup-only registry, shallow readiness |
| Validation | Broad offline regression coverage and Windows CI job | Broken live harness paths; limited actual GUI/native coverage |

## 2. Measurements and validation

### Environment and limitations

Linux sandbox, Python 3.11.2, a fresh virtual environment installed from both development requirement files. Notable resolved versions: Deepgram SDK 7.9.0, httpx 0.28.1, FastAPI 0.143.0, Starlette 1.8.0, pyahocorasick 2.3.1, python-bidi 0.6.11. Dependency ranges are not a lockfile; another installation can resolve differently.

No real patient recordings, microphone, Windows desktop, production server, or paid Deepgram requests were used. Real Deepgram endpoints were not available under this sandbox's network allowlist. No Windows CPU/RSS, end-to-end latency, WER/CER, TLS-network throughput, or actual clinic capacity is claimed. The 30-minute opt-in soak was not run.

### Existing checks

| Check | Result |
|---|---|
| `pytest tests -q -rs --durations=15 --cov=medical_stt --cov=host --cov-report=term` | **729 passed, 4 skipped**, 51.84 s |
| Skips | Two live-provider tests, one real-provider load test, one soak test |
| Combined statement coverage | **76%** |
| Relevant coverage | Control 41%; overlay 40%; Windows injection 0%; app 74%; provider 82%; host app 89%; host core 90% |
| `ruff check .` | Passed |
| `mypy medical_stt host benchmarks` | Passed, 40 source files |
| `python scripts/verify_client_host.py` | All checks passed, upstream grant mocked |
| Dependency warning | Starlette TestClient deprecates its httpx integration; not a test failure |

Windows coverage of 0% here is an environment limitation, not proof that the native backend is broken. Conversely, fake-widget tests do not establish actual Tk responsiveness.

### Existing 50-client capacity harness — mocked upstream

`python scripts/measure_capacity.py`:

- 50/50 successful token requests, no 429 responses.
- Token request p95: **95.6 ms**, with a fixed **50 ms mocked grant delay**.
- Concurrent batch duration: **0.097 s**, versus 2.5 s of upstream wait if serialized.
- Reported RSS delta: **+0.00 MB over 250 additional requests**.
- In-flight grants returned to zero; 50 limiter buckets for 50 clients.

This establishes a useful short-run application baseline, not a sustained-capacity or leak-free guarantee. The harness allows up to 32 MiB growth, does not exercise real sockets/TLS, and its “active sessions after shutdown” label actually checks completed **grants** without running application shutdown. Its `int(count * 0.99)` threshold would incorrectly accept 49/50 = 98%; the observed run achieved 50/50, so that bug did not change this result (`scripts/measure_capacity.py:143-214`).

### Additional saturation probe — mocked upstream

An in-process ASGI probe retained the default 100-grant cap, used a 50 ms fake grant, and raised the legacy-client/global burst budgets to avoid rate limiting confounding saturation. These are **request bursts**, not distinct doctors or real streams.

| Simultaneous requests | HTTP outcomes | p95 among successful requests |
|---|---|---:|
| 50 | 50 × 200 | 94.44 ms |
| 100 | 100 × 200 | 127.94 ms |
| 150 | 100 × 200, 50 × 503 | 213.35 ms |

All three returned to zero in-flight grants. The 150-request burst demonstrates deliberate shedding, not an accidental crash. Do not interpret these small batches as a maximum requests/second measurement.

### Deterministic text processing

Measured 100 passes over all 22 shipped corpus references (2,200 samples; longest input 221 characters), after warm-up, with 727 active rules out of 835. No GUI, audio, network, confidence parsing, or actual injection was included.

| Stage | p50 | p95 |
|---|---:|---:|
| Normalize | 0.018 ms | 0.050 ms |
| Terminology | 0.018 ms | 0.046 ms |
| Injected-direction formatting | 0.001 ms | 0.002 ms |
| Visual BiDi conversion | 0.060 ms | 0.105 ms |
| Combined stages | **0.097 ms** | **0.155 ms** |

Combined maximum was 0.627 ms. The visual conversion is included for comparison although it runs on the overlay thread in production. Warm cached rule read/deep-copy/build: p50 6.206 ms, maximum 7.070 ms across 10 samples. A separate fresh-process first read/build took 290.43 ms; that is not full EXE startup time.

Synthetic repeated clinical text, 20 samples per size, normalize + terminology + visual BiDi:

| Characters | p50 | p95 |
|---|---:|---:|
| 60 | 0.148 ms | 0.288 ms |
| 600 | 1.244 ms | 1.330 ms |
| 6,000 | 17.990 ms | 35.292 ms |

This suggests long-text work deserves a limit and profiling, but ordinary text processing is orders of magnitude smaller than clipboard settling or network waits.

## 3. Priority findings

### F1 — High: startup audio is silently discarded before connection readiness

**Reproduced;** `medical_stt/app.py:418-469`, `medical_stt/stt/deepgram_provider.py:273-302,376-388`, `medical_stt/ui/control.py:286-301`.

The provider, sender, and microphone start without a readiness handshake. Token acquisition and WebSocket establishment run asynchronously relative to capture. `send_audio()` returns normally when `_connection is None`; the caller removes and acknowledges that chunk anyway. The control window already says it is recording.

A direct probe consumed ten 2,560-byte PCM chunks with **zero opened connections and zero queue drops**. At default capture settings those chunks represent 0.8 s of audio. The actual loss window depends on token and handshake latency.

**Recommendation:** expose connecting/ready/failed states. Either delay capture until ready, or keep a bounded, timestamped pre-connect buffer with explicit overflow policy. Never treat a not-ready send as successful. Add delayed-token and delayed-WebSocket regression tests that account for every captured chunk.

### F2 — High: Stop does not cancel startup, and abandoned connection ownership is unsafe

**Reproduced startup case; additional code findings.** `deepgram_provider.py:264-302,353-365,402-421`; `app.py:380-383,497-525`.

Stopping while the token request is outstanding sets `_stop_event`, but returns because no connection exists. After the token arrives, `start()` does not check that event before opening and listening. A fake-SDK probe recorded **`listen_started` after Stop, with no finalize/close calls**.

Furthermore, the provider is reused across reconnects. Its connection and stop event are instance-wide, and each old `start()` unconditionally clears `_connection` in `finally`. Application callback generations do not protect these provider internals. After a join timeout, the app also does not invalidate callback generation immediately; it changes only when the next session begins. Consequently, late callbacks during backoff/after shutdown are not rejected by that check alone.

`provider.stop()` calls synchronous socket sends before the bounded provider-thread join. That call itself has no application-level deadline; the 2 s sender join plus 4 s provider join are not a complete shutdown bound. The control worker's 8 s join protects Tk from waiting forever, but does not terminate the underlying operation.

**Recommendation:** a per-connection lifecycle object/token, cancellation checks after every blocking startup phase, identity-checked connection cleanup, immediate callback invalidation when abandoning a session, and a transport-abort path with a complete shutdown deadline. Test Stop during token fetch, handshake, blocked send, and old-provider completion during reconnect.

### F3 — High: unsuccessful or uncertain dictation is presented as successful

**Reproduced injection case; warning visibility is a code finding.** `app.py:258-295`; `ui/overlay.py:170-198`.

`set_done()` displays “typed” before paste is attempted. On failure, only a log warning is issued; 80 ms later the overlay becomes idle. A failing backend produced **`done -> idle`**, even though no text was inserted. The finalized utterance has already left the accumulator and there is no bounded recovery/retry UI.

Low-confidence clinical tokens are preserved and logged, which is good, but neither GUI consumes `last_confidence_warning`. Likewise the exposed degraded-audio health is not connected to a visible warning. A clinician cannot distinguish a clean session from lost audio or uncertain tokens from the current normal indicators.

**Recommendation:** show success only after backend acceptance, explicitly distinguish acceptance from target consumption, and display persistent failure/degraded/confidence states. Retain a bounded in-memory failed utterance for explicit user-directed copy/retry without persisting patient text by default. Do not automatically replay into an unknown focused field.

### F4 — High performance priority: clipboard operations block transcript reception

**Code finding; timing constants verified.** `deepgram_provider.py:319-352`; `app.py:258-295`; `injection/_windows_backend.py:255-267,327-360`.

The SDK's synchronous message listener invokes the application callback inline. That callback performs processing, GUI scheduling, paste, and an extra 80 ms sleep. Default Windows clipboard settling adds 120 ms, so a normal successful final callback contains **at least 200 ms of explicit sleep**, excluding clipboard work, text work, and scheduling. One exhausted clipboard-open retry sequence sleeps 570 ms; several clipboard operations occur per paste.

The separate audio sender continues, so this is not a claim that each final immediately drops audio. It does delay receiving subsequent results and can create receive-side backpressure. Calling Tk `after()` from a worker also requires cross-thread Tk dispatch and should not be treated as a guaranteed nonblocking queue operation.

**Recommendation:** a bounded, ordered finalization/injection worker; coalesced “latest partial” GUI updates; no sleep in the receive callback. Schedule the idle transition with Tk's timer. Preserve ordering and make injection-queue overload a visible fault, not silent transcript loss. Do not simply reduce clipboard settle time without target-application tests.

### F5 — High validation priority: real-provider performance harnesses are not trustworthy yet

**Code findings plus a reproduced gate failure.** `benchmarks/run_benchmark.py:110-168`; `tests/test_live_deepgram.py:80-106`; `tests/test_live_load.py:40-53,99-147`; `tests/conftest.py:9-11`.

- Live benchmark and live provider test call blocking `provider.start()` on the same thread that is supposed to send audio afterward. Audio is not sent while the listener is running; by the time it returns, the connection has been cleared.
- The load suite requires `DEEPGRAM_API_KEY` at collection time, but `conftest.py` removes it first. A run with explicit opt-in and a **fake** ambient key still skipped for a missing key. No real request was made.
- Even if that gate is corrected, the load code calls `.on()` and `.start_listening()` on the object returned by `connect()`, rather than the socket yielded by its context manager in SDK 7.9.0. It also uses synchronous SDK/token operations inside async tasks.
- The load helper reads `MEDICAL_STT_HOST_URL`, inconsistent with the production `MEDICALSTT_HOST_URL`; the single-stream helpers omit the provisioned client ID. Those paths do not reliably test registry-mode deployment.
- The WAV reader validates sample width but not sample rate or channel count against the configured stream; the live benchmark does not supply the production ASR replacement list.

**Recommendation:** fix these tools before using them as release gates. Use a worker plus explicit readiness for the sync provider, or the actual async SDK for concurrent-load tests; bind the context-manager result; keep fake/offline tests isolated without disabling explicit live opt-in; match credential IDs and recognition configuration; validate audio format and final-result draining.

## 4. Medium-priority findings

### F6 — Tk startup remains synchronous and UI work is not bounded

`ui/control.py:286-301,340-357`; `app.py:123-151,617-645`; `ui/overlay.py:76-80,123-158`.

Start invokes the STT factory on the control thread. It loads settings/credentials/rules, creates the injector, and waits up to four seconds for another Tk interpreter. Cold rule work alone measured about 290 ms locally. Settings save performs DPAPI and file operations, including the secret store's `fsync`, on the UI thread.

The overlay calls `geometry()` every 40 ms even if the pointer/window position has not changed. Partial updates are not coalesced; repeated rendering of long text can accumulate work. Two separate Tk roots/threads also complicate destruction and ownership. `ControlWindow.start()` returns true when `_ready` is set even if `_run()` just reported a Tk initialization failure (`control.py:88-95,140-150`).

**Action:** run preparation/save work off Tk, introduce a starting state and cancellation, prefer one Tk owner with a `Toplevel`, use a bounded mailbox drained by Tk, update geometry only on change, and propagate initialization success separately from completion. Test multi-monitor negative coordinates/DPI; the current primary-screen clamping is not a full virtual-desktop placement policy.

### F7 — Accepted injection configuration is ignored

**Reproduced.** `config.py:168,426-427,493`; `app.py:282-284`.

`inject_mode: type` is validated and documented, but finalization unconditionally calls `paste_text`. The probe recorded one paste and zero type calls with type mode selected.

**Action:** honor the selected mode with identical formatting and failure semantics, or remove unsupported configuration. This matters for applications that disallow paste.

### F8 — Retry storm protection has gaps, and unlimited retries can overflow

**Reproduced.** `stt/reconnect.py:84-106,122-129`; `app.py:559-565`.

A shared 10 s Retry-After produced exactly **10.0 s for all 50 sampled clients**: taking a maximum with the hint removes jitter when the hint dominates. The 5 s rate-limit floor similarly collapses early retry delays. Upstream 429 hints are not forwarded by the host's grant error mapping.

`base_delay * 2 ** (attempt - 1)` is calculated before capping. Attempt 1,025 raised `OverflowError`. The reconnect count is cumulative over the instance, not reset after a stable connection.

**Action:** cap the exponent before multiplication; separate cumulative reconnect telemetry from consecutive-failure backoff; add bounded positive jitter after the server-required minimum; preserve usable upstream Retry-After hints. Add large-attempt and many-client hint tests.

### F9 — Audio latency, health reporting and long utterances need explicit budgets

`app.py:156,312-349,430-457`; `audio/queue.py`; `stt/accumulator.py:42-107`; `processing/fst.py:139-171,205-210`.

Defaults yield 1,280 frames/80 ms block, 2,560 bytes/block, 12.5 callbacks/s, and 32,000 bytes/s of mono 16-bit audio. A full 40-block queue holds **3.2 seconds / 102,400 bytes** before object overhead. Queue size is fixed in chunks, so the allowed 1 s block setting would permit 40 s of backlog.

Overflow drops newly captured audio while retaining old audio, favoring continuity of older content over freshness. That is a policy decision that needs explicit clinical acceptance, not an automatic recommendation to switch drop direction. `_report_audio_health()` is only called when a queue read times out: with a continuously nonempty queue or blocked send, reporting can be delayed indefinitely even though drop flags are set.

The accumulator has no time/size bound until endpoint/flush. Protected-range checking compares each candidate match with ranges, potentially O(matches × ranges); redundant sorting and whole-string BiDi work add long-input costs.

**Action:** timestamp chunks, report queue age and sent/dropped durations, size the queue in seconds, poll health independently of emptiness, and surface overload. Bound accumulated final segments with a safe segmentation/review policy. Optimize range lookup only after profiling long-input cases; ordinary text is already fast.

### F10 — Existing client latency logs do not measure speech-to-text latency accurately

`app.py:76-119,260,312-320,391`.

The clock starts at session startup or the next audio callback, including silence; VAD SpeechStarted events are not used. Thus “audio_to_first_interim” includes token acquisition/startup or arbitrary idle silence. Latency state is not reset by `_reset_session_state()`. “Injection” also includes overlay scheduling, and total excludes the subsequent 80 ms receive-thread sleep.

**Action:** separate token, WebSocket-ready, capture/send age, VAD speech onset, last voiced sample/endpoint, result receipt, processing, injection acceptance, and UI render timestamps. Use per-utterance state and collect p50/p95/p99 without transcript content. Do not tune endpointing using the current total as though it were post-speech delay.

### F11 — The API has byte limits, but incomplete time limits

`host/app.py:350-417`; `host/core.py:827-855,872-894`; `host/app.py:531-543`.

An authenticated request can remain in `request.stream()` without a body-read deadline. The in-flight cap is acquired afterward, so it does not bound pending body readers. A stalled-body probe remained pending after 250 ms while the grant gauge remained zero. This demonstrates where it waits, not an internet-scale denial-of-service measurement. Authentication/rate budgets still limit admission rates; they do not bound total readers over indefinite time.

The HTTP pool has useful connect/read/write/pool phase timeouts, but no whole-grant deadline. Those phases can accumulate, and read timeouts are inactivity limits rather than a strict wall-clock cap. The client's default 10 s host timeout can expire before a slow host grant completes (host connect default 5 s, other phase defaults 10 s).

**Action:** body-read timeout, bounded total admitted requests, reverse-proxy header/body limits, and an overall grant deadline inside the desktop request budget. Measure through the real TLS/proxy stack, not only ASGITransport. Keep the grant cap and pool size coordinated.

### F12 — Nonfinite TTL input causes an uncontrolled 500

**Reproduced.** `host/app.py:391-398`; `host/core.py:307-313`.

Authenticated `{"ttl_seconds":1e309}` is accepted by the JSON decoder as an infinite float; `int()` raises `OverflowError`, which `normalize_ttl()` does not catch. The ASGI probe returned **500**, not a controlled validation response.

**Action:** strict finite bounded-integer request validation with explicit 400/422 responses. Also review finite-value handling in environment float parsing and token-expiry parsers. Add malformed/nonfinite/oversized-number tests without using any real provider.

### F13 — Windows clipboard restore is not a complete restoration or acknowledgement protocol

`injection/_windows_backend.py:269-289,327-360`.

Only Unicode text is saved. An image/file/rich clipboard is not preserved as a full format set; when no previous text is available, dictation may remain in the clipboard despite restore being enabled. Restore unconditionally writes the previous text after the settle delay, potentially overwriting something the user copied in the meantime. A successful SendInput means events were accepted, not that the intended EMR consumed the clipboard or that focus stayed correct.

**Action:** document supported restoration semantics; check the clipboard sequence/ownership before restoration; avoid overwriting a newer user copy; offer a target/focus safety policy. Validate Notepad, Word, browser EMRs, elevated targets, RDP, clipboard managers, and slow consumers on Windows. Keep any patient-text retention explicit and bounded.

## 5. Backend API and deployment review

### Endpoint assessment

| Endpoint | Current behavior | Recommendation |
|---|---|---|
| `GET /healthz` | Lightweight liveness; common TLS middleware still applies | Keep cheap; verify the actual proxy/container probe scheme |
| `GET /readyz` | Checks grant-client existence and `closed`, not upstream reachability or usable key | Name/document it as local readiness; monitor recent grant failures separately rather than minting a token per probe |
| `GET /metrics` | Bounded counters/histograms; admin token optional | Require admin token or enforce network access control in production |
| `POST /v1/session` | Auth, rate limits, byte bounds, async grant, 503 shedding | Address F11/F12; add all-request outcome and full-handler latency accounting |

`session_starts_total` is incremented before rate/body/cap outcomes; `session_failures_total` does not include all those exits. `token_request_failures_total` includes capacity rejection even though `token_requests_total` is incremented only after acquisition. Consequently a naïve dashboard ratio can mislead. Establish explicit admitted/attempted/succeeded/rejected/cancelled semantics and separate upstream latency from full route latency (`host/app.py:330-437`).

### Scaling and operational constraints

- Default sustained global allowance is 600/minute = **10 grants/s**, with burst 1,200; default in-flight cap and HTTP pool are 100. These govern token issuance, **not concurrent dictation sessions**. Increasing them does not change Deepgram account stream limits.
- A single process owns registry, buckets, metrics and in-flight state. Multiple workers/replicas multiply independent budgets; a shared registry file alone does not make rate limits global. Keep the documented single-worker deployment or explicitly add distributed/gateway enforcement.
- Registry contents are loaded at startup. Provisioning/revocation changes require reload/restart and cannot close an already-open direct-to-Deepgram stream. Make this operational contract explicit.
- Authentication lockout is checked by peer before credential validation. Repeated bad credentials behind a hospital NAT can temporarily affect valid colleagues sharing that peer; successful clients do not consume the budget, but do encounter a previously locked bucket. Evaluate that deliberate security/availability trade-off (`host/app.py:288-308`).
- Bounded limiter state is good. At the 10,000-key bound, eviction/purge scans become CPU work on the event loop. It is not a 50-client bottleneck; use incremental/LRU structures if scaling materially beyond that target (`host/core.py:782-800`).
- The WSGI adapter correctly supplies one long-lived async loop. Passenger worker/thread limits still constrain actual inbound concurrency. ASGI unit load does not prove cPanel capacity. Lifespan cleanup is not run by this adapter; process exit reclaims resources, but it is not an exercised graceful-drain protocol.
- Docker's healthcheck uses loopback HTTP when no native certificate is configured. In a reverse-proxy TLS deployment with `HOST_ALLOW_HTTP` false, that probe lacks forwarded HTTPS metadata and can be rejected even while proxied traffic works (`host/Dockerfile`, `host/app.py:220-242`). Test both supported TLS topologies.
- Host request logging is synchronous. At this scale it is unlikely to dominate, but slow log sinks can stall the event loop. Measure first; then use a bounded logging queue if necessary.

## 6. Configuration, packaging and security observations

The split credential model, hashed device registry, redirect refusal, user-scope DPAPI and secret-safe metrics are sound design choices. This audit did not perform a penetration test or dependency-advisory scan and is not a security certification.

- The GUI edits URL/secret but not `host_client_id`, although registry mode needs it. Add an onboarding/import path and show a safe connection test rather than making operators edit YAML/environment variables.
- `save_host_credentials()` saves the secret before validating/writing the URL, and writes settings directly rather than atomically. Validate first and write settings using a temporary file/replace; report partial failure accurately (`config.py:367-409`).
- DPAPI protects at-rest data against other users, not arbitrary code running as the same user. The constant entropy is public application data, not a security barrier against same-user programs (`security/dpapi.py:25-30`).
- Standalone-folder packaging avoids one-file extraction cost and includes Tk/PortAudio checks. No actual Nuitka build or clean Windows machine startup was measured here. Compilation is not a substitute for credential isolation or signing.
- The Windows self-test loads the injection backend but does not paste into a real editor (`scripts/windows_selftest.py:70-77`). Add an interactive/native integration gate with explicit user-safe target setup.
- Runtime/build dependencies have broad version ranges. Retain compatibility testing but also record resolved dependencies for reproducible releases. The current TestClient deprecation warning shows why this matters.

## 7. Recommended work order and acceptance criteria

### First: correctness and release blockers

1. Fix connection readiness and cancellation/ownership (F1/F2).
2. Correct success/failure/degraded UI and failed-utterance handling (F3).
3. Repair live integration/benchmark tools (F5), while keeping opt-in gates and no real credentials in tests.
4. Add regressions for these cases, `inject_mode`, retry overflow and TTL validation.

**Acceptance:** every captured chunk is sent or explicitly accounted for; no connection/injection occurs after confirmed Stop; no success indication on failed backend acceptance; old connections cannot clear or mutate new connection state.

### Second: responsiveness and bounded work

1. Move final injection off provider receive callbacks; preserve ordered finals.
2. Make GUI startup/save asynchronous and coalesce partial updates.
3. Add queue-age budgets, independent health reporting and valid stage timings.
4. Add API body/overall deadlines and fix retry synchronization.

**Suggested targets to validate, not achieved claims:** normal GUI actions respond within 100 ms; idle overlay avoids redundant geometry work; ordinary local text processing stays under 5 ms p95; queue age remains within an agreed audio budget; shutdown completes within an explicitly tested deadline under failed network conditions.

### Third: production evidence

- On Windows 10/11: measure cold/warm EXE startup, idle/recording CPU and RSS, Tk event-loop lag, successful-paste latency, clipboard contention, DPI/multi-monitor placement, focus switching, and repeated Start/Stop cycles.
- Run at least 100 lifecycle cycles plus a 30–60 minute soak; inspect thread/handle counts and memory plateau, not only final RSS deltas.
- Through the real host TLS/proxy: burst 50 clients, vary grant delays, disconnect during body/grant, and verify 429/503 plus Retry-After behavior.
- After repairing the harness, run explicitly authorized real-provider 10/25/50-stream checks with provisioned identities and no API key on production desktops.
- Score consented/de-identified Persian and mixed-language recordings for WER/CER, dose/number preservation, negation and English medical-term recall. Report acoustic recognition and deterministic rewriting separately.

**Bottom line:** retain the current architectural split and deterministic engine. Fix lifecycle, feedback and blocking integration boundaries first. There is no measured justification here for adding a database, Redis, a web frontend, or more host workers merely to support 50 clinicians.

## Reproduction

Run the existing checks from the repository root with both requirement sets installed:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt -r host/requirements-dev.txt
.venv/bin/pytest tests -q -rs --durations=15 --cov=medical_stt --cov=host --cov-report=term
.venv/bin/ruff check .
.venv/bin/mypy medical_stt host benchmarks
.venv/bin/python scripts/verify_client_host.py
.venv/bin/python scripts/measure_capacity.py
```

Exact additional local probe scripts are preserved as fenced code in [PERFORMANCE_AUDIT_REPRODUCTION.md](PERFORMANCE_AUDIT_REPRODUCTION.md). They use fake credentials and mocked upstreams, perform no native injection, and do not require live network access. Timing values naturally vary by machine and run.
