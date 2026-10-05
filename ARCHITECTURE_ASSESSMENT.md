# Phase 1 — Internal Architecture Assessment (pre-implementation)

Written before any code change, from a full read of the tree, the tests,
the CI workflow and the Docker build.

## 1. Where the concurrency actually lives

This is **not** a server-side multi-tenant streaming service. It is:

* **50 independent Windows desktop processes**, one per doctor, each running
  at most **one** session (`SessionController` holds a single `LiveMedicalSTT`;
  `SingleInstance` enforces one per Windows session).
* **one** self-hosted FastAPI host that only mints short-lived Deepgram tokens.

So the concurrency problem splits cleanly:

| Concern | Where it lives |
|---|---|
| 50 concurrent audio streams | client -> Deepgram directly. **The host never sees audio.** |
| 50 concurrent `/v1/session` requests | **host process** — the real bottleneck. |
| Per-client robustness | one client process. |

## 2. Confirmed defects (in priority order)

1. **Per-IP rate limiting rejects legitimate hospital traffic.**
   `RateLimiter(30, 60)` keyed on `request.client.host`. 50 doctors behind one
   hospital NAT share one public IP -> request #31 gets a 429. This is the
   single defect that breaks the 50-user target, and it is a *correctness*
   bug, not a tuning issue.

2. **No client identity at all.** A single `HOST_SHARED_SECRET` is shared by
   every doctor. The server cannot tell client-A from client-B, cannot rate
   limit them independently, and cannot revoke one device without revoking
   all 50. This is what makes (1) unfixable without a real identity.

3. **Blocking I/O on the event loop.** `host/app.py` `create_session` is
   `async def` but calls `core.grant_session` -> `_httpx_grant` -> `httpx.post`,
   a **synchronous** call, inside the coroutine. Every token request stalls
   the whole event loop for the full Deepgram RTT. 50 concurrent requests
   serialize and each is delayed by the other 49.

4. **No session identifier.** Nothing correlates a log line to a session, so
   concurrency bugs are not diagnosable, and per-session metrics are impossible.

5. **Reconnect can storm.** `ReconnectPolicy.max_attempts` defaults to `0`
   (= unlimited) and jitter is *symmetric ±50% around the delay*, which does
   not decorrelate 50 clients that failed at the same instant. `Retry-After`
   is not honored anywhere.

6. **Audio queue overflow is invisible as a state.** Drops are counted and
   logged once per burst, but there is no persistent health signal, so
   "the microphone is unusable because the network is bad" is not
   distinguishable from "everything is fine".

## 3. What is already correct — do not touch

* **Architecture**: client -> Deepgram WebSocket direct. The host is a token
  service only. This is the right design and is preserved exactly.
* **Security model**: Deepgram API key exists only in the host environment;
  the client holds a shared secret in a DPAPI blob. Strongly preserved.
* **Provider isolation**: `DeepgramProvider` holds **no** global/shared
  state; one instance per `LiveMedicalSTT`; fresh token per connection
  (`test_a_fresh_token_is_fetched_for_every_connection`). Per-session WebSocket
  means 50 clients cannot affect each other here.
* **Processing pipeline**: `TerminologyEngine` is built once in `__init__`,
  the `ahocorasick` automaton is read-only after `make_automaton()`, and
  `DeterministicFST.apply` allocates only locals. No global mutable state
  anywhere in `medical_stt/` (verified by scan). Session-safe as-is.
* **Clinical safety**: number/negation protection, unsafe-rule blocking and
  low-confidence preservation are structurally enforced and are not touched.
* **Bounded audio queue** with non-blocking `put_nowait` and a lightweight
  microphone callback.
* **`RateLimiter` memory bound**: purge-on-window + hard cap at 10k keys.
* **Shutdown ordering**: sender drains before finalize (tested).

## 4. Shared/global mutable state (scan result)

| Location | Verdict |
|---|---|
| `host/core.py` `RateLimiter._hits` | Mutated under `self._lock`, capped at 10k. Safe; will be extended with per-client keys. |
| `host/app.py` `limiter` | One instance per app, shared by design. |
| `medical_stt/**` | **None.** No module-level mutable state, no `global`, no `lru_cache` on session data. |

## 5. Blocking network operations

* `host/core.py::_httpx_grant` — blocking `httpx.post` in an `async def` path.
  **The only one.** Must become `httpx.AsyncClient` with explicit timeouts,
  reused pool, closed on shutdown.
* `medical_stt/host_client.py` — `urllib.request` on the client's own
  dedicated `stt-provider` thread. Blocking is correct there (one thread per
  session, no shared event loop). Left alone.

## 6. Plan

* Host: async Deepgram grant with a reused, timeout-configured client pool;
  per-client identity (hashed secrets, DPAPI-provisioned); per-client +
  global token-bucket limits and an IP limiter reserved for *auth failures*;
  `session_id`; body-size and header limits; metrics + `/readyz`; graceful
  shutdown.
* Client: send `client_id`, honor `Retry-After`, bound the reconnect storm,
  surface an audio-queue health state, thread `session_id` through the logs.
* Prove it: a concurrency suite that runs 50 simulated clients in CI with a
  mocked Deepgram, plus an opt-in real-provider load test that reports
  application vs provider capacity separately.