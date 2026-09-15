"""Medical STT – real-time Persian + English medical dictation.

Pipeline: microphone -> STTProvider (Deepgram) -> normalization ->
terminology engine -> BiDi formatting -> text injection -> overlay.

Only this module wires the concrete pieces together; every other module is
independently testable (see tests/).
"""
from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from typing import Any, List, Optional

from .audio import BoundedAudioQueue
from .config import ConfigError, Settings, get_settings, load_correction_rules_raw, load_keyterms, validate_settings
from .injection import TextInjector
from .processing.bidi import to_injected
from .processing.normalize import normalize
from .processing.terminology import TerminologyEngine
from .stt.base import ErrorCategory, ProviderError, STTProvider, TranscriptEvent
from .stt.deepgram_provider import DeepgramProvider
from .stt.reconnect import ReconnectPolicy, decide
from .ui import TranscriptOverlay

log = logging.getLogger("medical_stt.app")


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

    def mark_utterance_start(self) -> None:
        self._capture_start = time.monotonic()
        self._first_interim = None

    def mark_first_interim(self) -> None:
        if self._capture_start is not None and self._first_interim is None:
            self._first_interim = time.monotonic()
            log.debug("latency: first interim after %.3fs", self._first_interim - self._capture_start)

    def mark_final(self, terminology_s: float, bidi_s: float, injection_s: float) -> None:
        if self._capture_start is None:
            return
        total = time.monotonic() - self._capture_start
        log.info(
            "latency: total=%.3fs terminology=%.3fs bidi=%.3fs injection=%.3fs",
            total, terminology_s, bidi_s, injection_s,
        )
        self._capture_start = None


class LiveMedicalSTT:
    def __init__(self, provider: Optional[STTProvider] = None) -> None:
        self.settings: Settings = get_settings()
        errors = validate_settings(self.settings)
        # DEEPGRAM_API_KEY missing is reported as a normal validation error
        # so it's never accidentally logged with its value anywhere.
        if errors:
            raise ConfigError("; ".join(errors))

        raw_rules = load_correction_rules_raw()
        self.terminology = TerminologyEngine(
            enable_context_dependent=self.settings.enable_context_dependent_terms
        )
        load_result = self.terminology.load(raw_rules)
        if load_result.conflicts:
            log.warning("terminology rule conflicts detected: %d", len(load_result.conflicts))

        self.keyterms = load_keyterms()
        self.provider: STTProvider = provider or DeepgramProvider(self.settings, self.keyterms)

        self.injector = TextInjector(
            dry_run=False,
            enable_smart_rewrite=True,
            restore_clipboard=self.settings.restore_clipboard,
            paste_settle_seconds=self.settings.paste_settle_seconds,
        )
        self.overlay = TranscriptOverlay(enabled=self.settings.overlay_enabled)
        self.latency = LatencyTracker()

        self._audio_q = BoundedAudioQueue(maxsize=40)
        self._stop = threading.Event()
        self._errors: List[ProviderError] = []
        self._reconnect_count = 0
        self._last_drop_logged = 0

    # -- transcript handling --------------------------------------------

    def _on_transcript(self, event: TranscriptEvent) -> None:
        if not event.is_final:
            self.latency.mark_first_interim()
            normalized = normalize(event.text)
            self.overlay.set_partial(normalized)
            return

        t0 = time.monotonic()
        normalized = normalize(event.text)
        rewritten = self.terminology.apply(normalized)
        t1 = time.monotonic()

        injected_repr = to_injected(rewritten)
        t2 = time.monotonic()

        self.overlay.set_done(rewritten)
        self.injector.reset_partial()
        ok = self.injector.paste_text(injected_repr + " ", add_rtl_mark=True)
        t3 = time.monotonic()
        if not ok:
            log.warning("injection failed for a final transcript (see injector.last_error)")

        self.latency.mark_final(
            terminology_s=t1 - t0,
            bidi_s=t2 - t1,
            injection_s=t3 - t2,
        )
        time.sleep(0.08)
        self.overlay.set_idle()

    def _on_provider_error(self, error: ProviderError) -> None:
        log.error("provider error: category=%s message=%s", error.category.value, str(error))
        self._errors.append(error)
        self._stop.set()

    def _on_audio(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        if status:
            log.warning("microphone status: %s", status)
        ok = self._audio_q.put_nowait(bytes(indata))
        if not ok:
            stats = self._audio_q.stats()
            # Rate-limit the warning so a sustained overload doesn't flood
            # the log from the real-time audio thread's perspective (the
            # log call itself happens on the sender thread via stats, not
            # here, but we still keep this branch cheap).
            if stats.dropped_total != self._last_drop_logged:
                self._last_drop_logged = stats.dropped_total
                log.warning("audio queue full: dropped_total=%d depth=%d", stats.dropped_total, stats.depth)

    # -- session lifecycle -------------------------------------------------

    def _run_session(self) -> None:
        sounddevice = load_sounddevice()
        s = self.settings
        blocksize = int(s.sample_rate * s.block_duration)

        self.provider.validate_config()

        self._stop.clear()
        self._errors.clear()
        self.latency.mark_utterance_start()

        session_stopped = threading.Event()

        def run_provider() -> None:
            try:
                self.provider.start(self._on_transcript, self._on_provider_error)
            except ProviderError as exc:
                self._on_provider_error(exc)
            finally:
                session_stopped.set()
                self._stop.set()

        provider_thread = threading.Thread(target=run_provider, daemon=True, name="stt-provider")
        provider_thread.start()

        def send_audio() -> None:
            while not self._stop.is_set():
                try:
                    chunk = self._audio_q.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    self.provider.send_audio(chunk)
                except ProviderError as exc:
                    self._on_provider_error(exc)
                finally:
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
                while not self._stop.wait(0.1):
                    pass
        except KeyboardInterrupt:
            log.info("shutdown requested by user")
            self._stop.set()
            raise
        except OSError as exc:
            self._errors.append(ProviderError(ErrorCategory.MICROPHONE, str(exc), cause=exc))
        finally:
            self._stop.set()
            self.provider.stop()
            sender.join(timeout=2.0)
            provider_thread.join(timeout=4.0)

        if self._errors:
            raise self._errors[0]

    def run(self) -> int:
        s = self.settings
        log.info("Medical STT starting: model=%s language=%s", s.model, s.language)
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
            while True:
                try:
                    self._run_session()
                    break
                except KeyboardInterrupt:
                    break
                except ProviderError as exc:
                    self._reconnect_count += 1
                    decision = decide(policy, exc, self._reconnect_count)
                    if not decision.should_retry:
                        log.error("not retrying: %s", decision.reason)
                        exit_code = 1 if exc.category != ErrorCategory.SHUTDOWN else 0
                        break
                    log.warning(
                        "reconnecting: %s — retry in %.1fs (attempt %d)",
                        decision.reason, decision.delay_seconds, self._reconnect_count,
                    )
                    time.sleep(decision.delay_seconds)
        finally:
            self.overlay.close()
            log.info("session ended")
        return exit_code


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def main() -> int:
    _configure_logging()
    try:
        return LiveMedicalSTT().run()
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return 2
    except RuntimeError as exc:
        log.error("startup error: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
