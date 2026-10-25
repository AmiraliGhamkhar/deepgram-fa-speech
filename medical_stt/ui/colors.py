"""The single dark palette shared by the control window and the overlay.

Both windows used to spell these hex values inline at every widget, so a
colour could drift between the palette, the overlay and the recovery
popup (and did: the popup shipped unstyled system colours). Every colour
either window uses lives here; no inline hex literals elsewhere in the
UI package.
"""
from __future__ import annotations

BG = "#1e1e2e"
SURFACE = "#313244"
BORDER = "#45475a"
FG = "#cdd6f4"
MUTED = "#a6adc8"
OK = "#a6e3a1"
REC = "#f38ba8"
ACCENT = "#89b4fa"
WARNING = "#f9e2af"
IDLE = "#6c7086"
#: Text drawn on a light fill (the start/stop circle).
ON_LIGHT = "#11111b"


def blend(foreground: str, background: str, weight: float = .5) -> str:
    """Muted 50% visual weight, derived only from the central palette."""
    channels = [round(int(foreground[i:i + 2], 16) * weight +
                      int(background[i:i + 2], 16) * (1 - weight)) for i in (1, 3, 5)]
    return "#" + "".join(f"{channel:02x}" for channel in channels)
