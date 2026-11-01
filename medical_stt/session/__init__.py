"""Session layer: one dictation session's state, threads, and lifecycle.

The objects here are deliberately small and single-purpose so that
`dictation.LiveMedicalSTT` only has to *compose* them:

==============================  ================================================
`latency.LatencyTracker`        per-utterance stage timing (never transcript text)
`recovery.RecoveryBuffer`       bounded in-memory "could not inject" text
`warnings.WarningBoard`         the one user-visible warning line
`audio_health.AudioHealth`      capture-callback flags, drained on a worker thread
`watchdog.AudioWatchdog`        microphone-silence detection + KeepAlive scheduling
`runtime.SessionHost`           the contract a session worker depends on
`transcripts.UtteranceRouter`   final/interim routing and accumulation
`renderer.UtteranceRenderer`    utterance -> injected text (or recovery)
`runner.SessionRunner`          one session: provider, sender, processor threads
`supervisor.ReconnectSupervisor` retry/backoff decisions across sessions
`controller.SessionController`  Start/Stop lifecycle, no GUI
`dictation.LiveMedicalSTT`      composition root + session state owner
"""
from __future__ import annotations

__all__ = [
    "audio_health",
    "controller",
    "dictation",
    "latency",
    "messages",
    "recovery",
    "renderer",
    "runner",
    "runtime",
    "supervisor",
    "transcripts",
    "warnings",
    "watchdog",
]
