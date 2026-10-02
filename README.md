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
  MedicalSTT.exe ───┤  POST /v1/session   (shared secret, DPAPI-stored)  │
  (no API key)      │        └──► Deepgram /v1/auth/grant ──► JWT (30s)  │
        │           └────────────────────────────────────────────────────┘
        │
        └──── wss://api.deepgram.com/v1/listen  (Authorization: Bearer <JWT>)
```

The desktop app:

- authenticates to the host with a **shared secret** you control;
- receives a **short-lived session token** (30s by default — it only has
  to be valid for the WebSocket handshake);
- stores that shared secret locally **only** as a Windows DPAPI-protected
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
├── core.py                # env config, auth, rate limiting, Deepgram grant
├── app.py                 # FastAPI wiring (HTTPS, /v1/session, /healthz)
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

See **[`host/README.md`](host/README.md)** for the full guide. The short
version:

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
  in a reverse proxy that sets `X-Forwarded-Proto`.
- **Rate limiting** is per client IP (30 requests / 60s by default,
  configurable).
- The Deepgram key needs at least **Member** permission to call
  `/v1/auth/grant`.
- Rotating the Deepgram key requires **no client change**. Rotating the
  shared secret requires re-entering it in the app.

---

## Building the Windows application

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
5. verifies the bundle actually starts (`MedicalSTT.exe --version` → exit 0).

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

> **Note:** the checked-in `.env.example` still documents the old
> `DEEPGRAM_API_KEY` variable and must be replaced with the
> `MEDICALSTT_HOST_URL` / `MEDICALSTT_HOST_SECRET` form shown above. It
> could not be edited in the environment where this change was made.

```bash
pytest tests/ -q          # 256 tests, no network, no Windows, no credentials
ruff check medical_stt tests scripts host
mypy medical_stt
```

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
├── test_normalize.py       # Unicode/digit/ZWNJ/whitespace normalization
├── test_terminology.py      # safety categorization & guardrails
├── test_fst.py               # longest-match correctness
├── test_numbers.py            # numeric-expression protection
├── test_negation.py            # negation/fidelity protection
├── test_bidi.py                  # logical/display/injected separation
├── test_config.py                  # settings validation, plaintext-secret rejection
├── test_streaming.py                 # error classification & backoff policy
├── test_host_client.py               # session fetch, error mapping, secret hygiene
├── test_host_service.py              # host auth, rate limiting, TTL, grant
├── test_secret_store.py              # DPAPI store behaviour (fake protector)
├── test_session_lifecycle.py         # single instance + Start/Stop controller
├── test_injection.py                 # TextInjector via DryRunBackend
├── test_audio_queue.py               # drop accounting + drain-on-stop
├── test_overlay.py                   # overlay shutdown lifecycle (no real GUI)
├── test_app.py                       # end-to-end pipeline, fake provider
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

---

## Security

See [`SECURITY.md`](SECURITY.md) for the credential-incident report that
started all this. Current state:

- The Deepgram API key is **only** in the host's environment.
- The client stores exactly one secret (the shared secret), DPAPI-protected
  in user scope; it is not readable by other accounts on the machine.
- `settings.yaml` rejects plaintext secrets.
- `.env` is git-ignored; CI runs a gitleaks scan over the full history and
  the build script refuses to run if a key pattern appears in the tree.

---

## CI

GitHub Actions runs on Python 3.10/3.11/3.12: dependency install, the
full test suite (mocked everything, no credentials), `ruff` over
`medical_stt tests scripts host`, `mypy`, and a gitleaks secret-scan job.

The Windows build runs separately on a Windows runner (or your own
machine) because Nuitka must produce a Windows binary.

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
  development convenience.
- The host's rate limiter is per worker process; with multiple workers,
  also enforce limits in the reverse proxy.
- No LLM is used in the transcription-correction path, by design; novel
  phrasing outside the explicit rule set is left unchanged rather than
  guessed.
