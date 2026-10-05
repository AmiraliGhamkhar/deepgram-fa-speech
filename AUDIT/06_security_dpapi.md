# 06 — Module review: `medical_stt/security/` (DPAPI + secret store)

Read in full: security/dpapi.py L1–207, security/secret_store.py L1–131.
Cross-read: config.py L330–354 (callers, module 11), ui/control.py (caller,
module 13 pending), paths.py L54.

## What it holds

dpapi.py: ctypes bindings for `CryptProtectData`/`CryptUnprotectData`
(user scope, app entropy `MedicalSTT/host-secret/v1`, UI-forbidden), explicit
argtypes/restypes, `LocalFree` in `_copy_out`. secret_store.py: atomic
temp-file + `os.replace` persistence of the DPAPI blob with magic prefix
`MSTTDP1`, 0600 chmod best-effort, `clear()`.

## Findings

**F1 · Info · DPAPI `pbData` output buffer is freed via `LocalFree` with the
correct restype.** Not a finding — verified the classic 64-bit truncation trap
is handled: dpapi.py:73–105 sets `LocalFree.restype = c_void_p` and
`restype = wintypes.BOOL` on both crypt32 calls (the truncation class the
module itself cites at L70–73). Recording as a positive because this exact
bug pattern exists in injection/backend.py (see module 12).

**F2 · Info · Plaintext secret exists as an immutable `str` in memory.**
FACT: secret_store.py:103–112 (`load()` returns `plaintext.decode("utf-8")`);
config.py:473 stores it in `Settings.host_secret` (`repr=False`,
config.py:175) and passes it to the provider (deepgram_provider.py:172).
Python strings cannot be zeroed, so a process memory dump can recover it —
inherent to the design, mitigated by DPAPI user scope and by the secret being
worthless off-host (host revocable per device). No fix short of a redesign;
noted for the security review trail.

**F3 · Info · `MEDICALSTT_HOST_SECRET` env override takes precedence over
the DPAPI store.** FACT: config.py:337–341 — `os.getenv("MEDICALSTT_HOST_SECRET")`
wins over the stored blob; dpapi.py:138–141 and the config docstring (L333)
frame it as a non-Windows dev path, but nothing restricts it to non-Windows.
On a production Windows machine a stale env var silently shadows the DPAPI
secret the user re-entered. Low operational surprise; a warning log when the
override is present on `win32` would remove the ambiguity.

**F4 · Info · `_libraries()` reloads crypt32/kernel32 on every call**
(dpapi.py:107–131, called per `protect`/`unprotect`). Negligible cost at
save/load frequency; noted only as a deliberate simplicity trade-off.

## Correct in this module (verified)

- Scope: user-level DPAPI only; `CRYPTPROTECT_LOCAL_MACHINE` deliberately not
  used (dpapi.py:5–10 docstring) — other local accounts cannot decrypt.
- Optional entropy binds the blob to this application (L26–31).
- Error messages contain no plaintext/ciphertext/entropy (dpapi.py:36–39,
  155–158, 178–183).
- Atomic write: `mkstemp` in the same dir → write → `fsync` → `os.replace`,
  temp unlinked on any failure (secret_store.py:60–84) — a crash mid-save
  cannot corrupt the existing secret.
- Magic prefix detects plaintext/foreign blobs early with actionable guidance
  (L16, 87–97).
- Permissions: 0600 best-effort; on Windows the %APPDATA% per-user ACL is the
  real control and the docstring says so honestly (L119–128).
- Logging logs the path, never the secret (L85).
- `save()` refuses empty secrets (L48–50); `clear()` returns a clean boolean
  (L114–124).

## Summary (3 lines)

The credential store does the fundamentals right: user-scope DPAPI with app
entropy, atomic durable writes, no secret in logs or errors. Only inherent
limits remain (plaintext `str` lifetime in memory, env override precedence on
Windows). The ctypes ABI handling — the usual source of real bugs here — is
explicitly correct.
