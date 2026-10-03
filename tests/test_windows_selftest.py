"""The Windows-only self-test must be honest about where it can run.

DPAPI, the named mutex and SendInput do not exist here, so what can be
tested on Linux/macOS is that the script (a) does not claim success off
Windows and (b) registers the three platform checks.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "windows_selftest.py"


def run_script() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=120
    )


def test_script_exists():
    assert SCRIPT.is_file()


def test_off_windows_it_reports_nothing_verified():
    if sys.platform == "win32":  # pragma: no cover - Windows only
        return
    result = run_script()
    assert result.returncode == 2, "a skipped platform check must not exit 0"
    combined = result.stdout + result.stderr
    assert "Not running on Windows" in combined
    assert "cannot be verified here" in combined


def test_registers_the_windows_only_checks():
    import importlib.util

    spec = importlib.util.spec_from_file_location("windows_selftest", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)

    names = [name for name, _ in module.CHECKS]
    assert any("DPAPI" in name for name in names)
    assert any("mutex" in name for name in names)
    assert any("SendInput" in name for name in names)
    assert module.ROUNDTRIP_VALUE and b"medical-stt" in module.ROUNDTRIP_VALUE
