"""Start/Stop lifecycle around one dictation, without any GUI.

The control window talks to this object and nothing else, so the window can be
replaced (or driven from a test) without touching the session machinery.

`start()` is deliberately strict: a configuration or credential problem raises
instead of silently retrying, because AUTH and CONFIG are never retryable by
design.
"""
from __future__ import annotations

import enum
import logging
import threading
from typing import Any, Callable, Dict, Optional

from ..config import ConfigError

log = logging.getLogger("medical_stt.app")


class ControllerState(enum.Enum):
    IDLE = "idle"
    RUNNING = "running"
    FAILED = "failed"


def _default_stt_factory() -> Any:
    """Build the production dictation object.

    Imported lazily: `dictation` wires the whole pipeline, and importing it
    from here at module load time would make this module (and the UI that
    imports it) depend on the audio/STT stack.
    """
    from .dictation import LiveMedicalSTT

    return LiveMedicalSTT()


class SessionController:
    """Start/Stop lifecycle around `LiveMedicalSTT`, without any GUI."""

    def __init__(
        self,
        stt_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._stt_factory = stt_factory or _default_stt_factory
        self._stt: Optional[Any] = None
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
            # Built here (not in __init__) so a settings change is picked up on
            # the next Start, and so errors surface to the user at the moment
            # they press the button.
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
        # start() swaps _stt and _thread together under this lock. Take a
        # consistent snapshot there too; otherwise a rapid Start→Stop can join
        # a stale thread (or None) while a new session spins up. The lock is
        # released before join(): never hold it across a wait.
        with self._lock:
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
