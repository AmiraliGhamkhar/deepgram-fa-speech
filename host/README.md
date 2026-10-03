# Medical STT — host service

This is the only component that ever holds the Deepgram API key. It has
two jobs and does nothing else:

1. Authenticate the desktop app with a **shared secret** you control.
2. Exchange that secret for a **short-lived Deepgram session token**
   (30s by default) using Deepgram's documented
   [token-based auth](https://developers.deepgram.com/guides/fundamentals/token-based-authentication)
   endpoint.

The desktop app then opens its WebSocket directly to Deepgram with
`Authorization: Bearer <short-lived token>`. The audio path stays
low-latency, and the long-lived API key never leaves this host.

```
desktop app ──HTTPS POST /v1/session (shared secret)──▶ host ──HTTPS──▶ Deepgram /v1/auth/grant
           ◀────────── { access_token, expires_in } ──┘                  (API key stays here)
           ────── wss://api.deepgram.com/v1/listen (Bearer <token>) ──────▶
```

## Files

| File | Purpose |
|------|---------|
| `core.py` | env config, constant-time auth, rate limiter, Deepgram grant. No web framework. |
| `app.py` | FastAPI wiring: HTTPS enforcement, `POST /v1/session`, `GET /healthz`. |
| `requirements.txt` | `fastapi`, `uvicorn`, `httpx`. The Deepgram SDK is **not** used here. |
| `.env.example` | template for all of the variables below; empty placeholders only. |

## Environment variables

The variables below are read from the **process environment**;
[`host/.env.example`](.env.example) is a ready-to-copy template for your
process manager or secret store (systemd `EnvironmentFile`, Docker
`--env-file`, Kubernetes Secret, ...). The service does not parse an env
file itself, so `host/.env` is only read if you load it, e.g.:

```bash
set -a; . host/.env; set +a
```

`host/.env` is git-ignored — never commit a filled-in copy.

| Variable | Required | Default | Meaning |
|----------|----------|---------|---------|
| `DEEPGRAM_API_KEY` | **yes** | — | The Deepgram key. Exists only here. |
| `HOST_SHARED_SECRET` | **yes** | — | Shared secret, min 24 chars. Must match the app. |
| `HOST_DEFAULT_TTL_SECONDS` | no | `30` | Requested token lifetime. |
| `HOST_MAX_TTL_SECONDS` | no | `3600` | Upper bound a client may request (Deepgram's max). |
| `HOST_RATE_LIMIT_REQUESTS` | no | `30` | Session requests per window, per client IP. |
| `HOST_RATE_LIMIT_WINDOW_SECONDS` | no | `60` | Rate limit window. |
| `HOST_GRANT_TIMEOUT_SECONDS` | no | `10` | Timeout for the Deepgram token request. |
| `HOST_TLS_CERTFILE` / `HOST_TLS_KEYFILE` | no* | — | PEM certificate and key. |
| `HOST_BIND` / `HOST_PORT` | no | `0.0.0.0` / `8443` | Listen address. |
| `HOST_FORWARDED_ALLOW_IPS` | no | `127.0.0.1` | Which peers may set `X-Forwarded-Proto`. |
| `HOST_ALLOW_HTTP` | no | `0` | `1` serves plaintext. **Development only.** |

The service **refuses to start** if `DEEPGRAM_API_KEY` is missing or
`HOST_SHARED_SECRET` is shorter than 24 characters.

Generate a shared secret:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Running it

### Option A — uvicorn with TLS directly (simplest)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r host/requirements.txt

export DEEPGRAM_API_KEY='...'          # or use your secret manager
export HOST_SHARED_SECRET='...'        # the value you will enter in the app
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
rejected as plaintext (covered by `tests/test_host_service.py`).

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host              $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Real-IP         $remote_addr;
}
```

Run the service as a dedicated, unprivileged user.

### Option C — Docker

```bash
docker build -t medical-stt-host .
docker run -d --name medical-stt-host \
  -p 8443:8443 \
  --env-file /secure/path/host.env \
  -v /etc/letsencrypt:/etc/letsencrypt:ro \
  medical-stt-host
```

## Verifying

```bash
curl -s https://stt.example.com/healthz
# {"status":"ok"}

curl -s -X POST https://stt.example.com/v1/session \
  -H "Authorization: Bearer $HOST_SHARED_SECRET" \
  -H 'Content-Type: application/json' \
  -d '{"ttl_seconds": 30}'
# {"access_token":"eyJ...","expires_in":30}
```

A wrong secret returns `401` and is **never** retried by the client; an
over-limit caller returns `429`.

If the grant fails with `502`, the *host's* Deepgram key is usually the
cause — specifically it needs at least **Member** permission to call
`/v1/auth/grant`. Fix the key in the Deepgram console; do not work around
it by giving the key to a client.

## Operational notes

- The in-memory rate limiter is per worker process, and its memory is
  bounded: inactive client entries are reclaimed, so a flood from many
  source addresses cannot grow it without limit. With multiple workers,
  enforce limits in the reverse proxy as well.
- Access logs contain method, path and status only. The shared secret and
  issued tokens are never logged.
- Rotate the shared secret by changing `HOST_SHARED_SECRET` and re-entering
  it in the app. Rotating the Deepgram key requires no client change at all.
