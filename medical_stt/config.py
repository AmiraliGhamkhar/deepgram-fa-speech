"""Load and validate settings and medical rules from YAML + local secrets.

Configuration is validated eagerly (see `validate_settings`) so invalid
values fail fast with a clear message instead of surfacing as a confusing
runtime error deep inside the audio or network stack.

**Security model (non-negotiable):** this application has no Deepgram API
key. The key lives only in the environment of the self-hosted service.
The client authenticates to that host with a *shared secret*, which is
stored locally only as a Windows DPAPI-protected blob (see
`medical_stt/security/`). `Settings` therefore carries a host URL and the
shared secret -- never a provider credential -- and a plaintext secret in
`settings.yaml` is treated as a configuration *error*.
"""
from __future__ import annotations

import copy
import math
import tempfile
import logging
import os
import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Tuple
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv

from . import paths
from .processing.normalize import normalize
from .security import SecretStore, SecretStoreError

log = logging.getLogger("medical_stt.config")

_PKG_DIR = Path(__file__).resolve().parent
_ROOT = _PKG_DIR.parent
#: Settings shipped with the build; copied to the user's profile on first
#: run and used as the fallback when running from a source checkout.
_CONFIG_DIR = _ROOT / "config"
_DATA_DIR = _ROOT / "data"

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


def deepgram_api_host(region: str) -> str:
    """Hostname of the Deepgram API for a configured region."""
    try:
        return DEEPGRAM_REGION_HOSTS[region]
    except KeyError:
        raise ConfigError(
            f"deepgram_region must be one of {sorted(DEEPGRAM_REGION_HOSTS)}, got {region!r}"
        ) from None

#: Plaintext hosts for which http:// is tolerated (development only).
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

#: Keys that must never appear in a YAML settings file.
_FORBIDDEN_SECRET_KEYS = ("api_key", "apikey", "deepgram_api_key", "host_secret", "secret")


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


_ASR_BLOCKED_TOKENS = frozenset({
    "mg", "mcg", "µg", "g", "kg", "ml", "l", "cc", "iu", "meq", "mmol", "mmhg",
    "iv", "im", "po", "sc", "hr", "bid", "tid", "qid", "prn", "npo",
    # These extra Latin clinical terms occur in data/corrections.yaml.
    "icu", "ecg", "mi", "diabetes", "hypertension",
    # Census-derived: every standalone uppercase Latin token of length >= 2
    # that appears in a shipped terminology rule. A provider-side
    # replacement must never introduce one of these without local review.
    'abc', 'abg', 'ac', 'acl', 'acr', 'afi', 'am', 'apache',
    'ards', 'arf', 'atfl', 'av', 'bd', 'bi', 'bmi', 'bnp',
    'bp', 'bpd', 'bph', 'bps', 'bs', 'bun', 'cbc', 'cbd',
    'cbr', 'ccu', 'chf', 'cmc', 'copd', 'cpr', 'cr', 'crif',
    'crl', 'crp', 'cts', 'cva', 'cvp', 'cxr', 'dbp', 'dc',
    'dj', 'ds', 'dvt', 'dw', 'dzp', 'edc', 'edv', 'ef',
    'efw', 'ekg', 'ems', 'ent', 'ercp', 'esr', 'ett', 'fbs',
    'ffp', 'fhr', 'fl', 'ga', 'gcs', 'ger', 'gfr', 'hc',
    'hdl', 'hf', 'hl', 'hps', 'hpv', 'htn', 'hvd', 'id',
    'ii', 'inr', 'iugr', 'ivc', 'lap', 'ldl', 'map', 'mcl',
    'md', 'mn', 'ng', 'ns', 'nt', 'ob', 'or', 'orif',
    'pcnl', 'pcwp', 'pds', 'pe', 'pfna', 'pi', 'plt', 'pm',
    'psa', 'psv', 'pt', 'ptt', 'qh', 'qs', 'rads', 'rbc',
    'ri', 'ros', 'rpoc', 'rr', 'sa', 'sbp', 'sofa', 'sol',
    'tds', 'tg', 'tha', 'tka', 'tsh', 'tul', 'tur', 'turp',
    'wbc',
})
_ASR_BLOCKED_TOKENS = frozenset(token.casefold() for token in _ASR_BLOCKED_TOKENS)
_ASR_LATIN_TOKEN_RE = re.compile(r"(?<!\w)[A-Za-zµμ]+(?!\w)")


def _has_blocked_asr_token(text: str) -> bool:
    return any(match.group().casefold() in _ASR_BLOCKED_TOKENS for match in _ASR_LATIN_TOKEN_RE.finditer(text))


def _contains_non_ascii_letter(text: str) -> bool:
    """True if `text` contains a Persian/Arabic (non-ASCII) letter.

    Persian and Arabic letters are non-ASCII; Latin/English letters and
    ASCII units are ASCII. Used to tell a real provider-side spelling fix
    from one that would smuggle a Latin/English term into the transcript.
    """
    return any(not ch.isascii() and ch.isalpha() for ch in text)


def keyterm_parameter(model: str) -> str:
    """Return the recognition-assistance parameter for `model`.

    `"keyterm"` for Nova-3 (Keyterm Prompting), `"keywords"` for the
    legacy models, `""` for a model we do not know.
    """
    return _KEYTERM_PARAMETER_BY_MODEL.get(model, "")


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


if not getattr(sys, "frozen", False):
    _env_path = _ROOT / ".env"
    if load_dotenv(_env_path, override=False):
        log.info("loaded .env from %s", _env_path)


class ConfigError(ValueError):
    """Raised when settings.yaml / environment values fail validation."""


@dataclass
class Settings:
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


#: Cache of parsed YAML keyed by (path, mtime_ns, size).
#:
#: The terminology/keyterm files are static, shipped with the build and read
#: only. Re-parsing them on every session start cost ~0.37s each -- measured
#: at 18.8s to build 50 sessions -- for a result that never changes. The
#: cache is keyed on size *and* nanosecond mtime, so editing a file still
#: invalidates it and no stale data can be served.
#:
#: A copy is returned on every hit because callers own their result: notably
#: `save_host_credentials` mutates the mapping it reads before rewriting it,
#: and handing out the cached object would corrupt it for everyone else.
_YAML_CACHE: Dict[Tuple[str, int, int], Any] = {}
_YAML_CACHE_LOCK = threading.Lock()

#: Hard cap on cached documents, so a process that reads many distinct paths
#: cannot grow this without bound.
_YAML_CACHE_MAX = 64


def _load_yaml(path: Path) -> Any:
    if not path.is_file():
        return {}
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = None

    if key is not None:
        with _YAML_CACHE_LOCK:
            cached = _YAML_CACHE.get(key)
        if cached is not None:
            return copy.deepcopy(cached)

    with path.open(encoding="utf-8") as f:
        try:
            parsed = yaml.safe_load(f) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"Malformed YAML in {path}: {exc}") from exc

    if key is not None:
        with _YAML_CACHE_LOCK:
            if len(_YAML_CACHE) >= _YAML_CACHE_MAX:
                _YAML_CACHE.clear()
            _YAML_CACHE[key] = parsed
    return copy.deepcopy(parsed)


def _reject_plaintext_secrets(cfg: Dict[str, Any], source: Path) -> None:
    """Fail fast if a settings file contains a secret.

    A plaintext secret in YAML would defeat the DPAPI protection silently
    (and would very likely end up committed or synced), so it is a hard
    error rather than a warning.
    """
    for key in _FORBIDDEN_SECRET_KEYS:
        if key in cfg and cfg[key] not in (None, ""):
            raise ConfigError(
                f"{source} contains a '{key}' entry. Secrets must not be stored "
                "in settings.yaml; set them in the app's settings panel so they "
                "are protected with Windows DPAPI."
            )


_TRUE_TOKENS = frozenset({"1", "true", "yes", "on"})
_FALSE_TOKENS = frozenset({"0", "false", "no", "off"})


def _num(cfg: Dict[str, Any], source: Path, key: str, convert: Any, default: Any) -> Any:
    """Read one numeric setting, refusing anything that is not a number.

    `settings.yaml` is hand-edited, so the two common shapes are a key left
    with no value (`host_timeout_seconds:`, which YAML reads as None) and a
    unit or a decimal comma pasted into it. Feeding either to `int()`/`float()`
    raises a raw `TypeError`/`ValueError` that escapes as a traceback naming
    config.py -- and `app.main()` only catches `ConfigError`, so a packaged
    build dies with a stack trace instead of telling the clinician which line
    to fix. Same contract as every other settings failure in this module.
    """
    raw = cfg.get(key, default)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raw = default
    try:
        return convert(raw.strip() if isinstance(raw, str) else raw)
    except (TypeError, ValueError):
        raise ConfigError(f"{source}: {key} must be a number, got {raw!r}") from None


def _flag(cfg: Dict[str, Any], source: Path, key: str, default: bool) -> bool:
    """Read one boolean setting.

    `bool()` is not the answer here: `bool("false")` is True, so a quoted
    `mip_opt_out: "false"` -- the exact kind of edit a user makes when reading
    a template -- would silently flip a data-retention choice to its opposite.
    YAML already resolves unquoted true/false/yes/no, so this only has to
    handle the quoted and stray forms, and refuse what it cannot read.
    """
    raw = cfg.get(key, default)
    if isinstance(raw, bool):
        return raw
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return bool(default)
    if isinstance(raw, str):
        token = raw.strip().lower()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
    elif isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw in (0, 1):
        return bool(raw)
    raise ConfigError(f"{source}: {key} must be true or false, got {raw!r}")


def user_settings_path() -> Path:
    """Path of the per-user settings file."""
    return paths.settings_path()


def bundled_settings_path() -> Path:
    """Path of the settings file shipped with the build."""
    return _CONFIG_DIR / "settings.yaml"


def initialize_user_config() -> Path:
    """First run: create the per-user data directory and config template.

    Never writes a secret. The template is the shipped `settings.yaml`
    plus a short header explaining where the host secret goes. An existing
    user file is left untouched so local edits survive upgrades.
    """
    paths.ensure_app_data_dir()
    target = user_settings_path()
    if target.is_file():
        return target

    bundled = bundled_settings_path()
    header = (
        "# Medical STT user settings.\n"
        "#\n"
        "# This file is created on first run and may contain no secrets.\n"
        "# Set host_url below to your self-hosted service.\n"
        "# The shared secret is NOT stored here: enter it in the app's\n"
        "# settings panel, where it is protected with Windows DPAPI and kept\n"
        "# in %APPDATA%\\MedicalSTT\\host_secret.dpapi.\n"
        "# The Deepgram API key is never configured on this machine.\n\n"
    )
    if bundled.is_file():
        body = bundled.read_text(encoding="utf-8")
    else:  # pragma: no cover - only if the build dropped config/
        body = (
            "model: nova-3\n"
            "language: fa\n"
            "specialty: general\n"
            "host_url: https://stt.example.com\n"
        )
    # Atomic, like every other settings write in this module: a crash
    # mid-write must not leave a truncated user settings file behind for
    # the next start to choke on.
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(header + body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    log.info("created user settings template at %s", target)
    return target


def load_host_secret() -> str:
    """Read the shared secret from the DPAPI store.

    `MEDICALSTT_HOST_SECRET` overrides it. That override exists purely for
    development/CI on platforms without DPAPI; it is never written to
    disk and never logged.
    """
    override = os.getenv("MEDICALSTT_HOST_SECRET")
    if override:
        return override
    try:
        secret = SecretStore(paths.secret_path()).load()
    except SecretStoreError as exc:
        # A stale/corrupt blob must not crash startup with a traceback --
        # it becomes a normal configuration error instead.
        log.warning("stored host secret is unusable (%s)", type(exc).__name__)
        return ""
    return secret or ""


def save_host_credentials(host_url: str, host_secret: str, client_id: str | None = None) -> None:
    """Validate before touching credentials; replace YAML atomically.

    The encrypted secret and YAML are separate files, not a transaction. If
    replacing YAML fails after secret storage, report that partial outcome.
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
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".settings-", suffix=".tmp")
    secret_saved = False
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            yaml.safe_dump(cfg, stream, allow_unicode=True, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        if host_secret:
            SecretStore(paths.secret_path()).save(host_secret)
            secret_saved = True
        os.replace(temporary, target)
    except (OSError, SecretStoreError) as exc:
        # SecretStoreError is a RuntimeError (DPAPI failure), not an
        # OSError: catching only OSError let it escape raw instead of
        # surfacing as the documented ConfigError partial outcome. Its
        # message never contains the secret itself.
        detail = "Secret saved, but settings were not replaced; retry saving" if secret_saved else "Settings were not saved"
        raise ConfigError(detail) from exc
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


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
    # two failure shapes they exist for (a raw traceback instead of a ConfigError,
    # and `bool("false") is True` inverting a retention setting).
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


MAX_KEYTERMS = 100

#: Deepgram's keyterm limit is 500 *tokens* per request, not a term count,
#: and the tokenizer is not public. Budgeting one token per two characters
#: is conservative for Persian and keeps the request well under the limit
#: (a connection over it is refused at the handshake).
MAX_KEYTERM_CHARS = 1000


def _terms_from_file(path: Path) -> List[str]:
    data = _load_yaml(path)
    terms = data.get("keyterms") if isinstance(data, dict) else data
    if terms is None:
        return []
    if not isinstance(terms, list):
        raise ConfigError(f"{path} must contain a 'keyterms' list")

    return [str(term).strip() for term in terms if term is not None and str(term).strip()]


def load_keyterms(
    specialty: str = "general", max_count: int = MAX_KEYTERMS, max_chars: int = MAX_KEYTERM_CHARS
) -> List[str]:
    """Load general plus selected specialty terms, preserving priority order.

    Both the term count and the total character budget are enforced in
    priority order: general terms first, then the specialty's, and loading
    stops at whichever limit is reached first.
    """
    if not _valid_specialty_name(specialty):
        raise ConfigError("invalid keyterm specialty name")
    if max_count < 0:
        raise ConfigError("maximum keyterm count must be non-negative")
    if max_count == 0:
        return []
    keyterm_dir = _DATA_DIR / "keyterms"
    if keyterm_dir.is_dir():
        paths_to_load = [keyterm_dir / "general.yaml"]
        if specialty != "general":
            selected = keyterm_dir / f"{specialty}.yaml"
            if not selected.is_file():
                raise ConfigError(f"unknown keyterm specialty: {specialty!r}")
            paths_to_load.append(selected)
    else:
        # Existing deployments with only the original file continue to work.
        paths_to_load = [_DATA_DIR / "keyterms.yaml"]

    result: List[str] = []
    seen = set()
    total_chars = 0
    for path in paths_to_load:
        for term in _terms_from_file(path):
            if term in seen:
                continue
            if total_chars + len(term) > max_chars:
                log.warning(
                    "keyterms truncated at %d terms: the %d-character budget "
                    "(Deepgram's 500-token limit) is exhausted by %s",
                    len(result), max_chars, path.name,
                )
                return result
            seen.add(term)
            result.append(term)
            total_chars += len(term)
            if len(result) == max_count:
                return result
    return result


def load_asr_replacements() -> List[str]:
    """Load safe Deepgram ``find:replace`` parameters (``replace`` feature).

    Ownership: this is the *only* provider-side text rewrite the project
    uses. It exists for acoustically confusable Persian spellings that the
    provider itself must fix, and its output then flows through the local
    pipeline (normalization -> terminology) like any other transcript. It
    must never carry clinical numbers, units or a Latin/English term that
    would bypass the locally-reviewed terminology rules -- Deepgram's own
    docs note that ``replace`` is applied verbatim and the find term must
    be lowercase. Violations are configuration errors, not warnings.
    """
    data = _load_yaml(_DATA_DIR / "asr_replacements.yaml")
    replacements = data.get("replacements", []) if isinstance(data, dict) else []
    if not isinstance(replacements, list):
        raise ConfigError("data/asr_replacements.yaml 'replacements' must be a list")

    result: List[str] = []
    seen: Dict[str, str] = {}
    for item in replacements:
        if not isinstance(item, dict) or not item.get("from") or item.get("to") is None:
            continue
        source, target = str(item["from"]).strip(), str(item["to"]).strip()
        if not source or not target:
            raise ConfigError("ASR replacements must have a non-empty from and to")
        # Deepgram advertises the find term as lowercase-only; an uppercase
        # source silently never fires, which would look like an ASR bug.
        if source != source.lower():
            raise ConfigError(
                f"ASR replacement source {source!r} must be lowercase (Deepgram requirement)"
            )
        # Keep provider-side replacement away from clinical numbers; those
        # remain protected locally during all terminology operations.
        if any(ch.isdigit() for ch in source + target) or ":" in source + target:
            raise ConfigError("ASR replacements cannot contain digits or ':'")
        # Provider-side replacements bypass local clinical review: check
        # units/abbreviations on BOTH sides, even for ASCII-only pairs.
        # Keep the stricter no-Latin-target rule for Persian spelling fixes;
        # benign ASCII placeholders still work if neither side is clinical.
        if (
            "%" in source or "%" in target
            or _has_blocked_asr_token(source) or _has_blocked_asr_token(target)
            or (_contains_non_ascii_letter(source) and any(ch.isascii() and ch.isalpha() for ch in target))
        ):
            raise ConfigError(
                f"ASR replacement {source!r} -> {target!r} contains a "
                "Latin/English term or unit; those belong in "
                "data/corrections.yaml where the category/context guards apply"
            )
        if source in seen and seen[source] != target:
            raise ConfigError(
                f"ASR replacement conflict: {source!r} maps to both {seen[source]!r} and {target!r}"
            )
        if source in seen:
            continue  # identical duplicate: harmless, keep one
        seen[source] = target
        result.append(f"{source}:{target}")
    return result


def load_correction_rules_raw() -> List[Dict[str, Any]]:
    """Return the raw categorized rule dicts (see processing/terminology.py
    for the schema). Kept separate from the legacy pair-tuple loader so
    callers can access category metadata."""
    data = _load_yaml(_DATA_DIR / "corrections.yaml")
    if not isinstance(data, dict) or "rules" not in data:
        if data:
            raise ConfigError("data/corrections.yaml must contain a top-level 'rules' list")
        return []
    rules = data.get("rules")
    if rules is None:
        return []
    if not isinstance(rules, list):
        raise ConfigError("data/corrections.yaml 'rules' must be a list")
    raw_rules = [r for r in rules if isinstance(r, dict)]
    from .processing.terminology import parse_rules

    parse_rules(raw_rules)  # fail at load time, including for legacy flat callers
    return raw_rules


def load_correction_rules() -> List[Tuple[str, str]]:
    """Backward-compatible flat (source, target) pairs, ignoring category
    metadata. Prefer TerminologyEngine for anything safety-sensitive."""
    pairs: List[Tuple[str, str]] = []
    for item in load_correction_rules_raw():
        src = item.get("from") or item.get("source") or item.get("src")
        tgt = item.get("to") or item.get("target") or item.get("tgt")
        if src and tgt is not None:
            # Legacy flat callers must see the same canonical form as the
            # terminology engine: a raw Arabic-yeh source would otherwise stay
            # unmatchable after normalize() has run on the transcript.
            pairs.append((normalize(str(src)), normalize(str(tgt))))
    return pairs
