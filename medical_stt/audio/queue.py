"""Bounded audio queue with drop accounting.

Real-time reliability rule: the audio capture callback must stay lightweight
and non-blocking. `put_nowait` never blocks and never raises into the
caller; on overflow it increments a counter and returns False so the caller
can decide whether/how to log (still without blocking).
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass

#: Consecutive dropped chunks after which the session is reported as
#: degraded. Sized so an isolated glitch (a GC pause, one momentarily full
#: queue) does not raise an alarm, but a genuinely stuck sender -- the real
#: cause of sustained overflow -- trips it almost immediately.
DEGRADED_AFTER_DROPS = 3

#: Consecutive successful sends required to end a degraded streak. A single
#: good frame between drops does not mean the sender caught up: flapping
#: audio would otherwise read as healthy while chunks are still being lost.
RECOVERED_AFTER_SENDS = 3


@dataclass(frozen=True)
class QueueStats:
    depth: int
    max_depth: int
    dropped_total: int
    drained_total: int
    put_total: int
    get_total: int
    last_drop_at: float
    #: Consecutive drops with no successful send in between. This is the
    #: signal that distinguishes a one-off hiccup from a sustained network
    #: problem: a permanently non-zero value means the sender cannot keep up
    #: and audio is being lost continuously, not sporadically.
    consecutive_drops: int = 0
    #: True while the queue is dropping audio -- the session health flag.
    degraded: bool = False


class BoundedAudioQueue:
    """A `queue.Queue[bytes]` wrapper that tracks depth and drop statistics
    without adding latency to the producer (audio callback) path."""

    def __init__(self, maxsize: int = 40) -> None:
        self._q: "queue.Queue[bytes]" = queue.Queue(maxsize=maxsize)
        self._lock = threading.Lock()
        self._dropped_total = 0
        self._drained_total = 0
        self._put_total = 0
        self._get_total = 0
        self._max_depth_seen = 0
        self._last_drop_at = 0.0
        self._consecutive_drops = 0
        self._consecutive_sends = 0

    def put_nowait(self, chunk: bytes) -> bool:
        """Non-blocking enqueue. Returns True if enqueued, False if the
        queue was full and the chunk was dropped. Never raises for a full
        queue (a real-time audio callback must not have to handle
        exceptions from this call)."""
        try:
            self._q.put_nowait(chunk)
        except queue.Full:
            with self._lock:
                self._dropped_total += 1
                self._consecutive_drops += 1
                self._consecutive_sends = 0
                self._last_drop_at = time.monotonic()
            return False
        with self._lock:
            self._put_total += 1
            # A successful enqueue means the sender had made room: count it
            # toward recovery, and only clear the degraded streak after a
            # sustained run of them (see RECOVERED_AFTER_SENDS).
            self._consecutive_sends += 1
            if self._consecutive_sends >= RECOVERED_AFTER_SENDS:
                self._consecutive_drops = 0
            depth = self._q.qsize()
            if depth > self._max_depth_seen:
                self._max_depth_seen = depth
        return True

    def get(self, timeout: float = 0.1) -> bytes:
        chunk = self._q.get(timeout=timeout)
        with self._lock:
            self._get_total += 1
        return chunk

    def task_done(self) -> None:
        self._q.task_done()

    def stats(self) -> QueueStats:
        with self._lock:
            return QueueStats(
                depth=self._q.qsize(),
                max_depth=self._max_depth_seen,
                dropped_total=self._dropped_total,
                drained_total=self._drained_total,
                put_total=self._put_total,
                get_total=self._get_total,
                last_drop_at=self._last_drop_at,
                consecutive_drops=self._consecutive_drops,
                degraded=self._consecutive_drops >= DEGRADED_AFTER_DROPS,
            )

    def drain(self) -> int:
        """Discard every buffered chunk and return how many were dropped.

        Called when a session stops. Audio captured before the stop but
        not yet sent would otherwise be streamed into the *next* session,
        producing a burst of stale transcript at its start.
        """
        drained = 0
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
            drained += 1
            self._q.task_done()
        with self._lock:
            if drained:
                self._drained_total += drained
            # A session reset is a clean slate even when the sender consumed
            # the last chunk before drain() ran. The next session must not
            # inherit the previous one's degraded streak.
            self._consecutive_drops = 0
            self._consecutive_sends = 0
        return drained

    def qsize(self) -> int:
        return self._q.qsize()
