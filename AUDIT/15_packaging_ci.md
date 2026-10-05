# 15 — Module review: packaging, CI, env templates, SECURITY, .gitignore, .gitleaks.toml

Read in full: pyproject.toml, .github/workflows/ci.yml, .gitignore,
.gitleaks.toml, requirements.txt, requirements-dev.txt, host/requirements.txt,
host/.env.example, root .env.example. Cross-read: SECURITY.md,
scripts/scan_secrets.py (module 16), tests/test_host_docs.py, bandit report.

## Surface

- **pyproject.toml**: ruff (E/F/W/B, E501 ignored), mypy (py310, excludes
  tests/scripts), pytest markers `live`/`soak`/`loadtest` (all three
  env-gated; verified skipped by default in this sandbox: `pytest
  tests/test_soak.py tests/test_live_load.py tests/test_live_deepgram.py`
  → 1 passed, 4 skipped).
- **ci.yml**: 4 jobs — test matrix (3.10–3.12, libportaudio2, coverage),
  host-test (host service + 50-client concurrency, Deepgram grant stubbed at
  the async transport), windows-check (real Windows runner: mutex/DPAPI/
  SendInput tests + `scripts/windows_selftest.py`, failure lines surfaced as
  GitHub annotations), secret-scan (gitleaks-action@v2 + project scanner on
  tree and full history). `DEEPGRAM_API_KEY: ""` at workflow scope.
- **.gitignore / .gitleaks.toml**: `.env` + `.env.*` except `!.env.example`,
  keys/pems/secrets.yaml ignored; gitleaks allowlist anchored to the two
  exact template paths and only empty-value lines.
- **SECURITY.md**: incident record — a real Deepgram key was committed in
  `.env` (commit `cc42196c`), documented as permanently compromised, with
  owner actions (revoke first, then history rewrite + force-push) and a
  fingerprinted baseline entry in `scripts/secret_scan_baseline.txt` until
  the scrub is pushed.

## Findings

**F1 · High (documentation debt, not a code defect) · `host/.env.example`
documents a superseded configuration.** FACT: the file describes only ~10
variables and still presents the deprecated `HOST_RATE_LIMIT_REQUESTS` as the
live "per-client-IP fixed window" mechanism, with no mention of
`HOST_CLIENTS_FILE`, `HOST_CLIENT_*`, `HOST_GLOBAL_*`, `HOST_AUTH_FAILURE_*`,
`HOST_MAX_INFLIGHT_GRANTS`, `HOST_MAX_REQUEST_BODY_BYTES`, `HOST_METRICS_ADMIN_TOKEN`,
or `HOST_GRACEFUL_SHUTDOWN`. `core.HostSettings.from_env` (module 02) reads
25 env vars; `tests/test_host_docs.py` enforces that `host/README.md` covers
all of them, so the README is authoritative and correct — the template is the
stale artifact. An operator filling in the template gets a working service
(defaults are 50-user-ready) but installs without the client registry
(`HOST_CLIENTS_FILE`) and without a protected `/metrics`. Constraint note:
the platform refuses writes to `*.env*` paths, so this file must be fixed by
the repo owner; until then `host/README.md` is the documented substitute.
Fix (suggestion only): regenerate the template from the README settings table
in one commit.

**F2 · Low · `.gitignore` misses the provisioning outputs the docs tell
operators to create.** FACT: `host/README.md` and `provision.py` default to
`clients.txt` (`--out` default) and per-device `*.secret` files written
wherever the operator runs the tool. `.gitignore` covers `.env`, `*.pem`,
`*.key`, `secrets.yaml/yml` — but not `clients.txt` or `*.secret`. If a
provisioning run happens inside a clone, `git add .` would stage the hash
registry (harmless) and any `*.secret` file (256-bit plaintext secrets —
exactly the class of accident SECURITY.md exists for; gitleaks would not
catch it either: URL-safe base64 tokens with no `dg_` prefix and no private
key header are outside its patterns). Fix (suggestion only): add `clients.txt`
and `*.secret` to `.gitignore`.

**F3 · Low · Secret scanner has no pattern for provisioned client secrets.**
FACT: `scan_secrets.py` PATTERNS are `deepgram_key` (`dg_…`), `deepgram_key_legacy`
(40-hex next to a key name) and `private_key`. A `token_urlsafe` secret
(43 chars of `[A-Za-z0-9_-]`) in a committed file matches nothing. Mitigation:
the secret only ever exists on the operator's terminal or in 0600 files, and
the *registry* stores hashes, so the exposure requires an operator to commit
a secrets-dir; still, the repo's own scanner is the CI line of defense and it
cannot see this repo's most likely future secret. Fix (suggestion only): add
a `*.secret` filename rule (any non-placeholder content in such a file is a
finding) — cheaper and far less false-positive-prone than a generic
high-entropy regex.

**F4 · Info · CI has no lockfile / unpinned deps.** FACT: requirements files
constrain major versions only (`fastapi>=0.115,<1.0`, `pytest>=8.0,<9.0` …);
no hashes, no `pip-audit` step. pip-audit in the sandbox found one dev-only
advisory (PYSEC-2026-1845, pytest). Acceptable for this project size; noted
because the host is security-critical and a supply-chain pin would be cheap.

**F5 · Info · gitleaks allowlist regexes must stay in sync with the
templates.** FACT: `.gitleaks.toml` enumerates exactly the placeholder lines
(`DEEPGRAM_API_KEY=`, `HOST_SHARED_SECRET=`, …). When F1 is fixed and new
`VAR=` lines are added to the templates, each new empty secret-shaped var
needs its own entry or CI's gitleaks job starts flagging the empty
template (empty lines actually match `\s*=\s*$`, so only *named* vars
matter — the list already includes the TLS pair). Low maintenance risk,
worth remembering in the same commit as F1.

## Correct in this module (verified)

- CI proves what the README claims: the "Proving it" table rows map 1:1 onto
  existing test files; host-test runs the 50-client suite on every push;
  opt-in tests are env-gated and verified skipped by default (no credit can
  be spent in CI; workflow sets `DEEPGRAM_API_KEY: ""` globally and conftest
  additionally strips a developer's real key).
- Windows checks run on a real runner and annotate failures, the only place
  DPAPI/SendInput/mutex paths execute.
- SECRET-scan story is coherent: known incident is fingerprinted (not a
  value), still *reported* on every run, strict mode fails, and
  `scrub_history.py` never pushes.
- pyproject lint/type scopes match what CI runs (`mypy medical_stt
  host/core.py` excludes tests/scripts deliberately, with
  `warn_unused_ignores` on).
- SECURITY.md does not overclaim: it explicitly says revocation must happen
  in the Deepgram console and nothing in the repo proves it happened.

## Summary (3 lines)

Packaging and CI are genuinely strong — the proof suite in CI matches the
README's claims, opt-in tests cannot spend credit, and the incident record
is honest about what remains unremediated. The real debt is documentation
surface: `host/.env.example` predates the 25-var settings model and still
teaches the deprecated rate-limit var, and neither `.gitignore` nor the
secret scanner would stop an operator from committing a provisioned
`*.secret` file. The template fix is blocked on the platform's `*.env*`
write refusal and needs a human commit.
