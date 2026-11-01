"""Microphone access, isolated behind one factory.

`sounddevice` is imported lazily and its failure is translated into a message
a clinician can act on, so a missing PortAudio library surfaces as a clear
configuration error instead of an import traceback in a windowless EXE.

The module is the *only* owner of the import: everything else asks
`load_sounddevice()` for the module, which also makes the microphone
replaceable in tests without a sound card.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("medical_stt.audio")


def load_sounddevice() -> Any:
    """Return the `sounddevice` module or raise a readable RuntimeError."""
    try:
        import sounddevice
    except (ImportError, OSError) as exc:
        raise RuntimeError(
            "Microphone requires sounddevice.\n"
            "  pip install sounddevice\n"
            "  (Linux: sudo apt install libportaudio2)"
        ) from exc
    return sounddevice
