# Security

## Current credential model

The Deepgram API key **exists only in the environment of the self-hosted
service** (`host/`). No client machine, executable, build artifact or
config file contains it.

| Secret | Where it lives | How it is protected |
|--------|----------------|---------------------|
| Deepgram API key | `host/` environment only | Server-side only; never sent to a client. Used solely to mint short-lived session tokens. |
| Host shared secret | The clinician's machine | Windows DPAPI (`CryptProtectData`, user scope) in `%APPDATA%\MedicalSTT\host_secret.dpapi`. |
| Session token | In memory, for one WebSocket handshake | ~30s lifetime, never persisted, never logged. |

Enforcement in the code:

- `medical_stt/config.py` rejects any plaintext secret key in
  `settings.yaml` (a hard `ConfigError`, not a warning).
- `Settings` has no `api_key` field; `Settings.host_secret` uses
  `field(repr=False)` so it cannot leak into a repr or traceback.
- `medical_stt/host_client.py` never includes the shared secret in an
  error message, and classifies `401`/`403` as `AUTH` so it is never
  retried.
- `scripts/build_windows.ps1` scans the tree for Deepgram key patterns
  and fails the build if any are found.
- Host logs record method, path and status only — never a presented
  credential or an issued token.

DPAPI scope is deliberately **user** scope (no
`CRYPTPROTECT_LOCAL_MACHINE`): the blob is decryptable only by the same
Windows account, on the same machine.

## Incident: committed Deepgram API key (ACTION STILL REQUIRED BY THE REPO OWNER)

A real Deepgram API key was committed to this repository in `.env`
(commit `cc42196c3e27bb4892d62aa864f4ee979bcb9ce9`, on `main`). That key
**must be treated as permanently compromised**, regardless of any history
rewrite performed anywhere:

1. **Revoke the exposed key in the Deepgram console immediately.**
   (Dashboard → API Keys → delete/revoke the key that was in `.env`.)
   This is the only step that actually neutralizes the exposure — no git
   history change makes a previously-pushed secret safe to keep using.
2. **Generate a new key** and put it only in the environment of your
   self-hosted service (`host/`), never on a client machine.
3. Assume the old key may already have been scraped by automated bots that
   crawl public GitHub pushes for secrets, even if this repository was
   briefly public/private-then-public, or forked.

This change set does **not** commit a replacement credential anywhere in
the repository, docs, CI config, or git history. It also cannot revoke the
key: revocation happens in the Deepgram console and must be done by the
account owner. Nothing in this repository should be read as evidence that
the exposed key has been revoked.

## What was changed

* `.env` was removed from the working tree.
* `.gitignore` now excludes `.env`, secrets, caches, virtual environments
  and generated files.
* `.env.example` contains placeholders only.
* No API keys, clipboard contents, or full transcript text are logged by
  default (see `README.md` > Logging).
* The repository was scanned for other accidental secrets (API keys,
  tokens, private keys); none were found besides the `.env` file above.

## History rewrite — status and what still needs to happen on GitHub

The local clone's history is scrubbed with this repository's own, tested
tooling (no external dependency, and no secret value is ever printed):

```bash
python scripts/scan_secrets.py --history        # confirm what is reachable
python scripts/scrub_history.py --yes \
    --path .env \
    --replace 'DEEPGRAM_API_KEY\s*=\s*\S+'    # dry run without --yes
python scripts/scan_secrets.py --history        # verify: must be clean
```

`scripts/scrub_history.py` writes a `git bundle` backup *outside* the
repository first, rewrites all refs with `git filter-branch`, deletes
`refs/original`, expires reflogs, prunes unreachable objects and re-scans
the rewritten history, failing loudly if anything is still reachable. It
never pushes: rewriting a shared repository is an operator decision.

What this does and does not achieve:

- **Local clone:** the `.env` blob is no longer reachable from any ref,
  including reflogs and `refs/original`. Verified with
  `scripts/scan_secrets.py --history`.
- **The remote still has it.** `cc42196c…` and the object it introduced are
  still reachable from `origin/main` on GitHub until someone with write
  access force-pushes rewritten history for every affected ref (at minimum
  `main`) and GitHub purges cached views, PR diffs and forks.
- **Restore path:** the backup bundle written before the rewrite
  (`medical-stt-history-backup-<timestamp>.bundle` in the operator's home
  directory) restores the pre-scrub state with
  `git clone <bundle> restored-repo`.
- **CI is intentionally strict.** `.github/workflows/ci.yml` runs
  `scripts/scan_secrets.py --history`; that job fails until the rewrite is
  pushed. A red history scan means "not yet scrubbed on the remote", not a
  code regression.

**Repo owner action required, in this order, regardless of whether this
PR is merged:**

1. Revoke the key in the Deepgram console (do this first, independent of
   any git surgery — it is the only step that removes real risk).
2. Rewrite `main`'s history (the script above, or `git filter-repo`/BFG on
   a machine that has it) and force-push the rewritten `main` to GitHub.
3. Re-clone or hard-reset every other clone and fork. A rewritten history
   does not update existing clones in place, and old objects can persist in
   local reflogs, CI caches, forks, or GitHub's own object/PR cache until
   they expire or are explicitly purged (see GitHub's "removing sensitive
   data" article; consider contacting GitHub Support to purge cached views).

## Fixes made alongside this incident

- **`X-Forwarded-Proto` is no longer trusted from anyone.** The host
  previously accepted the header from any client, so a direct HTTP request
  carrying a forged `X-Forwarded-Proto: https` was treated as secure. It now
  only believes the header when the request comes from a proxy in
  `HOST_FORWARDED_ALLOW_IPS` (default `127.0.0.1`), uses the last hop
  rather than the first, and rejects direct plaintext requests regardless of
  the header. Covered by `tests/test_host_service.py`.
- **Scanning is stricter.** `scripts/scan_secrets.py` covers both the
  current `dg_`-prefixed key form and the legacy 40-hex form that caused
  this incident, plus private keys; placeholder values in `.env.example`
  files are allowed, real-looking values are not. It is used by CI and by
  `scripts/build_windows.ps1` (which previously only looked for `dg_…`).
- `host/README.md` and `README.md` no longer suggest putting a key in a
  client-side file, and `.env.example` files contain placeholders only.

## Reporting

If you discover another secret committed to this repository, do not open a
public issue with the secret value. Revoke/rotate the credential first,
then report the finding.
