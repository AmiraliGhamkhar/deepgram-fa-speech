#!/usr/bin/env python3
"""Scan the repository (working tree and/or full history) for credentials.

Written for the incident in SECURITY.md: a real Deepgram API key was once
committed to `.env`. This script is the verification step -- run it before
and after any history rewrite.

It never prints a secret value. Findings are reported as a path/commit plus
a length and a short hash of the match, which is enough to confirm that two
findings are the same credential without disclosing it.

Usage:
    python scripts/scan_secrets.py                 # working tree
    python scripts/scan_secrets.py --history       # every blob in every ref
    python scripts/scan_secrets.py --history --quiet

Exit codes: 0 = clean, 1 = findings, 2 = usage/environment error.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent

#: Directories that never need scanning (not tracked, or generated).
_SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "build", "dist", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "node_modules",
}


@dataclass(frozen=True)
class Pattern:
    name: str
    regex: re.Pattern


PATTERNS: Tuple[Pattern, ...] = (
    # Deepgram keys: current `dg_...` form and the legacy 32/40-hex form
    # when it appears next to a Deepgram/API-key name.
    Pattern("deepgram_key", re.compile(r"\bdg_[A-Za-z0-9]{16,}\b")),
    Pattern(
        "deepgram_key_legacy",
        re.compile(
            r"(?i)deepgram[_-]?api[_-]?key\s*[=:\"']?\s*[A-Za-z0-9]{32,64}"
        ),
    ),
    Pattern("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)

#: Values that are obviously documentation, not credentials.
PLACEHOLDER_RE = re.compile(
    r"(?i)^(your[_-].*|.*[_-]here|<.*>|\$\{.*\}|xxx+|\*+|-+|example.*|placeholder.*|not[-_]real.*|fake.*)$"
)

#: Files whose contents are documented templates: an empty or placeholder
#: value is fine, a real-looking one is not.
TEMPLATE_SUFFIXES = (".example", ".sample", ".template")


#: Findings listed here (as `pattern fingerprint`) are *known and tracked*.
#: They are still reported, but do not fail the scan -- see the module
#: docstring. The entry must be removed once the history is rewritten.
DEFAULT_BASELINE = ROOT / "scripts" / "secret_scan_baseline.txt"


def fingerprint(value: str) -> str:
    """Stable 8-hex fingerprint of a matched value (never reversible here)."""
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:8]


@dataclass(frozen=True)
class Finding:
    location: str
    path: str
    pattern: str
    redacted: str
    fingerprint: str

    @property
    def key(self) -> str:
        """Identity of the *secret*, not of the location it was found in."""
        return f"{self.pattern} {self.fingerprint}"

    def __str__(self) -> str:
        return f"{self.location} {self.path}: {self.pattern} {self.redacted}"


def redact(value: str) -> str:
    """Describe a match without revealing it."""
    return f"<redacted len={len(value)} sha256={fingerprint(value)}>"


def load_baseline(path: Path) -> Dict[str, str]:
    """Parse `pattern fingerprint  # reason` lines. Missing file = empty."""
    if not path.is_file():
        return {}
    entries: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        parts = stripped.split()
        if len(parts) < 2:
            # A malformed baseline is a configuration error (exit 2), never a
            # silent pass: an unparsed entry could otherwise hide a finding.
            print(f"error: malformed baseline line in {path}: {line!r}", file=sys.stderr)
            raise SystemExit(2)
        reason = line.split("#", 1)[1].strip() if "#" in line else ""
        entries[f"{parts[0]} {parts[1]}"] = reason
    return entries


def is_placeholder(path: str, value: str) -> bool:
    stripped = value.strip().strip("\"'")
    if not stripped:
        return True
    if PLACEHOLDER_RE.match(stripped):
        return True
    return path.endswith(TEMPLATE_SUFFIXES) and len(stripped) < 24


def _assignment_values(line: str) -> List[str]:
    """Values on the right-hand side of `NAME=value` / `NAME: value`."""
    match = re.match(r"\s*[\"']?[A-Za-z_][A-Za-z0-9_ .-]*[\"']?\s*[=:]\s*(.+)$", line)
    return [match.group(1).strip()] if match else []


def scan_text(text: str, path: str, location: str) -> List[Finding]:
    findings: List[Finding] = []
    for pattern in PATTERNS:
        for match in pattern.regex.finditer(text):
            value = match.group(0)
            if pattern.name == "deepgram_key_legacy":
                candidate = re.split(r"[=:\"'\s]+", value)[-1]
                if is_placeholder(path, candidate):
                    continue
            if is_placeholder(path, value.split("=")[-1]):
                continue
            findings.append(
                Finding(location, path, pattern.name, redact(value), fingerprint(value))
            )
    return findings


def scan_file(path: Path, relative: str) -> List[Finding]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return scan_text(text, relative, "tree")


def iter_tree_files(root: Path = ROOT) -> Iterable[Tuple[Path, str]]:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        parts = set(path.relative_to(root).parts)
        if parts & _SKIP_DIRS:
            continue
        yield path, str(path.relative_to(root))


def scan_tree(root: Path = ROOT) -> List[Finding]:
    findings: List[Finding] = []
    for path, relative in iter_tree_files(root):
        findings.extend(scan_file(path, relative))
    return findings


def _git(*args: str, root: Path = ROOT) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def scan_history(root: Path = ROOT) -> List[Finding]:
    """Scan every blob reachable from every ref, including deleted files."""
    try:
        commits = _git("rev-list", "--all", root=root).split()
    except RuntimeError as exc:
        raise SystemExit(f"error: not a git repository? {exc}") from exc

    checked: set[str] = set()
    findings: List[Finding] = []
    for commit in commits:
        listing = _git("ls-tree", "-r", commit, root=root)
        for line in listing.splitlines():
            if "\t" not in line:
                continue
            meta, path = line.split("\t", 1)
            parts = meta.split()
            if len(parts) < 3:
                continue
            blob = parts[2]
            if blob in checked:
                continue
            checked.add(blob)
            content = subprocess.run(
                ["git", "cat-file", "-p", blob],
                cwd=str(root),
                capture_output=True,
                text=True,
                errors="replace",
            ).stdout
            findings.extend(scan_text(content, path, f"history[{commit[:8]}]"))
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true", help="scan all reachable git objects")
    parser.add_argument("--root", default=str(ROOT), help="repository root to scan")
    parser.add_argument("--quiet", action="store_true", help="only print the summary")
    parser.add_argument(
        "--baseline", default=str(DEFAULT_BASELINE),
        help="file of accepted `pattern fingerprint` entries (see SECURITY.md)",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="ignore the baseline: fail on every finding, including accepted ones",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2

    findings = scan_history(root) if args.history else scan_tree(root)
    scope = "history" if args.history else "working tree"

    # The baseline path is resolved next to *this* checkout's script, so a
    # scan of a foreign --root still uses the repository's own baseline.
    baseline = {} if args.strict else load_baseline(Path(args.baseline))
    accepted = [f for f in findings if f.key in baseline]
    unexpected = [f for f in findings if f.key not in baseline]

    if not findings:
        print(f"clean: no credentials found in the {scope}")
        if baseline:
            print(
                f"note: {len(baseline)} baseline entry/entries matched nothing -- the history "
                "looks scrubbed. Remove them from "
                f"{Path(args.baseline).name} so the check stays meaningful."
            )
        return 0

    if accepted:
        print(f"known (accepted) credential finding(s) in the {scope}:")
        if not args.quiet:
            for finding in accepted:
                reason = baseline.get(finding.key) or "no reason recorded"
                print(f"  {finding}  [{reason}]")

    if unexpected:
        print(f"FOUND {len(unexpected)} unexpected credential finding(s) in the {scope}:")
        if not args.quiet:
            for finding in unexpected:
                print(f"  {finding}")
        print(
            "\nDo not copy these values anywhere. Revoke/rotate the credential first,\n"
            "then scrub the history: python scripts/scrub_history.py --help\n"
            "(An accepted finding must be listed, with a reason, in "
            f"{Path(args.baseline).name} -- see SECURITY.md.)"
        )
        return 1

    print(
        f"no NEW findings in the {scope}: {len(accepted)} accepted finding(s) remain tracked.\n"
        "These stay until the rewritten history is force-pushed; SECURITY.md lists the steps."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
