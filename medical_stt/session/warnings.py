"""The single user-visible warning line.

Every warning the session raises goes through one object so that three
properties hold without each call site re-implementing them:

* only one message is shown at a time, and it is the *most recent* one;
* a condition can be withdrawn (`clear`) without clobbering a different
  warning raised in the meantime -- the withdrawal only applies when the
  current message is the one being withdrawn;
* a UI failure can never propagate: publishing is best effort.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from .messages import RECOVERY_FULL_SUFFIX

log = logging.getLogger("medical_stt.app")

Publisher = Callable[[str], None]


class WarningBoard:
    """Holds the current warning and pushes it to the UI."""

    def __init__(self, publish: Optional[Publisher] = None) -> None:
        self._message = ""
        self._publish = publish

    @property
    def message(self) -> str:
        return self._message

    def set(self, message: str, *, recovery_truncated: bool = False) -> None:
        """Show `message` (with the recovery-full suffix when it applies)."""
        if recovery_truncated and not message.endswith(RECOVERY_FULL_SUFFIX):
            message += RECOVERY_FULL_SUFFIX
        self._message = message
        self._notify(message)

    def clear(self, message: str) -> None:
        """Withdraw `message` once its cause is gone.

        A different warning raised in the meantime is left untouched.
        """
        if not self._message.startswith(message):
            return
        self._message = ""
        self._notify("")

    def clear_all(self) -> None:
        self._message = ""
        self._notify("")

    def _notify(self, message: str) -> None:
        if self._publish is None:
            return
        try:
            self._publish(message)
        except Exception as exc:  # noqa: BLE001 - a UI fault must not end dictation
            log.error("UI warning failed (%s)", type(exc).__name__)
