"""Routing of provider transcript events into utterances.

Three kinds of event arrive on the provider thread and each has exactly one
destination:

* `speech_started`    starts the latency measurement for the next utterance;
* a non-final result  updates the display-only overlay (bounded, latest wins);
* a final result      is accumulated into an utterance and rendered.

The accumulator is the only piece of state that survives between events, so
"what is currently being dictated" has exactly one owner.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from ..processing.normalize import normalize
from ..stt.accumulator import UtteranceAccumulator, UtteranceLimitError
from ..stt.base import ErrorCategory, ProviderError, TranscriptEvent
from ..ui import TranscriptOverlay
from .latency import LatencyTracker
from .messages import (
    PROCESSING_FAILED_WARNING,
    UTTERANCE_LIMIT_WARNING,
)
from .recovery import RecoveryBuffer
from .renderer import UtteranceRenderer
from .warnings import WarningBoard

log = logging.getLogger("medical_stt.app")

#: Bound on a display-only hypothesis. An interim never reaches the clinician's
#: document, so a runaway one must not reach the overlay either.
INTERIM_DISPLAY_LIMIT = 8000


class UtteranceRouter:
    """Turns transcript events into rendered utterances."""

    def __init__(
        self,
        *,
        renderer: UtteranceRenderer,
        accumulator: Callable[[], UtteranceAccumulator],
        recovery: RecoveryBuffer,
        warnings: WarningBoard,
        overlay: TranscriptOverlay,
        is_current_session: Callable[[Optional[int]], bool],
        on_fatal: Callable[[ProviderError], None],
    ) -> None:
        self._renderer = renderer
        self._accumulator = accumulator  # callable: the owner may swap it
        self._recovery = recovery
        self._warnings = warnings
        self._overlay = overlay
        self._is_current_session = is_current_session
        self._on_fatal = on_fatal
        #: Sticky: once a stage or the injector has failed, later utterances in
        #: this session are retained for recovery instead of being injected into
        #: a document whose state is now unknown.
        self._injection_failed = False

    # -- state -------------------------------------------------------------

    @property
    def injection_failed(self) -> bool:
        return self._injection_failed

    def set_injection_failed(self, failed: bool) -> None:
        self._injection_failed = failed

    def reset(self) -> None:
        """Forget the previous session's partial utterance and failure state."""
        self._accumulator().reset()
        self._injection_failed = False

    # -- events ------------------------------------------------------------

    def display_interim(self, event: TranscriptEvent) -> None:
        """One bounded normalization path for display-only hypotheses."""
        try:
            self._overlay.set_partial(normalize(event.text[:INTERIM_DISPLAY_LIMIT]))
        except Exception as exc:  # noqa: BLE001 - a UI fault must not end dictation
            log.error("overlay update failed (%s)", type(exc).__name__)

    def handle(
        self,
        event: TranscriptEvent,
        generation: Optional[int] = None,
        latency: Optional[LatencyTracker] = None,
    ) -> None:
        """Handle one transcript event with its own per-utterance tracker.

        Finals are processed on a worker thread; swapping a shared tracker
        there could attach one utterance's marks to another, so the caller
        passes the tracker that belongs to this event.
        """
        tracker = latency or LatencyTracker()
        if not self._is_current_session(generation):
            # A transcript from a superseded session must never be injected
            # into the one the user is dictating into now.
            log.debug("dropped a transcript from a superseded session")
            return
        if event.speech_started:
            tracker.mark_utterance_start(event.received_at)
            return
        if not event.is_final:
            tracker.mark_first_interim(event.received_at)
            self.display_interim(event)
            return

        if self._injection_failed:
            if event.text:
                self._recovery.retain(event.text)
            return
        try:
            utterance = self._accumulator().add(event)
        except UtteranceLimitError as exc:
            self._injection_failed = True
            # The accumulator reset itself; its exception carries the whole
            # utterance. Retain it once rather than appending only the final
            # segment and duplicating earlier ones on shutdown's flush.
            self._recovery.retain(exc.accumulated_text or event.text)
            self._warnings.set(UTTERANCE_LIMIT_WARNING, recovery_truncated=self._recovery.truncated)
            self._on_fatal(
                ProviderError(ErrorCategory.CONFIG, "Utterance limit reached; review recovery text")
            )
            return
        if utterance is None:
            return

        tracker.mark_speech_final(event.received_at)
        self.finalize(utterance, tracker)

    def finalize(self, utterance: TranscriptEvent, tracker: Optional[LatencyTracker] = None) -> None:
        """Render one accumulated utterance, preserving it if anything fails."""
        try:
            self._renderer.render(utterance, tracker)
        except Exception as exc:  # noqa: BLE001 - the whole utterance must survive
            log.error("utterance finalization failed (%s)", type(exc).__name__)
            self._injection_failed = True
            self._recovery.retain(utterance.text)
            self._warnings.set(PROCESSING_FAILED_WARNING, recovery_truncated=self._recovery.truncated)
            self._on_fatal(
                ProviderError(ErrorCategory.CONFIG, "Transcript processing failed; review recovery text")
            )

    def flush_pending(self, latency: Optional[LatencyTracker] = None) -> None:
        """Emit an utterance the provider finalized but never endpointed.

        Called once per session, right after shutdown has been requested and
        the audio queue has been drained into the provider. Without it, a
        segment that arrived as `is_final` without a following
        `speech_final`/`UtteranceEnd` would be recognized but never injected --
        i.e. the last words of a dictation would be lost.
        """
        tracker = latency or LatencyTracker()
        pending = self._accumulator().flush()
        if pending is None:
            return
        log.info("injecting the final buffered utterance during shutdown")
        if self._injection_failed:
            self._recovery.retain(pending.text)
            return
        tracker.mark_speech_final(pending.received_at)
        self.finalize(pending, tracker)
