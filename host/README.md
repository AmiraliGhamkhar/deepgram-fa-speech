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

Once a registry is configured the client id becomes mandatory: a request
without `X-Client-Id` resolves to `legacy`, which matches nothing, and is
rejected. There is no silent fallback from per-device back to shared auth.

## Rate limiting

Two layers, both **token buckets** keyed on the authenticated `client_id`
rather than the source address:

| Layer | Key | Default sustained | Default burst |
|---|---|---|---|
| Per client | `client_id` | 60 / window | 120 |
| Global | `global` | 600 / window | 1200 |
| Auth failures | source IP | 20 / window | fixed window |

A token bucket is used rather than a fixed window because **50 clinicians
pressing Start at the same moment is legitimate traffic that must succeed**.
The bucket makes a burst available immediately while bounding the sustained
rate, so a looping client is throttled without a shared NAT address being
penalised.

The per-IP limiter is retained **only for failed authentication**. That is
the one case where a shared address must not exempt anyone: 20 failed
authentications from one address in a window returns 429. Legitimate
requests never spend this budget — it is charged only when a request
actually fails to authenticate.

`Retry-After` is returned on every 429, and the client honors it (clamped),
so a throttled client slows down when told to instead of guessing.

**Upgrading from a single shared secret?** `HOST_RATE_LIMIT_REQUESTS` is no
longer used — see the table above. It is accepted and warned about rather
than rejected, so an existing configuration keeps loading.

### Sizing for 50 users

Defaults are chosen for the 50-session target with headroom:

```bash
HOST_CLIENT_RATE_LIMIT_REQUESTS=60    # per clinician, per 60s window
HOST_CLIENT_BURST=120                 # > 50, so a startup surge never 429s
HOST_GLOBAL_RATE_LIMIT_REQUESTS=600
HOST_GLOBAL_BURST=1200
HOST_AUTH_FAILURE_LIMIT=20
```

A clinician requests one token per session start, so a sustained rate of
60/minute is ~60× what normal use needs; the bucket exists to catch a
misbehaving client, not a busy one.

## Environment variables

Read from the **process environment**; `host/.env.example` is a
ready-to-copy template. Load it with your process manager, or:

```bash
set -a; . host/.env; set +a
```

`host/.env` is git-ignored — never commit a filled-in copy.

| Variable | Required | Default | Meaning |
|----------|----------|---------|---------|
| `DEEPGRAM_API_KEY` | **yes** | — | The Deepgram key. Exists only here. |
| `HOST_CLIENTS_FILE` | no* | — | `client_id:sha256hex` registry. *Required for many clinicians. |
| `HOST_SHARED_SECRET` | if no registry | — | Legacy single identity. |
| `HOST_DEFAULT_TTL_SECONDS` | no | `30` | Requested token lifetime. |
| `HOST_MAX_TTL_SECONDS` | no | `3600` | Upper bound a client may request. |
| `HOST_CLIENT_RATE_LIMIT_REQUESTS` | no | `60` | Per-client sustained rate per window. |
| `HOST_CLIENT_BURST` | no | `120` | Per-client burst capacity. |
| `HOST_GLOBAL_RATE_LIMIT_REQUESTS` | no | `600` | Global sustained rate per window. |
| `HOST_GLOBAL_BURST` | no | `1200` | Global burst capacity. |
| `HOST_AUTH_FAILURE_LIMIT` | no | `20` | Failed auths per IP per window. |
| `HOST_RATE_LIMIT_WINDOW_SECONDS` | no | `60` | Window for all of the above. |
| `HOST_RATE_LIMIT_REQUESTS` | no | — | **Ignored.** This was the old per-IP fixed-window budget. Session requests are now limited per authenticated `client_id`, so it has nothing to apply to. It is still accepted (so existing configs keep loading) and the service logs a warning when it is set, but changing it has no effect. |
| `HOST_GRANT_TIMEOUT_SECONDS` | no | `10` | Deepgram read/write/pool timeout. |
| `HOST_GRANT_CONNECT_TIMEOUT_SECONDS` | no | `5` | Deepgram connect timeout. |
| `HOST_GRANT_MAX_CONNECTIONS` | no | `100` | HTTP pool size. |
| `HOST_GRANT_MAX_KEEPALIVE_CONNECTIONS` | no | `20` | Pool keepalive size. |
| `HOST_MAX_INFLIGHT_GRANTS` | no | `100` | Concurrent grants; beyond this, 503. |
| `HOST_MAX_REQUEST_BODY_BYTES` | no | `4096` | Request body cap (413 beyond). |
| `HOST_METRICS_ADMIN_TOKEN` | no | — | If set, `/metrics` requires it. |
| `HOST_TLS_CERTFILE` / `HOST_TLS_KEYFILE` | no* | — | PEM certificate and key. |
| `HOST_BIND` / `HOST_PORT` | no | `0.0.0.0` / `8443` | Listen address. |
| `HOST_FORWARDED_ALLOW_IPS` | no | `127.0.0.1` | Which peers may set `X-Forwarded-Proto`. |
| `HOST_ALLOW_HTTP` | no | `0` | `1` serves plaintext. **Development only.** |

The service **refuses to start** if `DEEPGRAM_API_KEY` is missing, or if
`HOST_SHARED_SECRET` is set but shorter than 24 characters, or if
`HOST_CLIENTS_FILE` contains a malformed entry.

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

## Running it

### Option A — uvicorn with TLS directly (simplest)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r host/requirements.txt

export DEEPGRAM_API_KEY='...'            # or use your secret manager
export HOST_CLIENTS_FILE=/secure/host/clients.txt
export HOST_TLS_CERTFILE=/etc/letsencrypt/live/stt.example.com/fullchain.pem
export HOST_TLS_KEYFILE=/etc/letsencrypt/live/stt.example.com/privkey.pem

python -m host.app
```

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
`/healthz` and `/readyz` stay open for load balancers.

Published series:

| Metric | Type |
|---|---|
| `active_sessions`, `deepgram_connections_active`, `audio_queue_depth` | gauge |
| `session_starts_total`, `session_success_total`, `session_failures_total`, `session_reconnects_total` | counter |
| `token_requests_total`, `token_request_failures_total`, `auth_failures_total`, `rate_limited_total` | counter |
| `deepgram_connection_failures_total`, `audio_queue_drops_total` | counter |
| `token_request_latency`, `session_duration`, `first_interim_latency`, `final_transcript_latency` | histogram |

Alerts worth having:

* `rate_limited_total` rising above ~0 — legitimate clients are being
  refused; check `HOST_CLIENT_BURST` against your real startup surge.
* `token_request_latency` p95 > 2s — Deepgram or the pool is the bottleneck.
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
MEDICAL_STT_LOAD_TEST=1 DEEPGRAM_API_KEY=… pytest tests/test_live_load.py -m loadtest -v -s
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
project's concurrency allowance before promising 50 users.

## Operational notes

- The in-memory limiters are per worker and bounded; inactive keys are
  reclaimed and the tables are hard-capped.
- Access logs contain method, path, status and `session_id`. Secrets and
  issued tokens are never logged.
- Rotating one device's secret means re-running `host/provision` for it and
  replacing both its registry line and its DPAPI blob. Rotating the
  Deepgram key requires no client change at all.