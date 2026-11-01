"""First-run setup and local credential storage.

Two concerns, both about *files the user owns* rather than about values:

* **first run** -- create the per-user settings file from the shipped template,
  without ever writing a secret into it and without overwriting local edits on
  upgrade;
* **credentials** -- store the host shared secret with Windows DPAPI and keep
  only non-secret values (`host_url`, `host_client_id`) in YAML.

Everything is written atomically. The encrypted secret and the YAML are two
separate files, not a transaction, so a partial outcome is *reported as such*
instead of being left for the user to discover later.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import yaml

from .. import paths
from ..security import SecretStore, SecretStoreError
from .errors import ConfigError
from .validation import _valid_client_id, _valid_host_url
from .yaml_io import _load_yaml, atomic_write_text

log = logging.getLogger("medical_stt.config")

#: Header prepended to a freshly created user settings file.
_TEMPLATE_HEADER = (
    "# Medical STT user settings.\n"
    "#\n"
    "# This file is created on first run and may contain no secrets.\n"
    "# Set host_url below to your self-hosted service.\n"
    "# The shared secret is NOT stored here: enter it in the app's\n"
    "# settings panel, where it is protected with Windows DPAPI and kept\n"
    "# in %APPDATA%\\MedicalSTT\\host_secret.dpapi.\n"
    "# The Deepgram API key is never configured on this machine.\n\n"
)

#: Used only if the build dropped `config/` (the packaged template is then
#: missing) so a broken package still starts with a usable file.
_FALLBACK_TEMPLATE = (
    "model: nova-3\n"
    "language: fa\n"
    "specialty: general\n"
    "host_url: https://stt.example.com\n"
)


def user_settings_path() -> Path:
    """Path of the per-user settings file."""
    return paths.settings_path()


def bundled_settings_path() -> Path:
    """Path of the settings file shipped with the build."""
    return paths.bundled_config_dir() / "settings.yaml"


def initialize_user_config() -> Path:
    """First run: create the per-user data directory and config template.

    Never writes a secret. An existing user file is left untouched so local
    edits survive upgrades.
    """
    paths.ensure_app_data_dir()
    target = user_settings_path()
    if target.is_file():
        return target

    bundled = bundled_settings_path()
    body = bundled.read_text(encoding="utf-8") if bundled.is_file() else _FALLBACK_TEMPLATE
    atomic_write_text(target, _TEMPLATE_HEADER + body)
    log.info("created user settings template at %s", target)
    return target


def load_host_secret() -> str:
    """Read the shared secret from the DPAPI store.

    `MEDICALSTT_HOST_SECRET` overrides it. That override exists purely for
    development/CI on platforms without DPAPI; it is never written to disk and
    never logged.
    """
    override = os.getenv("MEDICALSTT_HOST_SECRET")
    if override:
        return override
    try:
        secret = SecretStore(paths.secret_path()).load()
    except SecretStoreError as exc:
        # A stale/corrupt blob must not crash startup with a traceback -- it
        # becomes a normal configuration error instead.
        log.warning("stored host secret is unusable (%s)", type(exc).__name__)
        return ""
    return secret or ""


def save_host_credentials(host_url: str, host_secret: str, client_id: Optional[str] = None) -> None:
    """Validate before touching credentials; replace YAML atomically.

    An empty `host_secret` means "keep the stored secret": the settings panel
    never displays it, so an empty field cannot be told apart from "unchanged"
    and must not erase a working credential.
    """
    valid, message = _valid_host_url(host_url.strip())
    if not valid:
        raise ConfigError(message)
    if client_id and not _valid_client_id(client_id):
        raise ConfigError(
            "host_client_id is invalid: use 1-64 ASCII characters of letters, "
            "digits, '-', '_' or '.', with at least one letter or digit. The "
            "host refuses a registry line containing anything else, so a "
            "non-ASCII id here is a device that can never connect."
        )
    initialize_user_config()
    target = user_settings_path()
    cfg = _load_yaml(target)
    if not isinstance(cfg, dict):
        raise ConfigError(f"{target} must contain a mapping at the top level")
    cfg["host_url"] = host_url.strip()
    if client_id is not None:
        cfg["host_client_id"] = client_id
    dumped = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)

    secret_saved = False
    try:
        if host_secret:
            SecretStore(paths.secret_path()).save(host_secret)
            secret_saved = True
        atomic_write_text(target, dumped)
    except (OSError, SecretStoreError) as exc:
        # SecretStoreError is a RuntimeError (DPAPI failure), not an OSError:
        # catching only OSError let it escape raw instead of surfacing as the
        # documented ConfigError partial outcome. Its message never contains
        # the secret itself.
        detail = (
            "Secret saved, but settings were not replaced; retry saving"
            if secret_saved
            else "Settings were not saved"
        )
        raise ConfigError(detail) from exc


__all__ = [
    "bundled_settings_path",
    "initialize_user_config",
    "load_host_secret",
    "save_host_credentials",
    "user_settings_path",
]
