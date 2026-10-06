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
from .stt.accumulator import UtteranceAccumulator
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
    """Lightweight end-to-end latency instrumentation.

    Never logs transcript content -- only durations and stage names -- so
    it is safe to enable at INFO level by default.
    """

    def __init__(self) -> None:
        self._capture_start: Optional[float] = None
        self._first_interim: Optional[float] = None
        self._speech_final: Optional[float] = None

    def mark_utterance_start(self) -> None:
        if self._capture_start is None:
            self._capture_start = time.monotonic()
            self._first_interim = None
            self._speech_final = None

    def mark_first_interim(self) -> None:
        if self._capture_start is not None and self._first_interim is None:
            self._first_interim = time.monotonic()
            log.debug("latency: first interim after %.3fs", self._first_interim - self._capture_start)

    def mark_speech_final(self) -> None:
        if self._capture_start is not None:
            self._speech_final = time.monotonic()

    def mark_final(self, terminology_s: float, bidi_s: float, injection_s: float) -> None:
        if self._capture_start is None:
            return
        now = time.monotonic()
        speech_final_s = (self._speech_final or now) - self._capture_start
        first_interim_s = None if self._first_interim is None else self._first_interim - self._capture_start
        log.info(
            "latency: total=%.3fs audio_to_first_interim=%s audio_to_speech_final=%.3fs "
            "terminology=%.3fs bidi=%.3fs injection=%.3fs",
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

        self._audio_q = BoundedAudioQueue(maxsize=40)
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
        }

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
        if not event.is_final:
            self.latency.mark_first_interim()
            self.overlay.set_partial(normalize(event.text))
            return

        utterance = self._utterance.add(event)
        if utterance is None:
            return

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
        self._finalize_utterance(pending)

    def _finalize_utterance(self, utterance: TranscriptEvent) -> None:
        """Run one completed utterance through terminology, BiDi, injection."""
        self.latency.mark_speech_final()
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

        self.overlay.set_done(rewritten)
        self.injector.reset_partial()
        ok = self.injector.paste_text(injected_repr + " ", add_rtl_mark=True)
        injection_end = time.monotonic()
        if not ok:
            log.warning("injection failed for a final transcript (see injector.last_error)")

        self.latency.mark_final(
            terminology_s=terminology_end - terminology_start,
            bidi_s=bidi_end - terminology_end,
            injection_s=injection_end - bidi_end,
        )
        time.sleep(0.08)
        self.overlay.set_idle()

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
        self.latency.mark_utterance_start()
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

    def _run_session(self) -> None:
        sounddevice = load_sounddevice()
        s = self.settings
        blocksize = int(s.sample_rate * s.block_duration)

        self.provider.validate_config()

        # Claim this session. Any thread still running for a previous one is
        # now stale and its callbacks are ignored (see `_is_current_session`).
        self._session_generation += 1
        generation = self._session_generation

        # A stop requested before the session started must not be cleared
        # here, otherwise it would be lost.
        if not self._shutdown.is_set():
            self._stop.clear()
        self._errors.clear()
        self._reset_session_state()
        self.latency.mark_utterance_start()

        def on_transcript(event: TranscriptEvent) -> None:
            self._on_transcript(event, generation)

        def on_provider_error(error: ProviderError) -> None:
            self._on_provider_error(error, generation)

        def run_provider() -> None:
            try:
                self.provider.start(on_transcript, on_provider_error)
            except ProviderError as exc:
                on_provider_error(exc)
            finally:
                if generation == self._session_generation:
                    self._stop.set()
                else:
                    # The join below timed out and this thread outlived the
                    # session it belonged to. Stopping `_stop` here would end
                    # the *next* session, which `run()` would then report as a
                    # clean exit -- the dictation would stop with no error.
                    log.warning(
                        "a provider thread from a previous session outlived its "
                        "join and is being abandoned; the current session is unaffected"
                    )

        provider_thread = threading.Thread(target=run_provider, daemon=True, name="stt-provider")
        provider_thread.start()

        def send_audio() -> None:
            """Stream queued audio until it is empty *and* the session is
            stopping.

            This is what makes shutdown lossless: when Stop sets `_stop`,
            the sender keeps sending whatever the capture callback already
            queued, and only exits once the queue is empty. The caller
            joins this thread *before* finalizing the Deepgram stream, so
            no captured audio can be dropped between Stop and Finalize.
            """
            while True:
                if generation != self._session_generation:
                    # Superseded: the audio queue belongs to a newer session
                    # now, so draining it here would feed one session's audio
                    # into another's connection.
                    return
                try:
                    chunk = self._audio_q.get(timeout=0.1)
                except queue.Empty:
                    self._report_audio_health()
                    if self._stop.is_set():
                        return
                    continue
                try:
                    self.provider.send_audio(chunk)
                except ProviderError as exc:
                    # The connection is unusable: report once and stop
                    # rather than reporting the same failure per chunk.
                    on_provider_error(exc)
                    self._audio_q.task_done()
                    return
                self._audio_q.task_done()

        sender = threading.Thread(target=send_audio, daemon=True, name="audio-sender")
        sender.start()

        try:
            with sounddevice.RawInputStream(
                samplerate=s.sample_rate,
                blocksize=blocksize,
                channels=s.channels,
                dtype="int16",
                callback=self._on_audio,
            ):
                while not self._stop.wait(0.1) and not self._shutdown.is_set():
                    pass
        except KeyboardInterrupt:
            log.info("shutdown requested by user")
            self.request_stop()
        except OSError as exc:
            # The `with` block above releases the microphone on the way out.
            self._errors.append(ProviderError(ErrorCategory.MICROPHONE, str(exc), cause=exc))
        finally:
            # Shutdown order matters; each step must finish before the next:
            #   1. the microphone is already released (the `with` block
            #      above exited), so no new audio can be captured;
            #   2. `_stop` lets the sender drain everything that was
            #      captured but not yet transmitted;
            #   3. joining the sender -- not finalizing first -- is what
            #      guarantees the last words reach Deepgram;
            #   4. Finalize + CloseStream, then wait for the provider
            #      thread, so the final transcript events are processed
            #      before the session state is reset;
            #   5. any utterance that Deepgram finalized but never closed
            #      with `speech_final` is injected now instead of being
            #      discarded;
            #   6. finally, reset the per-session state for the next Start.
            # Every step runs even if an earlier one raises: `stop()` and
            # the joins are best-effort during teardown, and letting one
            # exception skip the rest would leave threads, the queue, or
            # the injector in a stale state for the next Start.
            self._stop.set()
            sender.join(timeout=SENDER_JOIN_TIMEOUT_SECONDS)
            if sender.is_alive():
                log.warning(
                    "audio sender did not stop within %.1fs; queued audio is discarded",
                    SENDER_JOIN_TIMEOUT_SECONDS,
                )
            try:
                self.provider.stop()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                log.warning("provider stop raised during shutdown: %s", exc)
            try:
                provider_thread.join(timeout=PROVIDER_JOIN_TIMEOUT_SECONDS)
            except RuntimeError as exc:  # pragma: no cover - thread already gone
                log.debug("provider thread join skipped: %s", exc)
            if provider_thread.is_alive():
                # Reported, not swallowed: this thread is now abandoned and
                # outlives the session. Its callbacks are ignored from here on
                # (they carry an older `_session_generation`), so it can no
                # longer corrupt the next session -- but a leaked thread must
                # be visible in the log rather than discovered as a mystery
                # growth in thread count.
                log.warning(
                    "provider thread did not stop within %.1fs and is being "
                    "abandoned (a wedged provider socket); it will be ignored",
                    PROVIDER_JOIN_TIMEOUT_SECONDS,
                )
            self._flush_pending_utterance()
            self._reset_session_state()

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
                    # `exc.retry_after` is honored when the host sent one, so
                    # a rate-limited client slows down when told to instead of
                    # guessing -- which is what would otherwise turn 50
                    # clients into a synchronized retry storm.
                    decision = decide(
                        policy, exc, self._reconnect_count, retry_after=exc.retry_after
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
        self.last_error: Optional[str] = None

    @property
    def state(self) -> ControllerState:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._state is ControllerState.RUNNING

    def start(self) -> None:
        """Start a dictation session. Raises on unusable configuration."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            # Built here (not in __init__) so a settings change is picked
            # up on the next Start, and so errors surface to the user at
            # the moment they press the button.
            stt = self._stt_factory()
            self._stt = stt
            self.last_error = None
            self._state = ControllerState.RUNNING

            def run() -> None:
                try:
                    code = stt.run()
                    self._state = ControllerState.IDLE if code == 0 else ControllerState.FAILED
                    if code != 0 and self.last_error is None:
                        self.last_error = "session ended with an error"
                except Exception as exc:  # noqa: BLE001 - surfaced in the UI
                    log.error("session failed: %s", exc)
                    self.last_error = str(exc)
                    self._state = ControllerState.FAILED
                finally:
                    with self._lock:
                        self._thread = None

            self._thread = threading.Thread(target=run, daemon=True, name="stt-session")
            self._thread.start()

    def stop(self, timeout: float = 8.0) -> bool:
        """Stop the session and wait for a full clean shutdown.

        Returns True if the session thread finished within `timeout`.
        """
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
