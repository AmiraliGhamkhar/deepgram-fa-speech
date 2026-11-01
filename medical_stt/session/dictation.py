"""`LiveMedicalSTT`: the object that owns one dictation's state.

Two jobs, and deliberately only two:

1. **Composition** -- build the terminology engine, the pipeline, the injector,
   the overlay, the audio queue and every session collaborator once, from
   configuration (see `_compose`).
2. **State ownership** -- hold the session-scoped state (provider, queue,
   generation counter, errors, phase, warnings) and expose it to the
   collaborators through the `SessionHost` contract.

Behaviour lives elsewhere: `runner.SessionRunner` runs a session,
`supervisor.ReconnectSupervisor` decides about retries, `transcripts` routes
events, `renderer` turns an utterance into injected text, `watchdog` and
`audio_health` look after capture quality.

State is mutated by worker threads and read by the UI thread, so every
cross-thread transition is either an `threading.Event` or a single attribute
write; nothing here holds a lock across a blocking call.
"""
from __future__ import annotations

import logging
import math
import threading
from typing import Any, Callable, Dict, List, Optional

from ..audio import BoundedAudioQueue, QueueStats
from ..audio import microphone
from ..config import (
    ConfigError,
    Settings,
    get_settings,
    load_asr_replacements,
    load_correction_rules_raw,
    load_keyterms,
    validate_settings,
)
from ..injection import TextInjector
from ..injection.strategies import strategy_for
from ..processing.pipeline import TranscriptPipeline
from ..processing.terminology import TerminologyEngine
from ..stt.accumulator import UtteranceAccumulator
from ..stt.base import ProviderError, STTProvider, TranscriptEvent
from ..stt.deepgram_provider import DeepgramProvider
from ..stt.reconnect import ReconnectPolicy
from ..ui import TranscriptOverlay
from .audio_health import AudioHealth
from .latency import LatencyTracker
from .messages import CAPACITY_WARNING, MIC_SILENT_WARNING, MICROPHONE_UNAVAILABLE_WARNING
from .recovery import RecoveryBuffer
from .renderer import UtteranceRenderer
from .runner import SessionRunner
from .supervisor import ReconnectSupervisor
from .transcripts import UtteranceRouter
from .warnings import WarningBoard
from .watchdog import AudioWatchdog, WatchdogPolicy

log = logging.getLogger("medical_stt.app")

__all__ = ["CAPACITY_WARNING", "MICROPHONE_UNAVAILABLE_WARNING", "MIC_SILENT_WARNING", "LiveMedicalSTT"]

#: Fixed-time audio budget, independent of the configured block size.
AUDIO_BUFFER_SECONDS = 3.2

#: Width of one 16-bit sample, used for the seconds<->bytes conversions in the
#: health snapshot.
BYTES_PER_SAMPLE = 2


class LiveMedicalSTT:
    """One dictation, start to finish, with reconnect support."""

    def __init__(
        self,
        provider: Optional[STTProvider] = None,
        *,
        settings: Optional[Settings] = None,
        microphone_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.settings = settings or get_settings()
        errors = validate_settings(self.settings)
        # A missing/unusable host configuration is reported as a normal
        # validation error so no credential value is ever logged with it.
        if errors:
            raise ConfigError("; ".join(errors))

        self.terminology = self._build_terminology()
        self.keyterms = load_keyterms(self.settings.specialty)
        self._asr_replacements = load_asr_replacements() if self.settings.use_asr_replacements else []
        self._owns_provider = provider is None
        self._provider_used = False
        self.provider: STTProvider = provider or DeepgramProvider(
            self.settings, self.keyterms, self._asr_replacements
        )
        self._microphone_factory = microphone_factory

        # -- collaborators --------------------------------------------------
        self.injector = TextInjector(
            dry_run=False,
            enable_smart_rewrite=True,
            restore_clipboard=self.settings.restore_clipboard,
            paste_settle_seconds=self.settings.paste_settle_seconds,
        )
        self.overlay = TranscriptOverlay(enabled=self.settings.overlay_enabled)
        self.latency = LatencyTracker()
        self.last_confidence_warning = None

        self._audio_q = BoundedAudioQueue(
            maxsize=max(1, math.floor(AUDIO_BUFFER_SECONDS / self.settings.block_duration))
        )
        self.phase = "connecting"
        self._warnings = WarningBoard(self._publish_warning)
        self._recovery_buffer = RecoveryBuffer()
        self._health = AudioHealth(self._audio_q, self._warn)
        self._watchdog = AudioWatchdog(
            self._audio_q,
            WatchdogPolicy.from_defaults(),
            warn=self._warn,
            clear_warning=self._clear_warning,
        )
        self._accumulator = UtteranceAccumulator()
        self._renderer: Optional[UtteranceRenderer] = None
        self._router = self._build_router()
        self._runner = SessionRunner(self)

        # -- session-scoped state ------------------------------------------
        #: Set to end the current session (error, endpoint, or Stop).
        self._stop = threading.Event()
        #: Set by `request_stop()` to end `run()` itself.
        self._shutdown = threading.Event()
        self._errors: List[ProviderError] = []
        self._reconnect_count = 0
        self._abandoned_workers: List[threading.Thread] = []
        #: Identifies the session currently owned by this instance. One
        #: `LiveMedicalSTT` is reused across reconnects, so its provider and
        #: sender threads are per-session while `_stop`, `_errors` and the
        #: audio queue are not. A thread from session N that outlives its join
        #: timeout (a wedged socket) would otherwise stop session N+1, append
        #: an error to it, or drain its audio queue. Every session-scoped
        #: callback carries the generation it was created with and is ignored
        #: once `_session_generation` moves on.
        self._session_generation = 0
        self._connected_at: Optional[float] = None

    # -- composition -------------------------------------------------------

    def _build_terminology(self) -> TerminologyEngine:
        engine = TerminologyEngine(
            enable_context_dependent=self.settings.enable_context_dependent_terms
        )
        load_result = engine.load(load_correction_rules_raw())
        if load_result.conflicts:
            log.warning("terminology rule conflicts detected: %d", len(load_result.conflicts))
        return engine

    def _build_router(self) -> UtteranceRouter:
        pipeline = TranscriptPipeline.default(self.terminology, self.settings.medical_confidence_threshold)
        renderer = UtteranceRenderer(
            pipeline=pipeline,
            injector=lambda: self.injector,
            strategy=lambda: strategy_for(self.settings.inject_mode),
            overlay=self.overlay,
            recovery=self._recovery_buffer,
            warnings=self._warnings,
            set_injection_failed=self._set_injection_failed,
            confidence_sink=self._record_confidence_warning,
            on_fatal=self._on_render_failure,
        )
        self._renderer = renderer
        return UtteranceRouter(
            renderer=renderer,
            accumulator=lambda: self._accumulator,
            recovery=self._recovery_buffer,
            warnings=self._warnings,
            overlay=self.overlay,
            is_current_session=self._is_current_session,
            on_fatal=self._on_provider_error,
        )

    def _record_confidence_warning(self, warning: Any) -> None:
        self.last_confidence_warning = warning

    def _set_injection_failed(self, failed: bool) -> None:
        self._router.set_injection_failed(failed)

    def _on_render_failure(self, error: ProviderError) -> None:
        self._on_provider_error(error)

    # -- SessionHost contract ---------------------------------------------

    @property
    def audio_queue(self) -> BoundedAudioQueue:
        return self._audio_q

    @property
    def stop_event(self) -> threading.Event:
        return self._stop

    @property
    def shutdown_event(self) -> threading.Event:
        return self._shutdown

    @property
    def errors(self) -> List[ProviderError]:
        return self._errors

    @property
    def abandoned_workers(self) -> List[threading.Thread]:
        return self._abandoned_workers

    @property
    def generation(self) -> int:
        return self._session_generation

    @property
    def health(self) -> AudioHealth:
        return self._health

    @property
    def watchdog(self) -> AudioWatchdog:
        return self._watchdog

    @property
    def router(self) -> UtteranceRouter:
        return self._router

    @property
    def connected_at(self) -> Optional[float]:
        return self._connected_at

    @connected_at.setter
    def connected_at(self, at: Optional[float]) -> None:
        self._connected_at = at

    @property
    def reconnect_count(self) -> int:
        return self._reconnect_count

    def note_reconnect(self) -> None:
        self._reconnect_count += 1

    def begin_generation(self) -> int:
        self._session_generation += 1
        return self._session_generation

    def is_current_session(self, generation: Optional[int]) -> bool:
        return self._is_current_session(generation)

    def prepare_provider(self) -> STTProvider:
        """Return this session's provider, replacing a used one.

        A provider instance is single-use: reusing it would carry the previous
        session's stop flag and connection into the next one.
        """
        if self._owns_provider and self._provider_used:
            self.provider = DeepgramProvider(self.settings, self.keyterms, self._asr_replacements)
        self._provider_used = True
        return self.provider

    def prepare_session_events(self) -> None:
        if not self._shutdown.is_set():
            self._stop.clear()
        self._errors.clear()

    def prune_abandoned_workers(self) -> None:
        self._abandoned_workers = [worker for worker in self._abandoned_workers if worker.is_alive()]

    def handle_provider_error(self, error: ProviderError, generation: Optional[int] = None) -> None:
        self._on_provider_error(error, generation)

    def warn(self, message: str) -> None:
        self._warn(message)

    def clear_warning(self, message: str) -> None:
        self._clear_warning(message)

    def set_phase(self, phase: str) -> None:
        self.phase = phase

    def retain_recovery(self, text: str) -> None:
        self._retain_failed(text)

    def reset_session_state(self) -> None:
        self._reset_session_state()

    def microphone_factory(self) -> Any:
        if self._microphone_factory is not None:
            return self._microphone_factory
        return microphone.load_sounddevice()

    def on_audio(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        self._on_audio(indata, frames, time_info, status)

    # -- public state ------------------------------------------------------

    def request_stop(self) -> None:
        """Ask the current (or next) session to shut down cleanly.

        Safe to call from another thread, including before `run()` starts and
        while the reconnect loop is sleeping.
        """
        self._shutdown.set()
        self._stop.set()

    @property
    def is_stopping(self) -> bool:
        return self._shutdown.is_set()

    @property
    def warning(self) -> str:
        return self._warnings.message

    @property
    def audio_queue_stats(self) -> QueueStats:
        """Current queue depth and dropped-audio counters."""
        return self._audio_q.stats()

    @property
    def session_health(self) -> Dict[str, Any]:
        """Operational snapshot for a live session.

        Exposes whether the audio path is keeping up. `degraded` is a
        *controlled* signal: when the sender cannot drain as fast as the
        microphone fills it, audio is being lost and the user (or support)
        needs to know, rather than the loss being discovered later as a gap in
        the chart.
        """
        stats = self._audio_q.stats()
        bytes_per_second = self.settings.sample_rate * self.settings.channels * BYTES_PER_SAMPLE
        return {
            "degraded": stats.degraded,
            "audio_queue_depth": stats.depth,
            "audio_queue_drops_total": stats.dropped_total,
            "consecutive_drops": stats.consecutive_drops,
            "reconnect_count": self._reconnect_count,
            "phase": self.phase,
            "warning": self.warning,
            "audio_queue_age_seconds": stats.oldest_age_seconds,
            "audio_sent_seconds": stats.sent_bytes / bytes_per_second,
            "audio_lost_seconds": stats.dropped_bytes / bytes_per_second,
        }

    def recovery_text(self) -> str:
        """Bounded in-memory recovery only; never log or persist clinical text."""
        return self._recovery_buffer.text()

    def clear_recovery(self) -> None:
        self._recovery_buffer.clear()

    # -- warnings ----------------------------------------------------------

    def _publish_warning(self, message: str) -> None:
        self.overlay.set_warning(message)

    def _warn(self, message: str) -> None:
        self._warnings.set(message, recovery_truncated=self._recovery_buffer.truncated)

    def _clear_warning(self, message: str) -> None:
        """Withdraw `message` once its cause is gone, without clobbering a
        different warning that was raised in the meantime."""
        self._warnings.clear(message)

    # -- transcript handling ----------------------------------------------

    def _is_current_session(self, generation: Optional[int]) -> bool:
        """True when `generation` still refers to the session being run.

        `None` means "no generation supplied" (a direct call from a test or
        from the current session's own code path) and is always current, so
        the guard never changes single-session behaviour.
        """
        return generation is None or generation == self._session_generation

    def _display_interim(self, event: TranscriptEvent) -> None:
        self._router.display_interim(event)

    def _on_transcript(
        self,
        event: TranscriptEvent,
        generation: Optional[int] = None,
        latency: Optional[LatencyTracker] = None,
    ) -> None:
        self._router.handle(event, generation, latency)

    def _flush_pending_utterance(self, latency: Optional[LatencyTracker] = None) -> None:
        self._router.flush_pending(latency)

    def _finalize_utterance(
        self, utterance: TranscriptEvent, latency: Optional[LatencyTracker] = None
    ) -> None:
        self._router.finalize(utterance, latency)

    def _render_and_inject(
        self, utterance: TranscriptEvent, latency: Optional[LatencyTracker] = None
    ) -> Any:
        """Process and inject one utterance (see `session/renderer.py`).

        Directional formatting happens at the injection boundary, so this
        method contains no BiDi logic of its own.
        """
        assert self._renderer is not None  # set by `_build_router`
        return self._renderer.render(utterance, latency)

    def _retain_failed(self, text: str) -> None:
        self._recovery_buffer.retain(text)

    # -- accessors kept for tests and diagnostics --------------------------
    # These are the same objects the collaborators use (not copies), so
    # poking one from a test still exercises the production path.

    @property
    def _utterance(self) -> UtteranceAccumulator:
        return self._accumulator

    @_utterance.setter
    def _utterance(self, accumulator: UtteranceAccumulator) -> None:
        self._accumulator = accumulator

    @property
    def _injection_failed(self) -> bool:
        return self._router.injection_failed

    @_injection_failed.setter
    def _injection_failed(self, failed: bool) -> None:
        self._router.set_injection_failed(failed)

    @property
    def _recovery(self) -> RecoveryBuffer:
        return self._recovery_buffer

    @property
    def _recovery_truncated(self) -> bool:
        return self._recovery_buffer.truncated

    @property
    def _input_status_pending(self) -> threading.Event:
        return self._health.input_status_pending

    @property
    def _audio_drop_pending(self) -> threading.Event:
        return self._health.audio_drop_pending

    @property
    def _last_capture_at(self) -> float:
        return self._watchdog.last_capture_at

    @_last_capture_at.setter
    def _last_capture_at(self, at: float) -> None:
        self._watchdog.last_capture_at = at

    @property
    def _last_keep_alive_at(self) -> float:
        return self._watchdog.last_keep_alive_at

    @_last_keep_alive_at.setter
    def _last_keep_alive_at(self, at: float) -> None:
        self._watchdog.last_keep_alive_at = at

    def _on_provider_error(self, error: ProviderError, generation: Optional[int] = None) -> None:
        if not self._is_current_session(generation):
            # Belongs to a session that was already torn down: reporting it
            # would make the *current* session fail for the previous one's
            # reason (and `run()` would then back off or give up on a healthy
            # connection).
            log.debug(
                "dropped a provider error from a superseded session: category=%s",
                error.category.value,
            )
            return
        log.error("provider error: category=%s message=%s", error.category.value, str(error))
        self._errors.append(error)
        self._stop.set()

    # -- audio -------------------------------------------------------------

    def _on_audio(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        """Real-time capture callback: enqueue and return.

        Runs on sounddevice's audio thread, so it only does O(1) work with no
        logging and no locks beyond the queue's own. Anything worth reporting
        (queue overflow, device status) becomes a flag here and is logged by
        the session thread (`AudioHealth.flush`).
        """
        self._health.note_status(status)
        if not self._audio_q.put_nowait(bytes(indata)):
            self._health.note_drop()

    def _report_audio_health(self) -> None:
        """Log capture problems from a worker thread (never the callback).

        The lost-audio warning is deliberately sticky: queue recovery cannot
        recover the missing words, so the clinician must still review the note.
        Silence/capacity warnings, unlike data loss, are withdrawn on recovery.
        """
        self._health.flush()

    def _reset_capture_watch(self) -> None:
        self._watchdog.reset()

    def _watch_capture(self, provider: STTProvider) -> None:
        self._watchdog.check(provider.keep_alive)

    # -- session lifecycle -------------------------------------------------

    def _reset_session_state(self) -> None:
        """Drop everything that belongs to the previous session.

        * Buffered audio is discarded: it was captured before Stop and must not
          be streamed into the next session (which would produce a burst of
          stale transcript when it starts).
        * The utterance accumulator is cleared so a half-finished utterance
          cannot leak into the next one.
        * The injector's streaming delta is reset. Leaving it set would make the
          next session delete characters the user has since typed.
        """
        drained = self._audio_q.drain()
        if drained:
            log.info("discarded %d buffered audio chunk(s) on session reset", drained)
        self._accumulator.reset()
        self.injector.reset_partial()
        self.latency = LatencyTracker()

    def _run_session(self) -> None:
        """Run one session (see `session/runner.py`)."""
        self._runner.run_once()

    def run(self) -> int:
        """Run dictation until Stop, a terminal error, or a clean stream end."""
        settings = self.settings
        log.info("Medical STT starting: model=%s language=%s", settings.model, settings.language)
        log.info("Host: %s (short-lived sessions; no Deepgram key on this machine)", settings.host_url)
        log.info(
            "Correction rules active: %d / %d total",
            self.terminology.rule_count, self.terminology.total_rule_count,
        )
        log.info("Keyterms loaded: %d", len(self.keyterms))

        supervisor = ReconnectSupervisor(
            self,
            ReconnectPolicy(
                base_delay=settings.reconnect_delay,
                max_delay=settings.reconnect_backoff_max,
                jitter=settings.reconnect_jitter,
                max_attempts=settings.max_reconnect_attempts,
            ),
            self._run_session,
        )
        self.overlay.set_idle()
        try:
            return supervisor.run()
        finally:
            self.overlay.close()
            log.info("session ended")
