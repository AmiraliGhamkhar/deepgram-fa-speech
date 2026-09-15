"""Small accumulator for Deepgram-style finalized transcript segments."""
from __future__ import annotations

from typing import List, Optional

from .base import TranscriptEvent


class UtteranceAccumulator:
    """Collect finalized segments until the provider marks speech complete."""

    def __init__(self) -> None:
        self._segments: List[TranscriptEvent] = []

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

        if not event.speech_final:
            return None
        return self.flush()

    def flush(self) -> Optional[TranscriptEvent]:
        if not self._segments:
            return None

        segments, self._segments = self._segments, []
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

    def _is_duplicate(self, event: TranscriptEvent, text: str) -> bool:
        if not self._segments:
            return False
        previous = self._segments[-1]
        if event.words and previous.words:
            first_start = event.words[0].start
            previous_start = previous.words[0].start
            return text == previous.text and abs(first_start - previous_start) < 0.001
        return text == previous.text
