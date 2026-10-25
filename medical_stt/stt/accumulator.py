"""Small accumulator for Deepgram-style finalized transcript segments.

Duplicate detection is deliberately conservative: identical *text* is not
proof of an identical event. Saying "بله" twice, or dictating the same
measurement on two lines, is legitimate repetition that must survive.
A segment is only dropped when the provider demonstrably re-sent the same
event -- either the exact same event object, or an identical text whose
word timings are the same acoustic span. Without that metadata both
segments are kept.
"""
from __future__ import annotations

import time

from typing import List, Optional, Tuple

from .base import TranscriptEvent, WordInfo

#: Two segments with the same text count as the same acoustic span only if
#: their boundaries agree this closely (seconds).
_SPAN_TOLERANCE_SECONDS = 0.001


def _same_word_span(first: Tuple[WordInfo, ...], second: Tuple[WordInfo, ...]) -> bool:
    """True if two word lists describe the same span of audio."""
    if not first or not second:
        return False
    if len(first) == len(second):
        return all(
            abs(left.start - right.start) <= _SPAN_TOLERANCE_SECONDS
            and abs(left.end - right.end) <= _SPAN_TOLERANCE_SECONDS
            for left, right in zip(first, second, strict=True)
        )
    # Equal outer boundaries alone cannot prove identical word-level audio.
    return False


class UtteranceLimitError(ValueError):
    """The provider did not endpoint within the bounded utterance budget.

    `accumulated_text` is the complete utterance at the instant the budget
    tripped: every retained finalized segment joined with the segment that
    exceeded it. Callers must retain this in one recovery entry; the
    accumulator has already been reset and cannot reproduce the text.
    """

    def __init__(self, message: str, accumulated_text: str = "") -> None:
        super().__init__(message)
        self.accumulated_text = accumulated_text


class UtteranceAccumulator:
    """Collect finalized segments until the provider marks speech complete."""

    def __init__(self, max_chars: int = 8000, max_segments: int = 128, max_seconds: float = 60.0) -> None:
        self.max_chars = max_chars
        self.max_segments = max_segments
        self.max_seconds = max_seconds
        self._chars = 0
        self._started = 0.0
        self._segments: List[TranscriptEvent] = []
        #: Source events parallel to _segments, so a re-sent event can be
        #: recognized even when another segment arrived in between.
        self._source_events: List[TranscriptEvent] = []

    def add(self, event: TranscriptEvent) -> Optional[TranscriptEvent]:
        if not event.is_final:
            return None

        text = event.text.strip()
        # Providers may repeat a finalized segment around an endpoint event.
        if text and not self._is_duplicate(event, text):
            now = time.monotonic()
            if (self._chars + len(text) > self.max_chars or len(self._segments) >= self.max_segments
                    or (self._segments and now - self._started > self.max_seconds)):
                # Do not lose the already-finalized segments. The app retains
                # this complete text once, and the reset prevents a later
                # flush() from adding those segments a second time.
                full_text = " ".join(part for part in (self.pending_text(), text) if part)
                self.reset()
                raise UtteranceLimitError(
                    "Dictation exceeded the utterance budget; stop and review",
                    accumulated_text=full_text,
                )
            if not self._segments:
                self._started = now
            self._chars += len(text)
            self._segments.append(
                TranscriptEvent(
                    text=text,
                    is_final=True,
                    speech_final=False,
                    confidence=event.confidence,
                    words=event.words,
                    received_at=event.received_at,
                )
            )
            self._source_events.append(event)

        if not event.speech_final:
            return None
        return self.flush()

    def pending_text(self) -> str:
        """All currently retained finalized segments, in arrival order."""
        return " ".join(segment.text for segment in self._segments)

    def flush(self) -> Optional[TranscriptEvent]:
        if not self._segments:
            return None

        segments, self._segments = self._segments, []
        self._source_events = []
        self._chars = 0
        self._started = 0.0
        words = tuple(word for segment in segments for word in segment.words)
        confidences = [segment.confidence for segment in segments if segment.confidence is not None]
        confidence = sum(confidences) / len(confidences) if confidences else None
        return TranscriptEvent(
            text=" ".join(segment.text for segment in segments),
            is_final=True,
            speech_final=True,
            confidence=confidence,
            words=words,
            received_at=segments[-1].received_at,
        )

    def reset(self) -> None:
        self._segments.clear()
        self._source_events.clear()
        self._chars = 0
        self._started = 0.0

    def _is_duplicate(self, event: TranscriptEvent, text: str) -> bool:
        """True only when this is provably a re-send of a retained segment.

        Checking just the last segment let a provider re-sending segment N
        after N+1 slip through. All retained segments are compared, but the
        same conservative evidence is required: identical word timings, or
        the exact same event object when timing metadata is unavailable.
        Legitimate repetition (different timings or plain text) survives.
        """
        for previous, previous_source in zip(
            self._segments, self._source_events, strict=True
        ):
            if text != previous.text:
                continue
            if event.words and previous.words:
                if _same_word_span(event.words, previous.words):
                    return True
            elif event is previous_source:
                return True
        return False
