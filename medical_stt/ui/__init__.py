"""User-facing overlay UI (Tkinter). Optional: the application runs fine
without a display (`overlay_enabled: false` or Tkinter unavailable)."""
from .overlay import TranscriptOverlay

__all__ = ["TranscriptOverlay"]
