"""Speech-to-text provider abstraction.

`STTProvider` is the interface the rest of the application (audio capture,
processing pipeline, injection) depends on. `DeepgramProvider` is the only
implementation today, but nothing outside this package knows that -- the
processing/injection layers only ever see `TranscriptEvent` objects and the
`STTProvider` interface, so a future local Whisper/Qwen provider can be
added without touching them.
"""
from .base import (
    ConnectionClosed,
    ErrorCategory,
    ProviderError,
    STTProvider,
    TranscriptEvent,
    WordInfo,
)
from .deepgram_provider import DeepgramProvider

__all__ = [
    "STTProvider",
    "TranscriptEvent",
    "WordInfo",
    "ProviderError",
    "ErrorCategory",
    "ConnectionClosed",
    "DeepgramProvider",
]
