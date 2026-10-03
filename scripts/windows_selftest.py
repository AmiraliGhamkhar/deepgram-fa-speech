#!/usr/bin/env python3
"""Windows-only readiness checks (run on Windows, before packaging).

DPAPI, the single-instance mutex and the native SendInput injection backend
cannot be exercised on Linux CI, so this script is the check that runs on a
Windows machine:

    python scripts/windows_selftest.py

It imports each Windows-only component and proves it actually works:
`CryptProtectData`/`CryptUnprotectData` round-trip a throwaway string, the
named mutex is acquired and released, and the SendInput backend loads.

Exit codes: 0 = all checks passed, 1 = a check failed, 2 = not running on
Windows (nothing was verified — this must never be reported as success).

No secret is used or printed: the DPAPI round-trip value is a fixed,
non-credential test string.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, List, Tuple

#: Make `medical_stt` importable regardless of the working directory the
#: script is invoked from (CI runs it from the repo root, a build script
#: from anywhere). Without this the checks fail with ModuleNotFoundError
#: and look like platform failures.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: Not a credential; only proves the DPAPI call sequence works.
ROUNDTRIP_VALUE = b"medical-stt-windows-selftest"

CHECKS: List[Tuple[str, Callable[[], None]]] = []


def check(name: str) -> Callable[[Callable[[], None]], Callable[[], None]]:
    def register(func: Callable[[], None]) -> Callable[[], None]:
        CHECKS.append((name, func))
        return func

    return register


@check("DPAPI protect/unprotect round-trip")
def _dpapi_roundtrip() -> None:
    from medical_stt.security.dpapi import DPAPIProtector

    protector = DPAPIProtector()
    blob = protector.protect(ROUNDTRIP_VALUE)
    assert blob and blob != ROUNDTRIP_VALUE, "protect() did not transform the input"
    assert protector.unprotect(blob) == ROUNDTRIP_VALUE, "unprotect() did not restore the input"


@check("single-instance named mutex")
def _named_mutex() -> None:
    from medical_stt.app_instance import SingleInstance

    first = SingleInstance()
    second = SingleInstance()
    assert first.acquire() is True, "could not acquire the mutex"
    try:
        assert second.acquire() is False, "a second instance was allowed"
    finally:
        first.release()
    assert first.acquired is False, "the mutex was not released"


@check("Win32 SendInput injection backend loads")
def _injection_backend() -> None:
    from medical_stt.injection._windows_backend import WindowsBackend

    backend = WindowsBackend()
    assert hasattr(backend, "send_unicode_text")
    assert hasattr(backend, "paste_text")


def main() -> int:
    if sys.platform != "win32":
        print(
            "Not running on Windows: DPAPI, the named mutex and SendInput "
            "cannot be verified here.\n"
            "Run this script on the machine that builds the Windows binaries "
            "(scripts/build_windows.ps1 does it automatically).",
            file=sys.stderr,
        )
        return 2

    failures = 0
    for name, func in CHECKS:
        try:
            func()
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            failures += 1
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS  {name}")

    if failures:
        print(f"\n{failures} Windows check(s) failed.", file=sys.stderr)
        return 1
    print("\nAll Windows self-checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
