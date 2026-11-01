"""Microphone-silence watchdog.

A device that stops delivering frames (unplugged, grabbed by another
application, driver stall) raises no error -- only silence. Deepgram then
closes the stream after ~10 s and the clinician sees nothing but an endless
"reconnecting". This watchdog names the real cause, holds the stream open with
KeepAlive frames while it lasts, and withdraws the warning as soon as frames
arrive again.

It runs on the session thread (never the audio callback) and owns the
KeepAlive worker bookkeeping: at most one such worker exists at a time, and it
is never joined on the Stop path because a synchronous websocket send can wait
forever behind another writer.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from ..audio import BoundedAudioQueue
from .messages import MIC_SILENT_WARNING

log = logging.getLogger("medical_stt.app")

#: Seconds without a single captured frame before the microphone is reported
#: silent. Deepgram closes a stream that carries no audio (1011
#: NET-0001/NET-0002) after ~10 s, so this is comfortably below that.
DEFAULT_SILENCE_SECONDS = 3.0

#: KeepAlive period while silent. A KeepAlive every 3-5 s resets Deepgram's
#: idle timer, which is why both thresholds are 3 s.
DEFAULT_KEEP_ALIVE_INTERVAL_SECONDS = 3.0


@dataclass(frozen=True)
class WatchdogPolicy:
    """Thresholds for the silence watchdog."""

    silence_seconds: float = DEFAULT_SILENCE_SECONDS
    keep_alive_interval_seconds: float = DEFAULT_KEEP_ALIVE_INTERVAL_SECONDS

    @classmethod
    def from_defaults(cls) -> "WatchdogPolicy":
        """The module's configured defaults, read at construction time."""
        return cls(DEFAULT_SILENCE_SECONDS, DEFAULT_KEEP_ALIVE_INTERVAL_SECONDS)


class AudioWatchdog:
    """Detects a silent microphone and schedules KeepAlive frames."""

    def __init__(
        self,
        queue: BoundedAudioQueue,
        policy: WatchdogPolicy,
        *,
        warn: Callable[[str], None],
        clear_warning: Callable[[str], None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._queue = queue
        self._policy = policy
        self._warn = warn
        self._clear_warning = clear_warning
        self._clock = clock
        self._captured_seen = 0
        self._last_capture_at = 0.0
        self._last_keep_alive_at = 0.0
        self._silent = False
        self._keep_alive_worker: Optional[threading.Thread] = None
        self.reset()

    # -- observability (tests / health snapshots) --------------------------

    @property
    def silent(self) -> bool:
        return self._silent

    @property
    def last_capture_at(self) -> float:
        return self._last_capture_at

    @last_capture_at.setter
    def last_capture_at(self, at: float) -> None:
        self._last_capture_at = at

    @property
    def last_keep_alive_at(self) -> float:
        return self._last_keep_alive_at

    @last_keep_alive_at.setter
    def last_keep_alive_at(self, at: float) -> None:
        self._last_keep_alive_at = at

    # -- lifecycle ---------------------------------------------------------

    def reset(self) -> None:
        """Start a new session watch, withdrawing any earlier verdict."""
        stats = self._queue.stats()
        self._captured_seen = stats.put_total + stats.dropped_total
        self._last_capture_at = self._clock()
        self._last_keep_alive_at = 0.0
        self._silent = False
        self._clear_warning(MIC_SILENT_WARNING)

    def check(self, keep_alive: Callable[[], None]) -> None:
        """One watchdog tick; call from the session loop.

        `keep_alive` sends one KeepAlive frame on the *current* provider; it is
        only invoked once per interval and only while the microphone is silent.
        """
        stats = self._queue.stats()
        captured = stats.put_total + stats.dropped_total  # a dropped frame was still captured
        now = self._clock()
        if captured != self._captured_seen:
            self._captured_seen = captured
            self._last_capture_at = now
            if self._silent:
                self._silent = False
                log.info("microphone audio resumed")
                self._clear_warning(MIC_SILENT_WARNING)
            return
        if now - self._last_capture_at < self._policy.silence_seconds:
            return
        if not self._silent:
            self._silent = True
            log.warning(
                "no audio captured for %.0fs; keeping the stream alive",
                self._policy.silence_seconds,
            )
            self._warn(MIC_SILENT_WARNING)
        if now - self._last_keep_alive_at < self._policy.keep_alive_interval_seconds:
            return
        self._last_keep_alive_at = now
        # Keep at most one such worker across reconnects; never join it on the
        # session/Stop path. The closure owns the session's provider.
        if self._keep_alive_worker is not None and self._keep_alive_worker.is_alive():
            return

        def send_keep_alive() -> None:
            try:
                keep_alive()
            except Exception as exc:  # noqa: BLE001 - the receiver reports transport failure
                # No PHI and no message text: only the exception class.
                log.debug("keep-alive not sent (%s)", type(exc).__name__)

        self._keep_alive_worker = threading.Thread(
            target=send_keep_alive, daemon=True, name="keep-alive-sender"
        )
        self._keep_alive_worker.start()
