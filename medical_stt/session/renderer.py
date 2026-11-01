"""Turn one finalized utterance into injected text, or into a warning.

This is the only class that decides *what the clinician ends up seeing after a
final transcript*: run the deterministic pipeline, deliver the result through
the configured injection strategy, and report the outcome (overlay, warning,
recovery buffer, fatal error).

Everything it touches is injected, so a test can render an utterance with a
`DryRunBackend` and a fake overlay and observe exactly the same decisions the
production session makes.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from ..injection.strategies import InjectionStrategy
from ..injection.text_injector import TextInjector
from ..processing.confidence import MedicalConfidenceWarning
from ..processing.pipeline import PipelineOutcome, TranscriptPipeline
from ..stt.base import ErrorCategory, ProviderError, TranscriptEvent
from ..ui import TranscriptOverlay
from .latency import LatencyTracker, StageTimings
from .messages import INJECTION_FAILED_WARNING, LOW_CONFIDENCE_WARNING
from .recovery import RecoveryBuffer
from .warnings import WarningBoard

log = logging.getLogger("medical_stt.app")


class UtteranceRenderer:
    """Renders and injects a final utterance; never raises for user-facing faults."""

    def __init__(
        self,
        *,
        pipeline: TranscriptPipeline,
        injector: Callable[[], TextInjector],
        strategy: Callable[[], InjectionStrategy],
        overlay: TranscriptOverlay,
        recovery: RecoveryBuffer,
        warnings: WarningBoard,
        set_injection_failed: Callable[[bool], None],
        confidence_sink: Callable[[Optional[MedicalConfidenceWarning]], None],
        on_fatal: Callable[[ProviderError], None],
    ) -> None:
        self._pipeline = pipeline
        # Resolved per utterance: the owner may swap either one (a test
        # substituting a dry-run injector, or a settings change).
        self._injector = injector
        self._strategy = strategy
        self._overlay = overlay
        self._recovery = recovery
        self._warnings = warnings
        self._set_injection_failed = set_injection_failed
        self._confidence_sink = confidence_sink
        self._on_fatal = on_fatal

    # -- public API --------------------------------------------------------

    def render(self, utterance: TranscriptEvent, tracker: Optional[LatencyTracker] = None) -> PipelineOutcome:
        """Process and deliver one utterance.

        A stage that raises propagates to `UtteranceRouter`, which keeps the
        *whole* accumulated utterance in the recovery buffer and ends the
        session with a CONFIG error: partially typed clinical text is worse
        than none, and a stage bug must not silently continue dictating.
        """
        return self._render(utterance, tracker or LatencyTracker())

    # -- internals ---------------------------------------------------------

    def _render(self, utterance: TranscriptEvent, tracker: LatencyTracker) -> PipelineOutcome:
        started = time.monotonic()
        outcome = self._pipeline.run(utterance)
        self._confidence_sink(outcome.confidence_warning)

        # Directional formatting belongs to the injection boundary only.
        bidi_mark = time.monotonic()
        injector = self._injector()
        injector.reset_partial()
        delivered = self._strategy().deliver(injector, outcome.text)
        finished = time.monotonic()

        if not delivered:
            log.warning("injection failed for a final transcript")
            self._fail(outcome.text, INJECTION_FAILED_WARNING,
                       "Text injection failed; review recovery text")
        elif outcome.confidence_warning is not None:
            self._set_done(outcome.text)
            self._warnings.set(LOW_CONFIDENCE_WARNING)
        else:
            self._set_done(outcome.text)

        timings = StageTimings(
            terminology_s=outcome.timings.get("terminology", 0.0),
            bidi_s=bidi_mark - started,
            injection_s=finished - bidi_mark,
        )
        log.info(
            "latency: terminology=%.3fs bidi=%.3fs injection_acceptance=%.3fs",
            timings.terminology_s, timings.bidi_s, timings.injection_s,
        )
        tracker.mark_final(timings)
        return outcome

    def _set_done(self, text: str) -> None:
        try:
            self._overlay.set_done(text)
        except Exception as exc:  # noqa: BLE001 - UI failure must not lose text
            log.error("overlay update failed (%s)", type(exc).__name__)

    def _fail(self, text: str, warning: str, reason: str) -> None:
        self._recovery.retain(text)
        self._set_injection_failed(True)
        self._warnings.set(warning, recovery_truncated=self._recovery.truncated)
        self._on_fatal(ProviderError(ErrorCategory.CONFIG, reason))
