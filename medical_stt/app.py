"""Medical STT – real-time Persian + English medical dictation.

Pipeline: microphone -> STTProvider (Deepgram, via the self-hosted
session service) -> deterministic text pipeline -> injection -> overlay.

This module is the **entry point and public facade**, deliberately thin:

* `main()` wires process-level concerns (logging, single instance, first-run
  configuration, the control window) and nothing else;
* the names below are re-exported so that callers -- including tests and the
  packaging scripts -- keep a single stable import surface.

Where the behaviour actually lives:

===============================  ==============================================
`session/dictation.LiveMedicalSTT`  owns one dictation's state and composes it
`session/runner.SessionRunner`      one session's threads and teardown order
`session/supervisor`                retry/backoff policy between sessions
`session/transcripts`               provider events -> utterances
`session/renderer`                  utterance -> injected text (or recovery)
`processing/pipeline`               normalize -> confidence guard -> terminology
`injection/strategies`              type vs paste delivery
`session/controller`                Start/Stop lifecycle, no GUI
`session/watchdog`, `session/audio_health`  capture-quality reporting
`diagnostics`                       logging setup and version reporting
===============================  ==============================================
"""
from __future__ import annotations

import logging
import sys
from typing import List, Optional

from .app_instance import SingleInstance
from .config import ConfigError, initialize_user_config
from .diagnostics import configure_logging, print_version
from .session.controller import ControllerState, SessionController
from .session.dictation import LiveMedicalSTT
from .session.latency import LatencyTracker, StageTimings
from .session.messages import (
    CAPACITY_WARNING,
    DATA_LOSS_WARNING,
    MICROPHONE_UNAVAILABLE_WARNING,
    MIC_SILENT_WARNING,
)
from .session.recovery import RecoveryBuffer
from .session.warnings import WarningBoard

log = logging.getLogger("medical_stt.app")

__all__ = [
    "CAPACITY_WARNING",
    "ControllerState",
    "DATA_LOSS_WARNING",
    "LatencyTracker",
    "LiveMedicalSTT",
    "MICROPHONE_UNAVAILABLE_WARNING",
    "MIC_SILENT_WARNING",
    "RecoveryBuffer",
    "SessionController",
    "StageTimings",
    "WarningBoard",
    "main",
]


def main(argv: Optional[List[str]] = None) -> int:
    """Start the floating Start/Stop control window.

    The window is the primary interface; a session only begins when the user
    presses Start, which is also what loads and validates configuration.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if "--version" in args:
        print_version()
        return 0

    configure_logging()

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
