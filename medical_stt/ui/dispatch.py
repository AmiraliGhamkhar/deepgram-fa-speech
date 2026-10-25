"""Bounded latest-value mailboxes; worker threads never call Tcl/Tk."""
from __future__ import annotations

import threading
import logging
from collections import OrderedDict
from typing import Any, Callable


class Mailbox:
    def __init__(self, capacity: int = 64) -> None:
        self._pending: OrderedDict[str, Callable[[], None]] = OrderedDict()
        self._lock = threading.Lock()
        self._capacity = max(1, capacity)

    def post(self, key: str, fn: Callable[[], None]) -> None:
        with self._lock:
            if key not in self._pending and len(self._pending) >= self._capacity:
                # Cosmetic updates must never terminate a network callback.
                self._pending.popitem(last=False)
            self._pending[key] = fn

    def drain(self) -> None:
        with self._lock:
            pending, self._pending = self._pending, OrderedDict()
        for fn in pending.values():
            try:
                fn()
            except Exception as exc:
                # A failed render must not disable the pump (and lose Stop).
                # No exception message: it might include transcript contents.
                logging.getLogger("medical_stt.ui").error("UI update failed (%s)", type(exc).__name__)


# The control window owns the Tk interpreter in the desktop application.
# Standalone overlays (diagnostics) can still own a separate interpreter.
owner_root: Any = None
owner_mailbox: Mailbox | None = None


def install_owner(root: Any, mailbox: Mailbox) -> None:
    global owner_root, owner_mailbox
    owner_root, owner_mailbox = root, mailbox


def clear_owner() -> None:
    global owner_root, owner_mailbox
    owner_root = owner_mailbox = None
