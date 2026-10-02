"""Local secret handling for the desktop application.

Only one secret is ever stored locally: the shared secret used to
authenticate to the self-hosted service. It is protected with Windows
DPAPI (user scope) via `dpapi.py` and persisted by `secret_store.py`.

The Deepgram API key is **never** present on the client: it lives only in
the host's environment.
"""
from .dpapi import DPAPIError, DPAPINotAvailable, DPAPIProtector, is_available
from .secret_store import Protector, SecretStore, SecretStoreError

__all__ = [
    "DPAPIError",
    "DPAPINotAvailable",
    "DPAPIProtector",
    "Protector",
    "SecretStore",
    "SecretStoreError",
    "is_available",
]
