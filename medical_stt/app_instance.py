"""Single-instance enforcement.

Two copies of the application would fight over the microphone and could
interleave injected text, so the second instance exits immediately.

On Windows this is a named mutex (`CreateMutexW`): it is released
automatically by the kernel if the process is killed, which a lock file
cannot guarantee. Elsewhere (development/CI only) an advisory file lock is
used.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("medical_stt.instance")

#: Must be stable and app-specific; the mutex is machine-wide per session.
_WINDOWS_MUTEX_NAME = r"Global\MedicalSTT-SingleInstance"

_ERROR_ALREADY_EXISTS = 183


def _last_error() -> int:
    """`GetLastError()` as seen by the last ctypes call (0 off Windows)."""
    import ctypes

    return int(getattr(ctypes, "get_last_error", lambda: 0)())


class SingleInstance:
    """Context manager. `acquired` is False if another instance holds it."""

    def __init__(self, name: str = _WINDOWS_MUTEX_NAME, lock_path: Optional[Path] = None) -> None:
        self._name = name
        self._lock_path = lock_path
        self._handle: Optional[int] = None
        self._file: Any = None
        self.acquired = False

    def acquire(self) -> bool:
        if sys.platform == "win32":
            return self._acquire_windows()
        return self._acquire_file()

    def _acquire_windows(self) -> bool:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateMutexW(None, True, self._name)
        if not handle:
            raise OSError(
                f"CreateMutexW failed (Win32 error {_last_error()})"
            )
        if _last_error() == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self._handle = int(handle)
        self.acquired = True
        return True

    def _acquire_file(self) -> bool:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - Windows-only fallback path
            self.acquired = True
            return True
        from . import paths

        path = self._lock_path or paths.lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+")  # noqa: SIM115 - handle is kept until release
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._file = handle
        self.acquired = True
        return True

    def release(self) -> None:
        if self._handle is not None:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle(self._handle)
            self._handle = None
        if self._file is not None:
            try:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):  # pragma: no cover - best effort
                pass
            self._file.close()
            self._file = None
        self.acquired = False

    def __enter__(self) -> "SingleInstance":
        self.acquired = self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()
