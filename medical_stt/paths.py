"""Filesystem locations for per-user application data.

Everything the application writes at runtime lives under a single
per-user directory (never next to the executable, never in the source
tree):

    %APPDATA%\\MedicalSTT\\            (Windows -- the only supported platform)
        settings.yaml                 user settings template (no secrets)
        host_secret.dpapi             shared secret, Windows DPAPI-protected
        logs\\app.log                  rotating log file
        run.lock                      single-instance lock

`MEDICALSTT_APP_DATA_DIR` overrides the location. It exists so tests and
development runs can be fully isolated from the real user profile.
"""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "MedicalSTT"

#: Environment variable that relocates the whole per-user data directory.
APP_DATA_DIR_ENV = "MEDICALSTT_APP_DATA_DIR"


def app_data_dir() -> Path:
    """Return the per-user application data directory (not created here)."""
    override = os.getenv(APP_DATA_DIR_ENV)
    if override:
        return Path(override).expanduser()

    if os.name == "nt":
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


def ensure_app_data_dir() -> Path:
    """Create the per-user data directory (and the log subdirectory)."""
    directory = app_data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    log_dir().mkdir(parents=True, exist_ok=True)
    return directory
