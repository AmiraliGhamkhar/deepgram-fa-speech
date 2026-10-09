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
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Tuple
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv

from . import paths
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

#: Plaintext hosts for which http:// is tolerated (development only).
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

#: Keys that must never appear in a YAML settings file.
_FORBIDDEN_SECRET_KEYS = ("api_key", "apikey", "deepgram_api_key", "host_secret", "secret")


def _valid_specialty_name(value: str) -> bool:
    return bool(value) and value.replace("_", "").replace("-", "").isalnum()


def _valid_client_id(value: str) -> bool:
    """Mirror of `host.core.is_valid_client_id`.

    Duplicated deliberately rather than imported: the client package must
    not depend on the host package, which runs on a different machine.
    """
    if not 1 <= len(value) <= 64:
        return False
    return all(ch.isalnum() or ch in "-_." for ch in value)


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


load_dotenv(_ROOT / ".env")
load_dotenv()


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
    target.write_text(header + body, encoding="utf-8")
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
        log.warning("stored host secret is unusable: %s", exc)
        return ""
    return secret or ""


def save_host_credentials(host_url: str, host_secret: str) -> None:
    """Persist the host URL in settings.yaml and the secret via DPAPI."""
    initialize_user_config()
    if host_secret:
        SecretStore(paths.secret_path()).save(host_secret)

    target = user_settings_path()
    cfg = _load_yaml(target)
    if not isinstance(cfg, dict):
        raise ConfigError(f"{target} must contain a mapping at the top level")
    cfg["host_url"] = host_url.strip()
    with target.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, allow_unicode=True, sort_keys=False)


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

    if settings.sample_rate not in (8000, 16000, 22050, 24000, 44100, 48000):
        errors.append(f"sample_rate must be a standard rate, got {settings.sample_rate}")
    if settings.channels not in (1, 2):
        errors.append(f"channels must be 1 or 2, got {settings.channels}")
    if not (0.01 <= settings.block_duration <= 1.0):
        errors.append(f"block_duration must be between 0.01 and 1.0 seconds, got {settings.block_duration}")
    if not (10 <= settings.endpointing <= 10000):
        errors.append(f"endpointing must be between 10 and 10000 ms, got {settings.endpointing}")
    if not (0 <= settings.utterance_end_ms <= 20000):
        errors.append(f"utterance_end_ms must be between 0 and 20000 ms, got {settings.utterance_end_ms}")
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
            "host_client_id must be 1-64 characters of letters, digits, "
            "'-', '_' or '.' (see: python -m host.provision)"
        )
    if not (1.0 <= settings.host_timeout_seconds <= 60.0):
        errors.append("host_timeout_seconds must be between 1 and 60")
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

    return Settings(
        model=os.getenv("DEEPGRAM_MODEL", cfg.get("model", "nova-3")),
        language=os.getenv("DEEPGRAM_LANGUAGE", cfg.get("language", "fa")),
        sample_rate=int(cfg.get("sample_rate", 16000)),
        channels=int(cfg.get("channels", 1)),
        block_duration=float(cfg.get("block_duration", 0.08)),
        endpointing=int(cfg.get("endpointing", 400)),
        utterance_end_ms=int(cfg.get("utterance_end_ms", 1200)),
        inject_mode=cfg.get("inject_mode", "paste"),
        paste_settle_seconds=float(cfg.get("paste_settle_seconds", 0.12)),
        restore_clipboard=bool(cfg.get("restore_clipboard", True)),
        overlay_enabled=bool(cfg.get("overlay_enabled", True)),
        reconnect_delay=float(cfg.get("reconnect_delay", 2.0)),
        max_reconnect_attempts=int(cfg.get("max_reconnect_attempts", 0)),
        reconnect_backoff_max=float(cfg.get("reconnect_backoff_max", 30.0)),
        reconnect_jitter=float(cfg.get("reconnect_jitter", 0.5)),
        enable_context_dependent_terms=bool(cfg.get("enable_context_dependent_terms", False)),
        specialty=str(cfg.get("specialty", "general")),
        medical_confidence_threshold=float(cfg.get("medical_confidence_threshold", 0.65)),
        use_asr_replacements=bool(cfg.get("use_asr_replacements", True)),
        host_url=os.getenv("MEDICALSTT_HOST_URL", str(cfg.get("host_url", "") or "")),
        host_secret=load_host_secret(),
        host_client_id=os.getenv(
            "MEDICALSTT_HOST_CLIENT_ID", str(cfg.get("host_client_id", "") or "")
        ).strip(),
        host_timeout_seconds=float(cfg.get("host_timeout_seconds", 10.0)),
        session_ttl_seconds=int(cfg.get("session_ttl_seconds", 30)),
    )


MAX_KEYTERMS = 100


def _terms_from_file(path: Path) -> List[str]:
    data = _load_yaml(path)
    terms = data.get("keyterms") if isinstance(data, dict) else data
    if terms is None:
        return []
    if not isinstance(terms, list):
        raise ConfigError(f"{path} must contain a 'keyterms' list")

    return [str(term).strip() for term in terms if term is not None and str(term).strip()]


def load_keyterms(specialty: str = "general", max_count: int = MAX_KEYTERMS) -> List[str]:
    """Load general plus selected specialty terms, preserving priority order."""
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
    for path in paths_to_load:
        for term in _terms_from_file(path):
            if term not in seen:
                seen.add(term)
                result.append(term)
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
        # A fix for a Persian/Arabic *spelling* must not introduce a
        # Latin/English term or a unit in the target: those are owned by
        # data/corrections.yaml, where `requires_context`/`dangerous`
        # metadata and human review apply. A provider-side `replace` would
        # bypass that review and rewrite the transcript verbatim. ASCII-only
        # placeholder pairs (purely alphanumeric, no non-ASCII source) stay
        # allowed so tooling/tests are not needlessly constrained.
        if _contains_non_ascii_letter(source) and any(
            (ch.isascii() and ch.isalpha()) or ch == "%" for ch in target
        ):
            raise ConfigError(
                f"ASR replacement {source!r} -> {target!r} introduces a "
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
    return [r for r in rules if isinstance(r, dict)]


def load_correction_rules() -> List[Tuple[str, str]]:
    """Backward-compatible flat (source, target) pairs, ignoring category
    metadata. Prefer TerminologyEngine for anything safety-sensitive."""
    pairs: List[Tuple[str, str]] = []
    for item in load_correction_rules_raw():
        src = item.get("from") or item.get("source") or item.get("src")
        tgt = item.get("to") or item.get("target") or item.get("tgt")
        if src and tgt is not None:
            pairs.append((str(src), str(tgt)))
    return pairs
