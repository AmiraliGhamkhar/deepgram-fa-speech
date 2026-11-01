"""Bounded, in-memory holding area for text that could not be injected.

Clinical text that failed a stage is kept so the clinician can copy it out of
the recovery dialog instead of re-dictating it. It is deliberately:

* **bounded** -- at most `MAX_ENTRIES` utterances of `MAX_CHARS` each, so a
  long session cannot grow the process without limit;
* **memory-only** -- never logged, never written to disk, never sent to the
  host;
* **explicit** -- a full buffer is *reported* (`truncated`), because silently
  dropping the oldest fragment is exactly the failure a safety review would
  want to see.
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Deque, List

MAX_ENTRIES = 16
MAX_CHARS = 8000


class RecoveryBuffer:
    """Thread-safe bounded list of transcripts that were not injected."""

    def __init__(self, max_entries: int = MAX_ENTRIES, max_chars: int = MAX_CHARS) -> None:
        self._max_entries = max(1, max_entries)
        self._max_chars = max(1, max_chars)
        self._entries: Deque[str] = deque(maxlen=self._max_entries)
        self._lock = threading.Lock()
        self._truncated = False

    @property
    def truncated(self) -> bool:
        """True once text had to be dropped because the buffer was full."""
        with self._lock:
            return self._truncated

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def entries(self) -> List[str]:
        with self._lock:
            return list(self._entries)

    def retain(self, text: str) -> None:
        """Keep `text` for recovery; never raises and never blocks long."""
        with self._lock:
            if len(self._entries) >= self._max_entries or len(text) > self._max_chars:
                self._truncated = True
            if len(self._entries) < self._max_entries:
                self._entries.append(text[: self._max_chars])

    def text(self) -> str:
        """The retained utterances, oldest first, one per line."""
        with self._lock:
            return "\n".join(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._truncated = False
