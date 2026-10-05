# Repository audit — deepgram-fa-speech (final report)

**Audited:** 2026-10-05 · HEAD `5b0acc3` ("Guard host docs against configuration drift (#7)", main, in sync with origin) · working tree clean at audit start.
**Scope:** every module in the repository — host (`app.py`, `core.py`, `provision.py`, `metrics.py`, `Dockerfile`), client (`app.py`, `config.py`, `host_client.py`, `stt/*`, `audio/*`, `processing/*`, `injection/*`, `ui/*`, `security/*`), packaging/CI, scripts, benchmarks, tests, and the Deepgram/Persian domain assumptions.
**Method:** read-only. Each module note distinguishes FACT (verified against source, installed dependency source, live reproduction, or the vendor's own documentation) from HYPOTHESIS, and every "fix" is labeled *suggestion only* — no code was changed by this audit.
**Per-module detail:** `00`–`18` in this directory; tooling transcripts in `AUDIT/tools/`.

## 1. Executive summary

1. The security model is real, not decorative: the Deepgram key exists only in the host env, client secrets are DPAPI-protected and hash-only at the host, and audio goes client→Deepgram directly with no proxy, no LLM, no hidden network path.
2. No Critical or High **code** defect was found. Five Medium code defects, one High *documentation* defect, eighteen Low.
3. Highest-impact code defect: `host/app.py:308–325` buffers the whole request body before checking size, so `Transfer-Encoding: chunked` bypasses the cap → authenticated memory-DoS.
4. Second: `medical_stt/host_client.py:32–34` claims "never follow redirects" but installs stdlib `HTTPRedirectHandler`, which re-sends `Authorization` to a 301/302/303 target — **reproduced live**; the secret and client id arrived at a redirect target whose response was then trusted.
5. Third: `host/Dockerfile:40–41` health-checks `http://127.0.0.1` while the documented run has uvicorn terminating TLS on that same port → container is permanently `unhealthy`.
6. Fourth: `host/provision.py:46,77–83` hangs forever when `--prefix` ≥58 chars is combined with `--start-index` (reproduced; still looping at 50 iterations).
7. `host/.env.example` documents ~10 of 25 env vars and still advertises a deprecated rate-limit var — High, but documentation-only; the platform blocks `*.env*` writes so it needs an owner commit.
8. Domain assumptions are correct, not accidentally correct: `fa` is production-grade monolingual **nova-3**, and `nova-3-medical` is English-only — so Keyterm Prompting + the local FST engine is the right Persian-medical architecture.
9. Tooling at HEAD: ruff 0 · mypy 0 · **454 passed, 4 skipped** · bandit 1 Medium (B104) + 25 Low · pip-audit 1 dev-only advisory · gitleaks clean on tree and history.
10. Fix order: 07-F1 → 01-F1 → 14-F2 → 14-F1+F3 (+ one CLI test) → 04-F1 → owner-side 15-F1.

## 2. Overall risk score: 4 / 10

Justification, specific to this codebase: no Critical or High code defect exists; the secret lifecycle is sound in every path inspected (DPAPI at rest, SHA-256 hash-only at the host, no secret in logs/metrics/settings, constant-time comparison); and audio never touches a server the team controls, so the classic medical-data exposure failure mode is structurally absent.

The score is not lower than 4 because (a) `07-F1` is a *reproduced* credential-forwarding path, not a theoretical one, and the credential leaked is the per-clinic shared secret; (b) `01-F1` lets any authenticated client OOM the host process, which in a clinic means the shared service dies for **all** clinicians, not just the attacker; (c) `14-F1` hangs the provisioner, blocking onboarding of an entire clinic roster; (d) `14-F2` makes the container permanently report `unhealthy`, meaning the documented deployment is not the tested one.

None of these are reachable by an anonymous network attacker — each requires a valid client credential, a specific operator invocation, or a specific deployment topology — which is what holds them at Medium rather than High. This is a judgement on the *code*; the High documentation defect (`15-F1`) and the owner-side items are tracked separately and are not folded in.

## 3. Findings table

Severity scale: **Critical** (data loss, RCE, key exposure, or core flow broken) · **High** · **Medium** · **Low** · **Info**.

### Medium — fix first

| ID | Sev | Category | File:Line | Issue | Impact | Fix |
|---|---|---|---|---|---|---|
| 01-F1 | Medium | Security / DoS | `host/app.py:308–325` | `await request.body()` buffers the entire body before the size check; `Transfer-Encoding: chunked` sends no `Content-Length`, so the 4096-byte cap never applies | Any authenticated client can exhaust host memory; the shared token service goes down for every clinician at that hospital | Cap while streaming via `request.stream()` and abort past `max_request_body_bytes` |
| 07-F1 | Medium | Security / secrets | `medical_stt/host_client.py:32–34` | Comment says "never follow redirects"; `build_opener(HTTPHandler(), HTTPSHandler())` installs stdlib `HTTPRedirectHandler`, which follows 301/302/303 for POST **with the `Authorization` header** | Per-clinic shared secret + client id delivered to an arbitrary redirect target, whose response is then accepted as a valid session (reproduced live) | Subclass `HTTPRedirectHandler.redirect_request` to raise on any 3xx (~5 lines) |
| 14-F1 | Medium | Correctness | `host/provision.py:46,77–83` | `f"{safe_prefix}-{suffix}"[:64]` + `--start-index` makes every generated id identical once the prefix is ≥58 safe chars; `while len(ids) < count` then never terminates | Provisioner hangs forever instead of minting secrets; onboarding a whole clinic roster is blocked (reproduced, no bail-out in shipped code) | Validate `len(safe_prefix) + 5 <= 64` up front, or bail after `MAX_COUNT * 2` dedup attempts |
| 14-F2 | Medium | Deployment / ops | `host/Dockerfile:40–41` vs `host/app.py:408–424` | HEALTHCHECK probes `http://127.0.0.1:$HOST_PORT/readyz` while the documented run passes `ssl_certfile`/`ssl_keyfile` to uvicorn; the `harden` middleware has no loopback HTTP exemption | Container is permanently `unhealthy` under the documented deployment; real outages get masked by the check always failing | Make the probe protocol follow the cert env vars (`https` + unverified context when `HOST_TLS_CERTFILE` is set), or document a separate internal health port |
| 04-F1 | Medium | Async / race | `medical_stt/stt/deepgram_provider.py:308–314` | `send_audio()` copies `self._connection` under the lock but calls `connection.send_media(chunk)` **outside** it, racing `stop()` which finalizes the same object | Audio handed to a closing socket; bounded to a spurious error-recorded session stop rather than data loss | Generation/stopped check under the existing lock instead of re-locking across a network send |

### High — documentation (not code)

| ID | Sev | Category | File:Line | Issue | Impact | Fix |
|---|---|---|---|---|---|---|
| 15-F1 | High (doc) | Docs / config drift | `host/.env.example` | Documents ~10 of the 25 vars `HostSettings.from_env` reads; still presents deprecated `HOST_RATE_LIMIT_REQUESTS` as the live per-IP limiter; omits `HOST_CLIENTS_FILE`, `HOST_CLIENT_*`/`HOST_GLOBAL_*` bursts, `HOST_AUTH_FAILURE_*`, `HOST_MAX_INFLIGHT_GRANTS`, `HOST_MAX_REQUEST_BODY_BYTES`, `HOST_METRICS_ADMIN_TOKEN`, `HOST_GRACEFUL_SHUTDOWN` | An operator who trusts the template installs without a client registry and without a protected `/metrics`, and is told the wrong mechanism is rate-limiting. `host/README.md` is correct and CI-guarded by `tests/test_host_docs.py`, so the README is the workaround | Regenerate the template from the README settings table. **Blocked here** — the platform refuses `*.env*` writes; needs an owner commit |

### Low (18)

| ID | Sev | Category | File:Line | Issue | Impact | Fix |
|---|---|---|---|---|---|---|
| 01-F2 | Low | Security | `host/app.py:200–226` | App-level `X-Forwarded-Proto` peer check reads `request.client.host`, but uvicorn's `proxy_headers` rewrites `scope["client"]` first (`uvicorn/middleware/proxy_headers.py:53–59`), so the check never matches | Second HTTPS-enforcement layer is dead code; enforcement rests solely on uvicorn's `forwarded_allow_ips`. Not an open door — forging is still rejected | Drop the duplicated layer and document that uvicorn owns it, or pass the pre-rewrite peer in via middleware |
| 01-F3 | Low | Security | `host/app.py:248–251` | `/metrics` auth is guarded by `if resolved.metrics_admin_token:` — unauthenticated by default | Discloses load profile and failure rates to anyone reaching the port (compounds 01-F4) | Set `HOST_METRICS_ADMIN_TOKEN` in the deployment docs/template, or default-deny when unset |
| 01-F4 | Low | Security | `host/app.py:411` | Default bind `0.0.0.0` (bandit B104) | Correct for the container, risky when run bare on an office host with no firewall — with 01-F3 it exposes `/metrics` LAN-wide | Bind `127.0.0.1` by default and require opt-in for `0.0.0.0` |
| 01-F5 | Low | Metrics | `host/app.py:263` | `session_starts_total` incremented before authentication | Brute-force noise inflates the session-start metric | Move the increment after successful auth |
| 02-F1 | Low | Operability | `host/core.py` registry load path | Registry load failure surfaces as a traceback rather than a clean operator message | Slower diagnosis of a startup failure | Catch and print the offending line/path |
| 02-F2 | Low | Rate limiting | `host/core.py` legacy mode | Legacy single-secret mode shares one rate-limit bucket across all legacy clients | One noisy caller throttles everyone in that mode | Document, or key buckets per client id |
| 03-F1 | Low | Lifecycle | `medical_stt/app.py:422` (`finally`) | `stop()` exception scope can skip later cleanup steps | Shutdown accounting drifts; shutdown storms are noisier than needed | Widen the scope to `Exception` in cleanup paths |
| 04-F2 | Low | Lifecycle | `deepgram_provider.py:326–328` | `stop()` catches only `(OSError, RuntimeError)`; a `WebSocketException` from the close path escapes into the `finally` | Defeats clean shutdown accounting | Catch `Exception` here — the connection is discarded either way |
| 04-F3 | Low | Correctness | `deepgram_provider.py:123–125` | Words with missing/non-numeric `start`/`end` are silently `continue`d, with no log line | Confidence gating sees fewer words than were spoken; a provider format change would be invisible | Log once per connection when malformed words appear |
| 05-F1 | Low | Health semantics | `medical_stt/audio/queue.py` | Degraded streak resets on a good frame | Flapping audio is masked as healthy | Reset only after N consecutive good frames |
| 07-F2 | Low | Logging | `host_client.py:151,161–185` | `_classify_http_error` interpolates `exc.url` — after a followed redirect that is the attacker-chosen final URL | Attacker-controlled URL enters client log/UI (no secret leaked); adjacent to 07-F1 | Log the configured host, not the effective URL |
| 08-F1 | Low | Reconnect | `medical_stt/stt/reconnect.py` | `Retry-After` is clamped against the backoff floor, which can *shorten* a server-requested wait | Reconnects faster than the server asked, risking a 429 loop | Take `max(Retry-After, backoff)` instead of clamping down |
| 11-F1 | Low | Secrets | `medical_stt/config.py` | `as_dict()`-style dumps must keep excluding the secret field; verified today, unenforced by a test | A future refactor could start serializing the secret | Add a test asserting the key is absent |
| 13-F1 | Low | UI | `medical_stt/ui/control.py` | Stop joins its own Tk thread | Deadlock/UI-hang risk on shutdown | Join from a non-Tk thread |
| 14-F3 | Low | Correctness | `host/provision.py:95–96` | `--client-id` is stored verbatim, while `ClientRegistry.from_file` (`host/core.py:580–589`) rejects the **whole file** on any invalid id | One typo takes down the entire 50-client deployment at next start (loud, not silent) | Call `core.is_valid_client_id` on `--client-id`, exit 2 with a clear message |
| 14-F4 | Low | Operability | `host/provision.py:108` + `host/core.py:590` | Registry is opened in append mode and parsed into a dict, so a re-provisioned id silently replaces its hash | A device silently loses access with no warning | Warn on stderr when the id already exists |
| 15-F2 | Low | Packaging | `.gitignore` | Ignores `.env`, `*.pem`, `*.key`, `secrets.yaml` — but not `clients.txt` or `*.secret`, the exact files `provision.py` writes | `git add .` in a clone stages 256-bit plaintext secrets | Add `clients.txt` and `*.secret` |
| 15-F3 | Low | Security tooling | `scripts/scan_secrets.py` | Patterns are `dg_…`, legacy 40-hex next to a key name, and private key headers — none match a `token_urlsafe` provisioned secret | A committed `*.secret` passes CI; this is the repo's most likely future secret | Add a `*.secret` filename rule (cheaper and less false-positive-prone than generic high-entropy) |
| 17-F1 | Low | Test coverage | `tests/` | Provisioner CLI paths untested — precisely where 14-F1 and 14-F3 live | The two Medium provisioning bugs were invisible to CI | One CLI test invoking `main([...])` per flag combination |

### Info (selective)

| ID | Note |
|---|---|
| 01-F6 | `DEFAULT_MAX_INFLIGHT_GRANTS` (`host/app.py:88`) is defined but never used; the limiter is built from `resolved.max_inflight_grants` (L182) |
| 01-F7 | `_token_matches` uses `hmac.compare_digest` on `str`; a non-ASCII `HOST_METRICS_ADMIN_TOKEN` raises `TypeError` → 500 on `/metrics` |
| 14-F5 | Dockerfile runs `pip install` as root before `useradd` (L16–18); runtime posture (uid 10001) is fine |
| 14-F6 | `Metrics.quantiles` returns bucket upper bounds, not interpolated values — documented as deliberate; a single slow request reports p95 = 10.0 |
| 14-F7 | `_Histogram.observe` is O(buckets) per observation — irrelevant at 50 sessions |
| 15-F4 | Requirements pin majors only; no `pip-audit` step in CI; one dev-only advisory (PYSEC-2026-1845, pytest) |
| 15-F5 | When 15-F1 is fixed, `.gitleaks.toml` allowlist entries must be extended for each new named var |
| 16-F1 | `measure_capacity.py`'s fake env credentials are process-local and inert |
| 18-F3 | Cost is plan-dependent; token grants are unmetered, streamed audio-minutes are the bill — the README says so |
| 18-F4 | The WER harness is a regression tool, not an accuracy claim, and refuses to pretend otherwise |

## 4. Detailed write-ups (every High)

### 15-F1 · High (documentation) · `host/.env.example` teaches a superseded configuration

**Evidence.** FACT: `core.HostSettings.from_env` (read in module 02) reads 25 environment variables. `host/.env.example` documents ~10 of them and describes `HOST_RATE_LIMIT_REQUESTS` as the live "per-client-IP fixed window" mechanism. Absent from the template: `HOST_CLIENTS_FILE`, `HOST_CLIENT_*`, `HOST_GLOBAL_*`, `HOST_AUTH_FAILURE_*`, `HOST_MAX_INFLIGHT_GRANTS`, `HOST_MAX_REQUEST_BODY_BYTES`, `HOST_METRICS_ADMIN_TOKEN`, `HOST_GRACEFUL_SHUTDOWN`.

**Root cause.** The 50-concurrent-session hardening (PRs #6/#7) changed the settings model from a single per-IP limiter to per-client buckets, global bursts, auth-failure limiting, an in-flight cap, a body cap, and an optional metrics token. `host/README.md` was updated and is enforced by `tests/test_host_docs.py`; the env template was not.

**Failure scenario.** An operator follows the template (it is the file named in the docs), gets a working service because defaults are 50-user-ready, but installs with no client registry and no protected `/metrics`, and believes `HOST_RATE_LIMIT_REQUESTS` controls rate limiting when it is deprecated and ignored. When the hospital's real limits need tuning, there is no discoverable path from the template to the settings that matter.

**Suggested fix (not applied — the platform refuses `*.env*` writes, so this must be an owner commit).** Regenerate the template from the README settings table:

```
# host/.env.example — regenerate from host/README.md "Settings" (25 vars)
# Required
DEEPGRAM_API_KEY=
HOST_CLIENTS_FILE=./clients.txt
# Client limits (50-user ready by default)
HOST_CLIENT_RATE_LIMIT_REQUESTS=60
HOST_CLIENT_RATE_LIMIT_BURST=50
HOST_GLOBAL_RATE_LIMIT_REQUESTS=500
HOST_GLOBAL_RATE_LIMIT_BURST=250
HOST_AUTH_FAILURE_LIMIT=10
HOST_AUTH_FAILURE_WINDOW_SECONDS=60
# Request/grant guards
HOST_MAX_REQUEST_BODY_BYTES=4096
HOST_MAX_INFLIGHT_GRANTS=64
# TLS (leave unset only for local http)
HOST_TLS_CERTFILE=
HOST_TLS_KEYFILE=
# Observability
HOST_METRICS_ADMIN_TOKEN=
# Deprecated — retained for compatibility, NOT the live limiter
# HOST_RATE_LIMIT_REQUESTS=
```

Each new named secret-shaped var also needs a `.gitleaks.toml` allowlist entry in the same commit (15-F5), and `.gitignore` should gain `clients.txt` and `*.secret` (15-F2).

**Mitigation in force today.** `host/README.md` is accurate and CI-guarded against drift by `tests/test_host_docs.py`, so it is safe to use as the authoritative reference in the interim.

## 5. Quick wins (under 30 minutes each) vs structural refactors

### Quick wins — self-contained, no design change

| Item | Effort | Why it's quick |
|---|---|---|
| **07-F1** no-redirect opener | ~5 min | Subclass `HTTPRedirectHandler`, override `redirect_request` to raise. The intent is already written in the comment |
| **14-F2** HEALTHCHECK protocol | ~10 min | Read the two cert env vars in the probe; use `https` + an unverified SSL context when set |
| **14-F1** provisioner guard | ~10 min | One up-front length validation, or a dedup-attempt bail-out counter |
| **14-F3** validate `--client-id` | ~10 min | Call the existing `core.is_valid_client_id` and exit 2 |
| **01-F6** delete dead constant | ~2 min | `DEFAULT_MAX_INFLIGHT_GRANTS` is unreferenced |
| **01-F5** move counter past auth | ~5 min | Reorder one increment |
| **04-F3** log malformed words | ~10 min | One warning per connection |
| **15-F2 / 15-F3** `.gitignore` + scanner rule | ~15 min | Two lines in each file |
| **11-F1** assert secret excluded from `as_dict()` | ~10 min | One regression test |
| **08-F1** `max(Retry-After, backoff)` | ~5 min | Change a clamp to a floor |

### Structural refactors — design-level, schedule deliberately

| Item | Why it's structural |
|---|---|
| **01-F1** streaming body cap | Replaces "buffer then check" with an incremental reader throughout `POST /v1/session`; needs a decision on partial-read cleanup and on what status to return once bytes are already committed on the wire |
| **04-F1** send/close race | The lock-free read is deliberate (a lock held across a network send would block `stop()`). Needs a generation counter or stopped-flag under the existing lock, plus a decision about what a send-after-stop should do (drop silently vs record) |
| **01-F2** HTTPS enforcement ownership | Either delete the app layer and document uvicorn as the single owner, or thread the pre-rewrite peer through a uvicorn middleware. It is a question of where trust lives, and the answer changes the deployment contract |
| **14-F3 + 17-F1** provisioning correctness | The provisioner is written as library helpers with no CLI test seam. Fixing 14-F3 alone leaves the whole CLI surface untested; the structural fix is a `main(argv)` test entry point |
| **02-F2** legacy-mode bucket sharing | Splitting a shared bucket into per-client buckets changes behaviour for every existing legacy deployment — a migration concern, not a patch |
| **15-F4** dependency pinning | Adding lockfiles/hashes and a `pip-audit` CI step changes the install path for CI, Docker, and local dev simultaneously |
| **13-F1** Tk thread join | The overlay and control panels share a Tk loop; untangling ownership is a UI-architecture decision |

## 6. Top 10 missing tests that would catch these findings

| # | Test | Finding caught | Shape |
|---|---|---|---|
| 1 | POST `/v1/session` with `Transfer-Encoding: chunked`, no `Content-Length`, asserting the request is aborted at the cap rather than buffered | **01-F1** | Send a 10 MB chunked body from an authenticated client; assert 413 and stable RSS |
| 2 | Host client against two local servers, the first answering `302` — assert the second receives **no** `Authorization` header and the call raises | **07-F1** | Same sandbox shape as the audit's own repro; asserts the fix, not the absence of a comment |
| 3 | `generate_client_id("x"*62, i)` for two indices + a `main(["--count","2","--start-index","0","--prefix","x"*62])` run under a timeout | **14-F1** | Assert two distinct ids and a clean exit within a few seconds |
| 4 | Build the image, start it with `HOST_TLS_CERTFILE`/`HOST_TLS_KEYFILE` set, and assert `docker inspect .State.Health.Status` reaches `healthy` | **14-F2** | The only test that would have caught this; needs Docker in CI |
| 5 | `main(["--client-id","Dr. Smith (cardio)", ...])` → expect exit 2, and a round-trip `registry_line` → `ClientRegistry.from_file` | **14-F3** | Also the general CLI seam that would make tests 3 and 6–10 possible |
| 6 | `send_audio()` concurrent with `stop()` from two threads, repeated ~1000×, asserting no unhandled exception escapes and no error is recorded | **04-F1** | Deterministic race harness; also asserts the desired semantic (drop vs error) |
| 7 | `ClientRegistry.from_file` on a registry containing one invalid id — assert the startup failure names the **offending line** | **02-F1**, `14-F3` | Turns a traceback into an asserted operator message |
| 8 | `stop()` with the peer raising `WebSocketException` on close — assert every cleanup step still ran | **03-F1**, **04-F2** | Mock the close to raise; assert all cleanup markers are set |
| 9 | `/metrics` with `HOST_METRICS_ADMIN_TOKEN` unset — assert the documented behaviour (currently 200; either lock it in or make the test demand 401 after 01-F3) | **01-F3**, **01-F4** | Locks in a security-relevant default that is currently implicit |
| 10 | `scan_secrets.py` run against a temp tree containing a 0600 `*.secret` with a real `token_urlsafe` value — assert it is reported | **15-F3** | Plus a `.gitignore` assertion that `*.secret`/`clients.txt` are ignored |

Honourable mentions: `/readyz` 200-vs-503 state transition (currently unasserted at the endpoint level), a direct `test_host_metrics.py` covering `_escape_label_value`, the `other`-overflow fold, and `quantiles` boundaries, and a `--cov` `fail_under` threshold so coverage drops cannot land silently.

## 7. Coverage: files inspected vs not inspected

114 tracked files. Audited **58 of 58** non-test source, config, packaging, and documentation files in full or near-full; the 40 `tests/` files were inventoried and the relevant ones read; the 9 `data/` YAML files were reviewed as data rather than line-by-line code.

**Inspected (full or near-full read):**
- Host: `host/app.py` (440 L), `host/core.py`, `host/provision.py` (148 L), `host/metrics.py` (340 L), `host/Dockerfile` (46 L), `host/README.md`, `host/.env.example`, `host/requirements.txt`, `host/requirements-dev.txt`
- Client: `run.py`, `medical_stt/app.py`, `app_instance.py`, `config.py`, `paths.py`, `host_client.py` (217 L), `audio/queue.py`, `stt/{deepgram_provider (329 L), base, reconnect, accumulator}`, `processing/{normalize, numbers, negation, fst, terminology, confidence, bidi}`, `security/{dpapi, secret_store}`, `injection/{backend, text_injector, _windows_backend, _fallback_backend}`, `ui/{control, overlay}`
- Support: `scripts/scan_secrets.py`, `verify_client_host.py` (186 L), `measure_capacity.py`, `classify_corrections.py` (172 L), `windows_selftest.py`, `scrub_history.py` (278 L, partial skim), `benchmarks/{metrics, run_benchmark}`, `pyproject.toml`, `requirements.txt`, `requirements-dev.txt`, `.github/workflows/ci.yml`, `.gitignore`, `.gitleaks.toml`, `SECURITY.md`, `README.md`, `.env.example`
- Data: `config/settings.yaml`, `data/corrections.yaml`, `data/keyterms.yaml`, `data/keyterms/*.yaml` (5), `data/asr_replacements.yaml`, `benchmarks/corpus.yaml`
- Tests: `tests/conftest.py`, `test_host_concurrency.py`, `test_session_concurrency.py`, `test_host_docs.py`, plus the inventory in `AUDIT/17_tests.md`

**Not fully inspected, and why:**
- `scripts/scrub_history.py` — partial skim only. It is a one-shot incident tool that rewrites history; it is exercised by the documented `cc42196c` incident and never pushes. Residual risk: a logic error here would only matter during a future key scrub. Its use of `git filter-branch` is recorded as 16-F3.
- `scripts/build_windows.ps1` and `scripts/windows_selftest.py` — **not inspected line by line.** Both are Windows-only; the parts that matter for this audit (`windows_selftest.py`'s role in the CI Windows job) were confirmed from `ci.yml`, but the PowerShell build's contents are unverified. Residual risk is packaging-only — it cannot affect the runtime security model.
- `ARCHITECTURE_ASSESSMENT.md` — read as documentation context; it makes no claims this audit relies on.
- 30 of the 40 `tests/` files were inventoried (test counts, what each proves) rather than read in full. The concurrency, host-service, host-docs, and benchmark files — where the load-bearing assertions live — were read.
- Installed-dependency internals: `websockets` SDK internals were **not** read, which is why 04-F1's downstream effect is labelled HYPOTHESIS rather than FACT.
- Real Deepgram behaviour: no live provider call was made (no `DEEPGRAM_API_KEY` is set), so provider-side claims rest on Deepgram's published documentation, fetched 2026-10-05.

## 8. What is verified good (architecture invariants)

- **No audio proxy, no LLM, no hidden network path.** Re-verified in modules 03/04/07/10/11: the only outbound endpoints are the configured host (`/v1/session`) and `wss://api.deepgram.com`; `host/` performs exactly one outbound POST (`/v1/auth/grant`).
- **Secret lifecycle is airtight as designed:** DPAPI user-scope blob on the client; plaintext in `settings.yaml` is a hard error; the registry stores SHA-256 hashes only; the host never logs secrets or tokens; constant-time comparisons; timing-equalized unknown-id path; the metrics registry cannot receive a secret.
- **50-session capacity is proven for the application layer:** 50/50 grants, 0×429 behind one NAT, event loop unblocked during issuance, bounded limiter/label state under floods, no growth in soak — asserted in CI on every push, with soak/loadtest opt-in gated so CI can never spend credit.
- **Domain correctness:** `fa` is a production-grade monolingual nova-3 language (Deepgram, 2026-02-12 launch; models-languages matrix); Persian and Arabic-Indic digits plus U+066B/U+066C/U+066A are normalized before numeric protection; `keyterm` vs `keywords` is chosen per model; a fresh short-lived token is fetched per connection.
- **Honesty in docs:** the README's "Measured" table mirrors `scripts/measure_capacity.py`'s thresholds exactly, and its "what is *not* claimed" note correctly separates application capacity from provider capacity.

## 9. Owner-required (cannot be done from this workspace)

1. **`host/.env.example`** and root **`.env.example`** need a human edit — the platform refuses `*.env*` writes. Until then `host/README.md`'s 25-var settings table is authoritative and CI-guarded against drift.
2. **The pre-`cc42196c` Deepgram key remains owner-action** per `SECURITY.md`: revoke in the Deepgram console, then history rewrite + force-push. Nothing in this repo can prove revocation happened.
3. **Real-provider capacity** is unverified by design: needs `DEEPGRAM_API_KEY` and `MEDICAL_STT_LOAD_TEST=1 pytest tests/test_live_load.py -m loadtest -s`, which reports APPLICATION vs PROVIDER CAPACITY separately.

## 10. Tooling appendix (this sandbox, at HEAD)

ruff 0 findings · mypy 0 findings · pytest **454 passed, 4 skipped** (exit 0; opt-in suites re-verified as skipped) · bandit 1 Medium (B104 bind-all, cross-referenced as 01-F4) + 25 Low (mostly subprocess/assert in operator scripts) · pip-audit 1 dev-only advisory (PYSEC-2026-1845, pytest) · gitleaks clean (tree + full history) · project secret scanner clean · `scripts/verify_client_host.py` 16/16 PASS.

## 11. Module index

`00` map/LOC census · `01` host/app.py · `02` host/core.py · `03` client app lifecycle · `04` Deepgram provider · `05` audio queue · `06` DPAPI/secret store (clean) · `07` host client (redirect repro) · `08` reconnect/base · `09` accumulator (clean) · `10` processing pipeline (clean) · `11` config/paths · `12` injection (clean) · `13` UI · `14` host support (provision/metrics/Dockerfile) · `15` packaging/CI/env templates · `16` scripts/benchmarks · `17` tests inventory · `18` Deepgram/Persian domain.