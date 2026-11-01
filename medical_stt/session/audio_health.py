"""Capture-health reporting that is safe to use from the audio callback.

`sounddevice` calls back on its own real-time thread. That thread must not
log, must not take a lock that a slower thread holds, and must never let an
exception escape -- so anything worth reporting is *recorded here as a flag*
and turned into a log line / user warning by the session thread.

The split matters for clinical safety: a dropped chunk is lost audio, and the
warning for it is deliberately sticky (recovering the queue cannot recover the
missing words). A silent microphone, by contrast, is a condition that can
clear, so its warning is withdrawn when frames arrive again -- that part lives
in `watchdog.AudioWatchdog`.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from ..audio import BoundedAudioQueue
from .messages import DATA_LOSS_WARNING

log = logging.getLogger("medical_stt.app")


class AudioHealth:
    """Callback-side flags plus the worker-side reporting of them."""

    def __init__(self, queue: BoundedAudioQueue, warn: Callable[[str], None]) -> None:
        self._queue = queue
        self._warn = warn
        #: Set by the real-time callback, drained by `flush()` on the sender
        #: or session thread (the callback must never log).
        self.input_status_pending = threading.Event()
        self.audio_drop_pending = threading.Event()
        self._pending_input_status = ""

    # -- capture callback (must stay O(1), lock-free and exception-free) ---

    def note_status(self, status: object) -> None:
        """Record a device status string reported by the audio callback."""
        if status:
            # Device overflow flags can repeat hundreds of times per second,
            # so only the latest string is kept.
            self._pending_input_status = str(status)
            self.input_status_pending.set()

    def note_drop(self) -> None:
        """Record that a captured chunk could not be enqueued."""
        self.audio_drop_pending.set()

    # -- session/sender thread --------------------------------------------

    def flush(self) -> None:
        """Log whatever the callback recorded since the last call.

        Called from the session loop, independently of queue emptiness or a
        blocked send, so a stalled transport cannot hide a capture fault.

        The lost-audio warning is deliberately sticky: queue recovery cannot
        recover the missing words, so the clinician must still review the note.
        Silence/capacity warnings, unlike data loss, are withdrawn on recovery
        (see `watchdog.AudioWatchdog`).
        """
        if self.input_status_pending.is_set():
            self.input_status_pending.clear()
            status, self._pending_input_status = self._pending_input_status, ""
            if status:
                log.warning("microphone status: %s", status)
        if not self.audio_drop_pending.is_set():
            return
        self._warn(DATA_LOSS_WARNING)
        self.audio_drop_pending.clear()
        stats = self._queue.stats()
        # Persisting overflow is reported at ERROR and flips the session
        # health flag; an isolated drop stays a warning. Silently absorbing a
        # sustained loss of audio is the failure mode this prevents.
        level = logging.ERROR if stats.degraded else logging.WARNING
        log.log(
            level,
            "audio queue full: dropped_total=%d depth=%d consecutive=%d degraded=%s",
            stats.dropped_total, stats.depth, stats.consecutive_drops, stats.degraded,
        )

    def pending_status(self) -> Optional[str]:
        """The last unread device status (tests and diagnostics)."""
        return self._pending_input_status or None
