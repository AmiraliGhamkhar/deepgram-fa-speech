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
| `HOST_ALLOW_HTTP` | `1` | **yes (cPanel only)** — Apache terminates TLS for you |
| `HOST_FORWARDED_ALLOW_IPS` | `127.0.0.1` | **yes (cPanel only)** |
| `HOST_METRICS_ADMIN_TOKEN` | `python -c "import secrets;print(secrets.token_urlsafe(32))"` | recommended |
| `HOST_BIND` | `127.0.0.1` | recommended |
| `HOST_MAX_REQUEST_BODY_BYTES` | `4096` (default) | no |

Do **not** set `HOST_TLS_CERTFILE`/`HOST_TLS_KEYFILE` — Apache owns TLS.

Generate the shared secret with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## 7. Make Apache forward the HTTPS flag and disable buffering

Passenger runs the app on loopback; Apache proxies it. Two Apache rules
are **required** or the app will reject (or mis-handle) requests:
`X-Forwarded-Proto: https` (the app refuses plaintext without it) and
`Connection close`-friendly buffering off for the tiny JSON responses.

Create/edit `.htaccess` **in the document root of the subdomain**
(`~/apps/medical-stt/host/public` or as cPanel shows for the app URL):

```apache
# --- Medical STT host: proxy rules (required) -----------------------
# Tell the app the request arrived over HTTPS (Apache already terminated
# TLS). Without this the app returns 400 "HTTPS is required".
<IfModule mod_headers.c>
  RequestHeader set X-Forwarded-Proto "https"
</IfModule>

# Passenger: keep the app alive and log restarts.
PassengerAppRoot /home/USER/apps/medical-stt/host
PassengerStartupFile passenger_wsgi.py
PassengerAppEnv production
```

If your host does not allow `RequestHeader` in `.htaccess`, open a ticket
asking them to add `RequestHeader set X-Forwarded-Proto "https"` to the
vhost — without it the app **cannot** distinguish HTTPS from plaintext
and will 400 every request (or you must keep `HOST_ALLOW_HTTP=1`, which
is safe **only** because Apache enforces TLS on the public side).

## 8. Restart the app

cPanel → Setup Python App → **Restart**. Watch the app's log (the panel
shows a "Log" link, or):

```bash
tail -f ~/apps/medical-stt/host/stdout.log 2>/dev/null || \
tail -f ~/logs/medical-stt.log
```

A healthy boot prints:

```
host ready: clients=1 client_burst=120 global_burst=1200 max_inflight=100
```

A traceback naming `ConfigurationError` means an environment variable is
missing — recheck step 6, then Restart.

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
| 6 | Hit the app directly, bypassing Apache | `curl -s http://127.0.0.1:<passenger_port>/healthz` (port from `passenger-status`) | Works directly but 502 via Apache → Apache/Passenger proxy misconfig, not the app |
| 7 | `.htaccess` X-Forwarded-Proto rule present? | step 7 | 400 "HTTPS is required" (not 502 — but commonly confused with it) |
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
