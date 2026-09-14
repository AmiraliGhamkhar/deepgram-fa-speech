"""
Medical STT – real-time Persian + English medical dictation.

Mic → Deepgram Nova-3 → normalize → FST corrections → overlay + inject.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from typing import Any, List, Optional

from deepgram import DeepgramClient
from deepgram.core.events import EventType
from deepgram.listen.v1.types import ListenV1Results

from .config import get_settings, load_correction_rules, load_keyterms
from .fst import load_rules_from_pairs
from .injector import TextInjector
from .normalize import normalize
from .overlay import TranscriptOverlay


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


class LiveMedicalSTT:
    def __init__(self) -> None:
        self.settings = get_settings()
        if not self.settings["api_key"]:
            raise RuntimeError("DEEPGRAM_API_KEY is not set. Put it in .env or the environment.")

        rules = load_correction_rules()
        self.fst = load_rules_from_pairs(rules)
        self.keyterms = load_keyterms()

        self.injector = TextInjector(
            dry_run=False,
            enable_smart_rewrite=True,
            restore_clipboard=self.settings["restore_clipboard"],
            paste_settle_seconds=self.settings["paste_settle_seconds"],
        )
        self.overlay = TranscriptOverlay(enabled=self.settings["overlay_enabled"])

        self._audio_q: queue.Queue[bytes] = queue.Queue(maxsize=40)
        self._stop = threading.Event()
        self._closed = threading.Event()
        self._errors: List[Exception] = []
        self._reconnect_count = 0

    # ── Deepgram callbacks ──────────────────────────────────────────────

    def _on_message(self, message: object) -> None:
        if not isinstance(message, ListenV1Results):
            return
        if message.channel is None or not message.channel.alternatives:
            return

        raw = (message.channel.alternatives[0].transcript or "").strip()
        if not raw:
            return

        is_final = bool(message.is_final)

        if is_final:
            text = self.fst.apply(normalize(raw))
            self.overlay.set_done(text)
            self.injector.reset_partial()
            if self.settings["inject_mode"] == "paste":
                self.injector.paste_text(text + " ", add_rtl_mark=True)
            else:
                self.injector.type_text(text + " ")
            time.sleep(0.08)
            self.overlay.set_idle()
        else:
            # Partial: light normalize only (FST can wait for final)
            self.overlay.set_partial(normalize(raw))

    def _on_error(self, error: Exception) -> None:
        print(f"[Deepgram error] {error}", file=sys.stderr)
        self._errors.append(error)
        self._stop.set()

    def _on_close(self, _: object) -> None:
        self._closed.set()
        self._stop.set()

    def _on_audio(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        if status:
            print(f"[mic] {status}", file=sys.stderr)
        try:
            self._audio_q.put_nowait(bytes(indata))
        except queue.Full:
            pass

    # ── single connection session ───────────────────────────────────────

    def _run_session(self) -> None:
        sounddevice = load_sounddevice()
        s = self.settings
        blocksize = int(s["sample_rate"] * s["block_duration"])
        client = DeepgramClient(api_key=s["api_key"])

        self._stop.clear()
        self._closed.clear()
        self._errors.clear()

        with client.listen.v1.connect(
            model=s["model"],
            language=s["language"],
            encoding="linear16",
            sample_rate=s["sample_rate"],
            channels=s["channels"],
            interim_results=True,
            endpointing=s["endpointing"],
            utterance_end_ms=s["utterance_end_ms"],
            vad_events=True,
            smart_format=True,
            punctuate=True,
            keyterm=self.keyterms or None,
        ) as connection:

            connection.on(EventType.OPEN, lambda _: print("● Connected to Deepgram"))
            connection.on(EventType.MESSAGE, self._on_message)
            connection.on(EventType.ERROR, self._on_error)
            connection.on(EventType.CLOSE, self._on_close)

            listener = threading.Thread(target=connection.start_listening, daemon=True)
            listener.start()

            def send_audio() -> None:
                while not self._stop.is_set():
                    try:
                        chunk = self._audio_q.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    try:
                        connection.send_media(chunk)
                    except Exception as e:
                        self._errors.append(e)
                        self._stop.set()
                    finally:
                        self._audio_q.task_done()

            sender = threading.Thread(target=send_audio, daemon=True)
            sender.start()

            try:
                with sounddevice.RawInputStream(
                    samplerate=s["sample_rate"],
                    blocksize=blocksize,
                    channels=s["channels"],
                    dtype="int16",
                    callback=self._on_audio,
                ):
                    while not self._stop.wait(0.1):
                        if self._errors:
                            raise self._errors[0]
            except KeyboardInterrupt:
                print("\nStopping…")
                self._stop.set()
                raise
            finally:
                self._stop.set()
                sender.join(timeout=2.0)
                if not self._closed.is_set():
                    try:
                        connection.send_finalize()
                        connection.send_close_stream()
                    except Exception:
                        pass
                listener.join(timeout=4.0)

            if self._errors:
                raise self._errors[0]

    # ── outer loop with reconnection ────────────────────────────────────

    def run(self) -> int:
        s = self.settings
        print("=" * 60)
        print("Medical STT – Finite State Transducer post-processing")
        print(f"Model: {s['model']}  Language: {s['language']}")
        print(f"Correction rules loaded: {len(self.fst._outputs)}")
        print(f"Keyterms: {len(self.keyterms)}")
        print("Press Ctrl+C to stop.")
        print("=" * 60)

        self.overlay.set_idle()
        exit_code = 0

        try:
            while True:
                try:
                    self._run_session()
                    break  # clean exit
                except KeyboardInterrupt:
                    break
                except Exception as e:
                    self._reconnect_count += 1
                    max_attempts = s["max_reconnect_attempts"]
                    if max_attempts and self._reconnect_count > max_attempts:
                        print(f"Fatal: max reconnect attempts ({max_attempts}) reached.", file=sys.stderr)
                        exit_code = 1
                        break
                    delay = s["reconnect_delay"]
                    print(
                        f"[reconnect] {type(e).__name__}: {e} — retry in {delay}s "
                        f"(attempt {self._reconnect_count})",
                        file=sys.stderr,
                    )
                    time.sleep(delay)
        finally:
            self.overlay.close()
            print("Session ended.")
        return exit_code


def main() -> int:
    try:
        return LiveMedicalSTT().run()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
