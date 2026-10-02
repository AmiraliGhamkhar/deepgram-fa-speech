"""Persistent, DPAPI-protected storage for the host shared secret.

The only secret this application ever stores locally is the shared secret
used to authenticate against the self-hosted service. It is written to
`%APPDATA%\\MedicalSTT\\host_secret.dpapi` as a DPAPI-protected blob, which
only the same Windows user on the same machine can decrypt.

Design rules kept deliberately simple:
* No secret is ever written in plaintext, to a log, or to an environment
  variable inside the executable.
* Writes are atomic (temp file + replace) so a crash mid-save cannot leave
  a truncated blob that would needlessly invalidate the secret.
* The Deepgram API key is never handled here -- it only exists on the host.
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Optional, Protocol

from .dpapi import DPAPIError, DPAPIProtector

log = logging.getLogger("medical_stt.security.store")

#: Magic prefix so a plaintext file or an unrelated blob is detected early
#: with a clear message instead of a confusing DPAPI error.
_MAGIC = b"MSTTDP1"


class Protector(Protocol):
    """Minimal encryption interface (DPAPI in production, fake in tests)."""

    def protect(self, data: bytes) -> bytes: ...

    def unprotect(self, blob: bytes) -> bytes: ...


class SecretStoreError(RuntimeError):
    """Raised for unrecoverable local-secret storage problems."""


class SecretStore:
    """Read/write one named secret through a `Protector`."""

    def __init__(self, path: Path, protector: Optional[Protector] = None) -> None:
        self._path = Path(path)
        self._protector: Protector = protector or DPAPIProtector()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def exists(self) -> bool:
        return self._path.is_file()

    def save(self, secret: str) -> None:
        """Persist `secret`, replacing any previous value atomically."""
        if not secret:
            raise SecretStoreError("refusing to store an empty secret")
        try:
            blob = self._protector.protect(secret.encode("utf-8"))
        except DPAPIError as exc:
            raise SecretStoreError(str(exc)) from exc

        payload = _MAGIC + blob
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=".host_secret-", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self._path)
        except BaseException:
            # Never leave a partial secret file behind.
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        self._restrict_permissions()
        log.info("host shared secret stored (DPAPI-protected) at %s", self._path)

    def load(self) -> Optional[str]:
        """Return the stored secret, or None if nothing is stored yet."""
        if not self.exists:
            return None
        try:
            payload = self._path.read_bytes()
        except OSError as exc:
            raise SecretStoreError(f"cannot read stored secret: {exc}") from exc

        if not payload.startswith(_MAGIC):
            raise SecretStoreError(
                f"{self._path} is not a Medical STT secret file; remove it and "
                "re-enter the shared secret."
            )
        try:
            plaintext = self._protector.unprotect(payload[len(_MAGIC):])
        except DPAPIError as exc:
            raise SecretStoreError(str(exc)) from exc
        return plaintext.decode("utf-8")

    def clear(self) -> bool:
        """Delete the stored secret. Returns True if a file was removed."""
        try:
            self._path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise SecretStoreError(f"cannot delete stored secret: {exc}") from exc
        log.info("host shared secret removed from %s", self._path)
        return True

    def _restrict_permissions(self) -> None:
        """Best effort: owner-only access.

        On Windows the file inherits the per-user ACL of %APPDATA%, which is
        already user-only, and `chmod` has no useful effect -- so failures
        here are expected and ignored. On POSIX (development only) it does
        restrict the file.
        """
        try:
            os.chmod(self._path, 0o600)
        except OSError:  # pragma: no cover - platform dependent
            pass
