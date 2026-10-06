"""The client's configuration surface must match its documentation.

Three artifacts describe the same settings -- `medical_stt/config.py`,
`config/settings.yaml` and `.env.example` -- and nothing kept them in sync.
`host_client_id` was the drift that got noticed: `Settings` declared it, the
README told operators to set it, `deepgram_provider` sent it with every token
request, and it was absent from both the shipped YAML and the env template. A
clinic deploying from the template alone would have had no way to discover the
setting the 50-clinician path requires.

`tests/test_host_docs.py` does the same job for the host side.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Set

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = ROOT / ".env.example"
SETTINGS_YAML = ROOT / "config" / "settings.yaml"
README = ROOT / "README.md"

#: The client package plus its entry point. Deliberately not scripts/ or
#: benchmarks/: those set up a *fake host* and read host-side variables
#: (DEEPGRAM_API_KEY, HOST_ALLOW_HTTP, ...) that must never appear in the
#: client's own configuration template.
CLIENT_SOURCES = (ROOT / "medical_stt", ROOT / "run.py")

#: Environment variables the client reads that are not project configuration:
#: operating-system directories it is handed rather than something an operator
#: would ever set in a .env file.
_NOT_PROJECT_CONFIG = {"APPDATA", "XDG_CONFIG_HOME"}

_READ_PATTERNS = (
    r'os\.getenv\(\s*[\'"]([A-Z][A-Z0-9_]+)[\'"]',
    r'os\.environ\.get\(\s*[\'"]([A-Z][A-Z0-9_]+)[\'"]',
    r'os\.environ\[\s*[\'"]([A-Z][A-Z0-9_]+)[\'"]\s*\]',
)


def _client_sources() -> list[Path]:
    files: list[Path] = []
    for entry in CLIENT_SOURCES:
        files.extend(sorted(entry.rglob("*.py")) if entry.is_dir() else [entry])
    assert files, "the client source scan found nothing -- the paths are stale"
    return files


def _env_vars_read_by_client() -> Set[str]:
    # Read whole files, not line by line: several of these calls wrap the
    # variable name onto the next line, which a line-oriented scan misses --
    # and that is exactly how MEDICALSTT_HOST_CLIENT_ID went undocumented.
    text = "\n".join(path.read_text(encoding="utf-8") for path in _client_sources())
    found: Set[str] = set()
    for pattern in _READ_PATTERNS:
        found.update(re.findall(pattern, text))
    return found - _NOT_PROJECT_CONFIG


def test_every_client_env_var_is_in_the_env_template():
    read = _env_vars_read_by_client()
    assert read, "the env-var scan found nothing -- the patterns are stale"
    template = ENV_EXAMPLE.read_text(encoding="utf-8")
    mentioned = set(re.findall(r"\b[A-Z][A-Z0-9_]{3,}\b", template))
    missing = sorted(read - mentioned)
    assert not missing, (
        ".env.example does not mention these client environment variables: "
        + ", ".join(missing)
    )


def test_shipped_settings_yaml_covers_every_settings_key():
    """`Settings.as_dict()` is the serializable shape of the configuration.

    A key the dataclass declares but the shipped YAML omits has no discoverable
    default: an operator reading config/settings.yaml cannot tell the setting
    exists, and `host_client_id` -- required for any multi-clinician host --
    was exactly that.
    """
    from medical_stt.config import Settings

    dumped = Settings().as_dict()
    assert "host_secret" not in dumped, "the secret must never be serializable"
    shipped = yaml.safe_load(SETTINGS_YAML.read_text(encoding="utf-8"))
    missing = sorted(set(dumped) - set(shipped))
    assert not missing, f"config/settings.yaml omits these settings: {missing}"


def test_shipped_settings_yaml_has_no_unknown_keys():
    """The other direction: a key nobody reads is a typo waiting to happen."""
    from medical_stt.config import Settings

    shipped = yaml.safe_load(SETTINGS_YAML.read_text(encoding="utf-8"))
    unknown = sorted(set(shipped) - set(Settings().as_dict()))
    assert not unknown, f"config/settings.yaml has keys Settings ignores: {unknown}"


def test_the_host_client_id_is_documented_as_required_for_a_registry():
    """An operator must be able to find out when the client id is mandatory."""
    for path in (ENV_EXAMPLE, README):
        text = path.read_text(encoding="utf-8")
        assert "MEDICALSTT_HOST_CLIENT_ID" in text, f"{path.name} never mentions it"
    template = ENV_EXAMPLE.read_text(encoding="utf-8").lower()
    assert "registry" in template or "more than one clinician" in template, (
        ".env.example must say when the client id is required, not just that "
        "it exists"
    )


def test_the_env_template_does_not_carry_a_plaintext_secret():
    """The template documents the variable; it must never hold a value.

    Also the reason the client id is safe to keep in settings.yaml and the
    secret is not: config.py rejects a plaintext `host_secret` there.
    """
    template = ENV_EXAMPLE.read_text(encoding="utf-8")
    for line in template.splitlines():
        stripped = line.lstrip("# ").strip()
        if stripped.startswith("MEDICALSTT_HOST_SECRET="):
            assert stripped == "MEDICALSTT_HOST_SECRET=", (
                f".env.example carries a value for the secret: {stripped!r}"
            )
