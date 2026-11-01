"""The settings model: values, defaults and the Deepgram vocabulary.

`Settings` is a plain, immutable-in-intent dataclass. It is the *only* place
that knows the shape of the configuration; validation (`validation.py`) and
loading (`bootstrap.py`, `yaml_io.py`) are separate modules so that a stage can
be tested without touching a file or the environment.

**Security model (non-negotiable):** this application has no Deepgram API key.
The key lives only in the environment of the self-hosted service. The client
authenticates to that host with a shared secret, which is stored locally only
as a Windows DPAPI-protected blob (see `medical_stt/security/`). `Settings`
therefore carries a host URL and the shared secret -- never a provider
credential -- and a plaintext secret in `settings.yaml` is a configuration
*error*.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet

_VALID_INJECT_MODES = frozenset({"paste", "type"})

#: Recognition-assistance parameter per model. Keyterm Prompting is a
#: Nova-3 feature; Nova-2 and the older models use the legacy `keywords`
#: parameter instead (Deepgram rejects `keyterm` on those models).
#: See https://developers.deepgram.com/docs/keyterm
_KEYTERM_PARAMETER_BY_MODEL: Dict[str, str] = {
    "nova-3": "keyterm",
    "nova-2": "keywords",
    "nova": "keywords",
    "enhanced": "keywords",
    "base": "keywords",
}

#: Models this project knows how to configure. `nova-3` is the model the
#: Persian medical workflow is validated against.
_SUPPORTED_MODELS = frozenset(_KEYTERM_PARAMETER_BY_MODEL)

#: Languages this project validates, mapped to the models Deepgram
#: documents for them. Persian (`fa`) is a monolingual Nova-3 model: it is
#: not available on Nova-2 or the older models, so that combination is
#: rejected instead of being sent upstream to fail at the handshake.
_LANGUAGE_MODEL_SUPPORT: Dict[str, FrozenSet[str]] = {"fa": frozenset({"nova-3"})}

#: BCP-47-ish check ("fa", "en-US", ...), applied to every configured tag.
_LANGUAGE_TAG_RE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")

#: Name of the validated Persian configuration, used in error messages.
_VALIDATED_CONFIGURATION = "model: nova-3 with language: fa"

#: Deepgram's maximum temporary-token TTL. See
#: https://developers.deepgram.com/guides/fundamentals/token-based-authentication
_MAX_SESSION_TTL_SECONDS = 3600

#: Deepgram regional endpoints. Keys and session tokens are valid on every
#: region; a region never falls back to another one. See
#: https://developers.deepgram.com/reference/regional-endpoints
DEEPGRAM_REGION_HOSTS: Dict[str, str] = {
    "us": "api.deepgram.com",
    "eu": "api.eu.deepgram.com",
    "au": "api.au.deepgram.com",
    "in": "api.in.deepgram.com",
}


_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

#: Keys that must never appear in a YAML settings file.
_FORBIDDEN_SECRET_KEYS = ("api_key", "apikey", "deepgram_api_key", "host_secret", "secret")


@dataclass
class Settings:
    """Validated configuration for one client installation."""
    model: str = "nova-3"
    language: str = "fa"
    sample_rate: int = 16000
    channels: int = 1
    block_duration: float = 0.08
    endpointing: int = 400
    utterance_end_ms: int = 1200
    inject_mode: str = "paste"
    paste_settle_seconds: float = 0.12
    restore_clipboard: bool = True
    overlay_enabled: bool = True
    reconnect_delay: float = 2.0
    max_reconnect_attempts: int = 0
    reconnect_backoff_max: float = 30.0
    reconnect_jitter: float = 0.5
    enable_context_dependent_terms: bool = False
    specialty: str = "general"
    medical_confidence_threshold: float = 0.65
    use_asr_replacements: bool = True
    #: Deepgram region the audio is streamed to (`us`, `eu`, `au`, `in`).
    #: Session tokens work on every region; pick the closest one or the one
    #: your data-residency rules require.
    deepgram_region: str = "us"
    #: Opt the audio out of Deepgram's Model Improvement Program. Off by
    #: default (Deepgram's default); opting out is billed at a higher rate.
    mip_opt_out: bool = False
    #: Base URL of the self-hosted service that mints short-lived
    #: Deepgram sessions. Must be https:// (http:// only for localhost).
    host_url: str = ""
    #: Shared secret for that host. Loaded from the DPAPI-protected store;
    #: never written to settings.yaml and never logged.
    host_secret: str = field(default="", repr=False)
    #: Identifies *which* device is talking to the host, so the host can rate
    #: limit per clinician rather than per hospital NAT address. Not a
    #: credential, so it lives in settings.yaml next to `host_url`.
    host_client_id: str = ""
    host_timeout_seconds: float = 10.0
    #: Requested lifetime of the short-lived session token. Only needs to
    #: cover the WebSocket handshake (default 30s, Deepgram's own default).
    session_ttl_seconds: int = 30

    def as_dict(self) -> Dict[str, Any]:
        """A serializable view of the settings, **without the secret**.

        `as_dict()` is the shape every dump/log path goes through, so the
        shared secret is excluded at the source: a future refactor that
        starts serializing settings cannot leak it by accident. The
        plaintext value exists only in the DPAPI-protected store and this
        field.
        """
        return {
            "model": self.model,
            "language": self.language,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "block_duration": self.block_duration,
            "endpointing": self.endpointing,
            "utterance_end_ms": self.utterance_end_ms,
            "inject_mode": self.inject_mode,
            "paste_settle_seconds": self.paste_settle_seconds,
            "restore_clipboard": self.restore_clipboard,
            "overlay_enabled": self.overlay_enabled,
            "reconnect_delay": self.reconnect_delay,
            "max_reconnect_attempts": self.max_reconnect_attempts,
            "reconnect_backoff_max": self.reconnect_backoff_max,
            "reconnect_jitter": self.reconnect_jitter,
            "enable_context_dependent_terms": self.enable_context_dependent_terms,
            "specialty": self.specialty,
            "medical_confidence_threshold": self.medical_confidence_threshold,
            "use_asr_replacements": self.use_asr_replacements,
            "deepgram_region": self.deepgram_region,
            "mip_opt_out": self.mip_opt_out,
            "host_url": self.host_url,
            "host_client_id": self.host_client_id,
            "host_timeout_seconds": self.host_timeout_seconds,
            "session_ttl_seconds": self.session_ttl_seconds,
        }

    def __getitem__(self, key: str) -> Any:
        # Dict-like access retained for backward compatibility with call
        # sites/tests written against the old plain-dict settings.
        return self.as_dict()[key]



def deepgram_api_host(region: str) -> str:
    """Hostname of the Deepgram API for a configured region."""
    from .errors import ConfigError

    try:
        return DEEPGRAM_REGION_HOSTS[region]
    except KeyError:
        raise ConfigError(
            f"deepgram_region must be one of {sorted(DEEPGRAM_REGION_HOSTS)}, got {region!r}"
        ) from None


def keyterm_parameter(model: str) -> str:
    """Return the recognition-assistance parameter for `model`.

    `"keyterm"` for Nova-3 (Keyterm Prompting), `"keywords"` for the legacy
    models, `""` for a model we do not know.
    """
    return _KEYTERM_PARAMETER_BY_MODEL.get(model, "")
