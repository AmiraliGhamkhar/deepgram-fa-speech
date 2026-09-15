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


@dataclass(frozen=True)
class QueueStats:
    depth: int
    max_depth: int
    dropped_total: int
    put_total: int
    get_total: int
    last_drop_at: float


class BoundedAudioQueue:
    """A `queue.Queue[bytes]` wrapper that tracks depth and drop statistics
    without adding latency to the producer (audio callback) path."""

    def __init__(self, maxsize: int = 40) -> None:
        self._q: "queue.Queue[bytes]" = queue.Queue(maxsize=maxsize)
        self._lock = threading.Lock()
        self._dropped_total = 0
        self._put_total = 0
        self._get_total = 0
        self._max_depth_seen = 0
        self._last_drop_at = 0.0

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
                self._last_drop_at = time.monotonic()
            return False
        with self._lock:
            self._put_total += 1
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
                put_total=self._put_total,
                get_total=self._get_total,
                last_drop_at=self._last_drop_at,
            )

    def qsize(self) -> int:
        return self._q.qsize()
