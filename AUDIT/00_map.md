# 00 — Repository Map (deepgram-fa-speech)

Audited: 2026-10-05, HEAD `5b0acc3` ("Guard host docs against configuration drift (#7)").
Working tree clean (0 porcelain entries) at audit start.

## 1. Shape — two Python apps in one repo

```
Windows desktop client (medical_stt/, entry: run.py -> medical_stt.app:main)
  microphone -> bounded audio queue -> sender thread
    -> Deepgram streaming WebSocket (client connects DIRECTLY; audio is never proxied)
  transcript events -> utterance accumulator -> deterministic Persian post-processing
    (normalize -> number protection -> negation protection -> FST terminology -> BiDi)
  -> confidence gating -> UI overlay + Windows text injection
Self-hosted token service (host/, FastAPI)
  HTTPS POST /v1/session (client_id + secret) -> short-lived Deepgram access token
  The host never sees audio or transcripts.
```

Flow arrows marked above are the intended architecture; each arrow is verified against
source in modules 03/04/10 and corrected there if the code differs.

## 2. Language / LOC census (measured, excludes __pycache__ and tool caches)

| Ext | Files | Lines | What |
|---|---|---|---|
| py | 86 | 14,736 | client + host + tests + scripts + benchmarks |
| yaml | 13 | 5,553 | terminology/corrections/corpus data (clinical content) |
| md | 5 | 1,471 | README, host/README, ARCHITECTURE_ASSESSMENT, SECURITY |
| ps1 | 2 | 178 | Windows build/self-test |
| yml | 2 | 142 | CI workflow + benchmark corpus |
| toml | 3 | 57 | pyproject (project + 2 tool configs) |

Tests: 37 test files under tests/ (incl. fixtures/ and regression/).

## 3. Entry points and runtime

- `run.py` -> `medical_stt.app:main` (FACT, read).
- Host service: `host/app.py` FastAPI app, started under uvicorn (Docker CMD / README — verify in module 01/14).
- CI: `.github/workflows/ci.yml` (pytest matrix 3.10–3.12, Windows platform checks, host concurrency, secret scan).
- Scripts: ops tooling (`scan_secrets.py`, `verify_client_host.py`, `measure_capacity.py`, `windows_selftest.py`, `scrub_history.py`, `classify_corrections.py`, `build_windows.ps1`).

## 4. External services

- Deepgram REST `POST /v1/auth/grant` — host-side, authenticated with `DEEPGRAM_API_KEY` (verify host/core.py).
- Deepgram streaming WebSocket (Nova-3, language fa) — client-side, temporary token (verify provider module).
No other third-party network calls expected; a repo-wide grep for outbound HTTP is part of module 07/11 checks.

## 5. Data and configuration

- `config/settings.yaml` — client settings.
- `data/corrections.yaml`, `data/keyterms.yaml`, `data/keyterms/*.yaml` (5 specialties), `data/asr_replacements.yaml` — terminology source for the FST engine.
- `benchmarks/corpus.yaml` — benchmark corpus.
- `.env.example`, `host/.env.example` — credential templates (documented constraint: these two files are write-blocked by the platform, they are unchanged since before PR #6; their *content* is still reviewed here).

## 6. Module list and priority

P0 (public/reachability/correctness critical):
- `host/app.py` — HTTP surface: auth, rate limits, grant issuance, metrics endpoint (→ 01)
- `host/core.py` — settings, client registry, token buckets, grant client (→ 02)
- `medical_stt/app.py` — client session lifecycle, threads, reconnect loop, health (→ 03)
- `medical_stt/stt/deepgram_provider.py` — WebSocket lifecycle, event parsing (→ 04)
- `medical_stt/audio/queue.py` — bounded queue, drop/health semantics (→ 05)
- `medical_stt/security/dpapi.py`, `security/secret_store.py` — credential storage (→ 06)

P1 (functional core, lower exposure):
- `medical_stt/host_client.py` (→ 07); `stt/reconnect.py`, `stt/base.py` (→ 08); `stt/accumulator.py` (→ 09);
- `processing/{normalize,numbers,negation,fst,terminology,confidence,bidi}.py` (→ 10);
- `config.py`, `paths.py`, `app_instance.py` (→ 11); `injection/*` (→ 12); `ui/*` (→ 13)

P2 (support/ops):
- `host/provision.py`, `host/metrics.py`, `host/Dockerfile` (→ 14); packaging/CI/env templates (→ 15);
- `scripts/*`, `benchmarks/*` (→ 16); `tests/` inventory + conftest (→ 17)

Domain checks (Persian/Deepgram) → `18_domain_deepgram_persian.md`.
