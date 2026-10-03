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
    # Different word counts (e.g. punctuation-only differences): compare
    # the acoustic boundaries only.
    return (
        abs(first[0].start - second[0].start) <= _SPAN_TOLERANCE_SECONDS
        and abs(first[-1].end - second[-1].end) <= _SPAN_TOLERANCE_SECONDS
    )


class UtteranceAccumulator:
    """Collect finalized segments until the provider marks speech complete."""

    def __init__(self) -> None:
        self._segments: List[TranscriptEvent] = []
        self._last_source_event: Optional[TranscriptEvent] = None

    def add(self, event: TranscriptEvent) -> Optional[TranscriptEvent]:
        if not event.is_final:
            return None

        text = event.text.strip()
        # Providers may repeat a finalized segment around an endpoint event.
        if text and not self._is_duplicate(event, text):
            self._segments.append(
                TranscriptEvent(
                    text=text,
                    is_final=True,
                    speech_final=False,
                    confidence=event.confidence,
                    words=event.words,
                )
            )
            self._last_source_event = event

        if not event.speech_final:
            return None
        return self.flush()

    def flush(self) -> Optional[TranscriptEvent]:
        if not self._segments:
            return None

        segments, self._segments = self._segments, []
        self._last_source_event = None
        words = tuple(word for segment in segments for word in segment.words)
        confidences = [segment.confidence for segment in segments if segment.confidence is not None]
        confidence = sum(confidences) / len(confidences) if confidences else None
        return TranscriptEvent(
            text=" ".join(segment.text for segment in segments),
            is_final=True,
            speech_final=True,
            confidence=confidence,
            words=words,
        )

    def reset(self) -> None:
        self._segments.clear()
        self._last_source_event = None

    def _is_duplicate(self, event: TranscriptEvent, text: str) -> bool:
        """True only when this is provably the same segment as the last one.

        * Identical text with overlapping word timings -> the provider
          re-emitted the same finalized segment (dropped).
        * Identical text with different timings -> a genuinely repeated
          utterance, e.g. "بله" said twice (kept).
        * No word timings at all -> only the exact same event object is
          provably a repeat; identical text is kept.
        """
        if not self._segments:
            return False
        previous = self._segments[-1]
        if text != previous.text:
            return False
        if event.words and previous.words:
            return _same_word_span(event.words, previous.words)
        return self._last_source_event is not None and event is self._last_source_event
