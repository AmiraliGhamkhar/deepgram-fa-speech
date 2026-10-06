# Hosting the session service on cPanel (shared hosting)

This guide deploys the **host service** (`host/`) — the component that
holds the Deepgram API key and mints short-lived session tokens — on a
cPanel shared or VPS account using **Setup Python App (Passenger)**.

> **Why a dedicated guide?** The main host docs assume uvicorn with TLS
> on a VPS. cPanel is different in four ways that each cause an outage if
> missed: Apache sits in front of your app, the app runs under Passenger
> as WSGI (not ASGI), environment variables are set in the UI rather than
> a shell profile, and inbound WebSocket-style long connections are
> proxied by Apache. Follow every step below — each one exists because
> skipping it produces a specific failure, most often the **502 Bad
> Gateway on the Start button**.

```
MedicalSTT.exe ──HTTPS──▶ Apache (cPanel TLS) ──▶ Passenger ──▶ passenger_wsgi.py ──▶ FastAPI app
                                                                            └──▶ Deepgram /v1/auth/grant
```

The audio path is unchanged: clients stream straight to
`wss://api.deepgram.com` with the short-lived token. This host only
answers one small POST per session start, so shared-hosting CPU limits
are not a concern.

---

## 1. What you need before starting

| Item | Where |
|---|---|
| A cPanel account with **Setup Python App** (CloudLinux/easyapache "Passenger" feature) | your host |
| SSH access (Terminal in cPanel works) | cPanel → Terminal |
| A subdomain, e.g. `stt.example.com` | cPanel → Domains |
| A Deepgram API key with **Member** permission | console.deepgram.com |
| Python 3.10 – 3.12 selectable in Setup Python App | cPanel UI |

Check the Python selector first: `python3 --version` in Terminal must be
3.10+ or the app will refuse to start (f-strings and typing syntax).

## 2. Upload the code

In cPanel Terminal (or via Git/GitHub desktop upload):

```bash
cd ~
mkdir -p apps/medical-stt
cd apps/medical-stt
# copy host/ from the repository into this directory, so that:
ls host/         # app.py  core.py  metrics.py  provision.py  passenger_wsgi.py  requirements.txt
```

Only `host/` is needed on the server. Never upload `.env`, `clients.txt`,
or any `*.secret` file.

Create a protected directory for the client registry:

```bash
mkdir -p ~/secure
chmod 700 ~/secure
```

## 3. Create the Python app in cPanel

cPanel → **Setup Python App** → **Create Application**:

| Field | Value |
|---|---|
| Python version | 3.10 or newer |
| Application root | `apps/medical-stt/host` |
| Application URL | `stt.example.com` |
| Application startup file | `passenger_wsgi.py` |
| Expose as / entry point | `application` |

Click **Create**. cPanel builds a virtualenv at
`apps/medical-stt/host/venv` — note the path it prints; every command
below uses it.

## 4. Install dependencies

In Terminal, using the venv cPanel created (path from the previous step):

```bash
source ~/apps/medical-stt/host/venv/bin/activate
pip install --upgrade pip
pip install -r ~/apps/medical-stt/host/requirements.txt
```

That installs `fastapi`, `uvicorn`, `httpx`, and `a2wsgi` (the ASGI→WSGI
bridge Passenger needs). If pip is killed by the host (shared-host memory
limits), install one package at a time.

## 5. Provision client identities

One identity per clinician device. From the same venv:

```bash
cd ~/apps/medical-stt/host

# Single-clinician (legacy) mode: skip this step, use HOST_SHARED_SECRET.
# Multi-clinician (recommended for 2+ devices):
python -m host.provision --count 1 --client-id doctor-01 \
    --out ~/secure/clients.txt
# note: --client-id provisions exactly one device; the secret prints once.
```

Repeat (or use `--count 50 --prefix doctor --secrets-dir ~/secure/secrets`)
for more devices. **Each printed secret goes to exactly one clinician and
is never recoverable from the host later** — save them before closing the
terminal. The registry file contains only SHA-256 hashes and is safe to
keep where it is (mode 700 directory, outside `public_html`).

## 6. Set the environment variables

cPanel → Setup Python App → your app → **Environment variables** (or
`~/apps/medical-stt/host/.env` loaded by the app's wrapper):

| Variable | Value | Required |
|---|---|---|
| `DEEPGRAM_API_KEY` | your Deepgram key (Member permission) | **yes** |
| `HOST_CLIENTS_FILE` | `/home/USER/secure/clients.txt` | multi-clinician |
| `HOST_SHARED_SECRET` | 24+ random chars, *legacy mode only* | if no registry |
| `HOST_ALLOW_HTTP` | `1` | **only if step 7's check says Apache reports `http`** — see below |
| `HOST_FORWARDED_ALLOW_IPS` | *(usually leave unset)* | no — inert on most cPanel hosts, see step 7 |
| `HOST_METRICS_ADMIN_TOKEN` | `python -c "import secrets;print(secrets.token_urlsafe(32))"` | recommended |
| `HOST_BIND` | *(leave unset)* | no — **inert here**; read only by `python -m host.app`, which Passenger never runs. Apache/Passenger own the listener. |
| `HOST_PORT` | *(leave unset)* | no — **inert here**, same reason |
| `HOST_MAX_REQUEST_BODY_BYTES` | `4096` (default) | no |

Do **not** set `HOST_TLS_CERTFILE`/`HOST_TLS_KEYFILE` — Apache owns TLS.
With neither a certificate nor `HOST_ALLOW_HTTP`, `create_app` logs a warning
at import time explaining that every request will be rejected with 400; under
Passenger that warning is the boot diagnostic to look for (see step 8).

Generate the shared secret with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## 7. Make sure the app can tell that the request arrived over HTTPS

The app refuses a plaintext request. It decides in this exact order
(`host/core.py::request_is_secure`):

1. **the WSGI scheme** — `wsgi.url_scheme`, which becomes the ASGI
   `scope["scheme"]`. If Apache reports `https` here, the request is accepted
   and *nothing else matters*. PEP 3333 requires a server to report the
   scheme it received, and Apache terminates TLS in front of Passenger, so on
   most cPanel hosts this is already `https` and there is nothing to do.
2. **`X-Forwarded-Proto`** — consulted *only* if step 1 was not `https`, and
   honoured **only when the immediate peer address is listed in
   `HOST_FORWARDED_ALLOW_IPS`** (loopback by default). When the header carries
   several hops, the **last** one is used.

Step 2 is where cPanel differs from a uvicorn-behind-nginx deployment, and it
is the reason `HOST_FORWARDED_ALLOW_IPS=127.0.0.1` is *not* the fix it looks
like. mod_passenger runs the app inside the Apache process, so `REMOTE_ADDR`
is the **end client's** address, not loopback. A forwarded-proto header from
that peer is deliberately ignored — trusting it would let any client on the
internet forge `X-Forwarded-Proto: https` and have a plaintext request
accepted. Verified through `host/passenger_wsgi.py` with no
`HOST_ALLOW_HTTP`:

| `wsgi.url_scheme` | `REMOTE_ADDR` | `X-Forwarded-Proto` | result |
|---|---|---|---|
| `https` | end client | *(any)* | **200** — step 1 accepted it |
| `http` | end client | `https` | **400** — peer is not a configured proxy |
| `http` | `127.0.0.1` | `https` | **200** — step 2 accepted it |
| `http` | *(absent)* | `https` | **400** — a2wsgi sets `scope["client"]` only when both `REMOTE_ADDR` and `REMOTE_PORT` are present |

**So: find out which row you are in before changing anything.** Add this to
`passenger_wsgi.py`'s directory temporarily, or run it in cPanel → Terminal
against your live app:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://stt.example.com/healthz
# 200 -> Apache reports https. Leave HOST_ALLOW_HTTP unset. You are done.
# 400 -> Apache reports http to Passenger. Read on.
```

If it is 400, set **`HOST_ALLOW_HTTP=1`**. That is the supported answer on
shared hosting: it bypasses the scheme check because Apache has already
enforced TLS on the public side, and the app is not reachable directly. It is
safe *only* for that reason — never set it on a host that is reachable over
plaintext, and never as a substitute for TLS on a VPS.

Adding the Apache header below is still worthwhile as belt-and-braces (it
costs nothing and is ignored when the peer is not trusted), but on a
Passenger host it is **not** what makes the deployment work, and an earlier
version of this guide said it was required:

```apache
# --- Medical STT host: Passenger settings -----------------------------
<IfModule mod_headers.c>
  # Harmless belt-and-braces. Honoured only if the peer address is in
  # HOST_FORWARDED_ALLOW_IPS; under mod_passenger the peer is the end
  # client, so this is normally ignored. See the table above.
  RequestHeader set X-Forwarded-Proto "https"
</IfModule>

# Passenger: keep the app alive and log restarts.
PassengerAppRoot /home/USER/apps/medical-stt/host
PassengerStartupFile passenger_wsgi.py
PassengerAppEnv production
```

Create/edit `.htaccess` **in the document root of the subdomain**
(`~/apps/medical-stt/host/public` or as cPanel shows for the app URL).

One more cPanel-specific wrinkle: if your panel's WSGI environ omits
`REMOTE_PORT`, a2wsgi cannot build `scope["client"]` at all, so the app sees
*no* peer address. That is fine for authentication and for serving requests,
but it means failed-authentication lockout cannot be attributed to an IP —
the host then attributes it to the validated `client_id` instead and logs a
one-line warning at startup saying so. See `host/README.md` > Rate limiting.

## 8. Restart the app

cPanel → Setup Python App → **Restart**. Watch the app's log (the panel
shows a "Log" link, or):

```bash
tail -f ~/apps/medical-stt/host/stdout.log 2>/dev/null || \
tail -f ~/logs/medical-stt.log
```

**Do not look for a `host ready: clients=...` line.** That message is emitted
from the ASGI *lifespan* handler, and a2wsgi does not implement the lifespan
protocol — under Passenger it is never printed, by design. Its absence is not
a failure. (It is also why `app.state.grant_client` and the capacity gauges
are built in `create_app` rather than at startup, so `/readyz` and `/metrics`
report real values here.)

What a healthy boot *does* look like: no traceback, and step 9's `curl`
returns `{"status":"ok"}`. These warnings are normal and informational:

```
HOST_METRICS_ADMIN_TOKEN is not set: /metrics is readable by anyone who can
    reach this port. ...            # set the token, or accept it knowingly
HOST_RATE_LIMIT_REQUESTS is ignored: session requests are now limited per
    authenticated client_id ...     # remove the retired variable
```

A traceback naming `ConfigurationError` means an environment variable is
missing — recheck step 6, then Restart. A warning that every request will be
rejected with 400 means step 7 applies to you.

## 9. Verify from the server

```bash
# 1. Liveness (must be 200)
curl -s https://stt.example.com/healthz
# {"status":"ok"}

# 2. Readiness (must be 200)
curl -s https://stt.example.com/readyz
# {"status":"ready"}

# 3. A real token issuance (multi-clinician mode)
SECRET=$(cat ~/secure/secrets/doctor-01.secret)
curl -s -X POST https://stt.example.com/v1/session \
  -H "Authorization: Bearer $SECRET" \
  -H "X-Client-Id: doctor-01" \
  -H "Content-Type: application/json" \
  -d '{"ttl_seconds":30}'
# {"access_token":"eyJ...","expires_in":30,"session_id":"...","client_id":"doctor-01"}

# Legacy mode instead:
curl -s -X POST https://stt.example.com/v1/session \
  -H "Authorization: Bearer $HOST_SHARED_SECRET" \
  -H "Content-Type: application/json" -d '{}'
```

Any result other than `{"access_token":...}` means the *host* is the
problem — fix it before blaming the Windows app.

## 10. The 502 Bad Gateway on Start — diagnosis

A 502 means **Apache reached Passenger but the app did not answer**. It
is never a client bug. Work through this list in order; each row maps to
a specific cause.

| # | Check | Command / where | What you'll see if this is the cause |
|---|---|---|---|
| 1 | App actually running? | cPanel → Setup Python App → status | **Stopped** — Passenger killed it after a crash at import |
| 2 | App log for the boot error | the log link in the panel | `ConfigurationError: DEEPGRAM_API_KEY is not set` (or `HOST_SHARED_SECRET ... shorter than 24 characters`) |
| 3 | Startup file correct? | Setup Python App: `passenger_wsgi.py`, expose `application` | Wrong file → Passenger boots nothing → 502 on every request |
| 4 | Dependencies in the app's venv? | `source .../venv/bin/activate && pip list` | `ModuleNotFoundError: fastapi` / `a2wsgi` |
| 5 | Registry file readable? | `ls -l ~/secure/clients.txt` (owned by your user, mode 600) | `ConfigurationError: cannot read the client registry ...` in the log |
| 6 | Is the Passenger process alive at all? | `passenger-status` (or the panel's status) | **Stopped / not listed** → it crashed at import; row 2 has the reason. Note that `curl http://127.0.0.1:<port>/healthz` is *not* a clean liveness test here: Passenger does not necessarily listen on a TCP port, and a plaintext request returns **400 by design** unless `HOST_ALLOW_HTTP=1` — which reads like a broken app and is not one |
| 7 | Can the app tell the request was HTTPS? | step 7's `curl -w '%{http_code}'` check | **400** "HTTPS is required" (not 502 — but commonly confused with it). Work through step 7's decision table; on a Passenger host the fix is normally `HOST_ALLOW_HTTP=1`, **not** the `.htaccess` header |
| 8 | Resource limits? | cPanel → Resource Usage | `passenger` hitting the entry-process or memory LVE limit — raise the limit or reduce concurrency |
| 9 | WSGI timeout? | long `curl` to `/v1/session` (>60 s) | Deepgram unreachable **from the host** (firewall) → the grant times out; fix egress to `api.deepgram.com:443` |

The two most common endings:

- **Boot failure** (rows 2–5): the app never came up; fix, then Restart.
- **Timeout** (row 9): the app is up but Deepgram is unreachable from the
  shared server — ask the host provider to allow outbound 443 to
  `api.deepgram.com`, or move to a VPS.

After fixing anything, **Restart** the app in cPanel before retesting.

## 11. Client configuration (on each clinician's Windows machine)

In the floating control window:

- **میزبان (host)** — `https://stt.example.com`
- **کلید مشترک (shared secret)** — that device's secret (step 5)
- Optionally set `host_client_id: doctor-01` in
  `%APPDATA%\MedicalSTT\settings.yaml` so rate limiting and revocation
  are per device.

Press **ذخیره تنظیمات**, then **شروع**. Full client-side troubleshooting
lives in [`WINDOWS_POWERSHELL.md`](WINDOWS_POWERSHELL.md).

## 12. Security notes specific to shared hosting

- **Never** put `clients.txt`, `*.secret`, or a real `.env` inside
  `public_html` — they must be outside it (e.g. `~/secure`), mode 600.
- The Deepgram key lives only in the environment variables of this app.
  Anyone with cPanel access to *this account* can read it: give out
  cPanel credentials accordingly.
- Keep `HOST_METRICS_ADMIN_TOKEN` set; `/metrics` discloses load profile.
- Rotate a device by re-running `host.provision --client-id <id>` and
  giving the clinician the new secret (the host warns that the old one
  stops working). Rotate the Deepgram key from the console — no client
  change needed.
- Shared hosting reboots/respawns apps unpredictably; the client's
  reconnect logic handles short outages transparently.

## 13. Limitations of cPanel hosting

| Concern | Reality on shared cPanel |
|---|---|
| Long-lived connections | Irrelevant here — the host answers one short POST per session start |
| Sustained CPU | Fine: token issuance is ~1 ms of CPU |
| Outbound egress to Deepgram | Must be open; the #1 cause of 502/504 after boot issues |
| Passenger idle respawn | First request after idle can take 1–3 s; the client retries automatically |
| Multi-worker scaling | Not available; run exactly the one Passenger process (the registry and rate limiters are per-process) |
| 50-clinician target | Works if egress is open and LVE limits allow ~64 MB for the process; otherwise move to the VPS/Docker deployment in `host/README.md` |
