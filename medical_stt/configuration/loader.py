"""Assembling `Settings` from the user profile, the build, and the environment.

Precedence, highest first:

1. an explicit environment override (`MEDICALSTT_HOST_URL`, `DEEPGRAM_MODEL`,
   ...) -- used by operations and by tests;
2. the per-user `settings.yaml` in the application data directory;
3. the template shipped with the build.

`.env` is loaded once, at import, and only when *not* frozen: a packaged build
must never pick up a file that happens to sit next to the working directory,
because that would let a stray `.env` redirect a clinician's host URL.
"""
from __future__ import annotations

import logging
import os
import sys

from dotenv import load_dotenv

from .. import paths
from .bootstrap import bundled_settings_path, load_host_secret, user_settings_path
from .errors import ConfigError
from .model import Settings
from .yaml_io import _flag, _load_yaml, _num, _reject_plaintext_secrets

log = logging.getLogger("medical_stt.config")

if not getattr(sys, "frozen", False):
    _env_path = paths.package_root() / ".env"
    if load_dotenv(_env_path, override=False):
        log.info("loaded .env from %s", _env_path)


def get_settings() -> Settings:
    """Load settings from the user profile, falling back to the build's copy."""
    user_file = user_settings_path()
    source = user_file if user_file.is_file() else bundled_settings_path()
    cfg = _load_yaml(source)
    if not isinstance(cfg, dict):
        raise ConfigError(f"{source} must contain a mapping at the top level")
    _reject_plaintext_secrets(cfg, source)

    # Every numeric and boolean key goes through `_num`/`_flag`, never a bare
    # int()/float()/bool() on a value a human edited: see those helpers for the
    # two failure shapes they exist for (a raw traceback instead of a
    # ConfigError, and `bool("false") is True` inverting a retention setting).
    return Settings(
        model=os.getenv("DEEPGRAM_MODEL", cfg.get("model", "nova-3")),
        language=os.getenv("DEEPGRAM_LANGUAGE", cfg.get("language", "fa")),
        sample_rate=_num(cfg, source, "sample_rate", int, 16000),
        channels=_num(cfg, source, "channels", int, 1),
        block_duration=_num(cfg, source, "block_duration", float, 0.08),
        endpointing=_num(cfg, source, "endpointing", int, 400),
        utterance_end_ms=_num(cfg, source, "utterance_end_ms", int, 1200),
        inject_mode=cfg.get("inject_mode", "paste"),
        paste_settle_seconds=_num(cfg, source, "paste_settle_seconds", float, 0.12),
        restore_clipboard=_flag(cfg, source, "restore_clipboard", True),
        overlay_enabled=_flag(cfg, source, "overlay_enabled", True),
        reconnect_delay=_num(cfg, source, "reconnect_delay", float, 2.0),
        max_reconnect_attempts=_num(cfg, source, "max_reconnect_attempts", int, 0),
        reconnect_backoff_max=_num(cfg, source, "reconnect_backoff_max", float, 30.0),
        reconnect_jitter=_num(cfg, source, "reconnect_jitter", float, 0.5),
        enable_context_dependent_terms=_flag(
            cfg, source, "enable_context_dependent_terms", False
        ),
        specialty=str(cfg.get("specialty") or "general"),
        medical_confidence_threshold=_num(
            cfg, source, "medical_confidence_threshold", float, 0.65
        ),
        use_asr_replacements=_flag(cfg, source, "use_asr_replacements", True),
        deepgram_region=str(cfg.get("deepgram_region", "us") or "us").strip().lower(),
        mip_opt_out=_flag(cfg, source, "mip_opt_out", False),
        host_url=os.getenv("MEDICALSTT_HOST_URL", str(cfg.get("host_url", "") or "")),
        host_secret=load_host_secret(),
        host_client_id=os.getenv(
            "MEDICALSTT_HOST_CLIENT_ID", str(cfg.get("host_client_id", "") or "")
        ).strip(),
        host_timeout_seconds=_num(cfg, source, "host_timeout_seconds", float, 10.0),
        session_ttl_seconds=_num(cfg, source, "session_ttl_seconds", int, 30),
    )


__all__ = ["get_settings"]
