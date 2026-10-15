"""Static guard for the release dependency manifest writer."""
from pathlib import Path


def test_release_manifest_writer_is_bomless_on_powershell_51() -> None:
    # Static source pin, NOT a behavior test: PowerShell/Win32 cannot run here.
    script = (Path(__file__).resolve().parents[1] / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
    writers = [line for line in script.splitlines() if '"dependency-versions.json"' in line]
    assert len(writers) == 1
    writer = writers[0]
    assert "Set-Content -Encoding UTF8" not in writer
    assert "[IO.File]::WriteAllText" in writer
    assert "[Text.UTF8Encoding]::new($false)" in writer
    assert '"$json`n"' in writer  # pip's one-line JSON ends with LF, as in CI
    assert '$json = (& $Python -m pip list --format=json) -join "`n"' in script
