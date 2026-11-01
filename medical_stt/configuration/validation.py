"""Validation of settings values, URLs and identifiers.

Pure functions only: nothing here reads a file, an environment variable or the
clock, so every rule can be exercised directly and the error strings -- which
are shown to a clinician -- are the single source of truth for what is legal.

Validation is a *list of errors*, not a first exception, because a settings file
usually has more than one problem and fixing them one restart at a time is the
kind of friction that leads to hand-edited YAML.
"""
from __future__ import annotations

import logging
import math
import re
from typing import List, Tuple
from urllib.parse import urlparse

from .model import (
    _LANGUAGE_MODEL_SUPPORT,
    _LANGUAGE_TAG_RE,
    _LOCAL_HOSTS,
    _MAX_SESSION_TTL_SECONDS,
    _SUPPORTED_MODELS,
    _VALID_INJECT_MODES,
    _VALIDATED_CONFIGURATION,
    DEEPGRAM_REGION_HOSTS,
    Settings,
)

log = logging.getLogger("medical_stt.config")

def _valid_specialty_name(value: str) -> bool:
    return bool(value) and value.replace("_", "").replace("-", "").isalnum()

#: The whole accepted alphabet for `host_client_id`, ASCII only. See
#: `host.core.CLIENT_ID_PATTERN` for why `str.isalnum()` is not equivalent: it
#: is Unicode-aware, so a permissive check would let a clinician save a
#: homoglyph of the id the operator provisioned -- which then authenticates as
#: a *different* identity (and, if it is not in the host's registry, fails with
#: a 401 that reads like a wrong secret).
_CLIENT_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


def _valid_client_id(value: str) -> bool:
    """Mirror of `host.core.is_valid_client_id`.

    Duplicated deliberately rather than imported: the client package must
    not depend on the host package, which runs on a different machine. Keep
    the three copies (`host/core.py`, `host/provision.py`, here) in step: an id
    one of them accepts and another refuses is a device that can never connect,
    with an error message that blames the credential.
    """
    if _CLIENT_ID_RE.fullmatch(value) is None:
        return False
    # An id of only separators and dots (`..`, `-`) passes the class but
    # identifies nothing; `host.core` refuses it in the registry too.
    return any(ch.isascii() and ch.isalnum() for ch in value)

def validate_model_language(model: str, language: str) -> List[str]:
    """Return configuration errors for a (model, language) pair.

    Called both when settings are validated (so the error appears the
    moment the user presses Start) and by the provider before it opens the
    WebSocket, so an unsupported combination can never reach Deepgram.
    """
    errors: List[str] = []
    if not model:
        errors.append("model must not be empty")
    elif model not in _SUPPORTED_MODELS:
        errors.append(
            f"model {model!r} is not supported; supported models are "
            f"{sorted(_SUPPORTED_MODELS)} ({_VALIDATED_CONFIGURATION} is the "
            "validated configuration for this application)"
        )
    if not language:
        errors.append("language must not be empty")
    elif not _LANGUAGE_TAG_RE.match(language):
        errors.append(
            f"language {language!r} is not a valid language tag "
            "(expected a BCP-47 tag such as 'fa' or 'en-US')"
        )
    else:
        supported_models = _LANGUAGE_MODEL_SUPPORT.get(language)
        if supported_models is not None and model and model not in supported_models:
            errors.append(
                f"language {language!r} requires model "
                f"{sorted(supported_models)[0]!r}; model {model!r} does not "
                "support it"
            )
    return errors


def _valid_host_url(url: str) -> Tuple[bool, str]:
    try:
        parsed = urlparse(url)
        # A configured URL may be included in network errors and support
        # logs. Never allow URL userinfo or query/fragment values to become
        # a second, less-obvious place for credentials to leak.
        if (
            parsed.username is not None
            or parsed.password is not None
            or "?" in url.split("#", 1)[0]
            or "#" in url
        ):
            return False, "host_url must not include credentials, a query, or a fragment"
        if not parsed.hostname:
            return False, "host_url is missing a host name"
        # Accessing `.port` validates malformed/non-numeric/out-of-range ports.
        _ = parsed.port
    except ValueError:
        return False, "host_url is invalid"

    if parsed.scheme == "https":
        return True, ""
    if parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS:
        # Loopback only: never relax this for a real host name.
        return True, ""
    if parsed.scheme not in ("http", "https"):
        return False, "host_url must start with https://"
    return False, "host_url must use https:// (plain http is only allowed for localhost)"

def validate_settings(settings: Settings) -> List[str]:
    """Return a list of validation error strings; empty list means valid."""
    errors: List[str] = []
    for name in ("block_duration", "paste_settle_seconds", "reconnect_delay", "reconnect_backoff_max",
                 "reconnect_jitter", "host_timeout_seconds", "medical_confidence_threshold"):
        if not math.isfinite(getattr(settings, name)):
            errors.append(f"{name} must be finite")

    if settings.sample_rate not in (8000, 16000, 22050, 24000, 44100, 48000):
        errors.append(f"sample_rate must be a standard rate, got {settings.sample_rate}")
    if settings.channels not in (1, 2):
        errors.append(f"channels must be 1 or 2, got {settings.channels}")
    if not (0.01 <= settings.block_duration <= 1.0):
        errors.append(f"block_duration must be between 0.01 and 1.0 seconds, got {settings.block_duration}")
    if not (10 <= settings.endpointing <= 10000):
        errors.append(f"endpointing must be between 10 and 10000 ms, got {settings.endpointing}")
    # Deepgram's documented range; values outside it are rejected at the
    # handshake, which would otherwise surface as an opaque CONFIG error.
    if not (1000 <= settings.utterance_end_ms <= 5000):
        errors.append(f"utterance_end_ms must be between 1000 and 5000 ms, got {settings.utterance_end_ms}")
    if settings.deepgram_region not in DEEPGRAM_REGION_HOSTS:
        errors.append(
            f"deepgram_region must be one of {sorted(DEEPGRAM_REGION_HOSTS)}, got {settings.deepgram_region!r}"
        )
    if settings.inject_mode not in _VALID_INJECT_MODES:
        errors.append(f"inject_mode must be one of {sorted(_VALID_INJECT_MODES)}, got {settings.inject_mode!r}")
    if settings.reconnect_delay < 0:
        errors.append("reconnect_delay must be >= 0")
    if settings.max_reconnect_attempts < 0:
        errors.append("max_reconnect_attempts must be >= 0 (0 = unlimited)")
    if settings.reconnect_backoff_max < settings.reconnect_delay:
        errors.append("reconnect_backoff_max must be >= reconnect_delay")
    if not (0 <= settings.reconnect_jitter <= 1):
        errors.append("reconnect_jitter must be between 0 and 1 (fraction of delay)")
    # Model and language are validated together: the combination decides
    # which recognition-assistance parameter is legal (`keyterm` is Nova-3
    # only) and whether the language is supported by that model at all.
    errors.extend(validate_model_language(settings.model, settings.language))
    if not _valid_specialty_name(settings.specialty):
        errors.append("specialty must contain only letters, numbers, '_' or '-'")
    if not (0.0 <= settings.medical_confidence_threshold <= 1.0):
        errors.append("medical_confidence_threshold must be between 0 and 1")

    # -- host / credential checks -------------------------------------
    if not settings.host_url:
        errors.append(
            "host_url is not set (run the app once and enter your host URL, "
            "or set it in settings.yaml)"
        )
    else:
        ok, message = _valid_host_url(settings.host_url)
        if not ok:
            errors.append(message)
    if not settings.host_secret:
        errors.append(
            "no host shared secret available: enter it in the app's settings "
            "panel so it can be stored with Windows DPAPI"
        )
    if settings.host_client_id and not _valid_client_id(settings.host_client_id):
        errors.append(
            "host_client_id must be 1-64 ASCII characters of letters, digits, "
            "'-', '_' or '.', with at least one letter or digit (see: "
            "python -m host.provision)"
        )
    if not (1.0 <= settings.host_timeout_seconds <= 60.0):
        errors.append("host_timeout_seconds must be between 1 and 60")
    elif settings.host_timeout_seconds < 8.0:
        # Legal, but tight: session readiness allows this plus a fixed
        # margin (app.py) against the host's worst-case grant budget
        # (~7s: 2s body + grant deadline) and the SDK's own ~10s
        # handshake timeout, which is not configurable from here. Small
        # values make Start flake on slow networks with a phantom
        # "readiness timed out".
        log.warning(
            "host_timeout_seconds=%.1fs leaves little room over the host's "
            "grant budget plus the Deepgram handshake; values of 8s or more "
            "are recommended (the default is 10s)",
            settings.host_timeout_seconds,
        )
    if not (5 <= settings.session_ttl_seconds <= _MAX_SESSION_TTL_SECONDS):
        errors.append(
            f"session_ttl_seconds must be between 5 and {_MAX_SESSION_TTL_SECONDS} "
            "(the token only has to outlive the WebSocket handshake)"
        )

    return errors
