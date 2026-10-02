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
the repository, docs, CI config, or git history.

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

`git filter-repo` was used to scrub the key out of the commit history of
this working copy (the local `main`/PR-source history no longer contains
the plaintext key in any blob). **This local/PR-branch rewrite does not,
by itself, remove the key from GitHub's `main` branch.** The commit
`cc42196c3e27bb4892d62aa864f4ee979bcb9ce9` with the plaintext key is still
reachable from `origin/main` on GitHub as of this writing, because:

* This PR is opened from a feature branch and cannot force-push over
  `main`.
* A `git filter-repo`/BFG-style history rewrite only takes effect on
  GitHub once someone with write access **force-pushes the rewritten
  history to every affected ref** (at minimum `main`), and then GitHub's
  cached views, PR diffs, forks, and any CI artifact caches are cleared.

**Repo owner action required, in this order, regardless of whether this
PR is merged:**

1. Revoke the key in the Deepgram console (do this first, independent of
   any git surgery — it is the only step that removes real risk).
2. Separately rewrite `main`'s history (e.g. with `git filter-repo` or
   BFG) to strip the secret and force-push the rewritten `main` to
   GitHub.
3. Re-clone or hard-reset any other local clones/forks to the rewritten
   history; a rewritten history does not update existing clones in place,
   and git objects containing the old key can persist in local reflogs,
   CI caches, forks, or GitHub's own object/PR cache until they expire or
   are explicitly purged (see GitHub's "removing sensitive data" support
   article and consider contacting GitHub Support to purge cached views).

Until steps 1–3 above are performed by someone with push access to
`main`, the plaintext key remains visible in `main`'s history on GitHub
regardless of anything in this pull request.

## Reporting

If you discover another secret committed to this repository, do not open a
public issue with the secret value. Revoke/rotate the credential first,
then report the finding.
