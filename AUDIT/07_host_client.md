# 07 — Module review: `medical_stt/host_client.py` (token acquisition client)

Read in full: medical_stt/host_client.py L1–217. Cross-read: stdlib
`urllib.request.HTTPRedirectHandler.redirect_request` (sandbox CPython source),
live repro (below), stt/base.py, deepgram_provider.py L189–213 (caller).

## What it holds

urllib-based POST `/v1/session` with `Authorization: Bearer <secret>` (L111)
and optional `X-Client-Id` (L119); `Retry-After` delta-seconds parsing
(L51–66); status→`ErrorCategory` mapping (L148–186); strict session-response
validation (L187–217).

## Findings

**F1 · Medium · Shared secret is forwarded to arbitrary redirect targets —
the module's own stated invariant is not implemented.** FACT:
host_client.py:32–34 states "Refuse to follow redirects: a redirect could
send the shared secret to a host we did not intend to authenticate against",
but the opener at L34 is `build_opener(HTTPHandler(), HTTPSHandler())` —
stdlib `build_opener` installs `HTTPRedirectHandler` by default, and its
`redirect_request` (verified in sandbox CPython source) follows 301/302/303
for POST and builds the redirect request from a copy of `req.headers` —
which includes `Authorization`. Minimal repro (run in this sandbox,
two local HTTP servers): the client POSTs to a server that answers
`302 Location: http://127.0.0.1:<other>/x`; the second server received
`Authorization: Bearer TOP-SECRET-VALUE` and `X-Client-Id: doctor-07`, and
the client accepted the second server's JSON as a valid session
(`fetch_session()` returned "tok"). Trigger conditions: any 301/302/303 on
`/v1/session` — realistic paths are a plain-HTTP `host_url` behind a MITM or
captive portal, a misconfigured reverse proxy, or a compromised/malicious
host. Not exploitable against a correct HTTPS deployment without also
defeating TLS, hence Medium not High. 307 is correctly *not* followed
(stdlib raises HTTPError). Fix (suggestion only): add a handler that raises
on any 3xx, e.g. subclass `HTTPRedirectHandler` overriding `redirect_request`
to `raise HTTPError(...)`; the comment's intent is already written down.

**F2 · Low · Error messages embed `exc.url` (L151, 161–185).** FACT:
`_classify_http_error` interpolates `exc.url` — for a followed redirect this
is the *final* URL, i.e. the attacker-controlled redirect target. It leaks no
secret, but it puts an attacker-chosen URL into the client log/UI. Cosmetic
adjacent to F1.

**F3 · Info · `fetch_session` builds a new `HostSessionClient` per
connection** (deepgram_provider.py:198–203) — stateless by design; the
module-level `_OPENER_FACTORY` (L34) is shared and immutable, so no
cross-session state. Correct.

**F4 · Info · No response size cap on `response.read()`** (L86–88): the host
is trusted infrastructure; a hostile host could stream unbounded bytes into
memory. Sub-finding of the trust model; only relevant once F1 exists.

## Correct in this module (verified)

- 401/403 → AUTH (never retried), 429 → RATE_LIMIT carrying `Retry-After`
  (L157–170), 5xx → SERVER_DISCONNECT with `Retry-After` (L171–180), 400/404/
  405/422 → CONFIG — matches the reconnect policy's expectations (module 08).
- Timeouts: `URLError(socket.timeout)` and raw `socket.timeout` → TIMEOUT
  category (L101–117); explicit `timeout` threaded through `open()`.
- Token handling: `HostSession.access_token` is never logged; parse failures
  raise UNKNOWN with messages that quote nothing from the body (L187–217);
  `session_id` (non-secret correlation id) is extracted and propagated to
  the provider's log line (deepgram_provider.py:207–211).
- `Retry-After` parsing rejects malformed/non-positive values (L51–66),
  falling back to local backoff instead of trusting garbage.

## Summary (3 lines)

Solid status mapping and secret hygiene, one real violation: the "never
follow redirects" invariant is claimed in a comment but not implemented, and
the repro shows the shared secret (and client id) being replayed to a
redirect target, whose response is then trusted. Fix is a five-line
no-redirect opener. Everything else — timeouts, taxonomy, token handling —
is correct.
