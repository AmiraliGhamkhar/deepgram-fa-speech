import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Ensure tests never accidentally pick up a real API key from the developer's
# environment or a local .env file.
os.environ.pop("DEEPGRAM_API_KEY", None)


@pytest.fixture(autouse=True)
def _no_real_tk(monkeypatch):
    """Never let a test create real Tk windows or enter a real mainloop.

    Several tests construct `LiveMedicalSTT`/`ControlWindow` with default
    settings and only disable the overlay *after* construction
    (`stt.overlay.enabled = False  # no real display in CI`). On a machine
    where tkinter can actually open a display -- every Windows runner has
    one -- that is too late: `TranscriptOverlay.__init__` has already
    spawned the overlay daemon thread with a real `tk.Tk()` and `mainloop()`,
    and the lingering after-callbacks later abort the whole interpreter
    mid-session (Windows fatal exception 0x80000003 in
    `overlay._tick_follow`). Headless Linux hides the bug because
    `tk.Tk()` raises `TclError` there and the overlay disables itself.

    The suite is designed to exercise the UI through fakes (see
    `tests/test_overlay.py`, `tests/test_control.py`); forcing the modules'
    availability flags off keeps that true on every platform. A test that
    deliberately needs the GUI path can opt back in with its own
    `monkeypatch.setattr(..., "_TK_AVAILABLE", True)` (as
    `tests/test_performance_fixes.py` already does).
    """
    from medical_stt.ui import control, overlay

    monkeypatch.setattr(overlay, "_TK_AVAILABLE", False)
    monkeypatch.setattr(control, "_TK_AVAILABLE", False)
