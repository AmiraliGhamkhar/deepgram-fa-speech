"""Configuration facade.

The configuration package is split by responsibility:

=========================  ====================================================
`configuration.errors`     `ConfigError`
`configuration.model`      `Settings` and the Deepgram vocabulary/defaults
`configuration.validation` pure validation rules (`validate_settings`,
                           URL / client-id checks, model+language pairing)
`configuration.yaml_io`    cached YAML reads, typed scalar coercion, atomic writes
`configuration.bootstrap`  first-run template and DPAPI credential storage
`configuration.loader`     precedence: environment -> user file -> build
`configuration.catalogs`   keyterms, ASR replacements, terminology rules
=========================  ====================================================

**Why this module still exists:** everything (client code, scripts, the build
scripts and the tests) imports `medical_stt.config`. Keeping it as a thin,
side-effect-compatible re-export keeps that contract stable while the actual
code lives in small, independently testable modules.

**Security model (non-negotiable):** this application has no Deepgram API key.
The key lives only in the environment of the self-hosted service. The client
authenticates to that host with a shared secret, stored locally only as a
Windows DPAPI-protected blob (see `medical_stt/security/`). `Settings`
therefore carries a host URL and the shared secret -- never a provider
credential -- and a plaintext secret in `settings.yaml` is a configuration
*error*.
"""
from __future__ import annotations

from . import paths
from .configuration.bootstrap import (
    bundled_settings_path,
    initialize_user_config,
    load_host_secret,
    save_host_credentials,
    user_settings_path,
)
from .configuration.catalogs import (
    MAX_KEYTERMS,
    MAX_KEYTERM_CHARS,
    load_asr_replacements,
    load_correction_rules,
    load_correction_rules_raw,
    load_keyterms,
)
from .configuration.errors import ConfigError
from .configuration.loader import get_settings
from .configuration.model import (
    DEEPGRAM_REGION_HOSTS,
    Settings,
    deepgram_api_host,
    keyterm_parameter,
)
from .configuration.validation import (
    validate_model_language,
    validate_settings,
)
from .configuration.yaml_io import (
    atomic_write_text,
)

__all__ = [
    "ConfigError",
    "DEEPGRAM_REGION_HOSTS",
    "MAX_KEYTERMS",
    "MAX_KEYTERM_CHARS",
    "Settings",
    "atomic_write_text",
    "bundled_settings_path",
    "deepgram_api_host",
    "get_settings",
    "initialize_user_config",
    "keyterm_parameter",
    "load_asr_replacements",
    "load_correction_rules",
    "load_correction_rules_raw",
    "load_host_secret",
    "load_keyterms",
    "paths",
    "save_host_credentials",
    "user_settings_path",
    "validate_model_language",
    "validate_settings",
]
