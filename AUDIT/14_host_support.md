# 14 — Module review: `host/provision.py`, `host/metrics.py`, `host/Dockerfile` (host support)

Read in full: host/provision.py (148 lines), host/metrics.py (340 lines),
host/Dockerfile (46 lines), host/requirements.txt. Cross-read: host/app.py
`main()` L399–440, `harden` middleware L200–226, host/core.py `ClientRegistry`
L554–609; live repro of the provisioning loop (below); bandit report.

## Surface

`provision.py`: `generate_client_id` / `generate_secret` (256-bit URL-safe) /
`registry_line` (client_id:sha256hex), `--count/--prefix/--start-index/--out/
--secrets-dir`, append-mode registry writes, per-device 0600 secret files.
`metrics.py`: `Metrics` (counters/gauges/histograms + bounded client-label
cardinality), Prometheus text `render()`, `quantiles()`. `Dockerfile`:
python:3.12-slim, non-root uid 10001, runtime-only secrets, `/readyz`
HEALTHCHECK, STOPSIGNAL SIGTERM.

## Findings

**F1 · Medium · `--prefix` longer than ~56 chars with `--start-index` hangs
the tool forever.** FACT: provision.py:46 truncates
`f"{safe_prefix}-{suffix}"[:64]`, and `_generate` L77–83 dedupes with
`while len(ids) < count: candidate = generate_client_id(...); if candidate in
seen: continue`. With `--start-index`, the suffix is `f"{index:03d}"`, so any
prefix ≥ 58 safe characters makes every generated id truncate to the same
64-char value; the loop then never adds a second element and never exits.
Repro in this sandbox: prefix `"x"*62` yields `doctor-000`-style ids fine, but
`generate_client_id("x"*62, i)` is identical for every i and the dedup loop
was still running at 50 iterations (bail-out added by the audit; the shipped
code has none). Without `--start-index` the suffix is `token_hex(4)` and
collisions are merely astronomically unlikely, so only the combination
`--count 2+ --start-index --prefix <≥58 chars>` hangs. Impact is operator
annoyance, not security (no secret is minted before the loop exits), and a
hospital will realistically use `doctor`, but a tool that can spin forever on
a flag typo deserves a guard. Fix (suggestion only): validate
`len(safe_prefix) + 5 <= 64` up front and error out, or bail after
`MAX_COUNT * 2` dedup attempts.

**F2 · Medium · The Dockerfile HEALTHCHECK probes `http://127.0.0.1` while
the documented container run serves TLS — the check can never pass.** FACT:
Dockerfile:40–41 runs
`urllib.request.urlopen('http://127.0.0.1:'+os.getenv('HOST_PORT','8443')+'/readyz')`,
but README.md:129–132 and host/.env.example document `HOST_TLS_CERTFILE`/
`HOST_TLS_KEYFILE` as the normal deployment, and host/app.py:408–424 passes
them to `uvicorn.run(ssl_certfile=..., ssl_keyfile=...)` — i.e. uvicorn itself
terminates TLS inside the container on the same port the healthcheck hits.
A plain-HTTP request to a TLS listener fails with an SSL handshake error, so
under the documented configuration Docker marks the container unhealthy
forever. There is no loopback HTTP exemption in `harden` (host/app.py:204–216
rejects `http` scheme regardless of peer). The comment at Dockerfile:33–36
("TLS cert/key are mounted at runtime") confirms TLS is the expected mode; the
HEALTHCHECK contradicts it. Fix (suggestion only): make the probe protocol
follow the cert env vars (`https` + unverified context when
`HOST_TLS_CERTFILE` is set), or document a separate internal health port.

**F3 · Low · `--client-id` is stored verbatim without the id validation the
host will later apply.** FACT: provision.py:95–96 stores `args.client_id`
directly (`ids = [args.client_id]`) while the batch path sanitizes through
`generate_client_id`. `ClientRegistry.from_file` (host/core.py:580–589)
*refuses the whole file* (`ConfigurationError`) on any line whose id fails
`is_valid_client_id` (alnum + `-_.`, ≤64). So
`python -m host.provision --client-id "Dr. Smith (cardio)"` writes a registry
line the host then rejects at startup — a bad id in one invocation takes down
the entire 50-client deployment on next restart. The failure is loud (not
silent), which is why this is Low, but the tool should catch it first. Fix
(suggestion only): call `core.is_valid_client_id` on `--client-id` and exit 2
with a clear message.

**F4 · Low · Duplicate `--client-id` provisions silently replace.** FACT:
provision.py appends (`open("a")`, L108) and `ClientRegistry.from_file`
parses into `clients[client_id] = digest` (core.py:590) — dict semantics, so
a re-provisioned id silently supersedes the earlier hash. Harmless for the
intended "add one more device" flow, but nothing warns the operator that a
device just lost access (its stored secret no longer matches). Info-grade
operator ergonomics; a stderr note when the id already existed in the file
would suffice. (Combines with F3: appending an invalid line is possible but
then the *whole file* fails to load, so there is no partially-valid state.)

**F5 · Info · Dockerfile installs pip deps as root in a layer that also
creates the user, widening the attack window slightly.** FACT:
Dockerfile:16–18 runs `pip install` before `useradd` and the `USER stt`
switch; standard practice is to create the user first and install as that
user. No files are world-writable in the final image and `COPY host/` runs
before `USER`, so the runtime posture (uid 10001, no shell needs) is fine —
this is a hardening nicety, not an exposure.

**F6 · Info · `Metrics.quantiles` returns bucket upper bounds, not
interpolated values.** FACT: metrics.py:247–261 picks the first bucket whose
cumulative count reaches `q * count`; with LATENCY_BUCKETS' coarse upper end
(10s, 30s) a p95 among mostly-fast requests reports the *first exceeded*
boundary, e.g. a single slow 8s request among 49 sub-100ms ones reports
p95 = 10.0. The docstring says "deliberately naive" and it is only consumed
by tests/load reporting; fine as documented.

**F7 · Info · `_Histogram.observe` is O(buckets) per observation** (12
buckets, linear scan L92–99) — irrelevant at 50 sessions/60s but worth
knowing if metrics ever move to a hot path.

## Correct in this module (verified)

- **Secret hygiene is exemplary.** 256-bit `secrets.token_urlsafe(32)` with a
  comment justifying the entropy (L26–29); registry receives
  `sha256(secret)` only; plaintext goes to 0600-per-mode files created via
  `os.open(..., 0o600)` (L89–95) or printed once with an explicit "delete
  this output" warning (L132–138); nothing secret ever logged. MAX_COUNT=5000
  refuses absurd registries (L69–72).
- **Metrics privacy invariant holds.** Only counters/durations are stored;
  `render()` output contains metric names, labels, and numbers — no token or
  secret field is ever passed in (module 01 cross-check); client ids are
  bounded (`MAX_LABELLED_CLIENTS=2000`, overflow folds into `other`,
  L60–77); label values are escaped for the Prometheus text format
  (L44–48); rendering is snapshot-under-lock so no torn reads (L272–283).
- **Dockerfile basics are right:** runtime-only secrets (header comment),
  no COPYed credentials, non-root uid 10001, STOPSIGNAL SIGTERM matching
  uvicorn's `timeout_graceful_shutdown=15`, EXPOSE 8443 consistent with the
  default port, HEALTHCHECK present (but see F2).
- requirements.txt is deliberately minimal (fastapi/uvicorn/httpx, no SDK).

## Summary (3 lines)

Support tooling is mostly careful — hash-only registry, 0600 secret files,
bounded metrics cardinality, non-root container. Three real defects: the
provisioner can hang forever on a long `--prefix` with `--start-index`, and
its `--client-id` path skips validation the host will enforce file-wide; the
Dockerfile HEALTHCHECK speaks HTTP to a TLS-terminating listener and can
never pass under the documented deployment. All fixes are local and small.
