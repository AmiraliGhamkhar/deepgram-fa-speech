"""The contract between a running session and the object that owns it.

`LiveMedicalSTT` owns state (settings, provider, queue, counters); the worker
objects in this package *do* things with that state. Rather than letting each
worker reach into the owner ad hoc, they program against `SessionHost`: the
small, explicit surface a session worker is allowed to use.

Declaring it as a `Protocol` keeps the dependency pointing inwards -- the
worker modules import only this interface, so `LiveMedicalSTT` remains free to
change how it stores anything that is not listed here.
"""
from __future__ import annotations

import threading
from typing import Any, List, Optional, Protocol, runtime_checkable

from ..audio import BoundedAudioQueue
from ..config import Settings
from ..stt.base import ProviderError, STTProvider
from .audio_health import AudioHealth
from .transcripts import UtteranceRouter
from .watchdog import AudioWatchdog


@runtime_checkable
class SessionHost(Protocol):
    """What a session worker needs from its owner."""

    settings: Settings
    provider: STTProvider

    @property
    def audio_queue(self) -> BoundedAudioQueue:
        """The session's bounded audio queue."""

    @property
    def health(self) -> AudioHealth:
        """Capture-callback flag sink."""

    @property
    def watchdog(self) -> AudioWatchdog:
        """Microphone-silence watchdog."""

    @property
    def router(self) -> UtteranceRouter:
        """Transcript router (final utterances, interims, flush)."""

    @property
    def stop_event(self) -> threading.Event:
        """Set to end the current session."""

    @property
    def shutdown_event(self) -> threading.Event:
        """Set to end `run()` itself (Stop pressed)."""

    @property
    def errors(self) -> List[ProviderError]:
        """Terminal errors recorded for the current session."""

    @property
    def abandoned_workers(self) -> List[threading.Thread]:
        """Workers from a previous session that outlived their join."""

    @property
    def connected_at(self) -> Optional[float]:
        """Monotonic time the current stream became ready."""

    @connected_at.setter
    def connected_at(self, at: Optional[float]) -> None: ...

    @property
    def reconnect_count(self) -> int:
        """Number of reconnect attempts so far."""

    def note_reconnect(self) -> None:
        """Count one reconnect attempt."""

    @property
    def generation(self) -> int:
        """Identifier of the session currently owned by this host."""

    def begin_generation(self) -> int:
        """Move to a new session generation and return it."""

    def is_current_session(self, generation: Optional[int]) -> bool:
        """True when `generation` still refers to the running session."""

    def prepare_provider(self) -> STTProvider:
        """Return the provider for a new session (replacing a used one)."""

    def prepare_session_events(self) -> None:
        """Clear the per-session stop/error state before a session starts."""

    def prune_abandoned_workers(self) -> None:
        """Drop finished threads from the abandoned-worker list."""

    def on_audio(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        """sounddevice callback: enqueue one captured block."""

    def handle_provider_error(self, error: ProviderError, generation: Optional[int] = None) -> None:
        """Record a terminal error for the current session."""

    def warn(self, message: str) -> None:
        """Raise the user-visible warning line."""

    def clear_warning(self, message: str) -> None:
        """Withdraw `message` if it is the warning currently shown."""

    def set_phase(self, phase: str) -> None:
        """Publish the session phase (`connecting`, `recording`, ...)."""

    def retain_recovery(self, text: str) -> None:
        """Keep text that could not be injected, for explicit user recovery."""

    def reset_session_state(self) -> None:
        """Drop everything that belongs to the previous session."""

    def request_stop(self) -> None:
        """Ask the current session to shut down cleanly."""

    def microphone_factory(self) -> Any:
        """Return the module that provides `RawInputStream`."""
