# Audit progress

| # | Module (audit file) | Scope | Status |
|---|---|---|---|
| 00 | `00_map.md` | recon, tree, LOC, ranking | DONE |
| 01 | `01_host_app.md` | host/app.py | DONE (chunked-body DoS M; dead peer-check L) |
| 02 | `02_host_core.md` | host/core.py | DONE (registry traceback L; legacy shared bucket L) |
| 03 | `03_client_app.md` | medical_stt/app.py | DONE (stop() exception scope L) |
| 04 | `04_stt_provider.md` | stt/deepgram_provider.py | DONE (send/close race M; stop scope L) |
| 05 | `05_audio_queue.md` | audio/queue.py | DONE (degraded streak reset L) |
| 06 | `06_security_dpapi.md` | security/dpapi.py, secret_store.py | DONE (clean) |
| 07 | `07_host_client.md` | host_client.py | DONE (**redirect secret forwarding M, repro'd**) |
| 08 | `08_stt_reconnect_base.md` | stt/reconnect.py, stt/base.py | DONE (Retry-After vs floor L) |
| 09 | `09_accumulator.md` | stt/accumulator.py | DONE (clean) |
| 10 | `10_processing_pipeline.md` | processing/* (7 files) | DONE (safety logic verified; Info notes) |
| 11 | `11_config_paths.md` | config.py, paths.py, app_instance.py, settings.yaml | DONE (as_dict secret Low) |
| 12 | `12_injection.md` | injection/* (4 files) | DONE (clean; clipboard-restore Info) |
| 13 | `13_ui.md` | ui/control.py, ui/overlay.py | DONE (Stop joins on Tk thread L) |
| 14 | `14_host_support.md` | host/provision.py, host/metrics.py, host/Dockerfile | DONE (provision --start-index hang M; HEALTHCHECK http-vs-TLS M; --client-id unvalidated L) |
| 15 | `15_packaging_ci.md` | pyproject.toml, requirements*, .env.example x2, ci.yml, .gitleaks.toml, SECURITY.md, .gitignore | DONE (**host/.env.example stale High-doc**; clients.txt/*.secret not ignored L; no *.secret scanner pattern L) |
| 16 | `16_scripts_benchmarks.md` | scripts/*, benchmarks/* | DONE (Info only; metrics corpus + thresholds verified) |
| 17 | `17_tests.md` | tests/ inventory, conftest, coverage gaps | DONE (provisioner CLI + metrics module untested L; opt-in gating re-verified) |
| 18 | `18_domain_deepgram_persian.md` | Deepgram params, audio, Persian text, WER harness, cost | DONE (nova-3+fa verified vs Deepgram matrix; nova-3-medical en-only — FST design correct) |
| — | `REPORT.md` | final report | DONE — 0 High code, 5 Medium code, 1 High doc, 18 Low; remediation order inside |

All modules complete. Remaining items are owner-side: `*.env*` templates (platform write-blocked),
Deepgram key revocation per SECURITY.md, real-provider load test (needs DEEPGRAM_API_KEY).

Tooling (`AUDIT/tools/`): DONE — ruff 0 / mypy 0 / pytest 454 pass / bandit 1 Medium, 25 Low /
pip-audit: pytest PYSEC-2026-1845 (dev-only) / gitleaks clean (tree + 3-commit history) /
env-history: only `.env.example` templates ever committed.
Phase-4 repro executed in-sandbox: host_client redirect forwards `Authorization` (07 F1).
