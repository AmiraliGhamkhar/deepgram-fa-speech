"""Filesystem locations for per-user application data.

Everything the application writes at runtime lives under a single
per-user directory (never next to the executable, never in the source
tree):

    %APPDATA%\\MedicalSTT\\            (Windows -- the only supported platform)
        settings.yaml                 user settings template (no secrets)
        host_secret.dpapi             shared secret, Windows DPAPI-protected
        logs\\app.log                  rotating log file
        run.lock                      single-instance lock
        ui_state.json                 UI window positions (no secrets)

`MEDICALSTT_APP_DATA_DIR` overrides the location. It exists so tests and
development runs can be fully isolated from the real user profile. The
override is validated rather than trusted: it is expanded and resolved to
an absolute path, and refused (ValueError) when it resolves inside the
install directory or -- on Windows, where the DPAPI blob and log would
make it an attacker-relocatable target -- inside the system temp
directory. A `.env`/shortcut planted in a launch directory chain must not
be able to silently relocate `host_secret.dpapi` and `app.log` somewhere
writable by anyone.
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger("medical_stt.paths")

APP_NAME = "MedicalSTT"

#: Environment variable that relocates the whole per-user data directory.
APP_DATA_DIR_ENV = "MEDICALSTT_APP_DATA_DIR"

_IS_WINDOWS = os.name == "nt"
_DATA_DIR_LOGGED = False


def _install_root() -> Path:
    """The package's install root (the directory containing `medical_stt`)."""
    return Path(__file__).resolve().parent.parent


def _validated_override(raw: str) -> Path:
    resolved = Path(raw).expanduser().resolve()
    install_root = _install_root()
    if resolved == install_root or install_root in resolved.parents:
        raise ValueError(
            f"{APP_DATA_DIR_ENV} must not resolve inside the install "
            f"directory ({install_root}); got {resolved}"
        )
    if _IS_WINDOWS:
        temp_root = Path(tempfile.gettempdir()).resolve()
        if resolved == temp_root or temp_root in resolved.parents:
            raise ValueError(
                f"{APP_DATA_DIR_ENV} must not resolve inside the Windows "
                f"temp directory ({temp_root}); got {resolved}"
            )
    return resolved


def app_data_dir() -> Path:
    """Return the per-user application data directory (not created here)."""
    override = os.getenv(APP_DATA_DIR_ENV)
    if override:
        return _validated_override(override)

    if _IS_WINDOWS:
        appdata = os.getenv("APPDATA")
        if appdata:
            return Path(appdata) / APP_NAME
        return Path.home() / "AppData" / "Roaming" / APP_NAME

    # Non-Windows is unsupported for production use (the injection backend
    # is Win32), but a predictable location keeps development runs and
    # tests from writing into odd places.
    xdg = os.getenv("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / APP_NAME


def settings_path() -> Path:
    """User-editable settings file (no secrets; see `secret_path`)."""
    return app_data_dir() / "settings.yaml"


def secret_path() -> Path:
    """File holding the DPAPI-protected shared secret."""
    return app_data_dir() / "host_secret.dpapi"


def log_dir() -> Path:
    return app_data_dir() / "logs"


def log_path() -> Path:
    return log_dir() / "app.log"


def lock_path() -> Path:
    return app_data_dir() / "run.lock"


def ui_state_path() -> Path:
    """Small JSON with UI window positions (never settings or secrets)."""
    return app_data_dir() / "ui_state.json"


def ensure_app_data_dir() -> Path:
    """Create the per-user data directory (and the log subdirectory).

    Logs the final resolved directory once per process: after override
    validation, "where did my data actually go" must be answerable from
    the log alone, never guessable only from an environment dump.
    """
    global _DATA_DIR_LOGGED
    directory = app_data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    log_dir().mkdir(parents=True, exist_ok=True)
    if not _DATA_DIR_LOGGED:
        _DATA_DIR_LOGGED = True
        log.info("data directory: %s", directory)
    return directory
