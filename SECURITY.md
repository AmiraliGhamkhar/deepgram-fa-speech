# Security

## Incident: committed Deepgram API key (resolved in this change set)

A real Deepgram API key was committed to this repository in `.env`
(commit `cc42196c3e27bb4892d62aa864f4ee979bcb9ce9`). That key **must be
treated as permanently compromised**, regardless of any history rewrite
performed on this repo:

1. **Revoke the exposed key in the Deepgram console immediately.**
   (Dashboard → API Keys → delete/revoke the key that was in `.env`.)
2. **Generate a new key** and put it only in a local, untracked `.env`
   file (see `.env.example`).
3. Assume the old key may already have been scraped by automated bots that
   crawl public GitHub pushes for secrets, even if this repository was
   briefly public/private-then-public, or forked.

This change set does **not** commit a replacement credential anywhere in
the repository, docs, CI config, or git history.

## What was changed

* `.env` was removed from the working tree and from git history.
* `.gitignore` now excludes `.env`, secrets, caches, virtual environments
  and generated files.
* `.env.example` contains placeholders only.
* No API keys, clipboard contents, or full transcript text are logged by
  default (see `README.md` > Logging).
* The repository was scanned for other accidental secrets (API keys,
  tokens, private keys); none were found besides the `.env` file above.

## History rewrite

The exposed key was removed from all commits reachable from this branch
using `git filter-repo` (an index/history rewrite tool), which rewrites
every commit that touched `.env` so the secret no longer appears in any
blob reachable from the branch history. If you have any other local clones
or forks of this repository, they must be re-cloned from the rewritten
history; a rewritten history does not update existing clones in place, and
git objects containing the old key can persist in local reflogs, CI caches,
or unrewritten forks until they are garbage-collected or removed.

## Reporting

If you discover another secret committed to this repository, do not open a
public issue with the secret value. Revoke/rotate the credential first,
then report the finding.
