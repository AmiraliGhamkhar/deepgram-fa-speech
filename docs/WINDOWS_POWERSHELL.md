# Medical STT on Windows — PowerShell guide

Everything needed to build, install, configure, and troubleshoot the
Windows desktop client — from a clean Windows 10/11 machine to a
dictating clinician. All commands are PowerShell (5.1 or 7.x).

> **Security model reminder.** The Deepgram API key lives **only** on your
> self-hosted session service (`host/`). It is never entered, stored, or
> logged on the clinician's machine. The client authenticates to your host
> with a client id + secret, and receives a ~30-second session token.

---

## 1. Prerequisites (build machine)

| Component | Version | Check |
|---|---|---|
| Windows | 10 or 11 (x64) | `winver` |
| PowerShell | 5.1 or 7+ | `$PSVersionTable.PSVersion` |
| Python | 3.10 – 3.12 | `python --version` |
| Git | any recent | `git --version` |
| C compiler | MSVC Build Tools (Nuitka needs it) | `cl` resolves in a VS dev shell, or install via Visual Studio Installer → "Desktop development with C++" |
| Tkinter | ships with python.org installers | `python -c "import tkinter"` |

Notes:

- **Python from python.org** is strongly recommended. Windows Store
  Python has path aliasing and permission quirks that break Nuitka builds.
- During Python setup, enable **"Add python.exe to PATH"** and
  **"tcl/tk and IDLE"** (Tkinter is required — the floating control window
  is built with it).
- Nuitka downloads a matching C toolchain on first run
  (`--assume-yes-for-downloads` is already in the build script) — leave
  the MSVC option installed anyway for reproducible builds.

## 2. Get the source and set up a venv

```powershell
cd $HOME\source
git clone https://github.com/AmiraliGhamkhar/deepgram-fa-speech.git
cd deepgram-fa-speech

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements-dev.txt
```

If `Activate.ps1` is blocked by execution policy (error: *"running
scripts is disabled on this system"*):

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

That is a per-user, reversible change; `Bypass` with the `-File` flag (as
the build command below uses) also works without changing the policy.

## 3. Run the test suite (fast, offline, no credentials)

```powershell
pytest tests/ -q
```

478 tests, ~30 s. No Deepgram key, no microphone, no GUI needed — every
network and OS boundary is faked. If this fails, **stop**: the build
script refuses to continue on a red suite anyway.

Optional deeper checks:

```powershell
ruff check medical_stt tests scripts host     # lint
mypy medical_stt host/core.py                 # types
python scripts\scan_secrets.py                # credential scan (must be clean)
```

## 4. Verify Windows-only components

DPAPI, the single-instance mutex, and the SendInput injection backend
only exist on Windows, so the build machine is where they get verified:

```powershell
python scripts\windows_selftest.py
```

Expected output ends with a PASS line for each component. Any failure
here means the packaged app would silently misbehave at Start time —
fix it before building.

## 5. Build the application

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
```

What the script does, in order:

1. **Refuses `--upx`** — packed binaries trip antivirus heuristics and
   unpack themselves at runtime.
2. **Scans the tree for credential patterns** (`scripts/scan_secrets.py`)
   and fails if any Deepgram-shaped key is present.
3. **Runs the full test suite** and stops on failure.
4. **Runs the Windows self-test** (DPAPI round-trip, mutex, SendInput).
5. **Compiles with Nuitka standalone** — `--windows-console-mode=disable`,
   `--enable-plugin=tk-inter`, `config\` and `data\` bundled as data,
   `_sounddevice` + `_sounddevice_data` forced in so PortAudio lands in
   the bundle.
6. **Verifies the bundle**:
   - `MedicalSTT.exe --version` exits 0 (the app starts with no console
     and no Python);
   - `libportaudio*.dll` exists (see below);
   - `config\settings.yaml` and `data\corrections.yaml` are present.

Output: **`dist\MedicalSTT\`** — a self-contained folder. This is a
*folder* build, not `--onefile`, deliberately: faster startup, no
self-extraction to `%TEMP%`, far fewer antivirus false positives.

### Verify PortAudio landed in the bundle

```powershell
Get-ChildItem -Recurse dist\MedicalSTT -Filter "libportaudio*.dll"
```

**Zero results = broken build.** The symptom of a missing PortAudio DLL
is nasty: the app launches fine and fails *only* when the user presses
Start (microphone open). The build script checks this for you, but verify
it yourself after any change to the build script or the sounddevice
version.

### Optional: sign the executable

Code signing is intentionally not automated — it needs your certificate.

```powershell
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 `
  /f "C:\certs\medical-stt.pfx" /p $env:CERT_PASSWORD `
  dist\MedicalSTT\MedicalSTT.exe

signtool verify /pa /v dist\MedicalSTT\MedicalSTT.exe
```

Use an OV or EV certificate; an EV cert removes SmartScreen warnings
immediately, an OV builds reputation over installs.

## 6. Install on a clinician's machine

The target machine needs **nothing** — no Python, no dependencies.

1. Copy the whole `MedicalSTT\` folder (USB, RDP, SMB — anything), e.g.
   to `C:\Program Files\MedicalSTT`. There is no installer; the folder is
   the installation.
2. Run `MedicalSTT.exe` once. First run creates
   `%APPDATA%\MedicalSTT\` containing:
   - `settings.yaml` — the user-editable settings template;
   - `logs\app.log` — rotating log (1 MB × 3);
   - `host_secret.dpapi` — created when you save the secret (step 3).
3. In the floating control window, fill in:
   - **میزبان (host)** — `https://stt.example.com` (your host's URL);
   - **کلید مشترک (shared secret)** — the device's secret:

     | Deployment | Secret to enter |
     |---|---|
     | Single clinician | the value of `HOST_SHARED_SECRET` on the host |
     | Multi-clinician | the per-device secret from `host.provision` (the `<id>.secret` file) |

     Optionally also set the **client id** for multi-clinician mode —
     either in `%APPDATA%\MedicalSTT\settings.yaml`:
     `host_client_id: doctor-01`, or via the `MEDICALSTT_HOST_CLIENT_ID`
     environment variable.
   - Press **ذخیره تنظیمات** (save settings). The secret is immediately
     encrypted with Windows DPAPI (user scope) into
     `host_secret.dpapi`, and the text field is cleared. A plaintext
     secret pasted into `settings.yaml` is rejected at startup — that is
     a feature, not a bug.
4. Press **شروع** (Start) to dictate; **توقف** (Stop) to end. Transcripts
   are injected into whatever text field has focus; the floating overlay
   mirrors them.

### Firewall prompt

On first Start, Windows Defender may ask to allow `MedicalSTT.exe` on the
network. Allow it — the app makes outbound HTTPS to your host and outbound
WSS to `api.deepgram.com`. It never listens on a port.

## 7. Configuration reference

Settings live in `%APPDATA%\MedicalSTT\settings.yaml`:

```yaml
model: nova-3                # keep: the validated fa configuration
language: fa                 # keep: Persian ASR + English medical terms
specialty: general           # or cardiology / radiology / ...
sample_rate: 16000
channels: 1
endpointing: 400
overlay_enabled: true
enable_context_dependent_terms: false   # keep false unless reviewed
host_url: https://stt.example.com
host_client_id: doctor-01    # multi-clinician deployments only
session_ttl_seconds: 30
```

No secret may ever be written here — see the security model above.

## 8. Troubleshooting

The app has no console window; the log is the diagnostic surface:

```powershell
Get-Content "$env:APPDATA\MedicalSTT\logs\app.log" -Tail 50 -Wait
```

| Symptom | Cause | Fix |
|---|---|---|
| App won't launch at all | Missing VC++ runtime / blocked EXE | Install [VC++ redist](https://aka.ms/vs/17/release/vc_redist.x64.exe); unblock via file Properties if downloaded |
| "شروع ناموفق" / Start fails with `host_url is not set` | Settings never saved | Enter host + secret, ذخیره تنظیمات, then Start |
| Start fails with `no host shared secret is available` | Secret not saved (or DPAPI blob from another user) | Re-enter the secret in the window; DPAPI blobs are per-Windows-user |
| Start fails with `host rejected the shared secret (HTTP 401)` | Wrong secret or wrong client id | Verify against the host's `HOST_SHARED_SECRET` or the device's `.secret` file; check `host_client_id` spelling |
| Start fails with `host is unavailable (HTTP 502/503/504)` | Host down, overloaded, or Deepgram unreachable **from the host** | See the host's docs; 502 usually means the host's own Deepgram key lacks **Member** permission for `/v1/auth/grant` |
| Start fails with `host redirected the session request` | Host (or a proxy in front of it) is redirecting `/v1/session` — credentials must never follow | Fix the host: it must answer POST `/v1/session` directly over HTTPS with no redirect |
| Start fails with `host rate limit reached (HTTP 429)` | Burst limit hit | Defaults allow 50 clinicians; if you tuned them down, raise `HOST_CLIENT_BURST` |
| "Microphone requires sounddevice" | PortAudio missing from bundle | Rebuild and re-verify `libportaudio*.dll` (step 5) |
| Text not injected into the target app | Focus lost, or target runs elevated | Click into the target field after speaking; run MedicalSTT.exe elevated only if the target is elevated too |
| Persian shows as disconnected letters | Target app lacks RTL shaping | Use the `paste` inject mode (default); `type` mode is legacy |
| Single-instance error | Another copy is running | Kill the other instance or reboot; the mutex is per-user |

For deeper diagnosis, run the same operation from PowerShell:

```powershell
# Can this machine reach the host at all?
Invoke-WebRequest -Uri "https://stt.example.com/healthz" -UseBasicParsing

# Does the host accept this device's credentials? (substitute real values)
$secret = Get-Content .\doctor-01.secret -Raw
Invoke-WebRequest -Uri "https://stt.example.com/v1/session" `
  -Method Post -Headers @{ Authorization = "Bearer $secret"; "X-Client-Id" = "doctor-01" } `
  -ContentType "application/json" -Body '{"ttl_seconds":30}' -UseBasicParsing
```

A 200 with a JSON body containing `access_token` means the host side is
healthy and the problem is local (settings, DPAPI, microphone). Anything
else points at the host — see its documentation.

## 9. Uninstall / reset

```powershell
# Stop the app, then:
Remove-Item -Recurse -Force "C:\Program Files\MedicalSTT"   # the install folder
Remove-Item -Recurse -Force "$env:APPDATA\MedicalSTT"       # settings, DPAPI blob, logs
```

Deleting `%APPDATA%\MedicalSTT` erases the stored secret (it is
undecryptable without this Windows user anyway).
