#!/usr/bin/env python3
"""Scrub committed credentials from git history (local, verified, reversible).

This is the remediation tool for the incident documented in SECURITY.md: a
real Deepgram API key was committed to `.env`. Revoking the key in the
Deepgram console is step one and is *not* something this script can do.
This script removes the value from the repository's history so it is no
longer reachable from any branch.

What it does, in order:

1. Refuses to run on a dirty working tree (unless `--allow-dirty`).
2. Writes a safety backup of every ref to `git bundle` *outside* the
   repository, and prints the exact restore command.
3. Rewrites all refs with `git filter-branch --tree-filter`, dropping the
   configured paths and replacing the configured patterns with
   `***REMOVED***` in every other file.
4. Expires reflogs and prunes unreachable objects, so the old blobs are
   gone from this clone.
5. Re-scans the rewritten history and fails if anything is still found.

It never prints a matched value, and it never pushes: force-pushing the
rewritten history to the remote (and asking GitHub Support to purge cached
views/forks) is an explicit, separate operator action.

Usage:
    python scripts/scrub_history.py --yes \
        --path .env \
        --replace 'dg_[A-Za-z0-9]{16,}' \
        --replace 'DEEPGRAM_API_KEY\\s*=\\s*\\S+'

    # dry run: show what would be rewritten, change nothing
    python scripts/scrub_history.py --path .env --replace 'dg_[0-9a-f]{40}'
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Sequence

ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()
CONFIG_ENV_VAR = "MEDICAL_STT_SCRUB_CONFIG"
REPLACEMENT = "***REMOVED***"

#: File extensions we are willing to rewrite content in. Binary blobs are
#: never edited (the paths filter is what removes those).
TEXT_SUFFIXES = {
    "", ".env", ".py", ".yaml", ".yml", ".json", ".txt", ".md", ".cfg",
    ".ini", ".toml", ".sh", ".ps1", ".example", ".sql", ".html", ".js",
}


def git(*args: str, root: Path = ROOT, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)
    if check and result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout


def apply_to_tree(config_path: Path) -> int:
    """Filter-branch tree-filter worker: runs inside a checked-out tree."""
    import re

    config: Dict[str, List[str]] = json.loads(Path(config_path).read_text(encoding="utf-8"))
    drop = [Path(p) for p in config.get("paths", [])]
    patterns = [re.compile(p) for p in config.get("patterns", [])]

    for relative in drop:
        target = Path.cwd() / relative
        if target.is_dir():
            for child in sorted(target.rglob("*"), reverse=True):
                if child.is_file():
                    child.unlink()
            target.rmdir()
        elif target.is_file():
            target.unlink()

    if patterns:
        for path in sorted(Path.cwd().rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            if path.suffix not in TEXT_SUFFIXES and path.name not in (".env",):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            rewritten = text
            for pattern in patterns:
                rewritten = pattern.sub(REPLACEMENT, rewritten)
            if rewritten != text:
                path.write_text(rewritten, encoding="utf-8")
    return 0


def make_backup(root: Path, backup_path: Path) -> None:
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    if backup_path.exists():
        raise SystemExit(f"refusing to overwrite existing backup {backup_path}")
    git("bundle", "create", str(backup_path), "--all", root=root)


def rewrite(root: Path, paths: Sequence[str], patterns: Sequence[str], keep_backup: bool) -> None:
    config_path = root / ".git" / "medical-stt-scrub-config.json"
    config_path.write_text(
        json.dumps({"paths": list(paths), "patterns": list(patterns)}), encoding="utf-8"
    )
    env = dict(os.environ)
    env[CONFIG_ENV_VAR] = str(config_path)
    tree_filter = f'{sys.executable} "{SELF}" --apply-tree'

    print("Rewriting all refs (git filter-branch)...")
    result = subprocess.run(
        [
            "git", "filter-branch", "--force", "--prune-empty",
            "--tree-filter", tree_filter,
            "--tag-name-filter", "cat", "--", "--all",
        ],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
    )
    config_path.unlink(missing_ok=True)
    if result.returncode != 0:
        print(result.stdout[-4000:])
        print(result.stderr[-4000:], file=sys.stderr)
        raise SystemExit("history rewrite failed; nothing was pushed")

    print("Deleting filter-branch backups, expiring reflogs and pruning objects...")
    # filter-branch keeps its own backups under refs/original -- they keep
    # the old blobs alive, so they must be removed for the scrub to be real.
    for refname in git(
        "for-each-ref", "--format=%(refname)", "refs/original/", root=root
    ).splitlines():
        git("update-ref", "-d", refname.strip(), root=root)
    git("reflog", "expire", "--expire=now", "--all", root=root)
    git("gc", "--prune=now", "--quiet", root=root)


def scan(root: Path, patterns: Sequence[str]) -> List[str]:
    """Return redacted findings for `patterns` across all reachable blobs."""
    import re

    compiled = [re.compile(p) for p in patterns]
    findings: List[str] = []
    commits = git("rev-list", "--all", root=root).split()
    seen: set[str] = set()
    for commit in commits:
        for line in git("ls-tree", "-r", commit, root=root).splitlines():
            if "\t" not in line:
                continue
            meta, path = line.split("\t", 1)
            parts = meta.split()
            if len(parts) < 3 or parts[2] in seen:
                continue
            seen.add(parts[2])
            content = subprocess.run(
                ["git", "cat-file", "-p", parts[2]], cwd=str(root),
                capture_output=True, text=True, errors="replace",
            ).stdout
            for pattern in compiled:
                if pattern.search(content):
                    findings.append(f"{commit[:8]} {path}: {pattern.pattern}")
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--path", action="append", default=[], help="path to drop from every commit")
    parser.add_argument(
        "--replace", action="append", default=[],
        help="regex to replace with ***REMOVED*** in every text blob",
    )
    parser.add_argument("--yes", action="store_true", help="actually rewrite (otherwise dry run)")
    parser.add_argument("--allow-dirty", action="store_true", help="skip the clean-tree check")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--backup", default="", help="backup bundle path (default: outside the repo)")
    parser.add_argument("--apply-tree", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.apply_tree:  # invoked by filter-branch itself
        config = os.environ.get(CONFIG_ENV_VAR)
        if not config:
            print(f"{CONFIG_ENV_VAR} is not set", file=sys.stderr)
            return 2
        return apply_to_tree(Path(config))

    if not args.path and not args.replace:
        print("error: nothing to do; pass --path and/or --replace", file=sys.stderr)
        return 2

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2

    # Untracked files are irrelevant to history and are often exactly the
    # operator's local notes, so only staged/modified tracked files block.
    dirty = git("status", "--porcelain", "--untracked-files=no", root=root).strip()
    if dirty and not args.allow_dirty:
        print("error: working tree is not clean; commit or stash first (--allow-dirty to override)", file=sys.stderr)
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = Path(args.backup) if args.backup else Path.home() / f"medical-stt-history-backup-{stamp}.bundle"

    print("This will rewrite ALL refs (branches and tags) in:")
    print(f"  {root}")
    for path in args.path:
        print(f"  drop path      : {path}")
    for pattern in args.replace:
        print(f"  replace pattern: {pattern}")
    print(f"  backup bundle  : {backup}")

    if not args.yes:
        print("\nDRY RUN: nothing was changed. Re-run with --yes to perform the rewrite.")
        return 0

    make_backup(root, backup)
    print(f"\nBackup written: {backup}")
    print(f"Restore with : git clone {backup} restored-repo")

    # filter-branch refuses to run with modified tracked files. Stashing
    # around it (rather than only warning) keeps --allow-dirty honest.
    stashed = False
    if dirty:
        print("Stashing local modifications for the rewrite (restored afterwards)...")
        git("stash", "push", "--quiet", "--message", "medical-stt-scrub", root=root)
        stashed = True

    try:
        rewrite(root, args.path, args.replace, keep_backup=True)
    finally:
        if stashed:
            pop = subprocess.run(
                ["git", "stash", "pop", "--quiet"], cwd=str(root),
                capture_output=True, text=True,
            )
            if pop.returncode != 0:
                print(
                    "WARNING: could not re-apply the stashed changes automatically "
                    f"(`git stash pop` failed: {pop.stderr.strip()}). Your edits are "
                    "safe in `git stash list`.",
                    file=sys.stderr,
                )
            else:
                print("Local modifications restored.")

    findings = scan(root, args.replace)
    if findings:
        print("\nFAILED: patterns are still reachable after the rewrite:")
        for finding in findings:
            print(f"  {finding}")
        print(f"\nThe original history is intact in {backup}.")
        return 1

    print("\nRewritten history is clean for the supplied patterns.")
    print("Local rewrite complete. It is NOT yet effective on the remote:")
    print("  1. Revoke the exposed credential at the provider (if not already done).")
    print("     Removing it from history is not a substitute for revocation.")
    print("  2. Force-push each rewritten ref. Remote-tracking refs were rewritten")
    print("     too, so plain `--force-with-lease` will refuse; use either")
    print("       git push --force origin <branch>")
    print("     or `git fetch origin` first and then --force-with-lease.")
    print("  3. Ask every collaborator to re-clone, and GitHub Support to purge cached views.")
    print(f"  4. Keep the backup bundle until the pushed history is verified: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
