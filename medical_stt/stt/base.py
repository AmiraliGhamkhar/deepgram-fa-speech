"""Provider-agnostic STT interfaces and error taxonomy.

Every concrete provider (Deepgram today; local Whisper/Qwen or another cloud
API later) implements `STTProvider` and raises `ProviderError` with an
accurate `ErrorCategory` so the reconnect loop in medical_stt/app.py can
decide, generically, whether a failure is worth retrying.
"""
from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional


class ErrorCategory(enum.Enum):
    """Classification of STT provider failures.

    AUTH and CONFIG are never retried by the reconnect loop: retrying a bad
    API key or invalid configuration forever would hide a problem that
    requires human action and could look like "hanging" to the operator.
    Everything else is retryable with exponential backoff.
    """

    AUTH = "auth"                # invalid/revoked API key, 401/403
    CONFIG = "config"            # invalid request parameters, 400
    RATE_LIMIT = "rate_limit"    # 429 / quota exceeded
    NETWORK = "network"          # DNS, connection reset, TLS failure
    TIMEOUT = "timeout"          # connect/read timeout
    SERVER_DISCONNECT = "server_disconnect"  # clean/unclean close from server
    MICROPHONE = "microphone"    # local audio device failure
    SHUTDOWN = "shutdown"        # deliberate application shutdown
    UNKNOWN = "unknown"

    @property
    def is_retryable(self) -> bool:
        return self not in (ErrorCategory.AUTH, ErrorCategory.CONFIG, ErrorCategory.SHUTDOWN)


class ProviderError(Exception):
    """An STT provider failure, classified for the reconnect loop."""

    def __init__(self, category: ErrorCategory, message: str, *, cause: Optional[BaseException] = None) -> None:
        super().__init__(message)
        self.category = category
        self.__cause__ = cause


class ConnectionClosed(ProviderError):
    """The provider connection closed (cleanly or not)."""


@dataclass(frozen=True)
class TranscriptEvent:
    """A single transcript update from the provider, decoupled from any
    provider-specific message schema."""

    text: str
    is_final: bool
    confidence: Optional[float] = None


# Callback signatures used by STTProvider.
OnTranscript = Callable[[TranscriptEvent], None]
OnError = Callable[[ProviderError], None]


class STTProvider(ABC):
    """Minimal streaming STT interface the processing pipeline depends on.

    Implementations must:
    * Call `on_transcript` for every interim/final result.
    * Call `on_error` with a correctly classified `ProviderError` on any
      failure (never let a raw provider-SDK exception escape uncaught from
      the streaming thread).
    * Be safe to `stop()` from a different thread than `start()`.
    """

    @abstractmethod
    def validate_config(self) -> None:
        """Raise ProviderError(CONFIG) if the provider cannot be started
        with the current settings (missing key, invalid sample rate, ...).
        Must not open a network connection."""

    @abstractmethod
    def start(self, on_transcript: OnTranscript, on_error: OnError) -> None:
        """Open the streaming connection and begin delivering events.
        Blocks the calling thread until `stop()` is called or an
        unrecoverable error occurs (mirrors the previous single-threaded
        session loop so callers don't need to change their threading
        model)."""

    @abstractmethod
    def send_audio(self, chunk: bytes) -> None:
        """Send one chunk of raw PCM audio to the provider."""

    @abstractmethod
    def stop(self) -> None:
        """Request a graceful shutdown of the streaming session."""
