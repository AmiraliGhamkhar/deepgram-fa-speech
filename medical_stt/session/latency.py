"""Per-utterance latency accounting.

VAD receipt is not acoustic onset; injection acceptance is not target
consumption. Nothing here ever sees transcript content -- only durations and
stage names -- so a latency line can be logged or shipped without PHI.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("medical_stt.app")


@dataclass(frozen=True)
class StageTimings:
    """How long each local stage of one utterance took."""

    terminology_s: float = 0.0
    bidi_s: float = 0.0
    injection_s: float = 0.0


class LatencyTracker:
    """Per-utterance VAD/result-receipt and local-stage timing.

    One tracker belongs to exactly one utterance: a final transcript is
    processed on a worker thread, so a single shared tracker would attach one
    utterance's marks to another. `mark_utterance_start` is therefore the only
    entry point that (re)starts a measurement, and `mark_final` consumes it.
    """

    def __init__(self) -> None:
        self._capture_start: Optional[float] = None
        self._first_interim: Optional[float] = None
        self._speech_final: Optional[float] = None

    def mark_utterance_start(self, at: Optional[float] = None) -> None:
        if self._capture_start is None:
            self._capture_start = at if at is not None else time.monotonic()
            self._first_interim = None
            self._speech_final = None

    def mark_first_interim(self, at: Optional[float] = None) -> None:
        if self._capture_start is not None and self._first_interim is None:
            self._first_interim = at if at is not None else time.monotonic()
            log.debug("latency: first interim after %.3fs", self._first_interim - self._capture_start)

    def mark_speech_final(self, at: Optional[float] = None) -> None:
        if self._capture_start is not None:
            self._speech_final = at if at is not None else time.monotonic()

    def mark_final(
        self,
        timings: Optional[StageTimings] = None,
        *,
        terminology_s: Optional[float] = None,
        bidi_s: Optional[float] = None,
        injection_s: Optional[float] = None,
    ) -> None:
        """Log the whole utterance and reset the tracker.

        Accepts either a `StageTimings` value or the three keyword floats;
        the keyword form is kept because callers (and tests) use it directly.
        """
        resolved = timings or StageTimings(
            terminology_s=terminology_s or 0.0,
            bidi_s=bidi_s or 0.0,
            injection_s=injection_s or 0.0,
        )
        if self._capture_start is None:
            return
        now = time.monotonic()
        speech_final_s = (self._speech_final or now) - self._capture_start
        first_interim_s = None if self._first_interim is None else self._first_interim - self._capture_start
        log.info(
            "latency: total=%.3fs vad_to_first_interim=%s vad_to_final_receipt=%.3fs "
            "terminology=%.3fs bidi=%.3fs injection_acceptance=%.3fs",
            now - self._capture_start,
            "n/a" if first_interim_s is None else f"{first_interim_s:.3f}s",
            speech_final_s,
            resolved.terminology_s,
            resolved.bidi_s,
            resolved.injection_s,
        )
        self._capture_start = None
