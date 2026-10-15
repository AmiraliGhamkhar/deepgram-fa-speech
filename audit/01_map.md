# 01 — Repository map, trust boundaries, risk ranking

## Entry points

| Entry point | What it starts | Runs as |
| --- | --- | --- |
| `run.py` → `medical_stt.app:main` | Tk control window + `SessionController` + `LiveMedicalSTT` | clinician's Windows PC |
| `python -m host.app` (`host/app.py:main`) | uvicorn + FastAPI token service | self-hosted host (VPS/Docker) |
| `host/passenger_wsgi.py` | a2wsgi bridge, one dedicated event loop | cPanel/Passenger |
| `python -m host.provision` | mints `client_id:sha256` registry lines + `*.secret` files | operator machine |
| `scripts/*` | build, secret scanning, history scrub, capacity harness, benchmarks, Windows self-test | dev/CI |

## Trust boundaries and data flow

```
                       (e) CI / build pipeline  ........ GitHub Actions, Nuitka build
                                |
                                v
  clinician Windows PC                          self-hosted host (host/)
  +--------------------------+                  +-------------------------------+
  | run.py -> Tk control     |   (a) HTTPS POST  | /v1/session                   |
  |  SessionController       |  ------------->  |  Bearer <device secret>       |
  |  ! LiveMedicalSTT        |   /v1/session    |  X-Client-Id: <device id>     |
  |    DeepgramProvider      |                  |  +-- ClientRegistry (sha256)  |
  |    TextInjector ---------+-> (d) SendInput/ |  +-- rate limiters (in-proc)  |
  |    TranscriptOverlay     |     clipboard    |  +-- DEEPGRAM_API_KEY --------+--> (c) HTTPS POST
  |  DPAPI: host_secret.dpapi|                  |                               |   api.deepgram.com/v1/auth/grant
  +--------------------------+                  +-------------------------------+
       |  ^                                          returns ~30 s session token
       |  | PHI: audio (mic) ---------+              (never logged, never stored)
       |  +----------> (b) wss://api.deepgram.com  Authorization: Bearer <token>
       |                     ^ audio goes straight to Deepgram; the host never sees it
       +-- PHI: transcript, clipboard (paste mode), recovery text (memory)
```

**Boundaries in scope for this audit**

| # | Boundary | Crown jewels at risk |
| --- | --- | --- |
| a | unauthenticated/authenticated network client ↔ host | Deepgram API key, session tokens, host availability |
| b | PC ↔ `wss://api.deepgram.com` | PHI (audio), session token |
| c | host ↔ Deepgram grant | `DEEPGRAM_API_KEY` |
| d | injector ↔ arbitrary foreground window + clipboard | integrity of dictated clinical text, other apps' clipboard |
| e | CI / build / repo content | secrets in the tree, supply chain, agent-directed content |

**Threat model** (per README + SECURITY.md): unauthenticated network client; stolen device
secret; malicious/buggy target window or clipboard owner; same-user local process; poisoned PR/CI
input; hostile repo content aimed at AI agents. Out of scope: Deepgram internals, Windows
kernel/admin attackers, Deepgram plan limits, clinical correctness of individual rules beyond
ambiguity analysis.

## Risk ranking (what the audit actually spent time on)

| Tier | Unit | Files | Why ranked here |
| --- | --- | --- | --- |
| 1 | U1 host-auth-ratelimit | `host/core.py` (987), `host/app.py` (583), `host/metrics.py` (340) | holds the Deepgram key; pre-auth network reachability |
| 1 | U2 secrets-client-config | `medical_stt/security/*` (356), `host_client.py` (277), `config.py` (737), `paths.py`, `app_instance.py` | DPAPI, TLS, secret-at-rest, `yaml.safe_load` |
| 1 | U3 clinical-text-pipeline | `processing/*` (975), `data/corrections.yaml` (5,058), regression tests | silent clinical-text alteration is the top domain risk |
| 1 | U4 injection | `injection/*` (882) + call sites | wrong-window injection, over-deletion of unrelated text, clipboard loss |
| 1 | U5 streaming-session | `stt/*` (1,114), `audio/queue.py`, `app.py` (961), `ui/*` (749) | reconnect storms, Stop semantics, PHI in logs, Tk/thread boundaries |
| 2 | U6 provision-deploy | `host/provision.py`, `host/Dockerfile`, `host/.env.example`, `docs/CPANEL_HOSTING.md`, `host/README.md` | operator path; revocation, secret file modes, deploy guidance |
| 2 | U7 build-ci-supply-chain | `scripts/*` (1,708), `.github/workflows/ci.yml` (214), `requirements*.txt`, `.gitleaks.toml`, `.freebuff/`, `SECURITY.md` | build-time trust, secret scanning gate, action pinning |
| 2 | U8 tests-benchmarks-docs | `tests/*` (10,220), `benchmarks/*` (785), `docs/*`, `README.md` | pin strength, tautological assertions, doc/code drift |

Churn (`git log --since=90.days --name-only`) was not usable as a signal: the checkout is a
single-import squash of the upstream work (no per-file history in this clone beyond the merge
commit), so ranking used attack surface, safety impact and size instead. This is recorded as a
limitation.

## Ranked code-order for context budget

`host/core.py` → `host/app.py` → `processing/*` → `injection/*` → `stt/*` → `medical_stt/app.py`
→ `config.py`/`security/*` → `scripts/*` → tests. Every Tier-1 file was read in full (see
`coverage.csv`); Tier-2 files were read or skimmed as recorded there.
