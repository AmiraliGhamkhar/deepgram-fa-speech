"""Process-level diagnostics: logging setup and version reporting.

These are entry-point concerns, not dictation logic: they run once, before any
session exists, and they are the only places that touch the filesystem purely
to make the application *observable*.

The packaged EXE runs without a console window, so
``%APPDATA%\\MedicalSTT\\logs\\app.log`` is the only place a user can look for
diagnostics. Nothing sensitive is ever logged (see README > Logging).
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from typing import Optional

from . import paths

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

#: 1 MB per file, three generations: enough for a clinic day, bounded on disk.
LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 3


def configure_logging(level: int = logging.INFO) -> None:
    """Log to a rotating file; stderr only when a console is attached."""
    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter(LOG_FORMAT)

    file_handler: Optional[logging.Handler] = None
    try:
        paths.ensure_app_data_dir()
        file_handler = logging.handlers.RotatingFileHandler(
            paths.log_path(), maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
        )
    except OSError as exc:  # pragma: no cover - unwritable profile
        root.warning("file logging unavailable: %s", exc)
    if file_handler is not None:
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    if sys.stderr is not None and getattr(sys.stderr, "isatty", lambda: False)():
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        root.addHandler(console)


def version_string() -> str:
    from . import __version__

    return f"Medical STT {__version__}"


def print_version(stream: Optional[object] = None) -> None:
    """Print the version and note it in the log; never raise.

    The packaged EXE is built with ``--windows-console-mode=disable``, where
    ``sys.stdout`` is None. The build script verifies the bundle by exit code,
    so this must never raise.
    """
    text = version_string()
    target = sys.stdout if stream is None else stream
    if target is not None:
        try:
            print(text, file=target)  # type: ignore[arg-type]
        except (OSError, ValueError):  # pragma: no cover - detached handle
            pass
    try:
        paths.ensure_app_data_dir()
        with paths.log_path().open("a", encoding="utf-8") as handle:
            handle.write(f"{text}\n")
    except OSError:  # pragma: no cover - unwritable profile
        pass
