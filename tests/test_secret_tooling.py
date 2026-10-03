"""Tests for the credential scan / history-scrub tooling.

These use throwaway repositories under tmp_path with *fake* keys, so the
tests never touch the real history and never need a real credential.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCAN = ROOT / "scripts" / "scan_secrets.py"
SCRUB = ROOT / "scripts" / "scrub_history.py"

FAKE_KEY = "0123456789abcdef0123456789abcdef01234567"  # 40 hex chars, not a real key
LEAK_FILE = "DEEPGRAM_API_KEY=" + FAKE_KEY + "\n"


def _load_scanner():
    """Import scripts/scan_secrets.py by path (it is not a package module).

    dataclasses resolves the module by name, so it must be registered in
    sys.modules before execution.
    """
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("scan_secrets", SCAN)
    assert spec and spec.loader
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def run(script: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=str(cwd) if cwd else str(ROOT),
        capture_output=True,
        text=True,
        timeout=300,
    )


def make_repo(root: Path, leak: bool = True) -> Path:
    """A two-commit repository; the first commit leaks a fake key."""
    repo = root / "repo"
    repo.mkdir()
    env = {
        **__import__("os").environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.test",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.test",
    }
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, env=env, check=True)
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    (repo / ".env.example").write_text(
        "DEEPGRAM_API_KEY=your_deepgram_api_key_here\n", encoding="utf-8"
    )
    if leak:
        (repo / ".env").write_text(LEAK_FILE, encoding="utf-8")
        subprocess.run(["git", "add", "-f", ".env"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, env=env, check=True)
    if leak:
        (repo / ".env").unlink()
        subprocess.run(["git", "rm", "--cached", "-q", ".env"], cwd=repo, env=env, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "remove env"], cwd=repo, env=env, check=True)
    return repo


# -- scanner ---------------------------------------------------------------


def test_scanner_finds_a_leaked_key_in_the_tree(tmp_path):
    repo = make_repo(tmp_path, leak=True)
    # The leak is only in history, so the working tree is already clean...
    result = run(SCAN, "--root", str(repo))
    assert result.returncode == 0, result.stdout

    # ...but a re-added file is found, and the value is never printed.
    (repo / ".env").write_text(LEAK_FILE, encoding="utf-8")
    result = run(SCAN, "--root", str(repo))
    assert result.returncode == 1
    assert "deepgram_key_legacy" in result.stdout
    assert FAKE_KEY not in result.stdout
    assert "redacted" in result.stdout


def test_scanner_finds_the_leak_in_history(tmp_path):
    repo = make_repo(tmp_path, leak=True)
    result = run(SCAN, "--history", "--root", str(repo))
    assert result.returncode == 1
    assert ".env" in result.stdout
    assert FAKE_KEY not in result.stdout


def test_scanner_ignores_placeholders_and_clean_repos(tmp_path):
    repo = make_repo(tmp_path, leak=False)
    assert run(SCAN, "--root", str(repo)).returncode == 0
    assert run(SCAN, "--history", "--root", str(repo)).returncode == 0


def test_redact_never_reveals_the_value():
    module = _load_scanner()
    described = module.redact(FAKE_KEY)
    assert FAKE_KEY not in described
    assert described.startswith("<redacted len=40 sha256=")
    assert module.redact(FAKE_KEY) == described  # stable fingerprint


# -- scrubber --------------------------------------------------------------


def test_scrub_dry_run_changes_nothing(tmp_path):
    repo = make_repo(tmp_path)
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout
    result = run(
        SCRUB, "--root", str(repo), "--path", ".env",
        "--replace", r"DEEPGRAM_API_KEY\s*=\s*\S+",
    )
    assert result.returncode == 0
    assert "DRY RUN" in result.stdout
    after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert before == after


def test_scrub_removes_the_key_from_every_commit_and_keeps_a_backup(tmp_path):
    repo = make_repo(tmp_path)
    backup = tmp_path / "backup.bundle"
    result = run(
        SCRUB, "--root", str(repo), "--yes", "--backup", str(backup),
        "--path", ".env", "--replace", r"DEEPGRAM_API_KEY\s*=\s*\S+",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert FAKE_KEY not in result.stdout
    assert backup.is_file()

    # Nothing reachable contains the key any more...
    assert run(SCAN, "--history", "--root", str(repo)).returncode == 0
    listing = subprocess.run(
        ["git", "log", "--all", "--name-only", "--pretty=%H"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout
    assert "\n.env\n" not in "\n" + listing
    assert ".env.example" in listing, "the template must survive the scrub"

    # ...and the original history is still recoverable from the backup.
    restored = tmp_path / "restored"
    subprocess.run(["git", "clone", "-q", str(backup), str(restored)], check=True)
    original = subprocess.run(
        ["git", "log", "--all", "--oneline", "--", ".env"], cwd=restored,
        capture_output=True, text=True, check=True,
    ).stdout
    assert "removed env" in original or "initial" in original


def test_scrub_requires_an_clean_worktree_or_an_override(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "README.md").write_text("# changed\n", encoding="utf-8")
    args = ("--root", str(repo), "--yes", "--path", ".env")
    blocked = run(SCRUB, *args)
    assert blocked.returncode == 2
    assert "not clean" in blocked.stderr
    allowed = run(SCRUB, *args, "--allow-dirty")
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr


def test_scrub_refuses_without_anything_to_do(tmp_path):
    result = run(SCRUB, "--root", str(tmp_path))
    assert result.returncode == 2
    assert "nothing to do" in result.stderr


@pytest.mark.parametrize("script", [SCAN, SCRUB])
def test_scripts_are_executable_and_documented(script):
    assert script.is_file()
    result = run(script, "--help")
    assert result.returncode == 0
    assert "usage" in result.stdout.lower()


# -- accepted-finding baseline ---------------------------------------------


def test_scanner_reports_the_tracked_incident_without_failing():
    """The real repository is expected to be clean of *new* findings.

    The historical `.env` blob is listed in scripts/secret_scan_baseline.txt
    until the rewritten history is force-pushed, so the scan exits 0 while
    still printing the accepted finding.
    """
    result = run(SCAN, "--history")
    assert result.returncode == 0, result.stdout + result.stderr
    if "known (accepted)" in result.stdout:
        assert "redacted" in result.stdout
        assert FAKE_KEY not in result.stdout


def test_strict_mode_fails_on_the_accepted_finding():
    """`--strict` is how the operator verifies the scrub afterwards."""
    result = run(SCAN, "--history", "--strict")
    # Either the history is clean (post-scrub) or it fails loudly.
    if result.returncode == 0:
        assert "clean" in result.stdout
    else:
        assert "unexpected" in result.stdout


def test_baseline_entry_does_not_cover_a_different_secret(tmp_path):
    """A baseline entry is keyed to the value, so a new leak still fails."""
    repo = make_repo(tmp_path, leak=True)
    other_key = "f" * 40
    (repo / "notes.txt").write_text(
        # Built by concatenation: a literal '<NAME>=<40 chars>' in this
        # repository would itself look like a committed credential to the
        # scanner (a deliberate self-test, see test_this_file_has_no_key_shaped_literal).
        "DEEPGRAM_API_KEY=" + other_key + "\n",
        encoding="utf-8",
    )
    result = run(SCAN, "--root", str(repo), "--baseline", str(ROOT / "scripts" / "secret_scan_baseline.txt"))
    assert result.returncode == 1
    assert "unexpected" in result.stdout
    assert other_key not in result.stdout


def test_this_file_is_not_a_scan_finding_itself():
    """Fixtures must be assembled, not written as key-shaped literals.

    If a fixture looked like a committed credential, the scanner would have
    to be taught to ignore this file -- and an ignore rule for test files is
    exactly how a real credential slips through later. Concatenating the
    parts keeps the fixture shaped like a credential at runtime while the
    source text stays clean.
    """
    module = _load_scanner()
    source = Path(__file__).read_text(encoding="utf-8")
    findings = module.scan_text(source, "tests/test_secret_tooling.py", "tree")
    assert findings == [], [str(finding) for finding in findings]


def test_baseline_file_is_parseable_and_scoped():
    module = _load_scanner()

    baseline_path = ROOT / "scripts" / "secret_scan_baseline.txt"
    assert baseline_path.is_file()
    entries = module.load_baseline(baseline_path)
    assert entries, "the incident entry should be present until the rewrite ships"
    for key, reason in entries.items():
        assert len(key.split()) == 2
        assert reason, "every accepted finding must document why it is accepted"


def test_malformed_baseline_is_an_error(tmp_path):
    bad = tmp_path / "baseline.txt"
    bad.write_text("justoneword\n", encoding="utf-8")
    result = run(SCAN, "--baseline", str(bad))
    assert result.returncode == 2
    assert "malformed" in result.stderr
