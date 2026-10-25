"""Medical STT – real-time Persian + English medical dictation.

Pipeline: microphone -> STTProvider (Deepgram, via the self-hosted
session service) -> normalization -> terminology engine -> BiDi
formatting -> text injection -> overlay.

Only this module wires the concrete pieces together; every other module is
independently testable (see tests/).

Entry point: `main()` shows the floating Start/Stop control window
(`ui/control.py`), which drives one `SessionController` around
`LiveMedicalSTT`. The microphone is opened and released per Start/Stop,
never held open in the background.
"""
from __future__ import annotations

import enum
import math
from collections import deque
import logging
import logging.handlers
import queue
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from . import paths
from .app_instance import SingleInstance
from .audio import BoundedAudioQueue, QueueStats
from .config import (
    ConfigError,
    Settings,
    get_settings,
    initialize_user_config,
    load_asr_replacements,
    load_correction_rules_raw,
    load_keyterms,
    validate_settings,
)
from .injection import TextInjector
from .processing.bidi import to_injected
from .processing.confidence import MedicalConfidenceWarning, find_low_confidence_medical_words
from .processing.normalize import normalize
from .processing.terminology import TerminologyEngine
from .stt.accumulator import UtteranceAccumulator, UtteranceLimitError
from .stt.base import ErrorCategory, ProviderError, STTProvider, TranscriptEvent
from .stt.deepgram_provider import DeepgramProvider
from .stt.reconnect import ReconnectPolicy, decide
from .ui import TranscriptOverlay

log = logging.getLogger("medical_stt.app")

#: How long shutdown waits for the audio sender to drain the queue before
#: giving up on whatever is left (discarded, and reported in the log).
SENDER_JOIN_TIMEOUT_SECONDS = 2.0

#: How long shutdown waits for the provider thread to return after
#: `provider.stop()`. A wedged provider socket outlives this; the thread is
#: then abandoned rather than holding the user's Stop button open. Abandoned
#: threads carry an older `_session_generation`, so their callbacks cannot
#: reach the session that follows them.
PROVIDER_JOIN_TIMEOUT_SECONDS = 4.0

#: Seconds without a single captured frame before the microphone is
#: reported silent and the provider is kept alive instead. Deepgram closes a
#: stream that carries no audio (1011 NET-0001/NET-0002) after ~10 s; a
#: KeepAlive every 3-5 s resets that timer, so both thresholds are 3 s.
AUDIO_SILENCE_SECONDS = 3.0
KEEP_ALIVE_INTERVAL_SECONDS = 3.0

#: Warnings the session loop sets and later clears itself.
MIC_SILENT_WARNING = "میکروفون صدایی ارسال نمی‌کند؛ دستگاه ضبط را بررسی کنید"
CAPACITY_WARNING = "ظرفیت سرویس گفتار پر است؛ تا آزاد شدن ظرفیت دوباره تلاش می‌شود"


def load_sounddevice() -> Any:
    try:
        import sounddevice
    except (ImportError, OSError) as exc:
        raise RuntimeError(
            "Microphone requires sounddevice.\n"
            "  pip install sounddevice\n"
            "  (Linux: sudo apt install libportaudio2)"
        ) from exc
    return sounddevice


class LatencyTracker:
    """Per-utterance VAD/result-receipt and local-stage timing.

    VAD receipt is not acoustic onset; injection acceptance is not target
    consumption. Never logs transcript content, only durations/stage names.
    """

    def __init__(self) -> None:
        self._capture_start: Optional[float] = None
        self._first_interim: Optional[float] = None
        self._speech_final: Optional[float] = None

    def mark_utterance_start(self, at: Optional[float] = None) -> None:
        if self._capture_start is None:
            self._capture_start = at if at is not None else time.monotonic()
            self._first_interim = None
            self._speech_final = None

    def mark_first_interim(self, at: Optional[float] = None) -> None:
        if self._capture_start is not None and self._first_interim is None:
            self._first_interim = at if at is not None else time.monotonic()
            log.debug("latency: first interim after %.3fs", self._first_interim - self._capture_start)

    def mark_speech_final(self, at: Optional[float] = None) -> None:
        if self._capture_start is not None:
            self._speech_final = at if at is not None else time.monotonic()

    def mark_final(self, terminology_s: float, bidi_s: float, injection_s: float) -> None:
        if self._capture_start is None:
            return
        now = time.monotonic()
        speech_final_s = (self._speech_final or now) - self._capture_start
        first_interim_s = None if self._first_interim is None else self._first_interim - self._capture_start
        log.info(
            "latency: total=%.3fs vad_to_first_interim=%s vad_to_final_receipt=%.3fs "
            "terminology=%.3fs bidi=%.3fs injection_acceptance=%.3fs",
            now - self._capture_start,
            "n/a" if first_interim_s is None else f"{first_interim_s:.3f}s",
            speech_final_s,
            terminology_s,
            bidi_s,
            injection_s,
        )
        self._capture_start = None


class LiveMedicalSTT:
    def __init__(self, provider: Optional[STTProvider] = None) -> None:
        self.settings: Settings = get_settings()
        errors = validate_settings(self.settings)
        # A missing/unusable host configuration is reported as a normal
        # validation error so no credential value is ever logged with it.
        if errors:
            raise ConfigError("; ".join(errors))

        raw_rules = load_correction_rules_raw()
        self.terminology = TerminologyEngine(
            enable_context_dependent=self.settings.enable_context_dependent_terms
        )
        load_result = self.terminology.load(raw_rules)
        if load_result.conflicts:
            log.warning("terminology rule conflicts detected: %d", len(load_result.conflicts))

        self.keyterms = load_keyterms(self.settings.specialty)
        asr_replacements = load_asr_replacements() if self.settings.use_asr_replacements else []
        self._owns_provider = provider is None
        self._provider_used = False
        self._asr_replacements = asr_replacements
        self.provider: STTProvider = provider or DeepgramProvider(
            self.settings, self.keyterms, asr_replacements
        )

        self.injector = TextInjector(
            dry_run=False,
            enable_smart_rewrite=True,
            restore_clipboard=self.settings.restore_clipboard,
            paste_settle_seconds=self.settings.paste_settle_seconds,
        )
        self.overlay = TranscriptOverlay(enabled=self.settings.overlay_enabled)
        self.latency = LatencyTracker()
        self.last_confidence_warning: Optional[MedicalConfidenceWarning] = None
        self._utterance = UtteranceAccumulator()

        # Keep a fixed time budget even if capture block size changes.
        self._audio_q = BoundedAudioQueue(maxsize=max(1, math.floor(3.2 / self.settings.block_duration)))
        self.phase = "connecting"
        self.warning = ""
        self._reset_capture_watch()
        self._recovery: deque[str] = deque(maxlen=16)
        self._recovery_lock = threading.Lock()
        self._recovery_truncated = False
        self._injection_failed = False
        self._connected_at: Optional[float] = None
        self._abandoned_workers: list[threading.Thread] = []
        #: Set to end the current session (error, endpoint, or Stop).
        self._stop = threading.Event()
        #: Set by `request_stop()` to end `run()` itself.
        self._shutdown = threading.Event()
        self._errors: List[ProviderError] = []
        self._reconnect_count = 0
        #: Identifies the session currently owned by this instance. One
        #: `LiveMedicalSTT` is reused across reconnects, so its provider and
        #: sender threads are per-session while `_stop`, `_errors` and the
        #: audio queue are not. A thread from session N that outlives its
        #: join timeout (a wedged socket) would otherwise stop session N+1,
        #: append an error to it, or drain its audio queue. Every
        #: session-scoped callback carries the generation it was created
        #: with and is ignored once `_session_generation` moves on.
        self._session_generation = 0
        #: Set by the real-time callback, drained by `_report_audio_health`
        #: on the sender thread (the callback must never log).
        self._audio_drop_pending = threading.Event()
        self._input_status_pending = threading.Event()
        self._pending_input_status = ""

    def request_stop(self) -> None:
        """Ask the current (or next) session to shut down cleanly.

        Safe to call from another thread, including before `run()` starts
        and while the reconnect loop is sleeping.
        """
        self._shutdown.set()
        self._stop.set()

    @property
    def is_stopping(self) -> bool:
        return self._shutdown.is_set()

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
        needs to know, rather than the loss being discovered later as a gap
        in the chart.
        """
        stats = self._audio_q.stats()
        return {
            "degraded": stats.degraded,
            "audio_queue_depth": stats.depth,
            "audio_queue_drops_total": stats.dropped_total,
            "consecutive_drops": stats.consecutive_drops,
            "reconnect_count": self._reconnect_count,
            "phase": self.phase,
            "warning": self.warning,
            "audio_queue_age_seconds": stats.oldest_age_seconds,
            "audio_sent_seconds": stats.sent_bytes / (self.settings.sample_rate * self.settings.channels * 2),
            "audio_lost_seconds": stats.dropped_bytes / (self.settings.sample_rate * self.settings.channels * 2),
        }

    def recovery_text(self) -> str:
        """Bounded in-memory recovery only; never log or persist clinical text."""
        with self._recovery_lock:
            return "\n".join(self._recovery)

    def clear_recovery(self) -> None:
        with self._recovery_lock:
            self._recovery.clear()
            self._recovery_truncated = False

    def _retain_failed(self, text: str) -> None:
        with self._recovery_lock:
            if len(self._recovery) >= 16 or len(text) > 8000:
                self._recovery_truncated = True
            if len(self._recovery) < 16:
                self._recovery.append(text[:8000])

    def _warn(self, message: str) -> None:
        if self._recovery_truncated:
            message += " — ظرفیت بازیابی پر شده؛ بخشی از متن در دسترس نیست"
        self.warning = message
        self.overlay.set_warning(message)

    def _clear_warning(self, message: str) -> None:
        """Withdraw `message` once its cause is gone, without clobbering a
        different warning that was raised in the meantime."""
        if self.warning.startswith(message):
            self.warning = ""
            self.overlay.set_warning("")

    # -- transcript handling --------------------------------------------

    def _is_current_session(self, generation: Optional[int]) -> bool:
        """True when `generation` still refers to the session being run.

        `None` means "no generation supplied" (a direct call from a test or
        from the current session's own code path) and is always current, so
        the guard never changes single-session behaviour.
        """
        return generation is None or generation == self._session_generation

    def _on_transcript(self, event: TranscriptEvent, generation: Optional[int] = None) -> None:
        if not self._is_current_session(generation):
            # A transcript from a superseded session must never be injected
            # into the one the user is dictating into now.
            log.debug("dropped a transcript from a superseded session")
            return
        if event.speech_started:
            self.latency.mark_utterance_start(event.received_at)
            return
        if not event.is_final:
            self.latency.mark_first_interim(event.received_at)
            self.overlay.set_partial(normalize(event.text))
            return

        if self._injection_failed:
            if event.text:
                self._retain_failed(event.text)
            return
        try:
            utterance = self._utterance.add(event)
        except UtteranceLimitError:
            self._injection_failed = True
            self._retain_failed(event.text)
            self._warn("گفتار طولانی است؛ متن بازیابی را بررسی کنید")
            self._on_provider_error(ProviderError(ErrorCategory.CONFIG, "Utterance limit reached; review recovery text"))
            return
        if utterance is None:
            return

        self.latency.mark_speech_final(event.received_at)
        self._finalize_utterance(utterance)

    def _flush_pending_utterance(self) -> None:
        """Emit an utterance the provider finalized but never endpointed.

        Called once per session, right after shutdown has been requested
        and the audio queue has been drained into the provider. Without it,
        a segment that arrived as `is_final` without a following
        `speech_final`/`UtteranceEnd` would be recognized but never
        injected -- i.e. the last words of a dictation would be lost.
        """
        pending = self._utterance.flush()
        if pending is None:
            return
        log.info("injecting the final buffered utterance during shutdown")
        if self._injection_failed:
            self._retain_failed(pending.text)
        else:
            self.latency.mark_speech_final(pending.received_at)
            self._finalize_utterance(pending)

    def _finalize_utterance(self, utterance: TranscriptEvent) -> None:
        """Preserve the whole accumulated utterance if any processing stage fails."""
        try:
            self._render_and_inject(utterance)
        except Exception:
            self._retain_failed(utterance.text)
            self._injection_failed = True
            self._warn("پردازش متن ناموفق بود؛ متن بازیابی را بررسی کنید")
            self._on_provider_error(ProviderError(ErrorCategory.CONFIG, "Transcript processing failed; review recovery text"))

    def _render_and_inject(self, utterance: TranscriptEvent) -> None:
        self.last_confidence_warning = find_low_confidence_medical_words(
            utterance.words, self.settings.medical_confidence_threshold
        )
        if self.last_confidence_warning is not None:
            categories = {item.category for item in self.last_confidence_warning.uncertain_words}
            log.warning(
                "low-confidence medical tokens: count=%d categories=%s (original text preserved)",
                len(self.last_confidence_warning.uncertain_words),
                ",".join(sorted(categories)),
            )

        normalized = normalize(utterance.text)
        terminology_start = time.monotonic()
        # A low-confidence clinical token must not be transformed into a
        # more authoritative-looking concept. Preserve the provider text.
        rewritten = normalized if self.last_confidence_warning is not None else self.terminology.apply(normalized)
        terminology_end = time.monotonic()

        injected_repr = to_injected(rewritten)
        bidi_end = time.monotonic()

        self.injector.reset_partial()
        if self.settings.inject_mode == "type":
            ok = self.injector.type_text(injected_repr + " ")
        else:
            ok = self.injector.paste_text(injected_repr + " ", add_rtl_mark=True)
        injection_end = time.monotonic()
        if not ok:
            log.warning("injection failed for a final transcript")
            self._retain_failed(rewritten)
            self._injection_failed = True
            self._warn("درج متن ناموفق بود؛ متن بازیابی را بررسی کنید")
            self._on_provider_error(ProviderError(ErrorCategory.CONFIG, "Text injection failed; review recovery text"))
        elif self.last_confidence_warning is not None:
            self.overlay.set_done(rewritten)
            self._warn("واژه پزشکی با اطمینان پایین؛ متن درج‌شده را بررسی کنید")
        else:
            self.overlay.set_done(rewritten)

        log.info("latency: terminology=%.3fs bidi=%.3fs injection_acceptance=%.3fs",
                 terminology_end - terminology_start, bidi_end - terminology_end,
                 injection_end - bidi_end)
        self.latency.mark_final(
            terminology_s=terminology_end - terminology_start,
            bidi_s=bidi_end - terminology_end,
            injection_s=injection_end - bidi_end,
        )

    def _on_provider_error(self, error: ProviderError, generation: Optional[int] = None) -> None:
        if not self._is_current_session(generation):
            # Belongs to a session that was already torn down: reporting it
            # would make the *current* session fail for the previous one's
            # reason (and `run()` would then back off or give up on a
            # healthy connection).
            log.debug(
                "dropped a provider error from a superseded session: category=%s",
                error.category.value,
            )
            return
        log.error("provider error: category=%s message=%s", error.category.value, str(error))
        self._errors.append(error)
        self._stop.set()

    def _on_audio(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        """Real-time capture callback: enqueue and return.

        Runs on sounddevice's audio thread, so it only does O(1) work with
        no logging, no locks beyond the queue's own, and no exceptions.
        Anything worth reporting (queue overflow, device status) is turned
        into a flag here and logged by the sender thread.
        """
        if status:
            # Never log here: store the latest status string for the
            # sender thread to report (device overflow flags can repeat
            # hundreds of times per second).
            self._pending_input_status = str(status)
            self._input_status_pending.set()
        if not self._audio_q.put_nowait(bytes(indata)):
            self._audio_drop_pending.set()

    def _report_audio_health(self) -> None:
        """Log capture problems from a worker thread (never the callback)."""
        if self._input_status_pending.is_set():
            self._input_status_pending.clear()
            status, self._pending_input_status = self._pending_input_status, ""
            if status:
                log.warning("microphone status: %s", status)
        if self._audio_drop_pending.is_set():
            self._warn("بخشی از صدا از دست رفت؛ متن را بررسی کنید")
            self._audio_drop_pending.clear()
            stats = self._audio_q.stats()
            # Persisting overflow is reported at ERROR and flips the session
            # health flag; an isolated drop stays a warning. Silently
            # absorbing a sustained loss of audio is the failure mode this
            # is meant to prevent.
            level = logging.ERROR if stats.degraded else logging.WARNING
            log.log(
                level,
                "audio queue full: dropped_total=%d depth=%d consecutive=%d degraded=%s",
                stats.dropped_total, stats.depth, stats.consecutive_drops, stats.degraded,
            )

    def _reset_capture_watch(self) -> None:
        stats = self._audio_q.stats()
        self._captured_seen = stats.put_total + stats.dropped_total
        self._last_capture_at = time.monotonic()
        self._last_keep_alive_at = 0.0
        self._mic_silent = False
        # A verdict from the previous session; re-raised if still true.
        self._clear_warning(MIC_SILENT_WARNING)

    def _watch_capture(self, provider: STTProvider) -> None:
        """Detect a microphone that has stopped delivering frames.

        A device that goes quiet (unplugged, grabbed by another application,
        driver stall) produces no error here, only silence; Deepgram then
        closes the stream after ~10 s and the clinician would see nothing
        but an endless "reconnecting". The warning names the real cause and
        a KeepAlive holds the stream open; both are withdrawn as soon as
        frames arrive again. Runs on the session thread, never the callback.
        """
        stats = self._audio_q.stats()
        captured = stats.put_total + stats.dropped_total  # a dropped frame was still captured
        now = time.monotonic()
        if captured != self._captured_seen:
            self._captured_seen = captured
            self._last_capture_at = now
            if self._mic_silent:
                self._mic_silent = False
                log.info("microphone audio resumed")
                self._clear_warning(MIC_SILENT_WARNING)
            return
        if now - self._last_capture_at < AUDIO_SILENCE_SECONDS:
            return
        if not self._mic_silent:
            self._mic_silent = True
            log.warning("no audio captured for %.0fs; keeping the stream alive", AUDIO_SILENCE_SECONDS)
            self._warn(MIC_SILENT_WARNING)
        if now - self._last_keep_alive_at >= KEEP_ALIVE_INTERVAL_SECONDS:
            self._last_keep_alive_at = now
            try:
                provider.keep_alive()
            except ProviderError as exc:
                # A dead connection is reported by the provider thread itself.
                log.debug("keep-alive not sent: %s", exc)

    # -- session lifecycle -------------------------------------------------

    def _reset_session_state(self) -> None:
        """Drop everything that belongs to the previous session.

        * Buffered audio is discarded: it was captured before Stop and
          must not be streamed into the next session (which would produce
          a burst of stale transcript when it starts).
        * The utterance accumulator is cleared so a half-finished
          utterance cannot leak into the next one.
        * The injector's streaming delta is reset. This is the injection
          queue equivalent: injection itself is synchronous, but the
          injector's partial-hypothesis bookkeeping is what a later
          revision would try to backspace. Leaving it set would make the
          next session delete characters the user has since typed.
        """
        drained = self._audio_q.drain()
        if drained:
            log.info("discarded %d buffered audio chunk(s) on session reset", drained)
        self._utterance.reset()
        self.injector.reset_partial()
        self.latency = LatencyTracker()

    def _run_session(self) -> None:
        self._abandoned_workers = [worker for worker in self._abandoned_workers if worker.is_alive()]
        if len(self._abandoned_workers) >= 3:
            self._warn("اتصال قبلی پایان نیافته است؛ پس از رفع مشکل برنامه را باز کنید")
            raise ProviderError(ErrorCategory.CONFIG, "Too many abandoned network workers; restart after network recovery")
        sounddevice = load_sounddevice()
        s = self.settings
        if self._owns_provider and self._provider_used:
            self.provider = DeepgramProvider(s, self.keyterms, self._asr_replacements)
        self._provider_used = True
        provider = self.provider  # old workers must never send to a replacement provider
        provider.validate_config()
        provider.prepare()
        self._session_generation += 1
        generation = self._session_generation
        self.phase = "connecting"
        if not self._shutdown.is_set():
            self._stop.clear()
        self._errors.clear()
        self._reset_session_state()
        self._connected_at = None
        finals: queue.Queue[tuple[TranscriptEvent, LatencyTracker]] = queue.Queue(maxsize=16)
        receive_latency = LatencyTracker()
        processing_done = threading.Event()

        def on_error(error: ProviderError) -> None:
            self._on_provider_error(error, generation)

        def on_transcript(event: TranscriptEvent) -> None:
            nonlocal receive_latency
            if not self._is_current_session(generation):
                return
            if event.speech_started:
                receive_latency.mark_utterance_start(event.received_at)
                return
            # Interims are latest-value display data, not an unbounded backlog.
            if not event.is_final:
                receive_latency.mark_first_interim(event.received_at)
                self.overlay.set_partial(normalize(event.text[:8000]))
                return
            if len(event.text) > 8000 or len(event.words) > 4000:
                self._retain_failed(event.text)
                self._warn("گفتار بیش از حد طولانی است؛ متن بازیابی را بررسی کنید")
                on_error(ProviderError(ErrorCategory.CONFIG, "Oversized transcript; review recovery text"))
                return
            try:
                finals.put_nowait((event, receive_latency))
                if event.speech_final:
                    receive_latency = LatencyTracker()
            except queue.Full:
                self._retain_failed(event.text)
                self._warn("پردازش عقب افتاده است؛ متن بازیابی را بررسی کنید")
                on_error(ProviderError(ErrorCategory.CONFIG, "Transcript queue full; review recovery text"))

        def process_finals() -> None:
            while not processing_done.is_set() or not finals.empty():
                try:
                    event, tracker = finals.get(timeout=0.05)
                except queue.Empty:
                    continue
                try:
                    # This worker is fully drained before session state is reused.
                    self.latency = tracker
                    log.debug("latency: transcript_queue=%.3fs", time.monotonic() - event.received_at)
                    self._on_transcript(event)
                except Exception:
                    self._retain_failed(event.text)
                    self._injection_failed = True
                    self._warn("پردازش متن ناموفق بود؛ متن بازیابی را بررسی کنید")
                    self._on_provider_error(ProviderError(ErrorCategory.CONFIG, "Transcript processing failed; review recovery text"))
                finally:
                    finals.task_done()
            try:
                self._flush_pending_utterance()
            except Exception:
                self._on_provider_error(ProviderError(ErrorCategory.CONFIG, "Final transcript processing failed"))

        processor = threading.Thread(target=process_finals, daemon=True, name="transcript-worker")
        processor.start()

        def run_provider() -> None:
            try:
                provider.start(on_transcript, on_error)
            except ProviderError as exc:
                on_error(exc)
            except Exception:
                on_error(ProviderError(ErrorCategory.UNKNOWN, "Provider worker failed"))
            finally:
                if generation == self._session_generation:
                    self._stop.set()

        provider_thread = threading.Thread(target=run_provider, daemon=True, name="stt-provider")
        provider_thread.start()

        def send_audio() -> None:
            while generation == self._session_generation:
                try:
                    chunk = self._audio_q.get(timeout=0.1)
                except queue.Empty:
                    if self._stop.is_set():
                        return
                    continue
                if generation != self._session_generation:
                    self._audio_q.record_failed(chunk)
                    self._audio_q.task_done()
                    return
                try:
                    provider.send_audio(chunk)
                    self._audio_q.record_sent(chunk)
                except ProviderError as exc:
                    self._audio_q.record_failed(chunk)
                    on_error(exc)
                    return
                finally:
                    self._audio_q.task_done()

        sender: Optional[threading.Thread] = None
        try:
            # Margin over the host round trip: worst case is the host's own
            # grant budget (~7s) plus the SDK's handshake timeout (~10s and
            # not configurable from here), so 12s could undershoot a slow
            # but healthy start and report a phantom timeout.
            deadline = time.monotonic() + s.host_timeout_seconds + 15.0
            while not self._stop.is_set() and not self._shutdown.is_set():
                if provider.wait_ready(0.05):
                    break
                if time.monotonic() >= deadline:
                    on_error(ProviderError(ErrorCategory.TIMEOUT, "Provider readiness timed out"))
                    break
            if self._stop.is_set() or self._shutdown.is_set():
                if self._errors and not self._shutdown.is_set():
                    raise self._errors[0]
                return
            self._connected_at = time.monotonic()
            sender = threading.Thread(target=send_audio, daemon=True, name="audio-sender")
            sender.start()
            with sounddevice.RawInputStream(
                samplerate=s.sample_rate, blocksize=int(s.sample_rate * s.block_duration),
                channels=s.channels, dtype="int16", callback=self._on_audio,
            ):
                self.phase = "recording"
                self._clear_warning(CAPACITY_WARNING)
                self._reset_capture_watch()
                while not self._stop.wait(0.1) and not self._shutdown.is_set():
                    self._report_audio_health()  # independent of queue emptiness / blocked sends
                    self._watch_capture(provider)
        except KeyboardInterrupt:
            self.request_stop()
        except OSError as exc:
            on_error(ProviderError(ErrorCategory.MICROPHONE, "Microphone unavailable", cause=exc))
        finally:
            self.phase = "stopping"
            self._stop.set()
            if sender is not None:
                sender.join(timeout=SENDER_JOIN_TIMEOUT_SECONDS)
                if sender.is_alive():
                    log.warning("audio sender did not stop; aborting transport")
                    provider.abort()
            # Even a third-party provider's stop must not block teardown forever.
            def stop_provider() -> None:
                try:
                    provider.stop()
                except Exception:
                    log.warning("provider stop raised during shutdown")
            closer = threading.Thread(target=stop_provider, daemon=True, name="session-close")
            closer.start()
            closer.join(timeout=1.2)
            provider_thread.join(timeout=PROVIDER_JOIN_TIMEOUT_SECONDS)
            if provider_thread.is_alive() or closer.is_alive():
                log.warning("provider thread did not stop and is being abandoned")
                provider.abort()
            self._abandoned_workers.extend(
                worker for worker in (provider_thread, closer, sender)
                if worker is not None and worker.is_alive()
            )
            # Reject late callbacks now, including the entire backoff period.
            self._session_generation += 1
            processing_done.set()
            processor.join(timeout=4.0)
            if processor.is_alive():
                self._warn("درج متن هنوز پایان نیافته است؛ منتظر بمانید")
                # Native injection cannot be safely interrupted. Fail closed:
                # do not confirm Stop or launch a replacement while it can type.
                processor.join()
            self._reset_session_state()
            self.phase = "idle"
        if self._errors:
            raise self._errors[0]

    def run(self) -> int:
        s = self.settings
        log.info("Medical STT starting: model=%s language=%s", s.model, s.language)
        log.info("Host: %s (short-lived sessions; no Deepgram key on this machine)", s.host_url)
        log.info("Correction rules active: %d / %d total", self.terminology.rule_count, self.terminology.total_rule_count)
        log.info("Keyterms loaded: %d", len(self.keyterms))

        policy = ReconnectPolicy(
            base_delay=s.reconnect_delay,
            max_delay=s.reconnect_backoff_max,
            jitter=s.reconnect_jitter,
            max_attempts=s.max_reconnect_attempts,
        )

        self.overlay.set_idle()
        exit_code = 0
        consecutive_failures = 0

        try:
            while not self._shutdown.is_set():
                try:
                    self._run_session()
                    break
                except KeyboardInterrupt:
                    break
                except ProviderError as exc:
                    if self._shutdown.is_set():
                        # The user pressed Stop while the failure was being
                        # classified: that is a normal stop, not an error.
                        break
                    self._reconnect_count += 1
                    if self._connected_at is not None and time.monotonic() - self._connected_at >= 30.0:
                        consecutive_failures = 0
                    consecutive_failures += 1
                    self.phase = "reconnecting"
                    if exc.category is ErrorCategory.RATE_LIMIT:
                        # Deepgram's concurrent-stream cap (or the host's
                        # limiter) refused this device: say so instead of
                        # showing a generic "reconnecting" indefinitely.
                        self._warn(CAPACITY_WARNING)
                    else:
                        self._clear_warning(CAPACITY_WARNING)
                    # `exc.retry_after` is honored when the host sent one, so
                    # a rate-limited client slows down when told to instead of
                    # guessing -- which is what would otherwise turn 50
                    # clients into a synchronized retry storm.
                    decision = decide(
                        policy, exc, consecutive_failures, retry_after=exc.retry_after
                    )
                    if not decision.should_retry:
                        log.error("not retrying: %s", decision.reason)
                        exit_code = 1 if exc.category != ErrorCategory.SHUTDOWN else 0
                        break
                    log.warning(
                        "reconnecting: %s — retry in %.1fs (attempt %d)",
                        decision.reason, decision.delay_seconds, self._reconnect_count,
                    )
                    # Interruptible sleep: Stop must not wait out the backoff.
                    if self._shutdown.wait(decision.delay_seconds):
                        break
        finally:
            self.overlay.close()
            log.info("session ended")
        return exit_code


class ControllerState(enum.Enum):
    IDLE = "idle"
    RUNNING = "running"
    FAILED = "failed"


class SessionController:
    """Start/Stop lifecycle around `LiveMedicalSTT`, without any GUI.

    The UI owns one of these. `start()` is deliberately strict: a
    configuration or credential problem raises instead of silently
    retrying, because AUTH and CONFIG are never retryable by design.
    """

    def __init__(
        self,
        stt_factory: Callable[[], LiveMedicalSTT] = LiveMedicalSTT,
    ) -> None:
        self._stt_factory = stt_factory
        self._stt: Optional[LiveMedicalSTT] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._state = ControllerState.IDLE
        self._stop_requested = threading.Event()
        self.last_error: Optional[str] = None

    @property
    def state(self) -> ControllerState:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._state is ControllerState.RUNNING

    @property
    def health(self) -> Dict[str, Any]:
        return self._stt.session_health if self._stt is not None else {"phase": "idle"}

    def recovery_text(self) -> str:
        return self._stt.recovery_text() if self._stt is not None else ""

    def clear_recovery(self) -> None:
        if self._stt is not None:
            self._stt.clear_recovery()

    def start(self) -> None:
        """Start a dictation session. Raises on unusable configuration."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_requested.clear()
            # Built here (not in __init__) so a settings change is picked
            # up on the next Start, and so errors surface to the user at
            # the moment they press the button.
            if self._stt is not None and hasattr(self._stt, "recovery_text") and self._stt.recovery_text():
                raise ConfigError("Review and clear recovery text before starting another session")
            stt = self._stt_factory()
            self._stt = stt
            if self._stop_requested.is_set():
                stt.request_stop()
            self.last_error = None
            self._state = ControllerState.RUNNING

            def run() -> None:
                try:
                    code = stt.run()
                    self._state = ControllerState.IDLE if code == 0 else ControllerState.FAILED
                    if code != 0 and self.last_error is None:
                        self.last_error = getattr(stt, "warning", "") or "session ended with an error"
                except Exception as exc:  # noqa: BLE001 - surfaced in the UI
                    log.error("session failed: %s", exc)
                    self.last_error = str(exc)
                    self._state = ControllerState.FAILED
                finally:
                    with self._lock:
                        self._thread = None

            self._thread = threading.Thread(target=run, daemon=True, name="stt-session")
            self._thread.start()

    def cancel_start(self) -> None:
        """Nonblocking cancellation latch, including while the factory is running."""
        self._stop_requested.set()
        if self._stt is not None:
            self._stt.request_stop()

    def stop(self, timeout: float = 8.0) -> bool:
        """Stop the session and wait for a full clean shutdown.

        Returns True if the session thread finished within `timeout`.
        """
        self.cancel_start()
        stt = self._stt
        thread = self._thread
        if stt is not None:
            stt.request_stop()
        if thread is None:
            return True
        thread.join(timeout=timeout)
        finished = not thread.is_alive()
        if not finished:
            log.warning("session did not stop within %.1fs", timeout)
        return finished


def _configure_logging() -> None:
    """Log to a rotating file; stderr only when a console is attached.

    The packaged EXE runs without a console window, so the file in
    `%APPDATA%\\MedicalSTT\\logs\\app.log` is the only place a user can
    look for diagnostics. Nothing sensitive is ever logged (see
    README > Logging).
    """
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_handler: Optional[logging.Handler] = None
    try:
        paths.ensure_app_data_dir()
        file_handler = logging.handlers.RotatingFileHandler(
            paths.log_path(), maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
    except OSError as exc:  # pragma: no cover - unwritable profile
        root.warning("file logging unavailable: %s", exc)
    if file_handler is not None:
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    if sys.stderr is not None and getattr(sys.stderr, "isatty", lambda: False)():
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        root.addHandler(console)


def _print_version() -> None:
    from . import __version__

    version = f"Medical STT {__version__}"
    # The packaged EXE is built with --windows-console-mode=disable, where
    # sys.stdout is None. The build script verifies the bundle by exit code,
    # so this must never raise.
    if sys.stdout is not None:
        try:
            print(version)
        except (OSError, ValueError):  # pragma: no cover - detached handle
            pass
    try:
        paths.ensure_app_data_dir()
        with paths.log_path().open("a", encoding="utf-8") as fh:
            fh.write(f"{version}\n")
    except OSError:  # pragma: no cover - unwritable profile
        pass


def main(argv: Optional[List[str]] = None) -> int:
    """Start the floating Start/Stop control window.

    The window is the primary interface; a session only begins when the
    user presses Start, which is also what loads and validates
    configuration.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if "--version" in args:
        _print_version()
        return 0

    _configure_logging()

    instance = SingleInstance()
    if not instance.acquire():
        log.info("another instance is already running; exiting")
        return 0

    try:
        initialize_user_config()
        from .ui.control import ControlWindow

        window = ControlWindow(SessionController())
        window.run()
        return 0
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return 2
    except RuntimeError as exc:
        log.error("startup error: %s", exc)
        return 2
    finally:
        instance.release()


if __name__ == "__main__":
    sys.exit(main())
