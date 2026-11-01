"""Cached YAML reading, typed scalar coercion and atomic writes.

`settings.yaml` is hand-edited, so every value that reaches `Settings` passes
through `_num`/`_flag`: a bare `int()`/`float()`/`bool()` on human input raises
a raw traceback (which a packaged build cannot show) or -- worse for `bool` --
silently inverts a retention setting.

The cache exists because the terminology/keyterm files are static and shipped
with the build: re-parsing them per session start cost ~0.37 s each (18.8 s to
build 50 sessions). It is keyed on size *and* nanosecond mtime, so editing a
file invalidates it, and a copy is returned on every hit because callers own
their result.
"""
from __future__ import annotations

import copy
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Tuple

import yaml

from .errors import ConfigError
from .model import _FORBIDDEN_SECRET_KEYS

log = logging.getLogger("medical_stt.config")

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



def atomic_write_text(target: Path, text: str) -> None:
    """Write `text` to `target` atomically (temp file + fsync + rename).

    A crash mid-write must not leave a truncated settings file behind for the
    next start to choke on.
    """
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
