"""Dependency-lock invariants (issue #22 / audit N-07).

The `.txt` files are the edited inputs; `requirements/*.lock` are their
compiled, hash-pinned resolutions and are what CI and the host Docker
image install. These tests make sure that stays true without re-running
the resolver: locks exist, every pin carries hashes, every direct
requirement is pinned, the dev lock cannot drift from the runtime locks,
and the locks are still universal (a recompile on one platform must not
produce a lock another platform cannot install).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLIENT_LOCK = ROOT / "requirements" / "client.lock"
HOST_LOCK = ROOT / "requirements" / "host.lock"
DEV_LOCK = ROOT / "requirements" / "dev.lock"

_PIN_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([^ \\;]+)(.*)$")
_REQ_RE = re.compile(r"^([A-Za-z0-9_.-]+)")


def _logical_lines(text: str) -> list[str]:
    lines, buf = [], ""
    for raw in text.splitlines():
        if raw.rstrip().endswith("\\"):
            buf += raw.rstrip()[:-1] + " "
            continue
        lines.append(buf + raw)
        buf = ""
    return lines


def _pins(path: Path) -> dict[str, set[str]]:
    pins: dict[str, set[str]] = {}
    for line in _logical_lines(path.read_text(encoding="utf-8")):
        match = _PIN_RE.match(line.strip())
        if match:
            marker = match.group(3).strip()
            marker = marker.split("--hash=")[0].strip()
            pins.setdefault(match.group(1).lower().replace("_", "-"), set()).add(
                match.group(2) + marker
            )
    return pins


def _direct_requirements(path: Path) -> set[str]:
    names = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "-")):
            continue
        match = _REQ_RE.match(line)
        if match:
            names.add(match.group(1).lower().replace("_", "-"))
    return names


def test_every_lock_pin_carries_hashes():
    for lock in (CLIENT_LOCK, HOST_LOCK, DEV_LOCK):
        assert lock.is_file(), f"missing {lock.relative_to(ROOT)}"
        for line in _logical_lines(lock.read_text(encoding="utf-8")):
            if _PIN_RE.match(line.strip()):
                assert "--hash=sha256:" in line, f"unhashed pin in {lock.name}: {line.strip()[:80]}"


def test_locks_cover_every_direct_requirement():
    pairs = [
        (ROOT / "requirements.txt", CLIENT_LOCK),
        (ROOT / "host" / "requirements.txt", HOST_LOCK),
    ]
    for source, lock in pairs:
        missing = _direct_requirements(source) - set(_pins(lock))
        assert not missing, f"{source.name} not fully pinned in {lock.name}: {sorted(missing)}"
    dev_inputs = _direct_requirements(ROOT / "requirements-dev.txt") | _direct_requirements(
        ROOT / "host" / "requirements-dev.txt"
    )
    missing = dev_inputs - set(_pins(DEV_LOCK))
    assert not missing, f"dev inputs not fully pinned in dev.lock: {sorted(missing)}"


def test_dev_lock_cannot_drift_from_the_runtime_locks():
    dev = _pins(DEV_LOCK)
    for lock in (CLIENT_LOCK, HOST_LOCK):
        for package, versions in _pins(lock).items():
            assert dev.get(package) == versions, (
                f"{package} resolves differently in dev.lock than in {lock.name}: "
                "CI would test versions that do not ship (regenerate with "
                "scripts/update_locks.sh)"
            )


def test_locks_are_universal():
    """A lock compiled for one platform/Python only would break the other
    CI jobs at install time; the marker forks are the proof of universality."""
    client = CLIENT_LOCK.read_text(encoding="utf-8")
    assert "python_full_version" in client, "no per-Python fork: was --universal used?"
    assert "sys_platform" in client, "no per-platform marker: was --universal used?"
    # The one exact pin in the inputs must survive compilation unchanged.
    assert _pins(CLIENT_LOCK)["deepgram-sdk"] == {"7.9.0"}


def test_ci_and_docker_install_the_locks_with_hashes():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "--require-hashes -r requirements/dev.lock" in ci
    assert "pip install -r requirements-dev.txt" not in ci, (
        "CI must install the hashed lock, not the range inputs"
    )
    assert "-r requirements/client.lock -r requirements/host.lock" in ci, (
        "the dependency-audit job must audit what actually gets installed"
    )

    dockerfile = (ROOT / "host" / "Dockerfile").read_text(encoding="utf-8")
    assert "--require-hashes -r requirements/host.lock" in dockerfile

    script = (ROOT / "scripts" / "update_locks.sh").read_text(encoding="utf-8")
    for name in ("client.lock", "host.lock", "dev.lock", "--universal", "--generate-hashes"):
        assert name in script, f"update_locks.sh must regenerate ({name})"
