"""Persistence of small window state (currently: the control window position).

Kept apart from the widget code because it is the one part of the UI that
touches the filesystem, and because both halves deserve their own rules:

* **Loading never refuses to start.** A corrupt, truncated or absurd state
  file falls back to the default corner: this is a UI nicety, not
  configuration, so a broken file must not be able to keep the window from
  appearing.
* **Saving is atomic and best effort.** A half-written file would poison
  every later read, so the write goes to a temporary file in the same
  directory and is swapped in with `os.replace`. Any failure is logged at
  debug level and swallowed -- the window is still usable.

Nothing here records anything about the dictation itself; only two integers.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any, Optional, Tuple

from .. import paths

log = logging.getLogger("medical_stt.ui.control")

#: Where the window appears the first time, and whenever the state is unusable.
DEFAULT_POSITION: Tuple[int, int] = (40, 40)

#: A position beyond this is treated as corrupt. Windows have been dragged off
#: screen by a multi-monitor change; 100k is far past any real desktop.
MAX_ABS_COORDINATE = 100_000


def parse_position(raw: Any) -> Optional[Tuple[int, int]]:
    """Extract `(x, y)` from decoded JSON, or None if it is not sane.

    `bool` is excluded explicitly: it is an `int` subclass, so `True` would
    otherwise be accepted as a coordinate.
    """
    if not isinstance(raw, dict):
        return None
    x: Any = raw.get("x")
    y: Any = raw.get("y")
    for value in (x, y):
        if type(value) is not int or abs(value) > MAX_ABS_COORDINATE:
            return None
    return int(x), int(y)


def load_window_position() -> Tuple[int, int]:
    """Restore the last dragged position, or the default corner."""
    try:
        raw = json.loads(paths.ui_state_path().read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - see module docstring
        log.debug("window position not restored (%s)", type(exc).__name__)
        return DEFAULT_POSITION
    position = parse_position(raw)
    if position is None:
        log.debug("window position ignored (unusable contents)")
        return DEFAULT_POSITION
    return position


def save_window_position(x: int, y: int) -> None:
    """Persist `(x, y)`, atomically, ignoring any filesystem failure."""
    try:
        target = paths.ui_state_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            dir=str(target.parent), prefix=".ui-state-", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump({"x": int(x), "y": int(y)}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    except Exception as exc:  # noqa: BLE001 - a UI nicety, never fatal
        log.debug("window position not saved (%s)", type(exc).__name__)
