# 01 — Module review: `host/app.py` (HTTP surface)

Read in full: host/app.py L1–440 (440 lines). Cross-read: host/core.py (module 02),
uvicorn 0.54 `uvicorn/middleware/proxy_headers.py` (installed copy, lines 13–59),
`AUDIT/tools/bandit.txt`.

## Surface

`/healthz` L231–233; `/readyz` L235–243 (unauthenticated, boolean only);
`/metrics` L240–256; `POST /v1/session` L257–378. HTTPS middleware L200–226.
Module-level `app = create_app()` L394–397. Security headers L92–98
(nosniff, DENY, no-store, no-referrer) applied to every response L220–226.

## Findings

**F1 · Medium · Unbounded body buffering for chunked requests.**
FACT: host/app.py:308–316 caps only a *declared* `Content-Length`; the actual read
at L321 `raw = await request.body()` buffers the whole body before the post-hoc
check at L325. Starlette's `Request.body()` joins all chunks with no size cap, so
a request using `Transfer-Encoding: chunked` (no Content-Length) is buffered
without bound. Requires valid credentials (auth happens first, L271–292), so this
is an authenticated memory-DoS vector, not anonymous. It is directly reachable:
the Dockerfile/main() path exposes uvicorn with no proxy in front.
HYPOTHESIS: in the README-recommended reverse-proxy deployment, nginx's default
1 MB body cap would contain it — unverified for this repo's deployment.
Fix (suggestion only): wrap the stream (`request.stream()`) with a running cap
and abort past `max_request_body_bytes` instead of buffering then checking.

**F2 · Low · The app-level HTTPS/X-Forwarded-Proto trust layer is inoperative
behind a proxy.**
FACT: host/app.py:211 reads `request.client.host` and passes it to
`request_is_secure()` (L212–216). FACT: uvicorn 0.54
`middleware/proxy_headers.py` docstring lines 16–17 — "Modifies the `client` and
`scheme` information so that they reference the connecting client, rather that
the connecting proxy" — and lines 53–59 replace `scope["client"]` with the
X-Forwarded-For address for trusted peers. So behind the documented loopback
proxy, `peer_host` at L211 is the *external client IP*, which can never match
`forwarded_allow_ips`, and `peer_is_trusted_proxy()` (host/core.py) always
returns False. The scheme check still passes only because uvicorn itself
rewrites `scope["scheme"]` from X-Forwarded-Proto (proxy_headers.py L44–51).
Net effect: enforcement rests solely on uvicorn's `forwarded_allow_ips`
(passed at host/app.py:432–434); the app's second layer is dead code. Direct
header forging by a non-loopback client is still rejected (scheme stays http,
both layers deny), so this is robustness/docs drift, not an open door.
Fix (suggestion only): pass the pre-rewrite peer into the check via a uvicorn
middleware, or drop the duplicated layer and document that uvicorn owns it.

**F3 · Low · `/metrics` is unauthenticated unless `HOST_METRICS_ADMIN_TOKEN`
is set.** FACT: host/app.py:248–251 — the auth block is guarded by
`if resolved.metrics_admin_token:`; with the env var unset the endpoint returns
counters/latency histograms to anyone who can reach the port. Content is
operational only (module 14 verifies no secrets in `metrics.render()`), but it
discloses load profile and failure rates.

**F4 · Low · Default bind `0.0.0.0` (L411).** FACT: bandit B104,
`os.getenv("HOST_BIND", "0.0.0.0")`. Correct for the container topology; risky
when run bare on a Windows/office host without a host firewall — combined with
F3 it exposes unauthenticated `/metrics` LAN-wide by default.

**F5 · Info · Counters mix unauthenticated traffic.** FACT: L263 increments
`session_starts_total` before authentication, so brute-force noise inflates the
session-start metric (`auth_failures_total` separately tracks failures).

**F6 · Info · Dead/duplicated config.** FACT: `DEFAULT_MAX_INFLIGHT_GRANTS`
(L88) is defined but the limiter is built from `resolved.max_inflight_grants`
(L182) — the constant is never used.

**F7 · Info · `_token_matches` (L381–393)** uses `hmac.compare_digest` on
`str`; a non-ASCII `HOST_METRICS_ADMIN_TOKEN` raises `TypeError` → 500 on
`/metrics`. Operator-controlled value, cosmetic.

## Correct in this module (verified)

Constant-time compares (L388–393, core L612–615); errors from the grant are
mapped to safe statuses with no upstream body in messages (L351–360);
`Retry-After` honored/issued on 429/503 paths (L279–286, L335–345); auth failure
budget charged only on real failures (L289–291); security headers on the 400
path too (L220–226); `session_id` UUID4 per request, secrets never logged.

## Summary (3 lines)

HTTP surface is tight: constant-time auth, layered limits, mapped upstream
errors, no secret logging. Two real defects: chunked bodies bypass the size
cap (authenticated memory DoS), and the app-level X-Forwarded-Proto peer check
is dead code because uvicorn rewrites `client` before the app sees it. `/metrics`
defaults to unauthenticated.
