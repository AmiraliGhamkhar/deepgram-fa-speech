# Performance audit remediation

Date: 2026-10-14. Implemented on the session working branch; no deployment or live credentials changed.

This document supersedes the **status** of findings in [PERFORMANCE_AUDIT.md](PERFORMANCE_AUDIT.md), which remains the historical, pre-change analysis. The archived reproduction probes record the old behavior; current regression tests are the supported verification path.

## Implemented changes

| Finding | Remediation |
|---|---|
| F1: audio lost before readiness | Added explicit provider preparation/readiness. Microphone and sender start only after readiness. A send without a connection now reports failure instead of silently succeeding. GUI distinguishes connecting, recording, reconnecting, and stopping. |
| F2: cancellation/connection ownership | Stop is checked before and after token/handshake startup. Readiness/stop state is run-local; old cleanup cannot clear a newer connection. Production reconnects get fresh provider instances. Late callback generations are invalidated at teardown, including during backoff. Graceful close runs with a deadline and an isolated SDK-version-specific socket-abort fallback. Repeated abandoned network workers are capped rather than accumulating indefinitely. |
| F3: misleading success and hidden warnings | Overlay success is emitted only after backend acceptance and says to verify insertion. Failure, confidence, and audio-loss warnings are surfaced. Failed text is retained only in bounded memory for explicit review/copy/clear; no automatic replay. New sessions are blocked until retained failures are reviewed/cleared. |
| F4: blocking provider callbacks | Ordered bounded transcript worker handles finalization/injection separately from the listener. The unconditional 80 ms callback sleep was removed. Partials are coalesced. Worker threads no longer call Tk `after()`. Clipboard settling remains because target consumers are asynchronous. |
| F5: broken live validation | Added a shared concurrent-listener streaming harness: preparation, readiness wait, paced PCM, bounded shutdown, final-result draining. Benchmark and single-stream live tests use it. WAV rate/channel validation and production ASR replacements were added. Load tests use host-issued tokens and provisioned IDs, not an account key erased by conftest; concurrent ready intervals must overlap. |
| F6: GUI latency/lifecycle | Start preparation and settings writes run off Tk. The control window owns the production Tk interpreter; overlay is a `Toplevel`. Bounded latest-value mailboxes marshal updates. Initialization failure returns false. Geometry changes only when necessary and supports negative virtual-desktop coordinates. Client-ID editing, saved-configuration connection testing, and recovery review are available. |
| F7: ignored injection mode | Live finalization now honors `paste` versus `type`. |
| F8: retry behavior | Exponent is capped before multiplication, so huge retry counts cannot overflow. Retry-After/rate-limit floors receive positive jitter when room remains within the configured hint cap. Stable sessions reset consecutive-failure backoff independently from lifetime reconnect telemetry. Usable upstream Retry-After values propagate through the host. |
| F9: audio/utterance budgets | Audio capacity is derived from a 3.2-second budget, not a fixed chunk count. Queue age, sent bytes and lost bytes are tracked. Health is polled independently of sender progress/queue emptiness. Final-event and utterance sizes are bounded. Numeric/negation protected ranges are merged and binary-searched rather than scanned for every match. |
| F10: latency instrumentation | Removed session/silence-triggered speech clocks. VAD receipt and original transcript-receipt timestamps feed per-utterance trackers. Token acquisition, WebSocket readiness, transcript queue wait, processing, and injection acceptance are distinct. Measurements do not claim target-app consumption or actual acoustic onset. |
| F11: incomplete API bounds | Added whole-body timeout, admitted-request cap including body readers, overall grant deadline, and effective grant cap coordinated with HTTP pool size. Full-route outcomes/latency and active readers are observable. |
| F12: invalid TTL causes 500 | Session requests validate object/integer/range shape and return 422 for invalid TTL/JSON; nonfinite values cannot reach integer conversion unchecked. Environment float parsing, client retry hints and token-expiry conversions were hardened. |
| F13: clipboard/focus races | Restore checks sequence ownership while holding the clipboard. New user copies are not overwritten. Empty clipboard restoration is supported. Unsupported rich/file/image formats are refused when restoration is requested, rather than destroyed. Focus changes cancel paste/remaining key batches. Production refuses injection into its own process's settings/recovery windows. |

### Additional host and delivery work

- Correct credentials bypass the *failed-authentication* lockout so a malicious/broken device cannot lock out colleagues sharing a NAT. Authenticated per-device/global rate limits still apply.
- Token bucket eviction uses ordered eviction instead of repeatedly scanning all buckets.
- `/readyz` is documented as local readiness, not an upstream availability/key check.
- Metrics now distinguish complete HTTP outcomes from actual grant attempts and capacity rejections.
- Docker loopback readiness works behind TLS termination when loopback remains a trusted proxy peer.
- Passenger has a best-effort same-loop pool-close hook for normal exit.
- Settings URL/client ID are validated before credential changes; YAML replacement is atomic. A two-file partial credential/settings failure is explicitly reported.
- DPAPI documentation no longer claims public entropy protects against same-user programs.
- Capacity acceptance uses the actual success fraction; labels no longer imply that in-flight grant counts measure active audio sessions or a shutdown test.
- Release builds and CI record resolved dependency versions without index URLs or credentials.
- Windows CI includes the new policy and lifecycle regressions.

## Validation performed

All checks below were run locally in the Linux sandbox with Python 3.11.2. No live upstream, microphone, patient audio, or native Windows input was used.

| Check | Result |
|---|---|
| Full test suite | **777 passed, 4 skipped**, 37.69 seconds |
| New audit regressions | **48 additional tests** across lifecycle/API/GUI/configuration/native-policy paths |
| Ruff | Passed |
| mypy | Passed, 42 source files |
| `git diff --check` | Passed |
| Project working-tree secret scanner | No credentials found |
| Client/host protocol verifier | All checks passed, upstream grant mocked |
| 50-client capacity harness | 50/50 successful, zero 429, **104.0 ms p95** with a 50 ms mocked grant |
| Concurrent issuance batch | 0.100 s versus 2.50 s if upstream waits were serialized |
| Short-run RSS check | +0.00 MB over 250 additional requests; within the explicit 32 MiB allowance |

Four skips are intentional: two real-provider integration tests, one real-provider load test, and the long-running soak. Starlette emits one TestClient/httpx deprecation warning; it does not fail the suite. Dependency ranges remain compatibility ranges, not a cross-platform lockfile; resolved release manifests improve traceability but do not replace dependency updates or advisory scanning.

New native clipboard policy tests use fake DLLs. Executing those branches on Linux **does not verify the Win32 ABI, actual clipboard behavior, focus behavior, or EMR compatibility**.

### Commands

```bash
.venv/bin/pytest tests -q -rs
.venv/bin/ruff check .
.venv/bin/mypy medical_stt host benchmarks
.venv/bin/python scripts/scan_secrets.py
.venv/bin/python scripts/verify_client_host.py
.venv/bin/python scripts/measure_capacity.py
```

## Deliberate safety limits and trade-offs

- A confirmed Stop waits for any already-running native injection to finish. Python cannot safely kill a thread inside an OS input call. If injection is wedged, the GUI stays responsive and reports incomplete Stop; it does **not** claim success or start an overlapping injection session.
- Final queue: 16 events, at most 8,000 characters / 4,000 word records per event. Utterance accumulation: 8,000 characters, 128 finalized segments, or 60 seconds between accumulated final segments' processing. Exceeding a budget stops dictation for review rather than silently rewriting/truncating an unbounded utterance.
- Recovery: up to 16 entries of 8,000 characters each, held in memory only. Overflow is explicitly indicated. Nothing is automatically pasted after a failure, which might otherwise duplicate partially accepted input or target the wrong field.
- Audio overload still drops new chunks rather than changing the clinical continuity policy to discard old chunks. Loss duration and visible warnings make this explicit. No transcription correctness claim is made for audio that was dropped.
- Clipboard restore supports empty/plain-text clipboards, including Windows' synthesized text/locale formats. If you copied rich Word content, files or images, preserve that clipboard and select `inject_mode: type`, or intentionally replace/clear it before using paste. No silent clipboard-to-typing fallback occurs.
- Focus validation and SendInput acceptance are not a target-consumption acknowledgement. The clinician must verify the record. Neither the native backend nor the overlay claims otherwise.
- Retry hints are bounded at 60 seconds by default. At the exact cap there is no remaining jitter room; this is an explicit bounded-wait trade-off, not a claim of perfect storm resistance for every upstream hint.
- Multiple host workers still have independent registry/rate-limit/metrics state. A shared registry file alone does not provide global limits. Use the supported one-worker topology or implement deliberate gateway/distributed enforcement.
- The metrics token remains optional for backward compatibility. **Production must set it or restrict `/metrics` at the gateway.** Synchronous logging remains unchanged because no production log-sink bottleneck was demonstrated.

## Validation still required on real systems

These are release-validation tasks, not claims of completed measurements.

### Windows

1. Run the existing component self-test:
   ```powershell
   python scripts/windows_selftest.py
   ```
2. Run the new explicit native-input check:
   ```powershell
   python scripts/windows_injection_check.py --interactive
   ```
   It creates a dedicated synthetic-text test window. It refuses to send input without Windows and explicit opt-in. Keep that window focused and inspect Persian shaping and numeric order visually. A pass here does not certify Word, browser EMRs, elevated apps or RDP.
3. Build/sign the standalone release, then test on a clean Windows 10/11 machine. Inspect `dependency-versions.json` in the bundle.
4. Measure cold/warm startup, event-loop lag, recording CPU/RSS, thread/handle counts over at least 100 Start/Stop cycles, multi-monitor DPI placement, clipboard contention and slow target consumers.

### Real host/provider

- Test native TLS and reverse-proxy/cPanel deployments with real header/body timeouts, worker limits, slow grants, disconnections and 429/503 responses.
- After securely setting `MEDICALSTT_HOST_URL` and provisioned credentials, opt in explicitly:
  ```bash
  MEDICAL_STT_LIVE_TEST=1 pytest tests/test_live_deepgram.py -m live -v
  MEDICAL_STT_LOAD_TEST=1 pytest tests/test_live_load.py -m loadtest -v -s
  ```
- For distinct load identities, `MEDICAL_STT_LOAD_CLIENTS_FILE` points to a **noncommitted** JSON list of `client_id`/`secret` objects with at least 50 entries, such as a locally protected `*.secret` file. No Deepgram account key is required on that driver. Do not put real credentials into chat, logs, example files, or shell history.
- Run the opt-in 30–60 minute mocked soak, then explicitly authorized real-provider tests. Provider-account capacity and network latency cannot be inferred from the mocked host harness.
- Run the repaired benchmark on consented/de-identified recordings. WER/CER and clinical accuracy still require actual recordings and expert review; no accuracy values were invented.
