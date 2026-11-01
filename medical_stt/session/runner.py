"""One dictation session: microphone in, transcripts out, nothing leaked.

`SessionRunner` owns the four threads a session needs and their teardown
order -- capture (sounddevice's own thread), the provider's receive loop, the
audio sender, and the final-transcript processor -- plus the generation fence
that stops a worker from a previous session from touching the current one.

It deliberately owns **no policy**: whether a failure is retried belongs to
`supervisor.ReconnectSupervisor`, what a final transcript becomes belongs to
`transcripts.UtteranceRouter`, and where state lives belongs to the
`SessionHost`. That keeps this module readable as what it is: a thread
lifecycle.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable, Optional, Tuple

from ..stt.base import ErrorCategory, ProviderError, STTProvider, TranscriptEvent
from .latency import LatencyTracker
from .messages import (
    ABANDONED_WORKERS_WARNING,
    CAPACITY_WARNING,
    INJECTION_PENDING_WARNING,
    OVERSIZED_UTTERANCE_WARNING,
    TRANSCRIPT_QUEUE_FULL_WARNING,
)
from .runtime import SessionHost

log = logging.getLogger("medical_stt.app")

#: How long shutdown waits for the audio sender to drain the queue before
#: giving up on whatever is left (discarded, and reported in the log).
SENDER_JOIN_TIMEOUT_SECONDS = 2.0

#: How long shutdown waits for the provider thread to return after
#: `provider.stop()`. A wedged provider socket outlives this; the thread is
#: then abandoned rather than holding the user's Stop button open. Abandoned
#: threads carry an older session generation, so their callbacks cannot reach
#: the session that follows them.
PROVIDER_JOIN_TIMEOUT_SECONDS = 4.0

#: How long the final-transcript processor may take to drain before the runner
#: says so. Injection itself is synchronous and cannot be interrupted, so the
#: runner then waits for it rather than launching a second dictation.
PROCESSOR_JOIN_TIMEOUT_SECONDS = 4.0

#: Bounded hand-off between the provider thread and the processing thread. A
#: full queue means local processing cannot keep up with the provider.
FINALS_QUEUE_SIZE = 16

#: A single provider result larger than this cannot be one utterance.
MAX_FINAL_TEXT_CHARS = 8000
MAX_FINAL_WORDS = 4000

#: Abandoned network workers beyond this mean teardown is not working at all.
MAX_ABANDONED_WORKERS = 3

#: Margin over the host round trip: worst case is the host's own grant budget
#: (~7s) plus the SDK's handshake timeout (~10s and not configurable from
#: here), so 12s could undershoot a slow but healthy start.
READINESS_MARGIN_SECONDS = 15.0


class SessionRunner:
    """Runs exactly one session against a `SessionHost`."""

    def __init__(self, host: SessionHost) -> None:
        self._host = host

    # -- public API --------------------------------------------------------

    def run_once(self) -> None:
        """Run one session to completion.

        Returns normally for a clean stop (Stop pressed, or the stream ended
        after a shutdown request) and raises the session's `ProviderError`
        when it ended with a terminal failure, so the supervisor decides what
        happens next.
        """
        host = self._host
        host.prune_abandoned_workers()
        if len(host.abandoned_workers) >= MAX_ABANDONED_WORKERS:
            host.warn(ABANDONED_WORKERS_WARNING)
            raise ProviderError(
                ErrorCategory.CONFIG,
                "Too many abandoned network workers; restart after network recovery",
            )

        sounddevice = host.microphone_factory()
        settings = host.settings
        provider = host.prepare_provider()
        provider.validate_config()
        provider.prepare()

        generation = host.begin_generation()
        host.set_phase("connecting")
        host.prepare_session_events()
        host.reset_session_state()
        host.connected_at = None

        finals: "queue.Queue[Tuple[TranscriptEvent, LatencyTracker]]" = queue.Queue(
            maxsize=FINALS_QUEUE_SIZE
        )
        receive_latency = LatencyTracker()
        processing_done = threading.Event()
        error_emitted = [False]

        def on_error(error: ProviderError) -> None:
            # One terminal error per session: the SDK emits ERROR and then
            # CLOSE for a single disconnect.
            error_emitted[0] = True
            try:
                host.handle_provider_error(error, generation)
            except Exception as exc:  # noqa: BLE001 - never escape on the SDK thread
                log.error("Provider error callback failed (%s)", type(exc).__name__)

        def handle_transcript(event: TranscriptEvent) -> None:
            nonlocal receive_latency
            if not host.is_current_session(generation):
                return
            if event.speech_started:
                receive_latency.mark_utterance_start(event.received_at)
                return
            # Interims are latest-value display data, not an unbounded backlog.
            if not event.is_final:
                receive_latency.mark_first_interim(event.received_at)
                host.router.display_interim(event)
                return
            if len(event.text) > MAX_FINAL_TEXT_CHARS or len(event.words) > MAX_FINAL_WORDS:
                host.retain_recovery(event.text)
                host.warn(OVERSIZED_UTTERANCE_WARNING)
                on_error(ProviderError(ErrorCategory.CONFIG, "Oversized transcript; review recovery text"))
                return
            tracker = receive_latency
            # Rotate before the bounded hand-off below: one utterance's marks
            # must never be attached to the next one.
            if event.speech_final:
                receive_latency = LatencyTracker()
            try:
                finals.put_nowait((event, tracker))
            except queue.Full:
                host.retain_recovery(event.text)
                host.warn(TRANSCRIPT_QUEUE_FULL_WARNING)
                on_error(ProviderError(ErrorCategory.CONFIG, "Transcript queue full; review recovery text"))

        def on_transcript(event: TranscriptEvent) -> None:
            # SDK emitters do not isolate handlers. Never turn a local UI
            # failure into an upstream ERROR/reconnect cycle, or log PHI.
            try:
                handle_transcript(event)
            except Exception as exc:  # noqa: BLE001 - see comment above
                log.error("Transcript callback failed (%s)", type(exc).__name__)

        def process_finals() -> None:
            pending_tracker = receive_latency
            while not processing_done.is_set() or not finals.empty():
                try:
                    event, tracker = finals.get(timeout=0.05)
                except queue.Empty:
                    continue
                try:
                    # Pass the tracker explicitly: assigning a shared one here
                    # races with session reset and with other finals.
                    pending_tracker = tracker
                    log.debug("latency: transcript_queue=%.3fs", time.monotonic() - event.received_at)
                    host.router.handle(event, generation, tracker)
                except Exception:  # noqa: BLE001 - the utterance must survive
                    host.retain_recovery(event.text)
                    host.router.set_injection_failed(True)
                    host.warn(TRANSCRIPT_QUEUE_FULL_WARNING)
                    host.handle_provider_error(
                        ProviderError(ErrorCategory.CONFIG, "Transcript processing failed; review recovery text"),
                        generation,
                    )
                finally:
                    finals.task_done()
            try:
                host.router.flush_pending(pending_tracker)
            except Exception:  # noqa: BLE001 - last words must not crash teardown
                host.handle_provider_error(
                    ProviderError(ErrorCategory.CONFIG, "Final transcript processing failed"), generation
                )

        processor = threading.Thread(target=process_finals, daemon=True, name="transcript-worker")
        processor.start()

        def run_provider() -> None:
            try:
                provider.start(on_transcript, on_error)
            except ProviderError as exc:
                on_error(exc)
            except Exception:  # noqa: BLE001 - classified below
                on_error(ProviderError(ErrorCategory.UNKNOWN, "Provider worker failed"))
            finally:
                if generation == host.generation:
                    # A start() that just returns is a dead dictation, not a
                    # clean stop. Emit the error through the generation-fenced
                    # callback BEFORE setting the stop event, so the session
                    # loop and the supervisor classify it instead of reporting
                    # success. A deliberate Stop, an already-reported error, or
                    # a superseded generation must not produce a second error.
                    if not error_emitted[0] and not host.errors and not host.stop_event.is_set() \
                            and not host.shutdown_event.is_set():
                        on_error(ProviderError(ErrorCategory.UNKNOWN, "provider exited without error or stop"))
                    host.stop_event.set()

        provider_thread = threading.Thread(target=run_provider, daemon=True, name="stt-provider")
        provider_thread.start()

        def send_audio() -> None:
            while generation == host.generation:
                try:
                    chunk = host.audio_queue.get(timeout=0.1)
                except queue.Empty:
                    if host.stop_event.is_set():
                        return
                    continue
                if generation != host.generation:
                    host.audio_queue.record_failed(chunk)
                    host.audio_queue.task_done()
                    return
                try:
                    provider.send_audio(chunk)
                    host.audio_queue.record_sent(chunk)
                except ProviderError as exc:
                    host.audio_queue.record_failed(chunk)
                    on_error(exc)
                    return
                finally:
                    host.audio_queue.task_done()

        sender: Optional[threading.Thread] = None
        try:
            if not self._wait_until_ready(provider, settings.host_timeout_seconds, on_error):
                if host.errors and not host.shutdown_event.is_set():
                    raise host.errors[0]
                return
            host.connected_at = time.monotonic()
            sender = threading.Thread(target=send_audio, daemon=True, name="audio-sender")
            sender.start()
            with sounddevice.RawInputStream(
                samplerate=settings.sample_rate,
                blocksize=int(settings.sample_rate * settings.block_duration),
                channels=settings.channels,
                dtype="int16",
                callback=host.on_audio,
            ):
                host.set_phase("recording")
                host.clear_warning(CAPACITY_WARNING)
                host.watchdog.reset()
                while not host.stop_event.wait(0.1) and not host.shutdown_event.is_set():
                    host.health.flush()  # independent of queue emptiness / blocked sends
                    host.watchdog.check(provider.keep_alive)
        except KeyboardInterrupt:
            host.request_stop()
        except OSError as exc:
            on_error(ProviderError(ErrorCategory.MICROPHONE, "Microphone unavailable", cause=exc))
        finally:
            self._teardown(provider, provider_thread, processor, processing_done, sender)
        if host.errors:
            raise host.errors[0]

    # -- internals ---------------------------------------------------------

    def _wait_until_ready(
        self,
        provider: STTProvider,
        host_timeout_seconds: float,
        on_error: Callable[[ProviderError], None],
    ) -> bool:
        host = self._host
        deadline = time.monotonic() + host_timeout_seconds + READINESS_MARGIN_SECONDS
        while not host.stop_event.is_set() and not host.shutdown_event.is_set():
            if provider.wait_ready(0.05):
                return True
            if time.monotonic() >= deadline:
                on_error(ProviderError(ErrorCategory.TIMEOUT, "Provider readiness timed out"))
                return False
        return False

    def _teardown(
        self,
        provider: STTProvider,
        provider_thread: threading.Thread,
        processor: threading.Thread,
        processing_done: threading.Event,
        sender: Optional[threading.Thread],
    ) -> None:
        """Stop every worker in an order that cannot lose the last utterance."""
        host = self._host
        host.set_phase("stopping")
        host.stop_event.set()

        if sender is not None:
            sender.join(timeout=SENDER_JOIN_TIMEOUT_SECONDS)
            if sender.is_alive():
                log.warning("audio sender did not stop; aborting transport")
                provider.abort()

        # Even a third-party provider's stop must not block teardown forever.
        def stop_provider() -> None:
            try:
                provider.stop()
            except Exception:  # noqa: BLE001 - teardown must complete
                log.warning("provider stop raised during shutdown")

        closer = threading.Thread(target=stop_provider, daemon=True, name="session-close")
        closer.start()
        closer.join(timeout=1.2)
        provider_thread.join(timeout=PROVIDER_JOIN_TIMEOUT_SECONDS)
        if provider_thread.is_alive() or closer.is_alive():
            log.warning("provider thread did not stop and is being abandoned")
            provider.abort()
        host.abandoned_workers.extend(
            worker for worker in (provider_thread, closer, sender)
            if worker is not None and worker.is_alive()
        )

        # Reject late callbacks now, including the entire backoff period.
        host.begin_generation()
        processing_done.set()
        processor.join(timeout=PROCESSOR_JOIN_TIMEOUT_SECONDS)
        if processor.is_alive():
            host.warn(INJECTION_PENDING_WARNING)
            # Native injection cannot be safely interrupted. Fail closed: do
            # not confirm Stop or launch a replacement while it can type.
            processor.join()

        host.reset_session_state()
        host.set_phase("idle")
