"""Host documentation must cover every environment variable the host reads.

The `.env.example` templates ship with empty placeholders and are the
canonical list, but they are easy to forget when a setting is added. This
test fails CI when the code reads a variable that neither `host/README.md`
nor `host/.env.example` documents, so an operator cannot read through either
and find a setting silently missing.

It also pins the two claims that are easy to regress silently:

* the Deepgram grant endpoint is HTTPS;
* the audio path is not proxied through the host.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Set

ROOT = Path(__file__).resolve().parent.parent
HOST_README = ROOT / "host" / "README.md"
HOST_ENV_EXAMPLE = ROOT / "host" / ".env.example"
ROOT_README = ROOT / "README.md"

#: Modules that read configuration from the environment.
CONFIG_SOURCES = ("host/core.py", "host/app.py", "host/provision.py")

#: Every way the host reads a variable. All four forms must be matched or
#: this test silently under-reports -- `_int`/`_float` cover every numeric
#: setting, which is the majority of them.
_READ_PATTERNS = (
    r'source\.get\(\s*[\'"]([A-Z][A-Z0-9_]+)[\'"]',
    r'os\.getenv\(\s*[\'"]([A-Z][A-Z0-9_]+)[\'"]',
    r'os\.environ\.get\(\s*[\'"]([A-Z][A-Z0-9_]+)[\'"]',
    r'_(?:int|float)\(\s*[\'"]([A-Z][A-Z0-9_]+)[\'"]',
)


def _env_vars_read_by_host() -> Set[str]:
    source = "\n".join((ROOT / rel).read_text(encoding="utf-8") for rel in CONFIG_SOURCES)
    found: Set[str] = set()
    for pattern in _READ_PATTERNS:
        found.update(re.findall(pattern, source))
    return found


def _documented(text: str) -> Set[str]:
    return set(re.findall(r"`([A-Z][A-Z0-9_]+)`", text))


def test_every_host_env_var_is_in_the_settings_table():
    """No variable the host reads may be absent from the settings table.

    Checked against the table rows specifically, not "mentioned somewhere":
    a variable that appears only inside an example command is not
    documented for an operator who needs to know what it does or what it
    defaults to.
    """
    read = _env_vars_read_by_host()
    assert read, "the env-var scan found nothing -- the patterns are stale"

    readme = HOST_README.read_text(encoding="utf-8")
    table_vars = set()
    for line in readme.splitlines():
        if line.startswith("|"):
            table_vars.update(re.findall(r"`([A-Z][A-Z0-9_]+)`", line))

    missing = sorted(read - table_vars)
    assert not missing, (
        "host/README.md's settings table does not document these environment "
        "variables: " + ", ".join(missing)
    )


def test_every_host_env_var_is_documented():
    """Every variable the host reads must appear in host/README.md at all."""
    read = _env_vars_read_by_host()
    documented = _documented(HOST_README.read_text(encoding="utf-8"))
    missing = sorted(read - documented)
    assert not missing, (
        "host/README.md does not mention these environment variables: "
        + ", ".join(missing)
    )


def test_every_host_env_var_is_in_the_env_template():
    """`.env.example` is the file an operator actually copies.

    The README table above was already enforced; the template was not, and 12
    variables the service reads were absent from it -- including
    HOST_METRICS_ADMIN_TOKEN, HOST_CLIENTS_FILE, HOST_MAX_INFLIGHT_GRANTS and
    HOST_MAX_REQUEST_BODY_BYTES. An operator deploying from the template alone
    could not discover them, and could not tell that the ones they did see
    were the whole story.
    """
    read = _env_vars_read_by_host()
    template = HOST_ENV_EXAMPLE.read_text(encoding="utf-8")
    mentioned = set(re.findall(r"\b[A-Z][A-Z0-9_]{3,}\b", template))
    missing = sorted(read - mentioned)
    assert not missing, (
        "host/.env.example does not mention these environment variables: "
        + ", ".join(missing)
    )


def test_env_template_marks_the_inert_rate_limit_variable():
    """The template must not present a retired setting as tunable.

    The README table already has to say "Ignored"; the template is the more
    likely place for an operator to uncomment a line and expect an effect.
    """
    template = HOST_ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "HOST_RATE_LIMIT_REQUESTS" in template, (
        "the variable is still accepted for compatibility, so removing it "
        "from the template without a migration note would be worse than "
        "listing it"
    )
    index = template.index("HOST_RATE_LIMIT_REQUESTS")
    preceding = template[max(0, index - 400):index].lower()
    assert "inert" in preceding, (
        "HOST_RATE_LIMIT_REQUESTS must be labelled inert immediately above "
        "its line in host/.env.example"
    )


def test_client_credentials_are_documented():
    """The client id must be discoverable without reading the host source."""
    readme = HOST_README.read_text(encoding="utf-8")
    for term in ("X-Client-Id", "HOST_CLIENTS_FILE", "host.provision", "DPAPI"):
        assert term in readme, f"host/README.md should mention {term}"


def test_deprecated_rate_limit_var_is_flagged_not_silently_ignored():
    """`HOST_RATE_LIMIT_REQUESTS` is inert; the docs must say so.

    A retired setting that is silently accepted is the worst outcome: an
    operator tunes it and sees no effect. The host logs a warning, and the
    documentation must match.
    """
    readme = HOST_README.read_text(encoding="utf-8")
    # Locate the variable's row in the settings table rather than any
    # passing mention, so a prose sentence elsewhere cannot satisfy this.
    rows = [
        line
        for line in readme.splitlines()
        if line.startswith("|") and "`HOST_RATE_LIMIT_REQUESTS`" in line
    ]
    assert rows, "HOST_RATE_LIMIT_REQUESTS is missing from the settings table"
    row = rows[0].lower()
    assert "ignored" in row or "no longer" in row, (
        "the settings table must mark HOST_RATE_LIMIT_REQUESTS as not in "
        f"effect; row was: {rows[0]}"
    )


def test_deepgram_grant_is_https_only():
    assert "https://api.deepgram.com" in (ROOT / "host" / "core.py").read_text(
        encoding="utf-8"
    )
    assert "http://api.deepgram.com" not in (ROOT / "host" / "core.py").read_text(
        encoding="utf-8"
    )


def test_docs_do_not_promise_the_host_proxies_audio():
    """The host is a token service; the docs must not imply otherwise.

    Audio goes client -> Deepgram directly. If the documentation ever
    implies a proxy, an operator could size the host for audio throughput
    it will never carry.
    """
    for path in (ROOT_README, HOST_README):
        text = path.read_text(encoding="utf-8").lower()
        for phrase in (
            "proxies the audio",
            "host proxies audio",
            "audio is proxied",
            "streams audio through the host",
        ):
            assert phrase not in text, f"{path.name} implies the host proxies audio"


def test_production_readiness_claim_is_not_overstated():
    """The README must not promise verified provider capacity.

    Application capacity is proven by tests; Deepgram's own concurrent-stream
    allowance is not. The README has to keep those two apart.
    """
    readme = ROOT_README.read_text(encoding="utf-8")
    lowered = readme.lower()
    assert "application capacity" in lowered
    assert "provider capacity" in lowered
    # The unsupported claim must be refused explicitly, not implied.
    assert "what is *not* claimed" in lowered or "what is not claimed" in lowered
