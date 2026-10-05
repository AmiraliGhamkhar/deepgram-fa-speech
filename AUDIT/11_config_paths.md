# 11 — Module review: `config.py`, `paths.py`, `app_instance.py`, `config/settings.yaml`

Read in full: config.py L1–600, paths.py L1–79, app_instance.py L1–142,
config/settings.yaml L1–70. Cross-read: security module (06), host_client
(07), tests/test_config.py:199.

## What it holds

config.py: env+YAML settings with eager validation; model/language matrix
(`fa` requires `nova-3`, L60–66, L96–128); plaintext-secret rejection
(L267–279); mtime-keyed YAML cache (L229–265); keyterms/ASR-replacement/
corrections loaders (L497–600). paths.py: per-user `%APPDATA%\MedicalSTT`
layout with env override. app_instance.py: Windows named mutex (non-Global
namespace, documented privilege rationale) + POSIX file-lock fallback.

## Findings

**F1 · Low · `Settings.as_dict()` exposes `host_secret`.** FACT:
config.py:207 includes `"host_secret": self.host_secret` in the dict
(`repr=False` at L175 covers only the dataclass repr). Production callers:
none — the only consumer is the backward-compat `__getitem__` (L214–216),
used by no production code (repo grep). But the secret is one
`json.dumps(settings.as_dict())` away from a log line, and the only test
guard asserts `"api_key" not in as_dict()` (tests/test_config.py:199) — it
does not cover `host_secret`. Fix (suggestion only): omit the secret from
`as_dict()` or assert its absence in the same test.

**F2 · Info · `load_dotenv()` runs at config import** (L141–142): a `.env`
next to the checkout (or CWD) silently injects `MEDICALSTT_HOST_SECRET`,
which then overrides the DPAPI store (module 06 F3 / config.py:337–341).
Useful for dev; on a hospital workstation a forgotten `.env` shadows the
secret the user re-entered, with no log line saying so.

**F3 · Info · Client-side env overrides use Deepgram-branded names for
non-secret values** (`DEEPGRAM_MODEL`, `DEEPGRAM_LANGUAGE`, L453–454).
Confusable: an operator could reasonably infer these relate to a Deepgram
credential. Cosmetic; the values are model/language strings only.

**F4 · Info · `save_host_credentials` rewrites settings.yaml via
`yaml.safe_dump`** (L350–363), dropping the explanatory comments the
template ships with. Harmless; users lose the guidance text after the first
save.

## Correct in this module (verified)

- Secret handling: plaintext `host_secret`/`api_key`/`secret` keys in YAML
  are a hard `ConfigError` (L267–279, `_FORBIDDEN_SECRET_KEYS` L80–81); the
  template header says where the secret goes instead
  (settings.yaml:11–17); `load_host_secret()` degrades a corrupt DPAPI blob
  to a logged warning + empty string (L330–348) instead of a traceback.
- Transport policy: `_valid_host_url` requires https:// except loopback
  http (L365–377) — enforced client-side *and* server-side (host/app.py
  middleware), so the http:// case that matters for module 07 F1's redirect
  attack is a dev-only configuration.
- YAML cache: keyed on (path, mtime_ns, size) — edits invalidate; capped at
  64 entries with full clear (L229–265); returns deep copies because
  `save_host_credentials` mutates its input (L180–184 docstring) — no
  shared-mutable-state aliasing between sessions.
- `validate_settings` bounds every numeric knob to sane ranges (L379–441),
  including session TTL 5–3600 s matching host/core.py:39–40, and rejects
  invalid `host_client_id` with a pointer to the provisioning tool
  (L425–430).
- `load_asr_replacements` guards the one provider-side rewrite: lowercase
  find-term (Deepgram requirement), no digits, no ':' (L528–568) — so the
  `replace` parameter passed to Deepgram (deepgram_provider.py:253) can
  never carry numbers or a colon-ambiguity, and conflicts are hard errors.
  This is the only text path that bypasses local review, and it is fenced.
- Keyterms capped at 100 (`MAX_KEYTERMS`, L494; loop L519–526); specialty
  names validated before path construction (L501–502, `_valid_specialty_name`
  L84) — no path traversal from the specialty setting into
  `data/keyterms/<specialty>.yaml`.
- app_instance: mutex failure degrades to "continue" with a warning
  (app_instance.py:69–79) and says honestly that it is not a security
  boundary; non-Global namespace avoids the SeCreateGlobalPrivilege trap
  (L15–22); POSIX fallback uses `flock` non-blocking (L96–113).

## Summary (3 lines)

Configuration is the security story's backbone and holds: plaintext secrets
hard-fail, https is enforced both ends, caches are bounded and keyed
correctly, provider-side rewrites are fenced by validation. Two Low items:
`as_dict()` carries the secret (unused, unguarded by tests), and `.env`
loading plus the env override can silently shadow the DPAPI secret. Paths
and single-instance handling are clean.
