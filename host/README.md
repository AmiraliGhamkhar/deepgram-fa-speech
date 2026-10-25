# Medical STT — host service

This is the only component that ever holds the Deepgram API key. It has
three jobs and does nothing else:

1. Authenticate each desktop app by **client identity** (`client_id` + its
   own secret), so 50 clinicians on one hospital network are 50 distinct,
   separately rate-limited, separately revocable identities.
2. Exchange those credentials for a **short-lived Deepgram session token**
   (30s by default) using Deepgram's documented
   [token-based auth](https://developers.deepgram.com/guides/fundamentals/token-based-authentication)
   endpoint.
3. Mint that token **without blocking the event loop**, so 50 simultaneous
   token requests are served concurrently.

The desktop app then opens its WebSocket directly to Deepgram with
`Authorization: Bearer <short-lived token>`. The audio path stays
low-latency, and the long-lived API key never leaves this host.

```
desktop app ──HTTPS POST /v1/session (client id + secret)──▶ host ──HTTPS──▶ Deepgram /v1/auth/grant
              ◀───── { access_token, expires_in, session_id } ───┘                (API key stays here)
           ────── wss://api.deepgram.com/v1/listen (Bearer <token>) ──────▶
```

> **The host never carries audio.** 50 concurrent dictation sessions means
> 50 WebSockets from clients to Deepgram, plus 50 short token requests to
> this host. Nothing here scales with audio volume.

## Files

| File | Purpose |
|------|---------|
| `core.py` | env config, client registry, constant-time auth, token-bucket limiters, async Deepgram grant. No web framework. |
| `app.py` | FastAPI wiring: HTTPS enforcement, `POST /v1/session`, `/healthz`, `/readyz`, `/metrics`. |
| `metrics.py` | dependency-free counters/gauges/histograms in Prometheus text format. |
| `provision.py` | mints `client_id` + secret per device; writes only hashes to the registry. |
| `passenger_wsgi.py` | WSGI entry point for cPanel "Setup Python App" / Passenger; see [`docs/CPANEL_HOSTING.md`](../docs/CPANEL_HOSTING.md). Not used by uvicorn/Docker deployments. |
| `requirements.txt` | `fastapi`, `uvicorn`, `httpx`, `a2wsgi`. The Deepgram SDK is **not** used here. |
| `.env.example` | template for all of the variables below; empty placeholders only. |

## Authentication model

There is **no shared secret in a 50-clinician deployment.**

| | Single clinician | 50 clinicians |
|---|---|---|
| Identity | one (`legacy`) | one per device |
| Credential | `HOST_SHARED_SECRET` | per-device 256-bit secret |
| Stored on host | plaintext env var | SHA-256 hash only |
| Rate limited as | one identity | 50 identities |
| Revoke one device | revokes everything | delete one registry line |

### Provisioning devices

```bash
# 50 clinicians. The registry gets 50 hash lines; the plaintext secrets go
# to 50 separate files, one per device.
python -m host.provision --count 50 --prefix doctor \
    --out /secure/host/clients.txt \
    --secrets-dir ./device-secrets

# Or one device at a time (secret printed once, for manual transfer):
python -m host.provision --client-id doctor-01
```

Then point the service at the registry:

```bash
export HOST_CLIENTS_FILE=/secure/host/clients.txt
```

On each clinician's machine, enter the host URL, the **client id** and that
device's secret in the app's settings panel. The secret is immediately
protected with Windows DPAPI; the client id is an identifier, not a
credential, and may live in `settings.yaml` (or `MEDICALSTT_HOST_CLIENT_ID`
for development).

**The id grammar is ASCII on purpose.** A `client_id` is a rate-limit key,
the handle you delete to revoke a device, and a field in every log line.
`str.isalnum()` is Unicode-aware, so a permissive grammar accepts `саfé-01`
(Cyrillic) as a *different* identity from `safe-01` while looking identical in
an editor and in `grep` — which is exactly what makes revocation unsafe. The
same rule is enforced in three places (`host/core.py`, `host/provision.py`,
`medical_stt/config.py`) and `tests/test_backend_fixes.py::test_all_three_copies_of_the_grammar_agree`
keeps them in step; an id one accepts and another refuses is a device that can
never connect, reported as a credential error.

**Why hashes are enough.** The secret is 256 bits of CSPRNG output, not a
user-chosen password: there is no dictionary to attack, so a slow KDF buys
nothing. What matters is that a leaked registry file or a process memory
dump yields hashes rather than usable credentials.

**Revoking** a device is deleting its line from the registry and restarting.
No other clinician is affected.

### Backward compatibility

With `HOST_CLIENTS_FILE` unset the service still runs, authenticating
`HOST_SHARED_SECRET` as the single identity `legacy`, and the app needs no
`X-Client-Id`. That keeps an existing one-clinician deployment working while
it migrates. **It is not a 50-user configuration** — with one identity the
host can only ever rate limit and revoke one thing.

An **empty** registry file — zero entries, or only comments — is a startup
error rather than a running service that answers `401` to everyone. A
half-finished `rsync` of `clients.txt` otherwise looks perfectly healthy from
the outside while no device on the site can start a session.

Once a registry is configured the client id becomes mandatory: a request
without `X-Client-Id` resolves to `legacy`, which matches nothing, and is
rejected. There is no silent fallback from per-device back to shared auth.

While `HOST_CLIENTS_FILE` is unset the service logs **one warning at
startup**, because this mode is easy to keep by accident: every device shares
a single identity, so the whole site gets one token bucket (10 burst, 6 per
minute sustained), and 50 clinicians pressing Start at once is 10 tokens and
40 `429`s. Clinicians hear "the service is at capacity"; nothing tells the
operator that the deployment was never configured for a clinic.

## Rate limiting

Two layers, both **token buckets** keyed on the authenticated `client_id`
rather than the source address:

| Layer | Key | Default sustained | Default burst |
|---|---|---|---|
| Per client | `client_id` | 6 / window | 10 |
| Global | `global` | 600 / window | 1200 |
| Auth failures | source IP | 20 / window | fixed window |

A token bucket is used rather than a fixed window because **50 clinicians
pressing Start at the same moment is legitimate traffic that must succeed**.
That surge is 50 *devices* spending one token each, so it is absorbed by the
**global** bucket; the per-client bucket is deliberately small. Every token
is a potential Deepgram stream drawn from the pool the whole clinic shares
(see [Deepgram's concurrency limits](#deepgrams-concurrency-limits)), and one
looping or compromised device must not be able to open dozens of them in a
second. A clinician needs one token per Start plus the occasional reconnect
-- the client's own backoff saturates at 15-30 s between attempts -- so a
burst of 10 and 6 per minute never throttles a person, and a shared NAT
address is never penalised.

The per-IP limiter is retained **only for failed authentication**. That is
the one case where a shared address must not exempt anyone: 20 failed
authentications from one address in a window returns 429. Legitimate
requests never spend this budget — it is charged only when a request
actually fails to authenticate.

That budget is keyed on the **direct peer**, not on any forwarded header:
behind a loopback reverse proxy (Apache/Passenger on the same box) the
direct peer is the proxy itself, so all failed authentications share the
one bucket. This is deliberate — a forwarded client address is exactly
what an attacker forges — but it means that behind such a proxy the
brute-force lockout is effectively global rather than per-attacker.
Terminating TLS in the host process itself, or enforcing a per-source
failure limit at the proxy, restores per-source attribution.

`Retry-After` is returned on every 429, and the client honors it (clamped),
so a throttled client slows down when told to instead of guessing.

A `429` is a request that produced no token, so it is counted in
`session_failures_total` as well as in `rate_limited_total`; see
[Monitoring](#monitoring) for the one accounting rule the counters obey.

**Upgrading from a single shared secret?** `HOST_RATE_LIMIT_REQUESTS` is no
longer used — see the table above. It is accepted and warned about rather
than rejected, so an existing configuration keeps loading.

### Sizing for 50 users

Defaults are chosen for the 50-session target with headroom:

```bash
HOST_CLIENT_RATE_LIMIT_REQUESTS=6     # per device, per 60s window
HOST_CLIENT_BURST=10                  # one device can hold at most ~10 streams
HOST_GLOBAL_RATE_LIMIT_REQUESTS=600
HOST_GLOBAL_BURST=1200                # > 50, so a startup surge never 429s
HOST_AUTH_FAILURE_LIMIT=20
```

A clinician requests one token per session start (and one per reconnect),
so 6/minute sustained is several times what a person can generate; the
per-client bucket exists to catch a misbehaving device, not a busy one, and
to bound how much of the shared Deepgram pool a single device can take.

### Deepgram's concurrency limits

The host limits *token requests*; the number of *streams* that may be open
at once is decided by Deepgram, **per project**, and the host cannot see
it. Published limits for Nova-3 streaming (verify against
[Deepgram's rate-limit page](https://developers.deepgram.com/reference/api-rate-limits)
for your plan):

| Plan | Concurrent streams |
|---|---|
| Pay-as-you-go | 150 in every region |
| Growth | 225 in the US, 150 in the EU, AU and IN regions |
| Enterprise | by contract |

Three facts matter for a deployment:

* **All API keys of one project share one pool.** Creating a second key does
  not add capacity.
* **Secondary projects on a self-serve account are capped at 1 stream.**
  Deploy on the account's primary project; "a separate project for the
  clinic" on an existing pay-as-you-go account gives every second clinician
  a 429.
* Pay-as-you-go and Growth limits cannot be raised; only Enterprise can.

When the pool is full, Deepgram rejects the WebSocket handshake with
**429**. The host never sees that: it already issued the token and counted
`session_success_total`. The client classifies it as a rate limit, shows
"ظرفیت سرویس گفتار پر است" and retries with backoff until a slot frees. If
that message appears while fewer than 50 clinicians are dictating, check
which project the key belongs to before anything else.

## Environment variables

Read from the **process environment**; `host/.env.example` is a
ready-to-copy template. Load it with your process manager, or:

```bash
set -a; . host/.env; set +a
```

`host/.env` is git-ignored — never commit a filled-in copy.

| Variable | Required | Default | Meaning |
|----------|----------|---------|---------|
| `DEEPGRAM_API_KEY` | **yes** | — | The Deepgram key. Exists only here. Must be a printable ASCII token with no whitespace: a smart quote or stray newline from a paste makes the HTTP transport refuse the `Authorization` header, so it is checked at startup rather than failing every clinician's first Start. |
| `DEEPGRAM_REGION` | no | `us` | Deepgram region the grant request goes to: `us`, `eu`, `au` or `in`. Tokens are valid in every region, so this only decides where the host's own traffic terminates; the client has its own `deepgram_region` setting for the audio. Unknown values refuse to start. |
| `HOST_CLIENTS_FILE` | no* | — | `client_id:sha256hex` registry, ids limited to 1-64 ASCII `[A-Za-z0-9._-]`. Refuses to start if any line is malformed **or if the file lists no client at all**; duplicated ids load with a warning (last line wins). *Required for many clinicians. |
| `HOST_SHARED_SECRET` | if no registry | — | Legacy single identity. |
| `HOST_DEFAULT_TTL_SECONDS` | no | `30` | Requested token lifetime. |
| `HOST_MAX_TTL_SECONDS` | no | `3600` | Upper bound a client may request. Keep it at or above every deployed client's `session_ttl_seconds`: requests outside 5..max are rejected with 422 (never retried), not clamped. |
| `HOST_CLIENT_RATE_LIMIT_REQUESTS` | no | `6` | Per-client sustained rate per window. |
| `HOST_CLIENT_BURST` | no | `10` | Per-client burst capacity; bounds how many Deepgram streams one device can open at once. |
| `HOST_GLOBAL_RATE_LIMIT_REQUESTS` | no | `600` | Global sustained rate per window. |
| `HOST_GLOBAL_BURST` | no | `1200` | Global burst capacity. |
| `HOST_AUTH_FAILURE_LIMIT` | no | `20` | Failed-auth budget per window, keyed on the peer address, or on the validated `client_id` when the server supplies no peer address. |
| `HOST_RATE_LIMIT_WINDOW_SECONDS` | no | `60` | Window for all of the above. |
| `HOST_RATE_LIMIT_REQUESTS` | no | — | **Ignored.** This was the old per-IP fixed-window budget. Session requests are now limited per authenticated `client_id`, so it has nothing to apply to. It is still accepted (so existing configs keep loading) and the service logs a warning when it is set, but changing it has no effect. |
| `HOST_GRANT_TIMEOUT_SECONDS` | no | `10` | Deepgram read/write/pool timeout. |
| `HOST_GRANT_CONNECT_TIMEOUT_SECONDS` | no | `5` | Deepgram connect timeout. |
| `HOST_GRANT_MAX_CONNECTIONS` | no | `100` | HTTP pool size. |
| `HOST_GRANT_MAX_KEEPALIVE_CONNECTIONS` | no | `20` | Pool keepalive size. |
| `HOST_MAX_INFLIGHT_GRANTS` | no | `100` | Concurrent grants; beyond this, 503. |
| `HOST_MAX_ACTIVE_REQUESTS` | no | `128` | Total admitted session requests, including body readers; excess gets 503. |
| `HOST_REQUEST_BODY_TIMEOUT_SECONDS` | no | `2.0` | Wall-clock limit to read a session body; expiry gets 408. |
| `HOST_GRANT_DEADLINE_SECONDS` | no | `7.0` | Whole upstream grant deadline (all HTTP phases); expiry gets 504. |
| `HOST_MAX_REQUEST_BODY_BYTES` | no | `4096` | Request body cap (413 beyond). |
| `HOST_METRICS_ADMIN_TOKEN` | no | — | If set, `/metrics` requires it. |
| `HOST_TLS_CERTFILE` / `HOST_TLS_KEYFILE` | no* | — | PEM certificate and key. |
| `HOST_BIND` / `HOST_PORT` | no | `0.0.0.0` / `8443` | Listen address. Read only by `python -m host.app`; a WSGI host (cPanel/Passenger) owns its own listener and ignores both. |
| `HOST_FORWARDED_ALLOW_IPS` | no | `127.0.0.1` | Which peers may set `X-Forwarded-Proto`. |
| `HOST_ALLOW_HTTP` | no | `0` | `1` serves plaintext. **Development only.** |

The service **refuses to start** if `DEEPGRAM_API_KEY` is missing or is not a
printable ASCII token with no internal whitespace, if `HOST_CLIENTS_FILE`
contains a malformed entry, or if that file **lists no client at all**.
When `HOST_CLIENTS_FILE` is unset (legacy mode), `HOST_SHARED_SECRET` must be at least 24 characters.
When a registry is configured, `HOST_SHARED_SECRET` is ignored.

Every one of those is a startup refusal rather than a warning because each one
leaves a *running* service that no clinician can use: a key the HTTP transport
cannot send answers 502/500 forever, and a registry with zero entries answers
401 to everybody while `/healthz` stays green. Fail the deploy, not the
clinic. A key-shape refusal names the offending offset and character class and
never echoes the value.

## Concurrency

`/v1/session` performs an HTTPS POST to Deepgram on the event loop. That
call is made with a **single reused `httpx.AsyncClient`**, created at
startup and closed on shutdown, with explicit connect/read/write/pool
timeouts and a warm keepalive pool.

> The pre-hardening implementation called the *synchronous* `httpx.post()`
> from inside an `async def` handler. Every token request froze the entire
> event loop for the full Deepgram round trip, so 50 simultaneous clients
> serialized and each was delayed by the other 49. This is measured
> directly in `tests/test_host_concurrency.py::test_token_issuance_does_not_block_the_event_loop`.

Bounded by construction:

* request bodies capped at 4 KB (413 before buffering);
* rate-limit tables bounded and self-expiring (see `MAX_TRACKED_CLIENTS`);
* in-flight grants capped at `HOST_MAX_INFLIGHT_GRANTS`, beyond which the
  host returns **503** — an explicit controlled error, never a silent drop
  and never an unbounded queue;
* metrics label cardinality capped, with overflow folded away.

**What the upstream answer has to look like.** Only a `2xx` that can carry a
body is treated as a grant; `3xx` is refused even though `follow_redirects` is
already off, because a captive portal, an Apache `Redirect` rule or a
TLS-intercepting proxy that answers with JSON would otherwise have *its* body
parsed into a token; `204`/`205`/`206` are refused for the mirror-image reason.
A `Retry-After` from Deepgram is forwarded to the client. And every failure of
the upstream call — timeout, refusal, an unencodable key, a pool closed during
shutdown — arrives as a `GrantError` with a 502/503/504, never as a bare 500:
an unhandled exception is invisible to `token_request_failures_total`, which
would leave a fully broken host reporting zero failures.

## Running it

### Option A — uvicorn with TLS directly (simplest)

```bash
python -m venv .venv && . .venv/bin/activate
pip install --require-hashes -r requirements/host.lock

export DEEPGRAM_API_KEY='...'            # or use your secret manager
export HOST_CLIENTS_FILE=/secure/host/clients.txt
export HOST_TLS_CERTFILE=/etc/letsencrypt/live/stt.example.com/fullchain.pem
export HOST_TLS_KEYFILE=/etc/letsencrypt/live/stt.example.com/privkey.pem

python -m host.app
```

### Option A2 — local loopback host on Windows (verified)

Single-machine setup. Keep every secret outside the repo
(`%USERPROFILE%\.medical-stt\`, git-ignored by `*.pem` / `*.secret` /
`clients.txt`) and pass the Deepgram key as a process env var only — a
fresh console key, never a value that touched chat or git:

```powershell
python -m pip install cryptography
python scripts\make_local_cert.py  # self-signed localhost + 127.0.0.1
python -m host.provision --client-id doctor-01 `
  --out $env:USERPROFILE\.medical-stt\secure\clients.txt `
  --secrets-dir $env:USERPROFILE\.medical-stt\device-secrets

$env:DEEPGRAM_API_KEY='<fresh-key>'
$env:HOST_CLIENTS_FILE="$env:USERPROFILE\.medical-stt\secure\clients.txt"
$env:HOST_TLS_CERTFILE="$env:USERPROFILE\.medical-stt\secure\localhost-cert.pem"
$env:HOST_TLS_KEYFILE="$env:USERPROFILE\.medical-stt\secure\localhost-key.pem"
python -m host.app  # https://127.0.0.1:8443
```

`/healthz` → `{"status":"ok"}`, `/readyz` → `{"status":"ready"}`.
Point the app at `https://127.0.0.1:8443` (accept the self-signed cert
once) with client `doctor-01` and that device's secret.

### Option B — behind a reverse proxy (recommended)

Terminate TLS in nginx/Caddy and forward to a loopback port. Set
`HOST_TLS_CERTFILE=`/`HOST_TLS_KEYFILE=` empty and let the proxy set
`X-Forwarded-Proto: https`; the app verifies that header and refuses
plaintext otherwise. Keep `HOST_FORWARDED_ALLOW_IPS` set to the proxy's
address only. The header is honoured for that peer only, using the last
hop's value — a client connecting directly and forging the header is still
rejected as plaintext (`tests/test_host_service.py`).

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host              $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Real-IP         $remote_addr;
    client_max_body_size 8k;   # matches HOST_MAX_REQUEST_BODY_BYTES
}
```

Run the service as a dedicated, unprivileged user.

### Option C — Docker

```bash
docker build -t medical-stt-host .
docker run -d --name medical-stt-host \
  -p 8443:8443 \
  --env-file /secure/path/host.env \
  -v /secure/host/clients.txt:/srv/host/clients.txt:ro \
  -v /etc/letsencrypt:/etc/letsencrypt:ro \
  medical-stt-host
```

The image runs as uid 10001, contains no secret in any layer, ships a
`/readyz` healthcheck, and handles `SIGTERM` for graceful shutdown.

### Scaling

**Run one worker.** The client registry and limiters are per-process. With
multiple workers, rate limits multiply and revocation requires restarting
all of them. To scale past one host, front several instances with a load
balancer and move rate limiting to the proxy; the application-side limits
remain as defence in depth.

## Verifying

```bash
curl -s https://stt.example.com/healthz          # {"status":"ok"}
curl -s https://stt.example.com/readyz           # {"status":"ready"}

curl -s -X POST https://stt.example.com/v1/session \
  -H "Authorization: Bearer $CLIENT_SECRET" \
  -H "X-Client-Id: doctor-01" \
  -H 'Content-Type: application/json' \
  -d '{"ttl_seconds": 30}'
# {"access_token":"eyJ...","expires_in":30,"session_id":"...","client_id":"doctor-01"}
```

A wrong secret returns `401` and is **never** retried by the client; an
over-limit caller returns `429` with `Retry-After`.

If the grant fails with `502`, the *host's* Deepgram key is usually the
cause — specifically it needs at least **Member** permission to call
`/v1/auth/grant`. Fix the key in the Deepgram console; do not work around
it by giving the key to a client.

## Monitoring

`GET /metrics` (Prometheus text format, no exporter dependency). Set
`HOST_METRICS_ADMIN_TOKEN` to require `Authorization: Bearer <token>`;
`/healthz` and `/readyz` stay open for load balancers. **Without that token
`/metrics` is readable by anyone who can reach the port** — it carries no
credential and no transcript, but it does disclose traffic volume and the
registered client count, so set it on any host that is not loopback-only.
The endpoint is also **not rate-limited**: rendering is cheap (a pass over
the in-memory counters), so nothing needs the protection, but scrape
frequency is bounded only by whatever sits in front of the port — one
more reason to set the token or block the path at the gateway.

Published series — every one of them is a value this process actually
updates:

| Metric | Type | Meaning |
|---|---|---|
| `inflight_grants` | gauge | Deepgram grant requests in flight right now |
| `inflight_grants_capacity` | gauge | `HOST_MAX_INFLIGHT_GRANTS`; past it, 503 |
| `registry_clients` | gauge | client identities the host can authenticate |
| `session_starts_total` | counter | authenticated session requests received |
| `session_success_total` | counter | tokens issued |
| `session_failures_total` | counter | authenticated requests that produced no token: rate-limited, malformed body, refused at capacity, or failed upstream |
| `session_requests_by_client_total` | counter | the same outcomes per device: `{client_id, outcome}` with `outcome` in `ok`, `rate_limited`, `body_too_large`, `body_timeout`, `incomplete_body`, `invalid_request`, `upstream_saturated`, `upstream_error`, `internal_error`, `cancelled` (the client disconnected before a token was minted) |

`session_success_total + session_failures_total == session_starts_total` is the
invariant every terminal outcome in the route must preserve — a request that
produced no token is a failure, including the ones the host refuses itself.
Without it, "nobody is calling" and "everything is failing" render
identically, which is the one distinction a scrape of this service exists to
make. It is asserted in
`tests/test_backend_fixes.py::test_every_authenticated_request_is_counted_as_success_or_failure`.

`session_requests_by_client_total` is the only labelled series, and it is
labelled with the **authenticated** id, never a presented one: fabricated ids
cannot inflate the cardinality bound or attribute someone else's traffic.
| `token_requests_total`, `token_request_failures_total` | counter | upstream grant attempts / failures |
| `deepgram_connection_failures_total` | counter | grant failed because Deepgram was unreachable or too slow (503/504) |
| `auth_failures_total` | counter | rejected `POST /v1/session` credentials |
| `metrics_auth_failures_total` | counter | rejected `/metrics` admin tokens (counted separately, so a stale scraper does not look like credential stuffing) |
| `rate_limited_total` | counter | 429s issued (client, global, or brute-force budget) |
| `request_body_rejections_total` | counter | bodies refused over `HOST_MAX_REQUEST_BODY_BYTES` |
| `token_request_latency` | histogram | seconds per upstream grant, cumulative buckets |

Per-session audio and transcript timings are **not** published here, by
design: the host mints a token and never sees the stream, so `active_sessions`,
`audio_queue_depth` or `session_duration` would be a permanent, plausible
zero that a dashboard reads as "clinic idle". The client-side equivalents
are on `LiveMedicalSTT.session_health` and in the app log.

Alerts worth having:

* `rate_limited_total` rising above ~0 — a device is looping (expected to
  be throttled) or, if many client ids are affected, `HOST_GLOBAL_BURST` is
  below your real startup surge.
* `inflight_grants` sitting at `inflight_grants_capacity` — Deepgram is
  slower than your arrival rate and the host is shedding load.
* `token_request_latency` p95 > 2s — Deepgram or the pool is the bottleneck.
  Read it from the cumulative buckets (`histogram_quantile`), not from an
  average of `token_request_latency_sum / _count`.
* `auth_failures_total` spiking — credential stuffing or a stale device.

Metrics contain counters, durations and client ids. **Never** a token, a
secret, or transcript text.

Session logs are keyed by `session_id`:

```
session_started session_id=… client_id=doctor-01
token_issued    session_id=… client_id=doctor-01
session_error   session_id=… status=503
```

## Load testing

CI runs the mocked 50-client suite on every push. No Deepgram credit is
spent.

```bash
pytest tests/test_host_concurrency.py -v      # 50 simultaneous token requests
pytest tests/test_session_concurrency.py -v   # 50 concurrent sessions
```

Opt-in, long-running:

```bash
# 50 clients, 30 minutes, mocked Deepgram. Prints a full resource report.
MEDICAL_STT_SOAK=1 pytest tests/test_soak.py -v -s
```

Opt-in, **real** Deepgram — the only test that spends credit:

```bash
# Set MEDICALSTT_HOST_URL and provisioned credentials through your secure
# environment; do NOT put secrets in shell history. For distinct devices,
# MEDICAL_STT_LOAD_CLIENTS_FILE points to an external *.secret JSON list
# of {"client_id": ..., "secret": ...} objects (at least 50 entries).
MEDICAL_STT_LOAD_TEST=1 pytest tests/test_live_load.py -m loadtest -v -s
```

It opens 10, then 25, then 50 concurrent Nova-3 `fa` streams and reports
the application and provider results **separately**:

```
APPLICATION CAPACITY: PASS
PROVIDER CAPACITY: FAIL
```

That distinction matters. Whether this application can mint tokens and open
WebSockets is something the test proves. Whether your Deepgram plan allows
50 simultaneous streams is a commercial property of your account — the test
reports it rather than hiding it or working around it. Confirm your
project's concurrency allowance before promising 50 users: see
[Deepgram's concurrency limits](#deepgrams-concurrency-limits) above for the
published per-project numbers and the secondary-project trap.

## Operational notes

- The in-memory limiters are per worker and bounded; inactive keys are
  reclaimed and the tables are hard-capped.
- Access logs contain method, path, status and `session_id`. Secrets and
  issued tokens are never logged.
- Rotating one device's secret means re-running `host/provision` for it and
  replacing both its registry line and its DPAPI blob. Rotating the
  Deepgram key requires no client change at all.

## Performance-audit hardening (2026-10-14)

- Session bodies must be JSON objects; `ttl_seconds`, when supplied, must be
  an integer from 5 through `HOST_MAX_TTL_SECONDS`. Invalid input returns
  422, excessive bytes 413, and an incomplete body 400. Never mint a token
  after an invalid/disconnected request.
- The 2 s body + 7 s grant budgets leave a small network allowance inside the
  desktop's default 10 s host timeout. Set the client's budget above their
  sum plus your measured network margin if deployment requires longer waits.
- Effective grant concurrency is the smaller of `HOST_MAX_INFLIGHT_GRANTS`
  and `HOST_GRANT_MAX_CONNECTIONS`; increase both intentionally, not blindly.
- `session_http_requests_total`, `session_http_outcomes_total{status=...}`,
  `session_http_latency`, and `active_session_requests` describe the entire
  route, including rejected input, capacity rejection and cancellation.
  `token_requests_total` / `token_request_failures_total` describe actual
  upstream attempts only; `grant_capacity_rejections_total` is separate.
  `session_success_total + session_failures_total == session_starts_total`
  holds for every authenticated request, so the `session_*` trio *is* a
  success ratio for the route — which is what makes a divergence during an
  incident mean a code path was added without its accounting.
- Failed-auth lockout still throttles bad credentials, but valid credentials
  bypass that budget so a broken device/attacker cannot lock out colleagues
  sharing a hospital NAT. Per-device and global authenticated limits still apply.
- `/readyz` is **local pool readiness**, not a Deepgram reachability or
  account-key check. Alert separately on recent grant failures. Do not spend
  a token on every readiness probe.
- **Production:** configure `HOST_METRICS_ADMIN_TOKEN` or block `/metrics` at
  the gateway; the compatibility default is intentionally still optional.
- One worker is the supported deployment. A registry shared on disk is NOT
  shared rate-limit/capacity state. Multiple workers/replicas require gateway
  or distributed rate limits and deliberate metric aggregation.
- Registry updates/revocation require worker restart. Revoking a device
  prevents new tokens, not streams already authenticated directly to Deepgram.
- Passenger has a best-effort same-loop pool-close hook for normal process
  exit. Abrupt process termination still relies on OS socket cleanup; measure
  worker/thread ceilings and graceful-drain behavior on your actual host.
- Set proxy header/body read timeouts and connection limits as well as the
  application bounds. Restrict direct backend access. The Docker loopback
  probe supplies HTTPS metadata only from its trusted local peer; if you
  override `HOST_FORWARDED_ALLOW_IPS`, retain loopback for that probe or use
  native TLS. Test direct-TLS and reverse-proxy deployments separately.
- HTTP request logging remains synchronous. Benchmark the production log sink
  before adding a logging queue; the audit did not demonstrate it as a bottleneck.
