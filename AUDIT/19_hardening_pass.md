# Engineering hardening pass — deepgram-fa-speech

**Date:** 2026-10-06
**Base:** `5f1dcaa` (`main`) · **Branch:** `arena/13c5182e-deepgram-fa-speech` · **14 commits**, 46 files, +3145 / −252
**Method:** source-level analysis first, then implementation. Nothing here is taken from `AUDIT/00`–`18`, from commit
messages, from README claims, from comments, or from existing tests — every statement below was re-derived from the
code at `5f1dcaa`, and every claim that could be checked at runtime was checked against a running process.
**Gates at HEAD:** 705 passed / 4 skipped (709 collected) · `ruff check .` clean · `mypy medical_stt host benchmarks`
clean over 40 files · `pip-audit --strict` on both requirement files clean · `scripts/scan_secrets.py` clean on the
working tree **and** on full history.

---

## 1. Repository analysis and execution map

Three processes, one trust boundary. The **host** (`host/`) is the only thing that ever holds `DEEPGRAM_API_KEY`; it
mints ~30 s Deepgram session tokens. The **client** (`medical_stt/`) is a Windows desktop app that authenticates to the
host with a DPAPI-protected shared secret, receives a short-lived token, and streams audio **directly** to Deepgram —
no proxy, no relay, no LLM in the path. **Post-processing** is entirely local and deterministic.

The 30 named flows were traced and confirmed:

| Stage | Path |
|---|---|
| startup → mic capture | `run.py` → `medical_stt/app_instance.py` (named mutex, single-instance) → `app.py` → `audio/__init__.py` (sounddevice callback) |
| queue → provider | `audio/queue.py` (`threading.Lock`, bounded, degraded/recovery streak) → `stt/deepgram_provider.py` sender thread |
| token request → Deepgram auth | `host_client.py` (`urllib`, `_NoRedirectHandler`) → `host/app.py` `POST /v1/session` → `host/core.py` `grant_session_async` → `AsyncGrantClient` → Deepgram |
| WS lifecycle → audio | `deepgram_provider.start()` — fresh token **per connection**, `access_token=` not `api_key=`, `EventType.{OPEN,MESSAGE,ERROR,CLOSE}` |
| interim/final → accumulator | `transcript_event_from_result` → `stt/accumulator.py` (speech_final / utterance_end segmentation) |
| Persian normalization | `processing/normalize.py` (Arabic yeh→Persian ye, kaf, digit folding, whitespace) |
| numeric / negation protection | `processing/numbers.py` `find_numeric_spans`, `processing/negation.py` `find_negation_spans` |
| FST terminology | `processing/terminology.py` → `processing/fst.py` (pyahocorasick `iter_long`, deterministic overlap resolution) |
| BiDi → Windows injection | `processing/bidi.py` → `injection/text_injector.py` → `_windows_backend.py` (SendInput) / `_fallback_backend.py` |
| clipboard → shutdown | `pyperclip`/Win32 clipboard, paste-settle, `_session_generation` teardown |
| reconnect → error classification | `stt/reconnect.py` `decide()` on `ErrorCategory`, exponential backoff, `retry_after` honoured |
| host auth → rate limit → concurrency | `core.py` `authenticate_client` (hash-only client ids), `auth_failure_key(peer, client_id)`, `TokenBucketRateLimiter`, `app.py` `_InflightLimiter` |
| metrics → health | `host/metrics.py` (dependency-free Prometheus text exposition) → `/metrics`, `/healthz`, `/readyz` |
| provisioning → secret storage | `host/provision.py` CLI → `security/dpapi.py`, `security/secret_store.py` |
| Docker → cPanel | `host/Dockerfile` (non-root, TLS-aware healthcheck) · `host/passenger_wsgi.py` + `docs/CPANEL_HOSTING.md` |

**Domain assumptions re-checked against the vendor's own model matrix:** `fa` is served by monolingual **nova-3**;
`nova-3-medical` is English-only. Keyterm Prompting plus a local deterministic FST is therefore the correct
architecture for Persian medical dictation, not a workaround. `medical_stt/stt/deepgram_provider.py`
`_recognition_hint_parameters()` correctly sends `keyterm=` for nova-3 and `keywords=` for legacy models, and passes
words through unchanged rather than converting them into something the model rejects.

---

## 2. Defect hunt — what was found and fixed

Severity is assigned by what the defect does to a clinician's record or to the host, not by how hard it was to find.

### High

| # | Defect | Evidence | Fix |
|---|---|---|---|
| **H1** | An abandoned provider/sender thread's `finally: self._stop.set()` could kill the **next** session — cross-session contamination on reconnect | `medical_stt/app.py` | `_session_generation` / `_is_current_session()`; a callback from an older generation is ignored (`af86a4e`) |
| **H2** | Auth-failure throttling keyed on client id alone, so one attacker could lock out every legitimate user of that id | `host/core.py` | `auth_failure_key(peer, client_id)` (`5777827`) |
| **H3** | `Metrics.quantiles()` re-accumulated buckets that were **already** cumulative → every p50/p95/p99 was wrong | `host/metrics.py` | read cumulative buckets directly (`5777827`) |
| **H4** | The word separator between two dictations was dropped; and `PyperclipException` / `FailSafeException` could escape into the Deepgram callback thread and kill the session | `medical_stt/app.py`, `injection/` | `_trailing_separator()`; three `except OSError` → `except Exception` (`62e1604`) |
| **H5** | **The benchmark credited near misses.** `numeric_accuracy("120/8","120/80 mmHg") == 1.0`, `("7.2","7.25") == 1.0`, `("14:30","14:300") == 1.0`; `english_term_recall("give IV push","give DRIVE fast") == 0.667`; `medical_term_recall(...,"ADMINISTRATION",["MI"]) == 1.0` | `benchmarks/metrics.py` | boundary-aware containment at digit ends → 0.0 / 0.0 / 0.0 / 0.333 / 0.0 (`005f1a1`) |
| **H6** | **Terminology rules fired inside longer words**, welding Latin script into Persian and injecting diagnoses that were never spoken | `processing/fst.py`, `processing/terminology.py` | `require_word_boundaries` (§8) (`bd57ae7`) |

H5 and H6 are the two that matter most, and they are the same failure in two places: a substring comparison standing
in for a word comparison. H5 made the measurement instrument unable to see H6.

### Medium

| # | Defect | Fix |
|---|---|---|
| **M1** | `host/passenger_wsgi.py` reused itself to build a **second** FastAPI app — a second httpx pool, a second metrics registry, a second client registry — so `/metrics` and the throttle state described an instance that was not serving traffic | reuse `_host_app.app`, pass `loop=` to `ASGIMiddleware` (`9b7596f`) |
| **M2** | Spaced numeric punctuation was destroyed: `"ساعت 10 : 00"` | preserved (`a032189`) |
| **M3** | Date/time patterns were ordered after the generic numeric ones, so `2024-01-05`, `1403-06-12`, `5/1/2024` were split | date/time patterns first in `numbers._PATTERNS` (`a032189`) |
| **M4** | `TerminologyEngine.load()` re-loaded into the same FST, so a specialty switch **unioned** both rule sets instead of replacing them | build a fresh `DeterministicFST` per load (`a032189`) |
| **M5** | Over-cap metric samples were dropped silently instead of being counted | fold into `_OVERFLOW_LABEL` (`5777827`) |
| **M6** | 8 permanently-zero series in `/metrics`; `inflight_grants` / `inflight_grants_capacity` / `registry_clients` missing | removed / added, set in `create_app` (not `lifespan`) (`5777827`) |
| **M7** | `host/.env.example` documented 13 of 25 env vars read by the host, and described `HOST_RATE_LIMIT_REQUESTS` as an active limit — it is inert and `core.py` logs a warning when set | rewritten; `tests/test_host_docs.py` pins it (`dacdae2`) |
| **M8** | `docs/CPANEL_HOSTING.md` told operators a healthy boot prints `host ready: …` (a2wsgi implements **no** ASGI lifespan, so it never does) and called the `.htaccess` `X-Forwarded-Proto` rule **required** (it is inert) | rewritten against a measured truth table (§12) (`4e89ae4`) |
| **M9** | `host_client_id` was declared, validated, sent with every request and documented in `host/README.md`, but absent from both `config/settings.yaml` and `.env.example` | added; `tests/test_config_docs.py` (5 tests) pins the whole client-side surface (`dacdae2`) |
| **M10** | `scripts/scan_secrets.py` scanned for Deepgram keys but not for **this project's own** credentials | `host_credential_assignment` pattern, `group=1` so one leaked secret is one fingerprint whatever variable or file holds it; 11 tests, all fail when the pattern is removed (`a04c91c`) |
| **M11** | 413 (body too large) was neither counted nor logged | both (`5777827`) |
| **M12** | `host/app.py main()` accepted any `HOST_PORT`, and `host/provision.py MAX_PREFIX_LENGTH=59` left only 4 hex digits of suffix — 65 536 possible ids and **111 collisions in 4 000 draws** | port validated (int, 1..65535, exit 2); `MAX_PREFIX_LENGTH` 59→55 (`64-9`), now 4 000/4 000 unique at exactly 64 chars (`d8f1c8c`) |
| **M13** | The key-carrying async grant client inherited httpx's default `follow_redirects` behaviour | `follow_redirects=False` (`5777827`) |

### Low

L1 plaintext-without-proxy warning · L2 `MAX_TRACKED_CLIENTS` overflow labelling · L3 histogram `DECLARED_*`
consistency · L4 413 log line · L5 `medical_stt/audio/__init__.py` claimed the queue used "no locks beyond what
`queue.Queue` provides internally" while `queue.py` creates an explicit `threading.Lock` and the real-time microphone
callback takes it on both branches · L6 dead code (`terminology.load_rules_from_yaml_data`, `stt.base.ConnectionClosed`
exported but never raised) · L9 `host/Dockerfile` passed both `useradd --no-create-home` and `--create-home` ·
L10 `.gitleaks.toml` titled "deepgram-v6" · L11 README's "Measured" capacity table hard-coded `p95 < 0.01s` and
`+0.65 MB RSS, 30-min soak`, neither of which the harness had ever produced · L12 CI · L13 README table rows.

**Not defects — disproved and left alone:** `host/metrics.py` histogram buckets *are* cumulative (the bug was in
`quantiles()`, H3); cold-start performance is already solved by the YAML cache (`load_correction_rules_raw` 275.6 ms
cold / 3.0 ms cached, FST build 2.7 ms — no action justified); `host/README.md` was already complete with respect to
env vars; `docs/CPANEL_HOSTING.md` steps 9 and 13 were accurate.

---

## 3. Re-validation of previously claimed fixes

Every item on the re-validation list was checked at source level at `5f1dcaa`, not read from a report.

| Claimed fix | Verdict | Where |
|---|---|---|
| Chunked request body bypasses the size cap | **Real and fixed** — the body is streamed and counted, not buffered | `host/app.py:354–381` |
| Redirect forwards `Authorization` to an arbitrary target | **Real and fixed** — `_NoRedirectHandler.redirect_request` raises | `medical_stt/host_client.py:32–46` |
| `X-Forwarded-Proto` trust | **Fixed, but the docs about it were wrong** — see §12 | `host/core.py` `request_is_secure` / `peer_is_trusted_proxy` |
| Provisioning prefix hang | **Real and fixed** — `MAX_DEDUP_ATTEMPTS`, and M12 removed the collision blow-up that caused it | `host/provision.py:46` |
| Client-id validation | **Real and fixed** — `1 <= len <= MAX_CLIENT_ID_LENGTH` | `host/core.py:541` |
| Re-provisioning / dedup | **Real and fixed** | `host/provision.py` |
| Deepgram send/stop race | **Real and fixed** — per-connection `_stop_event`, sender thread joins on teardown | `stt/deepgram_provider.py` |
| Close/error handling | **Partly** — the CLOSE handler worked but reported a bare `ProviderError`; see §4 | `stt/deepgram_provider.py:304–345` |
| Malformed words | **Real and fixed** — `_warn_malformed_words` counts and logs, never raises | `stt/deepgram_provider.py:308` |
| `Retry-After` | **Real and fixed** — computed from the window, honoured by `reconnect.decide()` | `host/core.py:390,414` |
| Audio queue degraded/recovery | **Real and fixed** — consecutive-drop streak, cleared only after recovery | `audio/queue.py:78` |
| Secrets omitted from config dumps | **Real and fixed** — `_FORBIDDEN_SECRET_KEYS`, and `as_dict()` is the single shape every dump path goes through | `medical_stt/config.py:80,188,281` |
| Docker TLS healthcheck | **Real and fixed** — scheme chosen from `HOST_TLS_CERTFILE`/`HOST_TLS_KEYFILE`, `/readyz`, 4 s timeout | `host/Dockerfile:47–48` |
| 50-client CI deps | **Real but incomplete** — CI installed only `requirements-dev.txt`, which contains no fastapi/httpx, so the entire ASGI host surface was silently skipped. See §11 | `.github/workflows/ci.yml` |
| Credential / log leakage | **Real and fixed**, then **strengthened** — M10 taught the scanner this project's own credentials | `scripts/scan_secrets.py` |
| 50-session isolation | **Real** — `tests/test_session_concurrency.py`, `tests/test_host_concurrency.py` |
| Simultaneous shutdown | **Real** — `tests/test_shutdown_flow.py` (247 lines added this pass) |
| Reconnect storm | **Real** — exponential backoff with jitter and `Retry-After` override |

---

## 4. Code quality (no architecture churn)

The architecture is right for 50 concurrent clinicians and was left alone: no Redis, no Celery, no Kafka, no
Kubernetes, no microservices, no database, no broker, no LLM, no embeddings, no vector store, no new framework.

Changes were confined to truthfulness and dead surface:

- `stt.base.ConnectionClosed` was exported in `medical_stt.stt.__all__` and **never raised anywhere** — a consumer
  writing the natural `except ConnectionClosed:` got a silently dead handler. The Deepgram provider now raises it in
  the two places where it knows the socket closed (the SDK's CLOSE event, and an ERROR whose exception is one of
  websockets' `ConnectionClosed*`). The category is unchanged, so `reconnect.decide()` — which keys on
  `error.category` alone — behaves identically, and `ConnectionClosed` subclasses `ProviderError` so every existing
  handler still catches it. It is deliberately **not** raised for an upstream HTTP 5xx, which also classifies as
  `SERVER_DISCONNECT`: a 502 from the token endpoint is not a closed WebSocket (`445b698`).
- `terminology.load_rules_from_yaml_data()` removed — no callers, no tests, and a second differently-shaped loader for
  the same YAML that `config.load_correction_rules_raw()` already reads (`445b698`).
- `host/core.grant_session()` (sync) was flagged as production-dead and **kept**: it is the documented reference
  implementation of the grant exchange, six tests exercise the parse/validate rules through it, and
  `grant_session_async`'s own docstring defines itself against it. Removing it would move coverage onto the async path
  for no behavioural gain.
- Four comments/docstrings that contradicted their own code were corrected rather than deleted: the audio-package lock
  claim (L5), the Dockerfile `useradd` flag pair (L9), the pool-size comment (L9), and `TerminologyEngine.apply()`,
  which now documents *why* word boundaries are the pipeline's most important safety property, with the before/after
  strings inline.

`pyproject.toml` gained `explicit_package_bases = true` and `mypy_path = "."` plus one narrow
`[[tool.mypy.overrides]]` for `host.app` / `host.passenger_wsgi`. That override exists because the host deliberately
supports two import layouts (flat, for cPanel; packaged, for uvicorn), which produces 19 `no-redef` / `unused-ignore`
errors that are artefacts of the layout and not defects. The `# type: ignore[no-index]` comments in
`host/passenger_wsgi.py` are load-bearing in flat mode and must not be removed. No formatter config exists in this
repository and none was added — `ruff format` would rewrite 78 of 116 files, which is churn, not quality.

---

## 5. Configuration consistency

Five surfaces declare configuration: `medical_stt/config.py`, `config/settings.yaml`, `.env.example`,
`host/.env.example`, and the two READMEs. They had drifted.

- **`host/.env.example` listed 13 of the 25 environment variables the host actually reads.** Missing: the whole TLS
  block, the forwarded-proxy block, the grant-pool block, the metrics admin token, the auth-failure limits, and more.
  It also described `HOST_RATE_LIMIT_REQUESTS` as an active limit; the code ignores it and logs a warning when it is
  set. Rewritten, and now pinned by tests (`dacdae2`).
- **`host/README.md` was already complete** — verified by whole-file regex over `host/*.py`, not by `grep -o`, because
  `medical_stt/config.py:481` reads `os.getenv(\n "MEDICALSTT_HOST_CLIENT_ID", …)` across a line break and any
  line-oriented scan under-reports.
- **`host_client_id`** was declared in `Settings`, validated, sent with every token request, and documented in
  `host/README.md` — but absent from `config/settings.yaml` and `.env.example`. A user following the documented setup
  could not set it. Added to both (`dacdae2`).
- Two new test modules make drift a build failure rather than a review finding: `tests/test_config_docs.py` (client
  side, 5 tests) and additions to `tests/test_host_docs.py` (host side). Together they assert that every env var read
  by the host appears in `host/.env.example` and `host/README.md`, that inert variables are labelled inert, and that
  the client's documented keys exist in both client templates.
- Sabotage-verified: deleting both new config blocks fails 3 tests; restoring them passes 5.

---

## 6. Testing

**479 → 709 collected tests** (705 passed, 4 skipped). The 4 skips are the three opt-in suites that require real
Deepgram credentials or long wall-clock time — `tests/test_live_deepgram.py` (2), `tests/test_live_load.py` (1),
`tests/test_soak.py` (1) — each gated behind an explicit environment variable and each printing the exact command that
enables it. Nothing skips for an import failure.

Categories added or extended:

- **Unit** — FST boundary semantics, negation past tense, numeric span ordering, metric quantiles, provisioning id
  uniqueness, secret-scanner patterns.
- **Integration** — host `POST /v1/session` through `TestClient`, the WSGI entry point probed as a **subprocess** with
  `PYTHONPATH=<repo>/host` so it exercises the real flat-import layout, `main()` startup validation.
- **Concurrency** — 50 simultaneous grants, in-flight limiter saturation, rate-limiter bucket bounding, session
  isolation, simultaneous shutdown, teardown while a sender thread is mid-write. `tests/test_shutdown_flow.py` grew by
  247 lines for the H1 generation-guard fix alone.
- **Security regression** — redirect refusal, credential never in a log/metric/exception/serialized settings,
  auth-failure throttling keyed by peer *and* client, body-cap enforcement under `Transfer-Encoding: chunked`,
  metrics admin token, secret scanner over both tree and history.
- **Boundary** — empty text, unready FST, zero/maximum TTL, prefix at exactly `MAX_PREFIX_LENGTH`, 64-character ids,
  client id at `MAX_CLIENT_ID_LENGTH`, request body at `MAX_REQUEST_BODY_BYTES`.
- **Invariant** — the two corpus-wide structural invariants in §8: no rewrite may weld Latin script onto a Persian
  word, checked over every regression case **and** every keyterm phrase, under **both** engine configurations.

Every fix in this pass was **sabotage-verified**: the fix was reverted in the working tree and the new tests were
confirmed to fail. Counts: H6 → 24 failures, negation → 15, M8 → 2 of 3, M10 → 11 of 11, M7/M9 → 3, M2/M3/M4 → 4.
One lesson is recorded in §13 because it changed how the later tests were written.

Test-hygiene rules that had to be learned the hard way and are now reflected in the suite: `TestClient` needs
`core.AsyncGrantClient.grant` stubbed or a legitimate request returns 503; with `HOST_AUTH_FAILURE_LIMIT=2` the third
bad request is 429, not 401; a fake provider must use per-session gates; `quantiles()` resolution is the bucket
boundary; tests must use `monkeypatch.setenv`, never `os.environ` directly; stale `__pycache__` produces false
sabotage results.

---

## 7. Capacity validation — application vs. provider

`scripts/measure_capacity.py` measures **application** capacity against a mock upstream, and says so on every run.
It deliberately does not claim anything about Deepgram.

```
=== 50-concurrent-session acceptance criteria ===
(mocked Deepgram; this is application capacity, not provider latency)

  PASS  50 simultaneous session requests     50/50 ok, 0 x 429
  PASS  token acquisition p95 < 2s           98.6 ms
  PASS  event loop free during issuance      0.100s vs 2.50s if serialized
  PASS  no memory growth per session batch   +1.08 MB over 250 sessions
  PASS  active sessions after shutdown == 0  in-flight grants = 0
  PASS  rate limiter state bounded           50 buckets for 50 clients

RESULT: PASS (all application-side criteria met)
```

Run-to-run: p95 98.6–99.8 ms, loop-free 0.100–0.104 s, memory +1.08 to +1.35 MB per 250 sessions. The mock upstream
adds a 50 ms delay, so **a sub-10 ms p95 is arithmetically impossible** — which is precisely why the README's old
`p95 < 0.01s` figure was stale rather than merely optimistic (L11). The README now lists the criteria and the command
instead of hard-coded numbers, and a test fails CI if a hard-coded capacity figure returns.

**What this does not establish, and is not claimed:** Deepgram's concurrent-stream limit for this account. That is a
provider-side property of a paid account and can only be measured by spending real quota. The opt-in path exists and
is documented at the end of every harness run:

```
MEDICAL_STT_LOAD_TEST=1 pytest tests/test_live_load.py -m loadtest -s
```

It was **not** run during this pass — it requires live credentials and consumes paid minutes. Consequently no claim
of "50 real concurrent Deepgram streams" is made anywhere in this report or in the repository.

Also measured, no action taken: `load_correction_rules_raw` 275.6 ms cold / 3.0 ms cached; `DeterministicFST` build
2.7 ms; `data/corrections.yaml` 835 rules. Startup cost is already dominated by a one-time YAML read that is cached,
so there is no justified performance work here (§9).

---

## 8. Medical-accuracy safety review — the deterministic terminology pipeline

The governing principle for this pipeline is **preserve > rewrite**: an unchanged transcript is always safe, a
rewritten one has to earn it. Two violations of that principle were found, and they are the most clinically serious
defects in the repository.

### 8.1 Rules fired inside longer words (H6, `bd57ae7`)

`DeterministicFST` used pyahocorasick's `iter_long`, which matches **substrings**. Persian builds words by attachment,
and the shipped rule set contains short sources — `مش`→`Mesh`, `پا`→`Foot`, `دست`→`Hand`, `امی`→`MI`, `دما`→`T`,
`تنفس`→`RR`, `هیپ`→`Hip`, `ناف`→`Umbilicus`, `خال`→`Nevus`. 27 active rules have a source of ≤3 characters. The
result, with `enable_context_dependent_terms: true`:

```
مشکل تنفسی       ->  Meshکل RRی           "respiratory problem"
پاسخ دهید        ->  Footسخ دهید         "please answer"
دستگاه تنفس      ->  Handگاه RR           "ventilator"
دماسنج           ->  Tسنج                 "thermometer"
هیپوگلیسمی       ->  Hipوگلیسمی           "hypoglycaemia"
خالی از درد      ->  Nevusی از درد        "free of pain"
رحمت             ->  Uterusت
امید به زندگی    ->  MIد به زندگی        "life expectancy"
پایداری همودینامیک -> stableی همودینMIک    "haemodynamic stability"
```

The last two are the dangerous shape: **`MI` — myocardial infarction — injected into a sentence about life
expectancy**, then pasted into the clinician's record. A diagnosis that was never spoken.

Six phrases in this repository's *own* corpora were affected, including three regression fixtures:

```
MI در ECG مشاهده شد            ->  MI در ECG Meshاهده شد
توده به قطر 2 cm مشاهده شد     ->  Mass به قطر 2 cm Meshاهده شد
علائم دی وی تی مشاهده شد        ->  علائم DVT Meshاهده شد
```

`مشاهده` ("observed") is one of the commonest phrases in an imaging report, and `مش` (surgical mesh) is a substring of
it. **These fixtures passed CI**, because they assert hand-written `must_contain` / `must_not_contain` lists and no
list thought to forbid `Mesh`.

*Measured scope, honestly stated:* over the 107 phrases in `benchmarks/corpus.yaml`,
`tests/fixtures/medical_regression_corpus.yaml` and `data/keyterms/*.yaml` — 6 were corrupted with
`enable_context_dependent_terms: true` (the configuration `README.md:509` explicitly offers "a reviewed deployment"),
and **none** with the shipped default, where only `ناف`→`Umbilicus` (`safe_lexical`) reached ordinary words such as
`نافه` and `نافذ`. On a 27-phrase general-vocabulary probe the default path produced 3 mid-word welds and the opt-in
path 25; both are 0 after the fix.

*Fix:* `DeterministicFST.apply()` gained `require_word_boundaries`, **off by default** — the class is a general
longest-match rewriter and its substring semantics are pinned by `tests/test_fst.py` — and `TerminologyEngine.apply()`
turns it on. A match is dropped when a word character touches either end. `str.isalnum()` supplies letters and digits
(it is Unicode-aware, so it covers Persian and Arabic letters and the Persian/Arabic-Indic digits); the zero-width
joiners are added explicitly because a ZWNJ-joined compound is **one** word, and treating the joiner as a boundary
would turn `میلی‌جیوه` (millimetre of mercury) into `میلی‌Mercury`. Filtering happens after overlap resolution, which
is provably equivalent and is documented at the call site so the choice is not re-litigated.

Every intended whole-word rewrite still fires: `آی سی یو`→ICU, `سکته قلبی`→MI, `نوار قلب`→ECG, `کبد چرب`→Fatty liver,
`فشار خون 120/80 میلی‌جیوه`→`BP 120/80 میلی‌جیوه` with the numbers untouched. The offline benchmark is unchanged
(WER/CER 0.000, all fidelity metrics 1.00) because its references are already in written form and it runs the default
engine.

*Data-quality finding, reported not changed:* roughly ten entries in `data/corrections.yaml` have sources that already
contain another rule's English target — `آنزیمهای Heartی`, `Joint حیف`, `تعویض کامل Joint هیپ`, `V/S پایدار`,
`استنت DJ`. These look like output of this very bug, captured into the data while the corpus was being built. They
cannot match real ASR output, so they are inert rather than harmful, but they should be reviewed against the
recordings they came from. Rewriting clinical data was not this pass's call.

### 8.2 The negation guard only knew the present tense (`5664bd3`)

`processing/negation.py` promises one narrow thing: that a negated finding cannot be flipped into a positive one, and
that the markers themselves are never rewritten. It kept that promise only for the present tense. Clinical dictation
reports what **was** found:

```
بیمار شکایتی نداشت        "the patient had no complaint"    -> undetected
تب نداشت                  "there was no fever"              -> undetected
هیچ توده‌ای دیده نشد       "no mass was seen"                -> undetected
آنژیوگرافی انجام نشد       "the angiography was not done"    -> undetected
سابقه دیابت را نفی کرد     "denies a history of diabetes"    -> undetected
```

An undetected negation means the marker is unprotected from rewriting, and — worse — `negation_preservation` in
`benchmarks/metrics.py` returns a **vacuous 1.0** whenever it finds no spans. For a safety metric, a silent pass is
the worst possible failure mode.

Added: `وجود نداشت`, `نداشت`, `نبود`, `نشد`, `نشود`, `نمی‌شود`, `نمیشود`, `رد گردید`, `فاقد`, `هیچ`, `نفی` (11 → 22
markers). Because matching is substring-based, **every candidate was checked against the 945 distinct Persian tokens
in the repository's data, fixtures and documentation** for words it would match inside. Two shorter forms were
considered and rejected, with the reasons recorded in the source:

- `رد` alone sits inside `درصد` ("percent") and `مرداد` (a month name) — it would flag an ordinary oxygen-saturation
  reading as a negated finding, which is the dangerous direction. Only `رد شد` and `رد گردید` are listed.
- `نفی` alone sits inside `نفیس` ("exquisite"). It is listed anyway: a false positive protects three more characters
  and tells a consumer to be more careful, whereas listing only the verb forms would miss `نفی سابقه`, the
  noun-phrase Persian notes actually use. That asymmetry is the module's whole design principle, so the trade is
  recorded rather than silently made.

### 8.3 What was verified and needs no change

- **Numeric fidelity:** across 20 clinical measurements — `120/80`, `2.5 mg`, `180 mg/dL`, `38.4`, `94 درصد`,
  `1403-06-12`, `10:30`, `5-10`, `1:2`, `±2`, `GFR 45 mL/min`, `INR 2.5 و PT 13`, `K 3.5 Na 140 Cl 100`,
  Persian-digit `۱۲۰/۸۰`, `۵ mg هر ۸ ساعت`, `۰.۲۵` — every digit **value** survives the whole pipeline. The only
  transformation is Persian-digit→ASCII folding, which preserves value, and `SpO2` legitimately introduces a digit
  that is part of the term, not a measurement.
- **Dangerous rules stay blocked:** the 3 rules marked `dangerous: true` / category `unsafe` are
  `ناشتا`→`NPO` (a state rewritten as an order), `کاهش`→`DC` and `کم شدن`→`DC` ("decrease" rewritten as
  "discontinue" — an inversion of clinical intent). Verified never to fire under **both** engine configurations.
  `BLOCKED_CATEGORIES = frozenset({"unsafe"})` and `parse_rules` forces `dangerous=True` for that category.
- **Ambiguity is opt-in, not deleted:** `کلیه` (kidney / "all"), `نمونه` (specimen / "sample"), `جفت` (placenta /
  "pair"), `کشت` (culture / "sowing"), `شانه` (shoulder / "combed"), `رحم` (uterus / "have mercy") are all
  `context_dependent` and off by default. `tests/test_data_safety.py` pins this.
- **Confidence gating:** `medical_stt/app.py` skips terminology rewriting entirely when the transcript carries a
  confidence warning, so a low-confidence recognition is never "corrected" into a confident-looking wrong term.
- **Rule distribution:** 835 rules — `specialty_terminology` 486, `abbreviation_expansion` 197, `context_dependent`
  105, `safe_lexical` 44, `unsafe` 3.

**What is still not established, and is not claimed:** accuracy against real clinical Persian speech. There is no
clinical corpus in this repository, `benchmarks/corpus.yaml` is 22 synthetic cases across 11 categories, and the
repository correctly refuses to publish accuracy numbers. Everything in §8 is a review of the **deterministic
rewriting layer's fidelity guarantees**, which is what can be verified from source — not a validation of ASR accuracy.

---

## 9. Performance

Measured first, changed only where a measurement justified it.

| Observation | Action |
|---|---|
| `load_correction_rules_raw` 275.6 ms cold / **3.0 ms cached**; FST build 2.7 ms | none — already solved by the YAML cache |
| 50 concurrent grants serialize to 2.50 s if the loop blocks | none needed — measured 0.100 s loop-free, i.e. the async path is already correct |
| `Metrics.quantiles()` re-accumulated cumulative buckets | fixed for **correctness** (H3), which also removed redundant work |
| The audio callback takes one explicit `threading.Lock` on both branches | **left alone** — it is never held across a queue operation, and the alternative (lock-free counters) would trade a bounded, measured cost for unauditable code. The docstring now says so (L5) |
| `TerminologyEngine.load()` unioned rule sets across specialty switches | fixed for correctness (M4); rebuilding a 2.7 ms FST is not a cost worth optimizing |

No caching layer, no async rewrite, no batching, no pooling change was introduced. Nothing in the profile justified
one, and the user's constraint against over-engineering applies with full force here.

---

## 10. Dependency and supply-chain hardening

**State:** client deps are range-pinned (`python-dotenv>=1.0.1,<2.0`, `PyYAML>=6.0.1,<7.0`,
`sounddevice>=0.4.6,<0.6`, `pyahocorasick>=2.0.0,<3.0`, `python-bidi>=0.4.2,<0.7`,
`pyautogui`/`pyperclip` behind `sys_platform != "win32"`), with `deepgram-sdk==7.9.0` exactly pinned. Host deps are
range-pinned (`fastapi>=0.115,<1.0`, `uvicorn>=0.30,<1.0`, `httpx>=0.27,<1.0`, `a2wsgi>=1.10,<2.0`). Both dev files
resolve `-r requirements.txt` from their own directory, so `host/requirements-dev.txt` pulls the host set and not the
client set.

**Added:** a `dependency-audit` CI job that resolves **both** requirement files against the advisory database on every
push, with `--strict`. Advisories are published after a version is pinned, so a green build yesterday says nothing
about today (`d2bcbd7`). Verified locally: `pip-audit -r requirements.txt -r host/requirements.txt --strict` → *no
known vulnerabilities*.

**No lock file — deliberate, and argued both ways.** A single frozen lock would be *wrong* for this project:
`requirements.txt` carries `sys_platform != "win32"` markers, so one resolved set cannot be correct for both a Windows
clinician's desktop and the Linux host. Two platform-specific locks would work and is the honest remaining gap; it was
not added because it introduces a build step, a second artefact to keep in sync, and a failure mode (stale lock) that
is worse than the one it prevents for a 6-dependency client. `pip-audit` on every push is the implemented half of this
phase, and it catches the risk that actually materialises — a published advisory against a version inside the allowed
range. **Recorded as an open item, not as solved.**

Also hardened: `.gitleaks.toml` retitled from "deepgram-v6" (L10), and `scripts/scan_secrets.py` taught this project's
own credential shapes (M10) so the scanner covers `HOST_SHARED_SECRET`, `HOST_METRICS_ADMIN_TOKEN`,
`MEDICALSTT_HOST_SECRET` and `DEEPGRAM_API_KEY` with `group=1` — one leaked secret produces one fingerprint no matter
which variable or file holds it. Placeholder forms (`your-secret-here`, `changeme`, `xxx`) verified still clean.
`scripts/secret_scan_baseline.txt` retains one entry that matches nothing; it is kept deliberately because it
documents the SECURITY.md incident, and the scanner says so rather than failing.

---

## 11. CI hardening

**The defect (L12):** the `test` job installed only `requirements-dev.txt`, which contains no `fastapi` and no
`httpx`. Every ASGI-level host suite begins with `pytest.importorskip(...)`, so the entire host request surface —
authentication, rate limiting, body caps, metrics, `/v1/session` — was **skipped, and the job reported green.** A CI
configuration that cannot fail is worse than no CI, because it manufactures confidence.

**Fixed (`d2bcbd7`):**
- the `test` job now installs `host/requirements-dev.txt` as well;
- pytest runs with `-rs` so every skip reason is printed, output is `tee`'d to `pytest.log` under `pipefail`, and a
  dedicated step **fails the build** if the log contains `could not import`;
- `ruff check .` and `mypy medical_stt host benchmarks` run repo-wide, not per-directory;
- the `host-test` job's file list was completed;
- a `dependency-audit` job was added (§10).

**Five jobs now:** `test`, `host-test`, `windows-check`, `dependency-audit`, `secret-scan`.

**`windows-check` was audited rather than changed.** It runs on a real `windows-latest` runner, executes the five
suites that cannot run on Linux (session lifecycle/mutex, secret store/DPAPI, injection, self-test script, shutdown
flow) plus `scripts/windows_selftest.py` for DPAPI + named mutex + SendInput, and surfaces failures as GitHub
annotations so the reason is visible in the Checks tab. I checked it for the same silent-skip defect: none of those
five files uses `importorskip`, and the single `skipif` in `test_secret_store.py:106` fires when DPAPI **is**
available — i.e. exactly one off-Windows fallback test skips on Windows, and the exit code is propagated through
`$LASTEXITCODE` in PowerShell. No change was warranted.

**Still not covered by CI, stated plainly:** the live Deepgram integration, the 50-stream load test, and the soak test
all require credentials and wall-clock time and remain opt-in; the Docker image is never built in CI; there is no
Dependabot/Renovate configuration, so dependency updates are manual.

---

## 12. Documentation

Corrected only where the docs contradicted the code. The most consequential was `docs/CPANEL_HOSTING.md` (M8), which
was actively misleading operators on the shared-hosting path — the path most likely to be used by a small clinic.

**Empirically measured HTTPS truth table**, driven through the real `host/passenger_wsgi.py` callable with
`HOST_ALLOW_HTTP` unset and `HOST_FORWARDED_ALLOW_IPS` at its default:

| `wsgi.url_scheme` | `REMOTE_ADDR` | `X-Forwarded-Proto` | Result |
|---|---|---|---|
| `https` | end client | any | **200** — scheme is checked first |
| `http` | end client | `https` | **400** — peer is not a configured proxy |
| `http` | `127.0.0.1` | `https` | **200** |
| `http` | *(no `REMOTE_PORT`)* | `https` | **400** — a2wsgi sets `scope["client"]` only when both addr and port are present |

The conclusion now recorded in the guide: mod_passenger runs **in-process**, so `REMOTE_ADDR` is the *end client*, not
loopback. `HOST_FORWARDED_ALLOW_IPS=127.0.0.1` therefore never matches and the `.htaccess` header rule the guide
called **required** is inert (harmless, but inert) — trusting a client-supplied header would let anyone on the
internet forge HTTPS. `wsgi.url_scheme` is what decides, and `HOST_ALLOW_HTTP=1` is the supported answer on shared
hosting, with the guide stating exactly why that is safe there and nowhere else. `HOST_BIND` was "recommended" and is
read only by `python -m host.app`, which Passenger never runs; it and `HOST_PORT` are now marked inert.

The guide also promised a `host ready: clients=1 …` boot line. That line comes from the ASGI lifespan handler, and
**a2wsgi implements no ASGI lifespan protocol** — verified by importing `passenger_wsgi` with logging captured: no
such line, while `/readyz` returns `200 {"status":"ready"}`. An operator following the guide hunted for output that
cannot appear and concluded the deploy had failed. Troubleshooting rows 6 and 7 recommended curling a plaintext
`http://127.0.0.1:<port>/healthz`, which returns 400 by design unless `HOST_ALLOW_HTTP=1` — reading as a broken app
when it is not one. 279 → 342 lines.

Also corrected: README's "Measured" capacity table (L11, §7), two README table rows (L13), `host/Dockerfile`'s header
comment about pool sizes (L9), `.gitleaks.toml`'s title (L10), and `medical_stt/audio/__init__.py`'s lock claim (L5).
No document was rewritten for style, and no accurate claim was touched — steps 9 and 13 of the cPanel guide were
verified correct and left alone.

---

## 13. Implementation discipline

Smallest correct change, throughout. Concretely:

- No architecture change, no new dependency, no new service, no new framework, no new abstraction layer. The largest
  single behavioural addition is one keyword argument with a default that preserves the old contract.
- Correct implementations were left alone and are named as such in §2 ("disproved"), §9, §11 and §12.
- Backwards compatibility preserved: `DeterministicFST.apply()`'s new parameter defaults to `False`;
  `ConnectionClosed` subclasses `ProviderError` so every existing handler keeps working; the sync `grant_session` was
  kept rather than removed; the dead secret-scan baseline entry was kept rather than deleted.
- Security was never weakened to simplify, and no compatibility setting was removed without a migration path.
- Every fix has a regression test, and every regression test was **sabotage-verified** by reverting the fix.

One discipline failure is worth recording because it changed the later work. `tests/test_host_docs.py` gained a test
asserting that the stale README figures `< 0.01s` and `+0.65 mb` no longer appear — and it failed, because the rewritten
README paragraph quoted those exact strings in order to explain what had been wrong. The test was banning the literals
the documentation legitimately used. The fix was to remove the literals from the prose rather than to weaken the test:
a doc-content test should pin **structure** (does a hard-coded capacity figure appear in a table row?) not substrings,
and in this case the cleaner answer was not to repeat a wrong number at all. Two earlier instances of a related
mistake are also recorded: chaining `&& git commit` after a linter, which twice produced a commit that silently never
ran because `ruff check .` failed on a pre-existing `run.py:5` W292 — commit first, lint after, and verify with
`git log --oneline -1`.

---

## 14. Quality gates — final state

| Gate | Command | Result |
|---|---|---|
| Test suite | `pytest tests/` | **705 passed, 4 skipped** (from 479 passed / 4 skipped) |
| Skips justified | `pytest tests/ -rs` | 4 skips, all opt-in live/soak/load, each printing its enabling command |
| Lint | `ruff check .` | **All checks passed** |
| Types | `mypy medical_stt host benchmarks` | **no issues in 40 source files** |
| Known CVEs | `pip-audit -r requirements.txt -r host/requirements.txt --strict` | **No known vulnerabilities found** |
| Secrets (tree) | `python scripts/scan_secrets.py` | **clean**, exit 0 |
| Secrets (history) | `python scripts/scan_secrets.py --history` | **clean**, exit 0 |
| App capacity | `python scripts/measure_capacity.py` | **PASS**, all 6 criteria |
| Offline benchmark | `python benchmarks/run_benchmark.py --corpus … --hypotheses … --apply-processing` | WER 0.000, CER 0.000, numeric 1.00, negation 1.00, term recall 1.00 across 22 cases / 11 categories |
| gitleaks config | `.gitleaks.toml` | present, correctly titled |

Environment: a2wsgi 1.10.10, deepgram-sdk 7.9.0, fastapi 0.142.2, starlette 1.7.0, httpx 0.28.1, uvicorn 0.54.0,
pyahocorasick 2.3.1, python-bidi 0.6.11, pytest 8.4.2, pytest-cov 5.0.0, ruff 0.16.10, mypy 1.20.2.

**Gates that could not be run in this environment:** `docker build` and `hadolint` (no Docker daemon; `useradd` is
also absent, so the Dockerfile's user-creation step is unverifiable here — it was reviewed by reading), the live
Deepgram integration/load/soak tests (require credentials and paid quota), and the Windows DPAPI/mutex/SendInput paths
(Linux sandbox; covered only by the `windows-check` CI job).

**Commits:** `af86a4e` H1 · `5777827` H2/H3/M5/M6/M11/M13/L2/L3/L4 · `62e1604` H4 · `a032189` M2/M3/M4 + `run.py` ·
`005f1a1` H5 · `9b7596f` M1 · `d2bcbd7` L12 · `d8f1c8c` M12/L1 · `a04c91c` M10 · `dacdae2` M7/M9/L13 ·
`4e89ae4` M8/L5/L9/L10/L11 · `445b698` L6 · `bd57ae7` H6 · `5664bd3` negation past tense.

---

## 15. Scores

Each dimension scored 1–10 against what the repository actually does, not against what it intends. Passing tests is
not treated as evidence of production readiness, measured application capacity is not treated as evidence of provider
capacity, and a deterministic rewriting layer with intact fidelity guarantees is not treated as clinical validation.

| # | Dimension | Score | One-line justification |
|---|---|---|---|
| 1 | Architecture | **8** | The trust boundary is real — the key never leaves the host, clients hold only ~30 s tokens, audio goes straight to Deepgram, and nothing was added that a 50-user system does not need; the cost is a permanently awkward dual flat/package import layout and a WSGI path with no lifespan support. |
| 2 | Code quality | **7** | Consistently typed, ruff- and mypy-clean repo-wide with dead code and lying comments now removed, but `host/core.py` (897 lines) and `host/app.py` are near-monolithic and the flat-import layout forces type-ignore comments that are load-bearing rather than incidental. |
| 3 | Security | **8** | The credential path is genuinely narrow and auditable — no redirects on either grant client, hash-only client ids, DPAPI at rest, a body cap enforced while streaming, peer-aware auth throttling, and a scanner that now covers this project's own secrets; the residual exposure is that `HOST_ALLOW_HTTP=1` is the documented answer on shared hosting, which puts a bearer secret on plaintext. |
| 4 | Concurrency | **8** | 50 simultaneous grants measured with the event loop free 99.6% of the time, zero in-flight leaks after shutdown, bounded rate-limiter state and a bounded client registry with overflow folding; the audio callback still takes one explicit lock on the real-time path, which is bounded and never held across a queue operation, but is real-time work. |
| 5 | Reliability | **7** | Reconnect with exponential backoff and honoured `Retry-After`, per-session generation guards against abandoned threads, backend exceptions contained so they cannot kill the Deepgram callback thread, and a simultaneous-shutdown path extended by 247 lines this pass; a2wsgi implements no ASGI lifespan, so under Passenger there is no orderly pool shutdown at all — documented, not fixed. |
| 6 | STT integration | **8** | Correct against the vendor's own matrix — monolingual nova-3 for `fa` with Keyterm Prompting, `keywords` for legacy models, a fresh token per connection, `access_token` rather than `api_key`, malformed words counted not raised, utterance-end and speech-final segmentation; accuracy against real Persian medical audio is unmeasured and is not claimed. |
| 7 | Persian processing | **8** | The two defects found here were the most clinically serious in the repository — substring rewriting welding Latin into Persian words, and a negation guard that only knew the present tense — and both are now fixed with corpus-wide invariants covering both engine configurations; what remains unmeasured is ASR accuracy, not the deterministic layer. |
| 8 | Medical safety | **7** | Numeric values, negation markers and the 3 `dangerous`/`unsafe` rules are all verifiably preserved under both configurations, ambiguous mappings are opt-in rather than deleted, and low-confidence transcripts skip rewriting entirely; roughly ten rule entries in `data/corrections.yaml` appear to be corrupted output captured into the data and need review against their source recordings, and no clinical corpus exists to validate against. |
| 9 | Windows integration | **7** | DPAPI secret storage, a named mutex for single-instance, SendInput injection with a tested fallback, exception containment fixed, a self-test script, and a real `windows-latest` CI job that verifies DPAPI/mutex/SendInput and surfaces failures as annotations; none of it could be executed in this Linux sandbox, so the evidence is CI configuration plus source review. |
| 10 | Testing | **8** | 479 → 709 tests spanning unit, integration, concurrency, security-regression, boundary and invariant categories, with every fix sabotage-verified by reverting it; the corpus assertions were `must_contain`-style and let an entire corruption class through until structural invariants were added, and the live/soak/load suites remain unrun. |
| 11 | CI/CD | **7** | Five jobs, and the one that mattered was silently skipping the entire ASGI host surface until `-rs`, `pipefail` and a `could not import` guard made that a build failure; still no Docker build, no lock file, no Dependabot, and no live-provider coverage. |
| 12 | Deployment | **7** | A non-root image with a TLS-aware healthcheck and a cPanel guide whose HTTPS behaviour, boot output and inert variables are now measured rather than asserted; Passenger cannot run the ASGI lifespan so there is no orderly shutdown or boot diagnostic on that path, and the image could not be built or linted here. |
| 13 | Dependency management | **6** | Six client and four host dependencies, one exactly pinned and the rest range-pinned with correct platform markers, plus `pip-audit --strict` on both files in CI and a clean result today; there is no lock file, so two installs a month apart are not guaranteed to resolve identically, and updates are manual. |
| 14 | Observability | **8** | A dependency-free Prometheus exposition with correct quantiles, cumulative buckets read as cumulative, over-cap samples folded rather than dropped, in-flight and registry gauges that are actually set, `/healthz` and `/readyz`, one logger name, and no credential or transcript content in any log, metric or exception; under Passenger no startup line is emitted at all and `HOST_METRICS_ADMIN_TOKEN` defaults to unset (warned, and `/metrics` exposes volume and client counts). |
| 15 | Clinical validation | **3** | There is no clinical speech corpus, the shipped benchmark is 22 synthetic cases, and no accuracy figure has ever been measured against real Persian medical audio — the repository is right not to publish numbers, which means this dimension is honestly unsatisfied rather than merely undocumented. |

**Mean 7.1** (107 / 15). The two lowest scores are the two that cannot be fixed from inside a repository: dependency
reproducibility needs a lock-file decision the maintainer should make deliberately (13), and clinical validation needs
recordings from real clinicians (15). Everything else scored is limited by something that was measured, documented,
and left with an explicit open item rather than quietly assumed.

**Not claimed anywhere:** production readiness on the strength of passing tests; 50 concurrent real Deepgram streams;
medical accuracy without a clinical corpus.
