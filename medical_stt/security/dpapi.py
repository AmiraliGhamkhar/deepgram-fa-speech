"""Windows DPAPI (CryptProtectData / CryptUnprotectData) via ctypes.

Only the *host shared secret* is ever protected here. The Deepgram API key
never reaches this machine -- it lives exclusively on the self-hosted
service (see `host/`).

Scope is deliberately the default **user** scope: the blob can only be
decrypted by the same Windows user account, on the same machine, that
encrypted it. `CRYPTPROTECT_LOCAL_MACHINE` is intentionally NOT used --
that flag would make the secret readable by every account on the machine.

`crypt32.dll` is never touched at import time, so this module stays
importable (and testable) on Linux/macOS; the real calls are made lazily
by `DPAPIProtector.protect/unprotect`.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Any

# CRYPTPROTECT_UI_FORBIDDEN: never show UI. Required for a background /
# windowless process, and it guarantees the call cannot block on a prompt.
_CRYPTPROTECT_UI_FORBIDDEN = 0x1

#: Application-specific optional entropy. Binding the blob to this value
#: means a blob produced by another application cannot be decrypted here
#: even by code that runs as the same user.
APP_ENTROPY = b"MedicalSTT/host-secret/v1"

#: Human-readable description embedded in the protected blob.
APP_DESCRIPTION = "Medical STT host shared secret"


class DPAPIError(RuntimeError):
    """A DPAPI protect/unprotect call failed.

    The message never contains plaintext, ciphertext, or entropy.
    """


class DPAPINotAvailable(DPAPIError):
    """DPAPI was requested on a platform that does not provide it."""


def is_available() -> bool:
    """True when the Windows DPAPI can be used on this machine."""
    return sys.platform == "win32"


def _last_error() -> int:
    """`GetLastError()` as seen by the last ctypes call (0 off Windows)."""
    return int(getattr(ctypes, "get_last_error", lambda: 0)())


class _DATA_BLOB(ctypes.Structure):
    """Win32 `DATA_BLOB`. Field order/sizes are fixed by the ABI."""

    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _libraries() -> "tuple[Any, Any]":
    """Load crypt32/kernel32 with explicit argtypes/restypes.

    ctypes defaults every restype to a 32-bit `int`; on 64-bit Windows
    that silently truncates the `HLOCAL` returned into `pbData`, so the
    copy back out of DPAPI would read from a bogus address (the same
    class of bug documented in injection/backend.py).
    """
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]

    blob_pointer = ctypes.POINTER(_DATA_BLOB)
    crypt32.CryptProtectData.argtypes = [
        blob_pointer,
        wintypes.LPCWSTR,
        blob_pointer,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        blob_pointer,
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL

    crypt32.CryptUnprotectData.argtypes = [
        blob_pointer,
        ctypes.POINTER(wintypes.LPWSTR),
        blob_pointer,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        blob_pointer,
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL

    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def _make_blob(data: bytes) -> "_BlobHolder":
    return _BlobHolder(data)


class _BlobHolder:
    """Keeps the input buffer alive for as long as DPAPI holds a pointer."""

    def __init__(self, data: bytes) -> None:
        self._buffer = ctypes.create_string_buffer(data, len(data))
        self.blob = _DATA_BLOB(
            len(data), ctypes.cast(self._buffer, ctypes.POINTER(ctypes.c_ubyte))
        )

    @property
    def pointer(self) -> Any:
        return ctypes.byref(self.blob)


def _copy_out(out_blob: _DATA_BLOB, kernel32: Any) -> bytes:
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        # pDataOut is caller-allocated and must be released with LocalFree.
        if out_blob.pbData:
            kernel32.LocalFree(out_blob.pbData)


def protect(data: bytes, entropy: bytes = APP_ENTROPY) -> bytes:
    """Encrypt `data` with the current user's DPAPI master key."""
    if not is_available():
        raise DPAPINotAvailable(
            "Windows DPAPI is only available on Windows. "
            "Set MEDICALSTT_HOST_SECRET for development on this platform."
        )
    crypt32, kernel32 = _libraries()

    data_in = _make_blob(data)
    entropy_in = _make_blob(entropy)
    out_blob = _DATA_BLOB()

    ok = crypt32.CryptProtectData(
        data_in.pointer,
        APP_DESCRIPTION,
        entropy_in.pointer,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise DPAPIError(
            f"CryptProtectData failed (Win32 error {_last_error()})"
        )
    return _copy_out(out_blob, kernel32)


def unprotect(blob: bytes, entropy: bytes = APP_ENTROPY) -> bytes:
    """Decrypt a blob produced by `protect`.

    Raises `DPAPIError` when the blob was produced by a different user or
    machine, was tampered with (DPAPI adds a MAC), or is simply not a
    protected blob.
    """
    if not is_available():
        raise DPAPINotAvailable(
            "Windows DPAPI is only available on Windows. "
            "Set MEDICALSTT_HOST_SECRET for development on this platform."
        )
    crypt32, kernel32 = _libraries()

    data_in = _make_blob(blob)
    entropy_in = _make_blob(entropy)
    out_blob = _DATA_BLOB()

    ok = crypt32.CryptUnprotectData(
        data_in.pointer,
        None,
        entropy_in.pointer,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise DPAPIError(
            f"CryptUnprotectData failed (Win32 error {_last_error()}). "
            "The stored secret cannot be read by this Windows user or machine; "
            "re-enter the shared secret in the app."
        )
    return _copy_out(out_blob, kernel32)


class DPAPIProtector:
    """Object wrapper so the secret store can be tested with a fake."""

    def protect(self, data: bytes) -> bytes:
        return protect(data)

    def unprotect(self, blob: bytes) -> bytes:
        return unprotect(blob)
