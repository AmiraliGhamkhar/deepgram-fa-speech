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

#: Stable, app-specific mutex name in the *local* (per-session) namespace.
#: Deliberately not `Global\...`: creating an object in the global
#: namespace requires SeCreateGlobalPrivilege, which a standard user
#: account does not hold, so `Global\` could fail with ERROR_ACCESS_DENIED
#: and stop the application from starting at all. One instance per session
#: is all this application needs -- each Windows account has its own
#: %APPDATA% and its own microphone.
_WINDOWS_MUTEX_NAME = r"MedicalSTT-SingleInstance"

_ERROR_ALREADY_EXISTS = 183
#: Returned when the mutex NAME exists as a different kernel object (squatted
#: by another process/object type): the API answered and refused, which is
#: a "do not start a second copy" signal, not an availability problem.
_ERROR_ACCESS_DENIED = 5


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
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
            kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL

            handle = kernel32.CreateMutexW(None, True, self._name)
            error = _last_error()
            if not handle:
                if error == _ERROR_ACCESS_DENIED:
                    # The API answered and refused: the name is held by a
                    # different kind of kernel object. Failing open here
                    # was how two instances could both run; refuse instead.
                    log.error(
                        "single-instance mutex name is held by another "
                        "object (Win32 error %d); refusing to start a "
                        "second copy",
                        error,
                    )
                    return False
                raise OSError(f"CreateMutexW failed (Win32 error {error})")
            if error == _ERROR_ALREADY_EXISTS:
                kernel32.CloseHandle(handle)
                return False
        except (OSError, AttributeError) as exc:
            # The mutex prevents a second copy from fighting over the
            # microphone; it is not a security boundary. If the API is
            # unavailable or fails in an unclassified way, the application
            # must still start rather than refuse to run. (Only exception
            # TYPES are logged, as everywhere in this app.)
            log.warning(
                "single-instance mutex unavailable (%s); continuing", type(exc).__name__
            )
            self.acquired = True
            return True
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
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(path, "a+")  # noqa: SIM115 - handle is kept until release
        except OSError as exc:
            # An unwritable lock directory must degrade like an unavailable
            # mutex (fail open, loudly), not crash startup before any UI
            # exists to explain itself. Only a lock that flock REFUSES --
            # another instance really holding it -- returns False below.
            log.warning(
                "single-instance file lock unavailable (%s); continuing",
                type(exc).__name__,
            )
            self.acquired = True
            return True
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
