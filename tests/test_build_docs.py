"""The Windows build docs and build script cannot drift from CI's gates.

Regression pins for audit N-01/N-03/N-04 (2026-10-17):

* N-01: the documented Windows setup once installed only the client
  requirements, so five host suites (158 tests) silently skipped via
  `pytest.importorskip` -- and the build script's `pytest tests -q` gate
  neither showed skip reasons (no `-rs`) nor failed on them.
* N-03: the docs blessed a PyInstaller fallback via an unversioned,
  git-ignored `MedicalSTT.spec` -- a second, ungated build path.
* N-04: the doc quoted a stale test count and superseded check commands.

Stdlib only, on purpose: this module must run even on a machine whose
setup missed the host dependencies -- that is the exact scenario it guards.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PS_DOC = ROOT / "docs/WINDOWS_POWERSHELL.md"
CI = ROOT / ".github/workflows/ci.yml"
BUILD = ROOT / "scripts/build_windows.ps1"
README = ROOT / "README.md"


def _ci_check_commands() -> list[str]:
    """The lint/type commands CI actually runs (single-line `run:` steps)."""
    commands = []
    for line in CI.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("run:"):
            command = stripped[len("run:"):].strip()
            if command.startswith(("ruff check", "mypy ")):
                commands.append(command)
    return commands


def test_doc_installs_both_requirement_files() -> None:
    """N-01: without host deps, the host suites skip whole modules, exit 0."""
    text = PS_DOC.read_text(encoding="utf-8")
    assert "-r requirements-dev.txt" in text
    assert "-r host/requirements-dev.txt" in text, (
        "docs/WINDOWS_POWERSHELL.md must install the host requirements too: "
        "five suites pytest.importorskip fastapi/httpx/a2wsgi/uvicorn and "
        "skip silently without them (audit N-01)"
    )


def test_doc_check_commands_match_ci() -> None:
    """N-04: the doc's deeper checks must be the same commands CI runs."""
    text = PS_DOC.read_text(encoding="utf-8")
    commands = _ci_check_commands()
    assert commands, "expected CI to run at least one ruff/mypy command"
    for command in commands:
        assert command in text, (
            f"CI runs `{command}` but docs/WINDOWS_POWERSHELL.md no longer "
            "shows it -- sync the doc's check commands with CI (audit N-04)"
        )


def test_doc_quotes_no_hard_test_count() -> None:
    """N-04: a hard-coded suite size goes stale (it once said 478)."""
    text = PS_DOC.read_text(encoding="utf-8")
    assert "478 tests" not in text
    assert re.search(r"\b\d{3,} tests\b", text) is None, (
        "docs/WINDOWS_POWERSHELL.md must not quote a test count: it drifts "
        "with every suite change (audit N-04)"
    )


def test_doc_pytest_shows_skip_reasons() -> None:
    """N-01: plain `-q` prints no skip reasons, so skips stay invisible."""
    text = PS_DOC.read_text(encoding="utf-8")
    pytest_lines = [line for line in text.splitlines() if "pytest tests" in line]
    assert pytest_lines, "expected the doc to show a pytest invocation"
    assert any("-rs" in line for line in pytest_lines), (
        "the doc's pytest invocation must pass -rs so skipped modules are "
        "visible instead of silent (audit N-01)"
    )


def test_build_script_guards_silent_host_skips() -> None:
    """N-01: the build gate must fail when host suites skip on missing deps."""
    text = BUILD.read_text(encoding="utf-8")
    assert "-rs" in text, (
        "scripts/build_windows.ps1 must run pytest with -rs: without it, "
        "skip reasons are never printed and no guard can see them (audit N-01)"
    )
    assert "could not import '(fastapi|httpx|uvicorn|a2wsgi)'" in text, (
        "scripts/build_windows.ps1 must fail the build when a host suite "
        "skips for a missing dependency (audit N-01)"
    )


def test_build_script_guard_stays_windows_safe() -> None:
    """N-01: the guard pattern is narrow on purpose -- keep it narrow.

    A broad `could not import` match (like CI's Linux-only one) would fail
    every Windows build: tests/test_soak.py module-skips on the Unix-only
    stdlib `resource` module there. If soak ever drops that import, the
    build script's pattern may be widened -- not before.
    """
    soak = (ROOT / "tests/test_soak.py").read_text(encoding="utf-8")
    assert 'importorskip("resource")' in soak, (
        "tests/test_soak.py no longer skips on `resource`: re-examine whether "
        "scripts/build_windows.ps1 can use CI's broad skip guard (audit N-01)"
    )
    build = BUILD.read_text(encoding="utf-8")
    assert "could not import '(fastapi|httpx|uvicorn|a2wsgi)'" in build


def test_no_pyinstaller_fallback_path_is_documented() -> None:
    """N-03: Nuitka via scripts/build_windows.ps1 is the only build path.

    The README may still explain *why* Nuitka (a PyInstaller contrast), but
    neither document may bless spec-based fallback commands.
    """
    ps_text = PS_DOC.read_text(encoding="utf-8")
    readme_text = README.read_text(encoding="utf-8")
    assert "pyinstaller" not in ps_text.lower()
    assert "MedicalSTT.spec" not in ps_text
    assert "MedicalSTT.spec" not in readme_text
    assert "PyInstaller MedicalSTT.spec" not in readme_text
