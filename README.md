# Medical STT

Real-time Persian + English medical dictation for Windows.

**Microphone → short-lived Deepgram session (minted by your own host) →
deterministic normalization → medical terminology processing → RTL/BiDi
formatting → Windows text injection → floating overlay + Start/Stop
control.**

This is a **deterministic, rule-based dictation aid**, not a clinical NLP
system and not a medical device. See [Limitations](#limitations) before
using it in any clinical workflow.

---

## Security model (non-negotiable)

**The Deepgram API key lives only on a self-hosted server you control.**
It is never placed in the executable, never logged, never stored on a
clinician's machine.

```
                    ┌──────────────── your host (has DEEPGRAM_API_KEY) ───┐
  MedicalSTT.exe ───┤  POST /v1/session  (client id + secret, DPAPI-stored) │
  (no API key)      │        └──► Deepgram /v1/auth/grant ──► JWT (30s)  │
        │           └────────────────────────────────────────────────────┘
        │
        └──── wss://api.deepgram.com/v1/listen  (Authorization: Bearer <JWT>)
```

The desktop app:

- authenticates to the host with its own **client id + per-device secret**
  (a single shared secret is still accepted for one-clinician installs);
- receives a **short-lived session token** (30s by default — it only has
  to be valid for the WebSocket handshake) plus a `session_id` used only for
  logs and metrics;
- stores that secret locally **only** as a Windows DPAPI-protected
  blob (`CryptProtectData`, user scope) in
  `%APPDATA%\MedicalSTT\host_secret.dpapi`;
- never writes a secret into `settings.yaml` (a plaintext secret there is
  a hard configuration **error**, not a warning).

This follows Deepgram's documented
[token-based authentication](https://developers.deepgram.com/guides/fundamentals/token-based-authentication)
pattern, so audio still streams directly to Deepgram with no proxy in the
path.

### What is explicitly *not* here

- No Deepgram API key in the EXE, in the build stamp, or in any file it
  writes. `Settings` has no `api_key` field at all.
- No multi-provider routing, no key rotation, no account pooling.
- No LLM anywhere in the transcription or correction path.
- No demo expiry or time bomb.

---

## Architecture

```
medical_stt/
├── app.py                 # wires the pipeline; Start/Stop session controller
├── paths.py               # %APPDATA%\MedicalSTT locations
├── config.py              # settings + YAML loading & validation, first run
├── host_client.py         # fetches a short-lived session from the host
├── app_instance.py        # single-instance mutex
├── security/
│   ├── dpapi.py           # CryptProtectData / CryptUnprotectData via ctypes
│   └── secret_store.py    # DPAPI-protected local secret storage
├── audio/
│   └── queue.py           # bounded, instrumented audio queue (with drain)
├── stt/
│   ├── base.py            # STTProvider interface + ErrorCategory taxonomy
│   ├── deepgram_provider.py  # Deepgram via host-issued session tokens
│   └── reconnect.py        # exponential backoff + retry/no-retry policy
├── processing/
│   ├── normalize.py        # Unicode/digit/whitespace normalization
│   ├── numbers.py           # protects clinical numeric expressions
│   ├── negation.py          # protects negation/fidelity phrases
│   ├── fst.py               # deterministic longest-match rewriter
│   ├── terminology.py       # safety-categorized terminology engine
│   └── bidi.py               # logical / display / injected text views
├── injection/
│   ├── backend.py            # InjectionBackend interface + DryRunBackend
│   ├── _windows_backend.py    # native Win32 SendInput + clipboard
│   ├── _fallback_backend.py    # pyautogui/pyperclip (Linux/macOS)
│   └── text_injector.py        # delta backspacing + paste orchestration
└── ui/
    ├── control.py              # floating Start/Stop window (primary UI)
    └── overlay.py               # optional floating transcript overlay

host/                       # the self-hosted service (has the API key)
├── core.py                # env config, client registry, auth, rate limits,
│                           #   async Deepgram grant
├── app.py                 # FastAPI wiring (HTTPS, /v1/session, /healthz,
│                           #   /readyz, /metrics)
├── metrics.py             # dependency-free counters/gauges/histograms
├── provision.py           # mints client_id + secret per device
├── requirements.txt       # fastapi, uvicorn, httpx — no Deepgram SDK
└── Dockerfile
```

**Provider abstraction:** `STTProvider` (in `stt/base.py`) is the interface
the rest of the pipeline depends on, and it is unchanged. Only
`DeepgramProvider` implements it; the processing and injection layers never
import Deepgram.

**Injection abstraction:** `InjectionBackend` isolates every OS-specific
call, so `TextInjector`'s logic is fully testable through `DryRunBackend`
without a Windows GUI.

**Pipeline determinism:** the existing pipeline is unchanged —
normalize → numbers protection → FST terminology → BiDi → injection.

---

## Deploying the host

See **[`host/README.md`](host/README.md)** for the full guide. Platform
specific walk-throughs:

- **[`docs/CPANEL_HOSTING.md`](docs/CPANEL_HOSTING.md)** — shared hosting
  via cPanel "Setup Python App" (Passenger/WSGI), including the
  step-by-step diagnosis of a **502 Bad Gateway on Start**.
- **[`docs/WINDOWS_POWERSHELL.md`](docs/WINDOWS_POWERSHELL.md)** —
  building, installing and troubleshooting the Windows client entirely
  from PowerShell.

The short version:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r host/requirements.txt

export DEEPGRAM_API_KEY='...'        # only ever here
export HOST_SHARED_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export HOST_TLS_CERTFILE=/etc/letsencrypt/live/stt.example.com/fullchain.pem
export HOST_TLS_KEYFILE=/etc/letsencrypt/live/stt.example.com/privkey.pem

python -m host.app
```

Verify:

```bash
curl -s https://stt.example.com/healthz                       # {"status":"ok"}
curl -s -X POST https://stt.example.com/v1/session \
  -H "Authorization: Bearer $HOST_SHARED_SECRET" -d '{}'      # {"access_token":...}
```

Notes:

- **HTTPS is enforced.** Plaintext requests are refused unless
  `HOST_ALLOW_HTTP=1` (development only). Terminate TLS in the service or
  in a reverse proxy that sets `X-Forwarded-Proto`. That header is only
  trusted when the peer is listed in `HOST_FORWARDED_ALLOW_IPS` (default
  `127.0.0.1`) and the **last** hop's value is used, so a direct request
  with a forged header is still rejected as plaintext.
- **Rate limiting is per client identity, not per IP.** 50 doctors behind
  one hospital NAT share an address, so a per-IP limit would reject 19
  legitimate clinicians. Limits are token buckets keyed on `client_id`
  (plus a global ceiling); the per-IP limiter survives only for *failed*
  authentication, which is the one case a shared address must not exempt.
- The Deepgram key needs at least **Member** permission to call
  `/v1/auth/grant`.
- Rotating the Deepgram key requires **no client change**. Rotating one
  device's secret affects only that device.

---

## Capacity: 50 concurrent active streaming sessions

The design target is **50 concurrent active streaming sessions** — 50
clinicians dictating at once. Precisely: the audio of 50 simultaneous
users, of which the host carries only the token requests.

The architecture is unchanged in shape, because it was already right for
this: **audio goes client → Deepgram directly.** The host issues ~50
short-lived tokens and nothing else, so it never scales with audio volume
and needs no streaming infrastructure of its own.

What changed to make 50 *actually work*:

| Concern | Before | Now |
|---|---|---|
| Token issuance | blocking `httpx.post()` inside `async def` — froze the event loop | reused `httpx.AsyncClient` pool with explicit timeouts, closed on shutdown |
| Identity | one shared secret for everyone | `client_id` + per-device secret, hashed at rest |
| Rate limiting | per IP, 30/min — rejected 19 of 50 on one NAT | token buckets per client + global; IP limit only for failed auth |
| Correlation | none | `session_id` on every session, in logs and metrics |
| Reconnect | symmetric jitter, no `Retry-After` | full jitter, `Retry-After` honored (and clamped) |
| Queue health | drops counted, not surfaced | sustained overflow raises a degraded session state |
| Observability | none | `/metrics`, `/readyz`, bounded label cardinality |
| Startup cost | terminology YAML re-parsed per session (0.37s) | parsed once, cached on (path, mtime, size) |

### Deploying for 50

```bash
export DEEPGRAM_API_KEY='...'

# 50 device identities; only hashes are written to the host.
python -m host.provision --count 50 --prefix doctor \
    --out /secure/host/clients.txt --secrets-dir ./device-secrets
export HOST_CLIENTS_FILE=/secure/host/clients.txt

# Defaults already suit 50; shown for explicitness:
export HOST_CLIENT_BURST=120          # > 50 so a startup surge never 429s
export HOST_GLOBAL_BURST=1200
export HOST_METRICS_ADMIN_TOKEN='...' # protects /metrics

python -m host.app
```

On each machine, enter the host URL, that device's **client id** and its
secret in the settings panel. Run **one** uvicorn worker: the registry and
limiters are per-process.

### Proving it

| Claim | Test |
|---|---|
| 50 simultaneous token requests, 0 legitimate 429s | `tests/test_host_concurrency.py` |
| 50 clients on one NAT all allowed; one abuser throttled alone | `tests/test_host_concurrency.py` |
| Token issuance does not block the event loop | `tests/test_host_concurrency.py` |
| 50 concurrent sessions isolated, no thread leaks, no queue cross-talk | `tests/test_session_concurrency.py` |
| Reconnect storm decorrelated; retry limits enforced | `tests/test_session_concurrency.py` |
| 50 simultaneous shutdowns release every thread, queue and session | `tests/test_session_concurrency.py` |
| Sustained 50-client soak (opt-in, mocked) | `tests/test_soak.py` |
| Real provider capacity (opt-in, spends credit) | `tests/test_live_load.py` |

```bash
pytest tests/ -q     # includes the 50-client concurrency suite
```

### Measured (mocked Deepgram, 50 clients, this repository)

The numbers are produced by a harness, not recorded here, so they cannot go
stale. `scripts/measure_capacity.py` prints one PASS/FAIL line per criterion
and exits non-zero if any of them fails:

```bash
python scripts/measure_capacity.py
```

| Criterion | Threshold the harness enforces |
|---|---|
| 50 simultaneous session requests | ≥99% success, and 0 × 429 |
| Token acquisition p95 | < 2 s |
| Event loop during token issuance | not blocked — compared against the 2.5 s a serialized implementation would take |
| Memory growth per session batch | no growth trend across 250 sessions |
| Active sessions after shutdown | 0 in-flight grants |
| Rate limiter state | one bounded bucket per client, never more |

Exact milliseconds and megabytes depend on the machine, so they are
deliberately not quoted here. An earlier revision of this table did quote
them, and the figures it quoted were not what the harness had measured — its
mock upstream adds a 50 ms delay, so any claim of a single-digit-millisecond
p95 is arithmetically impossible, and the memory row attributed a 250-session
batch result to a 30-minute soak that this script does not run. A stale figure
presented as a measurement is worse than no figure at all, because it is the
one a reader repeats. `tests/test_host_docs.py` fails CI if a hard-coded
capacity figure comes back.

The same distinction applies to the soak: `tests/test_soak.py` is the 30-minute
50-client run, and it is opt-in (`MEDICAL_STT_SOAK=1`) because it must never
execute in CI.

Latency figures come from mocked Deepgram on this machine and are **not**
a claim about production speech-to-text latency. Measure those against the
real provider.

> **What is *not* claimed.** "50 concurrent active streaming sessions" is
> proven for the application: it issues 50 tokens and opens 50 sockets.
> Whether your Deepgram project permits 50 simultaneous Nova-3 streams is a
> property of your plan. Run the opt-in real-provider test, which reports
> `APPLICATION CAPACITY` and `PROVIDER CAPACITY` separately rather than
> hiding the difference.

---

## Building the Windows application

A complete PowerShell walk-through (prerequisites, venv setup, signing,
install, and a Start-failure troubleshooting table) lives in
**[`docs/WINDOWS_POWERSHELL.md`](docs/WINDOWS_POWERSHELL.md)**.

```powershell
# On a Windows machine with Python 3.10-3.12
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
```

This produces **`dist\MedicalSTT\`** — a complete folder that runs on a
clean Windows 10/11 machine with **zero Python installed**.

The script, in order:

1. refuses to run with `--upx`;
2. scans the source tree for anything matching a Deepgram key pattern and
   **fails the build** if one is found;
3. runs the test suite;
4. compiles with **Nuitka standalone** (`--windows-console-mode=disable`,
   `--enable-plugin=tk-inter`, `--include-data-dir=config/data`);
5. verifies the bundle actually starts (`MedicalSTT.exe --version` → exit 0)
   and that PortAudio was bundled (`libportaudio*.dll` is present — without
   it the app still launches and fails only when a microphone is opened).

### PortAudio in the bundle

`sounddevice` imports the CFFI module `_sounddevice` and, on Windows, loads
`libportaudio<arch>.dll` from `_sounddevice_data/portaudio-binaries/`.
That data package only ships in the Windows/macOS wheels, and Nuitka
classifies a `.dll` as code rather than data, so it is not picked up as
ordinary package data. The build therefore passes
`--include-module=_sounddevice` together with the `_sounddevice_data`
package/data directives, and Nuitka's built-in `sounddevice` package
configuration (present since Nuitka 1.4.1) supplies the platform DLL. The
build script then **fails fast** if no `libportaudio*.dll` is in the
output, because the symptom is otherwise delayed and misleading: the app
starts fine and fails only when the user presses Start. Verify after any
change to the build script:

```powershell
Get-ChildItem -Recurse dist\MedicalSTT -Filter "libportaudio*.dll"
```

Zero results means the microphone will fail on a clean machine.

Nuitka compiles to C rather than shipping a PyInstaller archive, which is
harder to unpack and inspect. `--onefile` is deliberately **not** used: a
folder build starts faster, avoids self-extraction to `%TEMP%`, and
triggers far fewer antivirus false positives.

### Installing on a clinician's machine

1. Copy the whole `MedicalSTT\` folder anywhere (e.g. `C:\Program Files\MedicalSTT`).
2. Run `MedicalSTT.exe` once. It creates
   `%APPDATA%\MedicalSTT\` with a `settings.yaml` template and a log file.
3. In the floating window, enter:
   - **میزبان (host)** — e.g. `https://stt.example.com`
   - **کلید مشترک (shared secret)** — the same value as
     `HOST_SHARED_SECRET`

   Press **ذخیره تنظیمات**. The secret is immediately protected with
   Windows DPAPI and written to `host_secret.dpapi`; the field is then
   cleared.

   For a multi-clinician deployment, provision per-device credentials (see
   [Capacity](#capacity-50-concurrent-active-streaming-sessions)): the
   client sends `X-Client-Id` alongside the secret so the host can rate
   limit and revoke each device independently. Set the id via
   `MEDICALSTT_HOST_CLIENT_ID` or `host_client_id` in `settings.yaml`.
4. Press **شروع** to dictate and **توقف** to stop.

No Deepgram API key is ever entered, requested, or stored on that machine.

### Code signing

Code signing is intentionally **not** implemented here — it needs your own
certificate. After building, sign the executable:

```powershell
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 `
  /f "C:\certs\medical-stt.pfx" /p $env:CERT_PASSWORD `
  dist\MedicalSTT\MedicalSTT.exe

# verify
signtool verify /pa /v dist\MedicalSTT\MedicalSTT.exe
```

Use a standard (OV/EV) code-signing certificate. Signing after Nuitka has
produced the final binary is correct; do not re-sign inside the build
script, so the certificate never has to be present on a build machine that
also runs CI.

---

## Development

```bash
git clone https://github.com/AmiraliGhamkhar/deepgram-fa-speech.git
cd deepgram-fa-speech
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

# Windows DPAPI is unavailable here, so point the dev client at the host
# through the environment instead (development only -- never a real key):
export MEDICALSTT_HOST_URL=https://stt.example.com
export MEDICALSTT_HOST_SECRET='your-shared-secret'

python run.py
```

For development on Linux/macOS, `MEDICALSTT_HOST_SECRET` supplies the
shared secret directly. On Windows, enter it in the app instead: it is
stored with DPAPI. Neither variable is ever written to disk by the app.

The checked-in `.env.example` documents the same variables
(`MEDICALSTT_HOST_URL`, `MEDICALSTT_HOST_SECRET`,
`MEDICALSTT_APP_DATA_DIR`); copy it to `.env` for a source checkout.
`host/.env.example` is the equivalent template for the host service.

```bash
pytest tests/ -q          # no network, no Windows, no credentials
ruff check medical_stt tests scripts host
mypy medical_stt host/core.py
```

The default run includes the 50-client concurrency suite
(`tests/test_host_concurrency.py`, `tests/test_session_concurrency.py`) with
Deepgram mocked, so CI never spends credit. The soak and real-provider load
tests are opt-in via `MEDICAL_STT_SOAK=1` and `MEDICAL_STT_LOAD_TEST=1`.

---

## Configuration

| File | Purpose |
|------|---------|
| `%APPDATA%\MedicalSTT\settings.yaml` | user settings — **no secrets allowed** |
| `%APPDATA%\MedicalSTT\host_secret.dpapi` | shared secret, DPAPI-protected |
| `%APPDATA%\MedicalSTT\logs\app.log` | rotating log (1 MB × 3) |
| `config/settings.yaml` | template copied to the user profile on first run |
| `data/keyterms/*.yaml` | general and specialty Deepgram Keyterms |
| `data/asr_replacements.yaml` | optional, explicit known ASR substitutions |
| `data/corrections.yaml` | categorized terminology rules |

Runtime environment overrides (development only): `MEDICALSTT_HOST_URL`,
`MEDICALSTT_HOST_SECRET`, `MEDICALSTT_APP_DATA_DIR`, `DEEPGRAM_MODEL`,
`DEEPGRAM_LANGUAGE`.

All settings are validated at startup; invalid sample rate, channel count,
block duration, endpointing, inject mode, reconnect settings, model,
language, host URL (must be `https://`) or TTL fail fast with a clear
error.

### Session lifecycle

`LiveMedicalSTT` is created **when the user presses Start**, so a
configuration or credential problem surfaces at that moment instead of
hiding at launch. Stop performs a full clean reset:

1. discard buffered audio (it must not stream into the next session);
2. clear the utterance accumulator;
3. reset the injector's streaming delta (so the next session cannot
   backspace text the user has since typed);
4. finalize and close the WebSocket;
5. release the microphone.

The reconnect backoff sleeps on an interruptible event, so Stop is
immediate rather than waiting out a 30-second backoff.

`AUTH` and `CONFIG` are **never retried** — a wrong shared secret or an
invalid configuration needs human action, and retrying forever would just
look like a hang (`stt/reconnect.py`).

Reconnect backoff uses **full jitter**: the wait is drawn uniformly from
`[ceiling*(1-jitter), ceiling]` rather than symmetrically around the
ceiling. This is what stops 50 clients that lost the same upstream at the
same instant from retrying in lockstep and hitting a recovering provider
with a synchronized spike. An upstream `Retry-After` is honored when
present, clamped to 60s so a confused server cannot pin a session open.

### Audio queue health

The bounded queue (40 chunks) drops rather than blocks, because the
microphone callback must never wait. A drop is only *noise* occasionally;
sustained drops mean the sender cannot keep up and audio is being lost
continuously. Three consecutive drops therefore flip the session to a
**degraded** state, logged at ERROR and readable via
`LiveMedicalSTT.session_health`. The queue is deliberately **not** enlarged
to paper over a slow network — that would convert dropped audio into
increasing latency instead.

### Inject modes

- `paste` (default) — clipboard + Ctrl+V. Best for Persian BiDi shaping,
  because the target application runs its own Unicode Bidi Algorithm.
- `type` — Unicode key events (still clipboard-routed off Windows, since
  PyAutoGUI cannot emit non-ASCII directly on X11/macOS).

### Reconnection

`reconnect_delay`, `reconnect_backoff_max`, `reconnect_jitter` and
`max_reconnect_attempts` control exponential backoff with bounded jitter
for **retryable** failures (network, timeout, rate limit, server
disconnect, microphone).

---

## Terminology philosophy

`data/corrections.yaml` is **not** a blind find-and-replace dictionary.
Every rule carries metadata:

```yaml
- from: 'آی سی یو'
  to: 'ICU'
  category: abbreviation_expansion   # safe_lexical | abbreviation_expansion |
                                      # specialty_terminology |
                                      # context_dependent | unsafe
  confidence: high                   # high | medium | low
  requires_context: false            # opt-in only if true
  dangerous: false                   # never applied if true
```

- **`safe_lexical` / `abbreviation_expansion` / `specialty_terminology`**
  rules are applied by default.
- **`context_dependent`** rules (e.g. "نبض" → "HR") are **disabled by
  default**. Enable with `enable_context_dependent_terms: true` only after
  reviewing the risk for your dictation population.
- Rules whose **source is everyday Persian** that merely *has* a medical
  reading are always `context_dependent`, never active by default:
  `کلیه` ("all") ≠ Kidney, `نمونه` ("sample") ≠ Specimen, `جفت` ("pair")
  ≠ Placenta, `کشت` ("cultivation") ≠ Culture, `شانه` ("comb") ≠ Shoulder,
  `تراشه` ("chip") ≠ Trachea, `رحم` ("mercy") ≠ Uterus. They are kept (not
  deleted) so a reviewed deployment can switch them on, and
  `tests/test_data_safety.py` pins both halves: ordinary sentences must
  not be "medicalized", and unambiguous medical mappings must still fire.
  Everyday phrases mapped to chart abbreviations (`به مقدار کافی` → `QS`,
  `عدم پیگیری` → `DS`, `خون در مدفوع` → `OB`) are treated the same way.
- **`unsafe` / `dangerous`** rules are **never** applied automatically.
  Two examples kept out of the default behavior: `"ناشتا"` (fasting) →
  `"NPO"` and `"کاهش"` (decrease) → `"DC"` (discontinue) — both can
  silently invert or over-specify clinical intent.

**Principle:** if uncertain, the system preserves the original transcript
rather than inventing a medical abbreviation. This model is unchanged by
the host refactor.

Numeric clinical expressions (`120/80 mmHg`, `98%`, `5 mg`, `500 mg IV`,
date/time-like patterns, ranges) are protected by
`medical_stt/processing/numbers.py`. Recognized negation/fidelity phrases
(`ندارد`, `وجود ندارد`, `مشاهده نشد`, `بدون`, `منفی است`, `رد می‌شود`) are
protected by `medical_stt/processing/negation.py` for the same reason —
see `tests/regression/test_medical_cases.py`.

### The longest-match guarantee

The terminology engine (`processing/fst.py`) uses `pyahocorasick`'s
`iter_long()` to guarantee a **longest valid match at each position,
left-to-right, independent of YAML rule order**. Verified in
`tests/test_fst.py` against the classic Aho-Corasick case
(`{"he","her","here"}` on `"he here her"`) and against this repository's
own overlap: with both `"آی" → X` and `"آی سی یو" → ICU` defined,
`"آی سی یو"` becomes `"ICU"`, never a partial `"X سی یو"`.

---

## Mixed RTL/LTR text

Three representations are kept explicitly distinct
(`medical_stt/processing/bidi.py`):

- **logical** — the transcript exactly as recognized/normalized. The only
  representation used for terminology processing.
- **display** — visual-order string for the Tkinter overlay (which does
  not implement the Unicode Bidi Algorithm itself).
- **injected** — the logical string plus **at most one** leading
  directional mark. The system does **not** wrap a mixed sentence in
  RLE…PDF: real target applications already run the full Unicode Bidi
  Algorithm, and forcing an embedding previously corrupted the visual
  order of embedded numbers/units/Latin abbreviations in exactly the kind
  of sentence this system must handle, e.g. `فشار خون 120/80 mmHg`,
  `MI در ECG مشاهده شد`, `HbA1c برابر 7.2 درصد است`, `TKA سمت راست`.

---

## Windows text injection

`medical_stt/injection/_windows_backend.py` uses native `SendInput` and
Win32 clipboard APIs directly via `ctypes` — no PyAutoGUI/PyWin32
dependency on Windows:

- explicit 64-bit-safe `ctypes` prototypes (unset `restype` truncates
  64-bit handles to 32 bits);
- UTF-16 code-unit-accurate backspacing (a surrogate pair counts as 2);
- ZWNJ/combining-mark-safe delta computation;
- correct clipboard ownership (`SetClipboardData` transfers ownership;
  freed only on failure) with retry;
- modifier hygiene: stray Ctrl/Shift/Alt/Win keys are released before
  synthesizing Ctrl+V and restored afterwards;
- long text batched into `SendInput` chunks with the accepted event count
  verified.

The DPAPI code in `medical_stt/security/dpapi.py` declares its prototypes
the same way, for the same reason.

---

## Deepgram / streaming

- Persian uses `model: nova-3` with `language: fa`, interim results,
  smart formatting and punctuation. Do not use the English-oriented
  `nova-3-medical` model for this workflow.
- Interim results update only the overlay. `is_final` segments are
  buffered, and only `speech_final` (or Deepgram `UtteranceEnd`) completes
  one logical utterance for terminology, BiDi and permanent injection.
  Repeated final segments are deduplicated.
- Final results retain provider-neutral word timestamps and confidence.
  Low-confidence numbers, doses, abbreviations, drug-like words,
  procedures and diagnoses produce an in-memory warning; recognized text
  is preserved, never guessed.
- `specialty` selects `general + specialty` terms from `data/keyterms/`,
  deduplicated, capped at 100 terms.
- SDK pinned to `deepgram-sdk==7.9.0`, and `access_token=` is used (not
  `api_key=`) so the SDK sends `Authorization: Bearer <short-lived token>`
  for the WebSocket handshake. A **fresh token is requested for every
  connection**, so a reconnect never presents a stale one.
- Errors are classified into `ErrorCategory` (`stt/base.py`): `AUTH`,
  `CONFIG`, `RATE_LIMIT`, `NETWORK`, `TIMEOUT`, `SERVER_DISCONNECT`,
  `MICROPHONE`, `SHUTDOWN`, `UNKNOWN`. **`AUTH` and `CONFIG` are never
  retried.**

### Language scope: Persian ASR + English *terminology*, not code-switching

The session runs in **one language**, Persian (`language: fa`, Nova-3).
English support is *lexical*, not acoustic: keyterm prompting plus the
terminology rules turn well-known spoken forms and English medical terms
into their written equivalents (`آی سی یو` → `ICU`, `هایپرتنشن` →
`hypertension`). This is deliberately **not** unrestricted
Persian↔English code-switching or automatic language detection:

- no `detect_language`/multilingual parameter is sent (pinned by
  `tests/test_streaming.py`);
- a speaker who dictates a full English sentence may get Persian-shaped
  recognition of it, because the acoustic model is Persian;
- the deterministic local layers can only normalize terms they know and
  never guess the language of a sentence.

If your dictation population needs true bilingual transcription, that is a
different configuration (and a different accuracy review) — it is not what
this project claims or tests.

### Who owns what: Deepgram vs. local post-processing

Two layers touch the text, and the split is deliberate:

| Concern | Owner | Why there |
| --- | --- | --- |
| Acoustic decoding, punctuation, smart formatting, endpointing | Deepgram (`smart_format`, `punctuate`, `endpointing`, `utterance_end_ms`) | it has the audio model; re-doing it locally would guess |
| Confusable *spelling* corrections (`replace`) | Deepgram, driven by `data/asr_replacements.yaml` | the provider sees the wrong form first; the list is validated to contain no digits/units/English targets (see `load_asr_replacements()`) |
| Number/unit/BP protection, terminology, negation, bidi, line structure | Local pipeline | deterministic, reviewable, and the provider has no access to the rule metadata (`requires_context`, `dangerous`) |

The local pipeline never re-does Deepgram's punctuation/formatting work,
and `data/asr_replacements.yaml` never carries a correction that the local
terminology layer owns. A rule that appears in both places is a bug: the
provider copy would bypass the local category/context guards.

### Formatting ownership (one owner per concern)

| Responsibility | Single owner |
| --- | --- |
| Unicode form, Arabic→Persian letterforms, digit/separator glyphs, whitespace runs, ZWNJ, punctuation spacing | `processing/normalize.py` |
| Which spans are clinical numbers/units (protected from rewriting) | `processing/numbers.py` |
| Terminology and abbreviation substitution (longest match, left to right) | `processing/terminology.py` + `processing/fst.py` |
| Negation/fidelity markers that must survive untouched | `processing/negation.py` |
| Low-confidence clinical token warnings (text is preserved, never guessed) | `processing/confidence.py` |
| Directional marks and visual order (logical/display/injected views) | `processing/bidi.py` — the only module allowed to emit U+200E/U+200F |
| Line breaks and transport whitespace for clipboard/keystroke injection | `injection/text_injector.normalize_injected_whitespace()` |
| Provider-side spelling fixes | `data/asr_replacements.yaml` |

`tests/test_formatting_ownership.py` pins this map: it fails if another
module starts emitting directional marks, collapses newlines into spaces,
expands terminology during normalization, or if a clinical number/unit is
altered by any stage. Line breaks are preserved end to end — a dictated
line break (`\n`) survives normalization, terminology and injection.

---

## Audio pipeline

- The microphone callback stays lightweight: it never blocks, never does
  string/YAML work, and never raises into sounddevice's real-time thread.
- `medical_stt/audio/queue.py` is a bounded queue with drop accounting:
  `stats()` exposes `depth`, `max_depth`, `dropped_total`, `drained_total`,
  `put_total`, `get_total`. A dropped chunk is logged (rate-limited).
- Latency instrumentation measures audio to first interim, audio to speech
  final, terminology, BiDi, injection and total — **never** transcript
  content.

---

## Logging

Standard `logging` throughout (`medical_stt.*` loggers). Since the packaged
EXE has no console, logs go to `%APPDATA%\MedicalSTT\logs\app.log`
(rotating, 1 MB × 3). The application **never logs**:

- the shared secret or any session token (the Deepgram SDK additionally
  redacts the `Authorization` header from `websockets` debug logs);
- clipboard contents;
- full medical transcript text by default (only stage timings and
  event/error metadata).

Host access logs contain method, path and status only.

---

## Testing

```bash
pytest tests/ -v
```

```
tests/
├── test_normalize.py          # Unicode/digit/ZWNJ/whitespace normalization
├── test_terminology.py         # safety categorization & guardrails
├── test_fst.py                  # longest-match correctness
├── test_numbers.py               # numeric-expression protection
├── test_negation.py               # negation/fidelity protection
├── test_bidi.py                    # logical/display/injected separation
├── test_config.py                   # settings validation, plaintext-secret rejection
├── test_streaming.py                 # error classification, backoff, keyterm/keywords
├── test_utterance_accumulator.py      # final-segment dedupe (timing/identity based)
├── test_shutdown_flow.py               # ordered stop/finalize/drain lifecycle
├── test_audio_callback.py               # real-time-safety of the mic callback
├── test_host_client.py                   # session fetch, error mapping, secret hygiene
├── test_host_service.py                  # host auth, rate limiting, TTL, grant
├── test_secret_store.py                  # DPAPI store behaviour (fake protector)
├── test_session_lifecycle.py             # single instance + Start/Stop controller
├── test_injection.py                     # TextInjector via DryRunBackend (line structure)
├── test_data_safety.py                   # ambiguous terminology rules stay opt-in
├── test_formatting_ownership.py          # one owner per formatting concern
├── test_audio_queue.py                   # drop accounting + drain-on-stop
├── test_overlay.py                       # overlay shutdown lifecycle (no real GUI)
├── test_app.py                           # end-to-end pipeline, fake provider
├── test_benchmark.py                      # metric math + harness honesty
├── test_windows_selftest.py                # Windows-only checks refuse to fake success
├── test_secret_tooling.py                   # scanner + history-scrub tooling
├── test_audit_fixes.py                       # regression pins for the audit remediation
├── test_live_deepgram.py                     # opt-in live test (skipped by default)
├── regression/
│   └── test_medical_cases.py         # semantic-preservation corpus
└── fixtures/
    └── medical_regression_corpus.yaml
```

No test requires a real Deepgram credential, a real host, a real Windows
GUI, or a real audio device — the provider, host client, host service,
secret store, injection backend and overlay are all exercised through
fakes. DPAPI itself is Windows-only, so `tests/test_secret_store.py` uses
a fake protector and asserts the real thing refuses to run off Windows.
`tests/test_live_deepgram.py` is the only test that talks to the real
service; it is skipped unless `MEDICAL_STT_LIVE_TEST=1` and host
credentials are set.

---

## Benchmarking

`benchmarks/run_benchmark.py` is a small, reproducible harness for
transcript accuracy. It ships **no accuracy numbers**: the repository
contains no measured baseline, and none is implied — measured WER/CER
depend on your microphone, your speakers and your audio. Run it against
your own recordings.

Offline scoring (no network, no credentials):

```bash
# hypotheses.json maps corpus case ids to the transcript a system produced
python benchmarks/run_benchmark.py --hypotheses hypotheses.json --out report.json
python benchmarks/run_benchmark.py --hypotheses hypotheses.json --apply-processing
```

`--apply-processing` runs the local pipeline (normalize → terminology →
injection whitespace) over each hypothesis, so you can see what the
deterministic layer adds. Terminology rewrites declared in
`benchmarks/corpus.yaml` are then treated as expected, not as errors
(`--spoken-reference` disables that if you want to see them as edits).

Metrics: word error rate, character error rate, numeric/unit/BP accuracy
(each clinical expression must survive verbatim), negation preservation,
English/Latin term recall and medical-term recall. Categories:
normal dictation, terminology, medications, numbers, blood pressure, SpO2,
dosage, fast/noisy speech, mixed language, repeated phrases, long
dictation. `tests/test_benchmark.py` checks the metric math against
hand-computed values, so a harness regression cannot quietly inflate
results.

Live measurement (opt-in; real network, real cost):

```bash
MEDICAL_STT_BENCHMARK_LIVE=1 \
MEDICALSTT_HOST_URL=https://host.example.com MEDICALSTT_HOST_SECRET=... \
python benchmarks/run_benchmark.py --live --audio-dir recordings/ \
  --out report.json
```

Recordings are `<case-id>.wav` (16 kHz, mono, 16-bit PCM). Without the
opt-in environment the harness exits 3 and measures nothing.

---

## Verification status

What is verified, where, and what is not — stated explicitly so no one has
to guess from a green badge:

| Area | How it is verified | Status |
| --- | --- | --- |
| Text pipeline (normalize/numbers/terminology/negation/bidi/injection) | unit + regression tests on any OS | verified by the test suite |
| Streaming protocol, error classification, reconnect policy, keyterm/keywords selection | fake SDK, no network | verified by the test suite |
| Host service (auth, rate limits, TTL, grant error mapping, proxy trust) | `tests/test_host_service.py`, `tests/test_host_app.py` | verified by the test suite |
| Shutdown/finalize ordering, audio queue draining, accumulator dedupe | fake provider + fake sounddevice | verified by the test suite |
| DPAPI secret store, named mutex, `SendInput` injection | `windows-check` CI job on `windows-latest` (real `CryptProtectData` round-trip, real mutex acquisition/refusal/release, backend load) plus `scripts/windows_selftest.py`, which `scripts/build_windows.ps1` runs before packaging | verified on Windows CI; **not** verifiable in a Linux development environment |
| Live Deepgram streaming | `tests/test_live_deepgram.py`, opt-in via `MEDICAL_STT_LIVE_TEST=1` | **not run here** (needs real credentials) |
| Transcription accuracy / WER | `benchmarks/run_benchmark.py` with your audio | **no numbers measured here** |
| Windows installer / signed EXE | `scripts/build_windows.ps1` on a Windows machine | **not run here** (the script is not executed by CI; only its Windows checks are) |

---

## Security

See [`SECURITY.md`](SECURITY.md) for the credential-incident report that
started all this. Current state:

- The Deepgram API key is **only** in the host's environment.
- The client stores exactly one secret (the shared secret), DPAPI-protected
  in user scope; it is not readable by other accounts on the machine.
- `settings.yaml` rejects plaintext secrets.
- `.env` is git-ignored; CI runs a gitleaks scan plus this project's own
  `scripts/scan_secrets.py` (working tree *and* full history), and the
  build script refuses to run if a key pattern appears in the tree.
- The exposed key from the incident **must be revoked at the provider** —
  that is the only real remediation, and this repository cannot do it for
  you. See `SECURITY.md` for the current state and the remaining
  force-push steps. A history rewrite is an operator action, never
  something CI or the application does.
- The CI history scan fails on any credential finding that is not listed,
  with a reason, in `scripts/secret_scan_baseline.txt`. The one entry there
  is the tracked incident blob: it is still printed on every run (accepted,
  not hidden), and `scripts/scan_secrets.py --history --strict` — which
  ignores the baseline — is how you verify a scrub afterwards. Once the
  rewritten history is pushed, the entry matches nothing and the scanner
  tells you to delete it.

---

## CI

GitHub Actions jobs:

- **Test** on Python 3.10/3.11/3.12 (Ubuntu): dependency install, the full
  test suite (everything mocked, no credentials, no network), `ruff` over
  `medical_stt tests scripts host`, `mypy medical_stt host/core.py`.
- **Host service tests** (Ubuntu): `tests/test_host_service.py` and
  `tests/test_host_client.py` without any Deepgram key.
- **Windows platform checks** (`windows-latest`): the platform tests plus
  `scripts/windows_selftest.py`, which exercises real DPAPI, the real
  named mutex and the real `SendInput` backend — the only automated place
  where Windows-only code runs.
- **Secret scan** (Ubuntu, full history): gitleaks plus
  `scripts/scan_secrets.py` (tree and history).

No CI job needs a Deepgram credential or the internet (beyond package
installation). The Windows *build* still runs on a Windows machine, because
Nuitka must produce a Windows binary; `scripts/build_windows.ps1` runs the
test suite and the Windows self-test before packaging.

---

## Limitations

- This is **not** a certified medical device and makes **no guarantee of
  clinical accuracy**. A human must review the final text before it
  becomes part of a medical record.
- The terminology engine's longest-match behavior is deterministic and
  tested, but the *correctness of individual rules* in
  `data/corrections.yaml` depends on each rule's own
  category/confidence — `context_dependent` and `unsafe` rules are
  intentionally not applied by default.
- BiDi handling preserves logical content but is **not** a universal
  guarantee that every target application renders every mixed string
  identically.
- Negation protection is a small, fixed marker list, not a clinical NLP
  negation-scope detector.
- Windows-native injection is the primary, most-tested path. The
  Linux/macOS fallback (`pyautogui`/`pyperclip`) is provided for
  development convenience. DPAPI, the single-instance mutex and
  `SendInput` are verified on Windows (CI job + self-test); they are not
  verified in a Linux development environment.
- The ASR is **Persian-only** (`language: fa`). English medical terms work
  through keyterm prompting plus deterministic terminology rules, not
  through language switching; a fully English dictation is out of scope.
- The benchmark harness measures accuracy but has **no measured baseline in
  this repository** — see Verification status.
- The host's rate limiter is per worker process; with multiple workers,
  also enforce limits in the reverse proxy.
- No LLM is used in the transcription-correction path, by design; novel
  phrasing outside the explicit rule set is left unchanged rather than
  guessed.
