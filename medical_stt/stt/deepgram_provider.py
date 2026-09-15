"""Deepgram implementation of the STTProvider interface.

Keeps the working streaming architecture (`listen.v1.connect`, Nova-3,
`interim_results`, endpointing, smart formatting, keyterms) but isolates all
Deepgram-specific error handling behind the generic `ErrorCategory`
taxonomy so the reconnect loop never needs to know about Deepgram SDK
exception types.
"""
from __future__ import annotations

import logging
import socket
import threading
from typing import Any, List, Optional

from ..config import Settings
from .base import ErrorCategory, OnError, OnTranscript, ProviderError, STTProvider, TranscriptEvent

log = logging.getLogger("medical_stt.stt.deepgram")


def classify_deepgram_exception(exc: BaseException) -> ErrorCategory:
    """Map a raw exception raised anywhere in the Deepgram SDK / websocket
    stack to a generic ErrorCategory. Import errors from optional
    dependencies are handled defensively so this works even if the
    `websockets` transport is swapped out by a future SDK version.
    """
    # Deepgram SDK's ApiError carries an HTTP-like status_code for
    # handshake-time failures (auth, bad request).
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        if status_code in (401, 403):
            return ErrorCategory.AUTH
        if status_code == 429:
            return ErrorCategory.RATE_LIMIT
        if 400 <= status_code < 500:
            return ErrorCategory.CONFIG
        if status_code >= 500:
            return ErrorCategory.SERVER_DISCONNECT

    try:
        import websockets.exceptions as ws_exc

        if isinstance(exc, ws_exc.ConnectionClosedOK):
            return ErrorCategory.SERVER_DISCONNECT
        if isinstance(exc, ws_exc.ConnectionClosedError):
            return ErrorCategory.SERVER_DISCONNECT
        if isinstance(exc, ws_exc.ConnectionClosed):
            return ErrorCategory.SERVER_DISCONNECT
        if isinstance(exc, ws_exc.InvalidStatus):
            code = getattr(getattr(exc, "response", None), "status_code", None)
            if code in (401, 403):
                return ErrorCategory.AUTH
            if code == 429:
                return ErrorCategory.RATE_LIMIT
            if code is not None and 400 <= code < 500:
                return ErrorCategory.CONFIG
        if isinstance(exc, (ws_exc.InvalidHandshake, ws_exc.WebSocketException)):
            return ErrorCategory.NETWORK
    except ImportError:  # pragma: no cover - defensive only
        pass

    if isinstance(exc, (socket.timeout, TimeoutError)):
        return ErrorCategory.TIMEOUT
    if isinstance(exc, (socket.gaierror, ConnectionError, OSError)):
        return ErrorCategory.NETWORK

    return ErrorCategory.UNKNOWN


class DeepgramProvider(STTProvider):
    def __init__(self, settings: Settings, keyterms: Optional[List[str]] = None) -> None:
        self._settings = settings
        self._keyterms = keyterms or []
        self._stop_event = threading.Event()
        self._connection: Any = None
        self._connection_lock = threading.Lock()

    def validate_config(self) -> None:
        s = self._settings
        if not s.api_key:
            raise ProviderError(ErrorCategory.CONFIG, "DEEPGRAM_API_KEY is not set")
        if not s.model:
            raise ProviderError(ErrorCategory.CONFIG, "model must not be empty")
        if not s.language:
            raise ProviderError(ErrorCategory.CONFIG, "language must not be empty")
        if s.sample_rate <= 0:
            raise ProviderError(ErrorCategory.CONFIG, "sample_rate must be positive")
        if s.channels not in (1, 2):
            raise ProviderError(ErrorCategory.CONFIG, "channels must be 1 or 2")

    def start(self, on_transcript: OnTranscript, on_error: OnError) -> None:
        # Imported lazily so environments that only run the deterministic
        # processing tests (no network deps needed) don't require the
        # Deepgram SDK to be installed.
        from deepgram import DeepgramClient
        from deepgram.core.events import EventType
        from deepgram.listen.v1.types import ListenV1Results

        s = self._settings
        self._stop_event.clear()

        try:
            client = DeepgramClient(api_key=s.api_key)
            with client.listen.v1.connect(
                model=s.model,
                language=s.language,
                encoding="linear16",
                sample_rate=s.sample_rate,
                channels=s.channels,
                interim_results=True,
                endpointing=s.endpointing,
                utterance_end_ms=s.utterance_end_ms,
                vad_events=True,
                smart_format=True,
                punctuate=True,
                keyterm=self._keyterms or None,
            ) as connection:
                with self._connection_lock:
                    self._connection = connection

                def _on_message(message: object) -> None:
                    if not isinstance(message, ListenV1Results):
                        return
                    if message.channel is None or not message.channel.alternatives:
                        return
                    text = (message.channel.alternatives[0].transcript or "").strip()
                    if not text:
                        return
                    confidence = getattr(message.channel.alternatives[0], "confidence", None)
                    on_transcript(TranscriptEvent(text=text, is_final=bool(message.is_final), confidence=confidence))

                def _on_provider_error(exc: object) -> None:
                    category = classify_deepgram_exception(exc if isinstance(exc, BaseException) else Exception(str(exc)))
                    on_error(ProviderError(category, str(exc), cause=exc if isinstance(exc, BaseException) else None))

                def _on_close(_: object) -> None:
                    if not self._stop_event.is_set():
                        on_error(ProviderError(ErrorCategory.SERVER_DISCONNECT, "Deepgram connection closed"))

                connection.on(EventType.OPEN, lambda _: log.info("Deepgram connection established"))
                connection.on(EventType.MESSAGE, _on_message)
                connection.on(EventType.ERROR, _on_provider_error)
                connection.on(EventType.CLOSE, _on_close)

                connection.start_listening()
        except BaseException as exc:  # noqa: BLE001 - single classification boundary
            with self._connection_lock:
                self._connection = None
            if self._stop_event.is_set():
                raise ProviderError(ErrorCategory.SHUTDOWN, "Provider stopped during startup", cause=exc) from exc
            category = classify_deepgram_exception(exc)
            raise ProviderError(category, str(exc), cause=exc) from exc
        finally:
            with self._connection_lock:
                self._connection = None

    def send_audio(self, chunk: bytes) -> None:
        with self._connection_lock:
            connection = self._connection
        if connection is None:
            return
        try:
            connection.send_media(chunk)
        except BaseException as exc:  # noqa: BLE001 - reclassified for caller
            category = classify_deepgram_exception(exc)
            raise ProviderError(category, f"failed to send audio: {exc}", cause=exc) from exc

    def stop(self) -> None:
        self._stop_event.set()
        with self._connection_lock:
            connection = self._connection
        if connection is None:
            return
        try:
            connection.send_finalize()
            connection.send_close_stream()
        except (OSError, RuntimeError) as exc:
            log.debug("error while closing Deepgram connection: %s", exc)
