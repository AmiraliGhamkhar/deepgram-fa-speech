"""User-facing UI (Tkinter).

Two windows:
* `ControlWindow` -- the primary, always-on-top Start/Stop control.
* `TranscriptOverlay` -- optional floating transcript display.

Both are optional in the sense that the application degrades gracefully
without a display (headless development/CI), but the control window is
what a normal desktop user interacts with.
"""
from .control import ControlWindow
from .overlay import TranscriptOverlay

__all__ = ["ControlWindow", "TranscriptOverlay"]
