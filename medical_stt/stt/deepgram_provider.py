"""Deepgram implementation of the STTProvider interface.

Keeps the working streaming architecture (`listen.v1.connect`, Nova-3,
`interim_results`, endpointing, smart formatting, keyterms) but isolates all
Deepgram-specific error handling behind the generic `ErrorCategory`
taxonomy so the reconnect loop never needs to know about Deepgram SDK
exception types.

**Recognition hints.** The parameter used to bias recognition depends on
the model: Nova-3 uses Keyterm Prompting (`keyterm`, multi-word phrases
allowed), Nova-2 and the older models use the legacy `keywords` parameter.
The provider picks the right one from `config.keyterm_parameter` and
refuses combinations Deepgram would reject *before* opening the socket
(`validate_config`).

**Credential path.** This client has no Deepgram API key. Before each
connection it asks the self-hosted service for a *short-lived* session
token (see `medical_stt/host_client.py`), which the SDK sends as
`Authorization: Bearer <token>`. The token is used once for the
WebSocket handshake, never stored, and never logged; the real API key
stays in the host's environment.
"""
from __future__ import annotations

import logging
import socket
import threading
from typing import Any, Callable, List, Optional

from ..config import Settings, keyterm_parameter, validate_model_language
from .base import (
    ConnectionClosed,
    ErrorCategory,
    OnError,
    OnTranscript,
    ProviderError,
    STTProvider,
    TranscriptEvent,
    WordInfo,
)

log = logging.getLogger("medical_stt.stt.deepgram")

#: A callable that returns a fresh short-lived Deepgram session token.
TokenProvider = Callable[[], str]


def is_connection_closure(exc: BaseException) -> bool:
    """True when the provider's own exception says the socket closed.

    `ErrorCategory.SERVER_DISCONNECT` cannot answer this on its own: it is
    also what an upstream HTTP 5xx classifies to, and a 502 from the token
    endpoint is not a closed WebSocket. `stt.base.ConnectionClosed` is
    exported in `medical_stt.stt.__all__`, so it has to mean something -- a
    consumer that catches an exception the provider never raises gets a
    silently dead handler.
    """
    try:
        import websockets.exceptions as ws_exc
    except ImportError:  # pragma: no cover - defensive only
        return False
    # ConnectionClosedOK and ConnectionClosedError both derive from this.
    return isinstance(exc, ws_exc.ConnectionClosed)


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


def _optional_float(value: object) -> Optional[float]:
    try:
        return float(value) if value is not None else None  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def transcript_event_from_result(
    message: object, on_malformed_words: Optional[Callable[[int], None]] = None
) -> Optional[TranscriptEvent]:
    """Convert one Deepgram result object to the small internal event model.

    `on_malformed_words`, when given, is called once with the number of
    final words that were dropped for missing text or timestamps. Words
    are skipped silently otherwise, which would hide a provider format
    change from confidence gating: it would simply see fewer words than
    were spoken, with nothing in the logs.
    """
    channel = getattr(message, "channel", None)
    alternatives = getattr(channel, "alternatives", None)
    if not alternatives:
        return None

    alternative = alternatives[0]
    text = str(getattr(alternative, "transcript", "") or "").strip()
    is_final = bool(getattr(message, "is_final", False))
    speech_final = bool(getattr(message, "speech_final", False))
    if not text and not (is_final and speech_final):
        return None

    words: tuple[WordInfo, ...] = ()
    if is_final:
        parsed_words = []
        skipped_words = 0
        for word in getattr(alternative, "words", None) or ():
            raw_text = getattr(word, "punctuated_word", None) or getattr(word, "word", "")
            word_text = str(raw_text or "").strip()
            start = _optional_float(getattr(word, "start", None))
            end = _optional_float(getattr(word, "end", None))
            if not word_text or start is None or end is None:
                skipped_words += 1
                continue
            parsed_words.append(
                WordInfo(
                    text=word_text,
                    start=start,
                    end=end,
                    confidence=_optional_float(getattr(word, "confidence", None)),
                )
            )
        if skipped_words and on_malformed_words is not None:
            on_malformed_words(skipped_words)
        words = tuple(parsed_words)

    return TranscriptEvent(
        text=text,
        is_final=is_final,
        speech_final=speech_final,
        confidence=_optional_float(getattr(alternative, "confidence", None)),
        words=words,
    )


class DeepgramProvider(STTProvider):
    def __init__(
        self,
        settings: Settings,
        keyterms: Optional[List[str]] = None,
        asr_replacements: Optional[List[str]] = None,
        token_provider: Optional[TokenProvider] = None,
    ) -> None:
        self._settings = settings
        self._keyterms = keyterms or []
        self._asr_replacements = asr_replacements or []
        self._token_provider = token_provider
        self._stop_event = threading.Event()
        self._connection: Any = None
        self._connection_lock = threading.Lock()
        #: Bumped every time `stop()` claims the connection. A `send_audio`
        #: that raced a concurrent `stop()` detects the change under the
        #: same lock and drops the result instead of reporting a spurious
        #: send failure for audio that was on its way out anyway.
        self._stop_generation = 0
        #: Number of connections this provider has opened. Instance-local,
        #: never a module global: 50 clients share no provider state, so one
        #: session can never influence another.
        self._connections_opened = 0

    def _request_session_token(self) -> str:
        """Get a short-lived Deepgram session token from the host.

        Imported lazily: `host_client` depends on `stt.base`, so a
        module-level import here would create an import cycle.
        """
        if self._token_provider is not None:
            return self._token_provider()

        from ..host_client import HostSessionClient

        s = self._settings
        client = HostSessionClient(
            base_url=s.host_url,
            secret=s.host_secret,
            timeout=s.host_timeout_seconds,
            client_id=s.host_client_id,
        )
        session = client.fetch_session(ttl_seconds=s.session_ttl_seconds)
        # Correlation only: safe to log, and the only host-side handle that
        # ties this WebSocket to a host log line. The token below is not.
        log.info(
            "deepgram_session_granted session_id=%s expires_in=%ds",
            session.session_id or "n/a", session.expires_in,
        )
        # Intentionally not logged and not stored beyond this return value.
        return session.access_token

    def validate_config(self) -> None:
        """Refuse to open a WebSocket for a configuration Deepgram would
        reject (or silently ignore), before any network call is made."""
        s = self._settings
        if not s.host_url:
            raise ProviderError(ErrorCategory.CONFIG, "host_url is not set")
        if not s.host_secret and self._token_provider is None:
            raise ProviderError(ErrorCategory.CONFIG, "no host shared secret is available")
        model_language_errors = validate_model_language(s.model, s.language)
        if model_language_errors:
            raise ProviderError(ErrorCategory.CONFIG, "; ".join(model_language_errors))
        if s.sample_rate <= 0:
            raise ProviderError(ErrorCategory.CONFIG, "sample_rate must be positive")
        if s.channels not in (1, 2):
            raise ProviderError(ErrorCategory.CONFIG, "channels must be 1 or 2")

    def _recognition_hint_parameters(self) -> dict:
        """Return the keyterm/keywords parameters legal for this model.

        Nova-3 uses Keyterm Prompting (`keyterm`, phrases allowed). The
        legacy models use `keywords`, which Deepgram rejects for Nova-3 and
        which cannot boost multi-word phrases -- words are passed through
        unchanged rather than being turned into something unsupported.
        """
        if not self._keyterms:
            return {}
        parameter = keyterm_parameter(self._settings.model)
        if parameter == "keyterm":
            return {"keyterm": self._keyterms}
        if parameter == "keywords":
            return {"keywords": self._keyterms}
        return {}

    def start(self, on_transcript: OnTranscript, on_error: OnError) -> None:
        # Imported lazily so environments that only run the deterministic
        # processing tests (no network deps needed) don't require the
        # Deepgram SDK to be installed.
        from deepgram import DeepgramClient
        from deepgram.core.events import EventType
        from deepgram.listen.v1.types import ListenV1Results, ListenV1UtteranceEnd

        s = self._settings
        self._stop_event.clear()

        # A fresh short-lived token per connection. It is only valid for the
        # handshake, which is exactly why it is requested here and not at
        # construction time (a reconnect would otherwise present a stale
        # token and fail with an auth error).
        session_token = self._request_session_token()

        try:
            # `access_token` makes the SDK send `Authorization: Bearer ...`
            # instead of a long-lived API key, for both the websocket
            # handshake and any HTTP request.
            client = DeepgramClient(access_token=session_token)
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
                replace=self._asr_replacements or None,
                **self._recognition_hint_parameters(),
            ) as connection:
                with self._connection_lock:
                    self._connection = connection

                #: Warned at most once per connection: a provider format
                #: change must be visible in the logs without flooding them.
                malformed_words_warned = False

                def _warn_malformed_words(count: int) -> None:
                    nonlocal malformed_words_warned
                    if not malformed_words_warned:
                        malformed_words_warned = True
                        log.warning(
                            "deepgram returned %d final word(s) without text or "
                            "timestamps; they are skipped and confidence gating "
                            "sees fewer words than were spoken",
                            count,
                        )

                def _on_message(message: object) -> None:
                    if isinstance(message, ListenV1UtteranceEnd):
                        # UtteranceEnd can close segments when no result carried speech_final.
                        on_transcript(TranscriptEvent(text="", is_final=True, speech_final=True))
                        return
                    if not isinstance(message, ListenV1Results):
                        return

                    event = transcript_event_from_result(message, on_malformed_words=_warn_malformed_words)
                    if event is not None:
                        on_transcript(event)

                def _on_provider_error(exc: object) -> None:
                    cause = exc if isinstance(exc, BaseException) else Exception(str(exc))
                    category = classify_deepgram_exception(cause)
                    if is_connection_closure(cause):
                        on_error(ConnectionClosed(category, str(exc), cause=cause))
                    else:
                        on_error(ProviderError(category, str(exc), cause=cause))

                def _on_close(_: object) -> None:
                    if not self._stop_event.is_set():
                        on_error(
                            ConnectionClosed(
                                ErrorCategory.SERVER_DISCONNECT, "Deepgram connection closed"
                            )
                        )

                connection.on(EventType.OPEN, lambda _: log.info("deepgram_connected"))
                connection.on(EventType.MESSAGE, _on_message)
                connection.on(EventType.ERROR, _on_provider_error)
                connection.on(EventType.CLOSE, _on_close)
                self._connections_opened += 1
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
            # Do not keep the session credential alive in this frame any
            # longer than the connection needed it.
            session_token = ""

    @property
    def connections_opened(self) -> int:
        """How many Deepgram connections this provider has opened.

        Lets a test prove each reconnect mints a *new* connection rather than
        reusing a dead one, and that no state is shared between clients.
        """
        return self._connections_opened

    def send_audio(self, chunk: bytes) -> None:
        # The connection snapshot is taken under the lock, but the network
        # send happens outside it: holding the lock across a send would
        # block `stop()` for the whole round trip. The generation counter
        # -- checked under the same lock -- is what detects a `stop()`
        # that raced this send: such a send targets a connection that is
        # deliberately being closed, so its failure is a normal shutdown,
        # never a session error (and never a trigger for reconnect).
        with self._connection_lock:
            connection = self._connection
            generation = self._stop_generation
        if connection is None:
            return
        try:
            connection.send_media(chunk)
        except BaseException as exc:  # noqa: BLE001 - reclassified for caller
            # A failure on a connection that is being (or was) stopped is a
            # normal shutdown, never a session error and never a reconnect
            # trigger: the generation counter catches a send that was in
            # flight when `stop()` claimed the connection, and the stop
            # event catches a late send issued after `stop()` returned.
            with self._connection_lock:
                raced_stop = self._stop_generation != generation or self._stop_event.is_set()
            if raced_stop:
                log.debug("audio send raced session stop; dropped instead of reporting an error")
                return
            category = classify_deepgram_exception(exc)
            raise ProviderError(category, f"failed to send audio: {exc}", cause=exc) from exc

    def stop(self) -> None:
        self._stop_event.set()
        with self._connection_lock:
            connection = self._connection
            self._stop_generation += 1
        if connection is None:
            return
        try:
            connection.send_finalize()
            connection.send_close_stream()
        except Exception as exc:  # noqa: BLE001 - the connection is discarded either way
            # Any failure here (socket already closed by the peer, a
            # websockets library exception on the close path, ...) must not
            # escape into shutdown bookkeeping: `stop()` runs in `finally`
            # scopes where an exception would skip the remaining cleanup
            # steps and mislabel the session as failed.
            log.debug("error while closing Deepgram connection: %s", exc)
