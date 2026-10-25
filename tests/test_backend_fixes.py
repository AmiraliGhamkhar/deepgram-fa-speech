"""Regression tests for the host/backend bug hunt.

Each test pins one finding so the defect cannot return silently. Everything
here was reproduced against the pre-fix tree; the tests are written to fail on
that tree and pass on the fixed one.

* B1  a grant failure that is not a `GrantError` used to escape as an
      uncontrolled 500 the counters never recorded;
* B2  a 3xx response body from the grant endpoint was parsed as a token grant;
* B3  an empty `HOST_CLIENTS_FILE` started a host that can authenticate nobody;
* B4  `client_id` accepted any Unicode alphanumeric, so a homoglyph of an
      existing id was a *different* identity rather than the same one;
* B5  single-identity (legacy) mode silently throttled a whole clinic and said
      nothing at startup;
* B6  duplicated registry lines were resolved "last line wins" in silence;
* B7  a malformed numeric/boolean setting bypassed `ConfigError`;
* B8  the per-client metric machinery was never wired up, the sync transport
      ignored `Retry-After`, and a stale `authorize()` helper sat next to the
      live authentication path.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOST_DIR = ROOT / "host"
if str(HOST_DIR) not in sys.path:
    sys.path.insert(0, str(HOST_DIR))

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

# `host/app.py` builds its app at import time -- that is how a missing key
# fails the deploy instead of the first Start press -- so the environment has
# to be plausible before this module imports it. Both values are fake, and
# nothing else is seeded: every request-level test passes `allow_http` in its
# own `HostSettings`, so this module cannot leak a plaintext-mode default into
# another suite's assertions. (`tests/conftest.py` has already popped the real
# `DEEPGRAM_API_KEY`, which is why these defaults apply at all.)
os.environ.setdefault("DEEPGRAM_API_KEY", "dg_fake_host_side_only")
os.environ.setdefault("HOST_SHARED_SECRET", "s" * 40)

import app as app_module  # type: ignore[import-not-found]  # noqa: E402
import core  # type: ignore[import-not-found]  # noqa: E402
import metrics as metrics_module  # type: ignore[import-not-found]  # noqa: E402
import provision  # type: ignore[import-not-found]  # noqa: E402

API_KEY = "dg_key_not_real"
SECRET = "s" * 40
FAKE_GRANT = {"access_token": "issued-token", "expires_in": 30}


def _settings(**overrides: Any) -> "core.HostSettings":
    base: Dict[str, Any] = dict(
        deepgram_api_key=API_KEY,
        shared_secret=SECRET,
        default_ttl_seconds=30,
        max_ttl_seconds=3600,
    )
    base.update(overrides)
    return core.HostSettings(**base)


def _run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


class _StubAsyncClient:
    """An `httpx.AsyncClient` stand-in whose `post` does whatever we need."""

    def __init__(self, response: Any = None, error: BaseException | None = None) -> None:
        self._response = response
        self._error = error
        self.closed = False

    async def post(self, *_args: Any, **_kwargs: Any) -> Any:
        if self._error is not None:
            raise self._error
        return self._response

    async def aclose(self) -> None:
        self.closed = True


class _StubResponse:
    def __init__(self, status_code: int = 200, payload: Any = None, headers: Any = None) -> None:
        self.status_code = status_code
        self.headers = dict(headers or {})
        self._payload = payload

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


# ---------------------------------------------------------------------------
# B1: no failure may escape the grant as an uncontrolled 500
# ---------------------------------------------------------------------------


def test_a_non_httpx_failure_becomes_a_controlled_grant_error() -> None:
    """A closed pool / an unencodable header must not surface as a bare 500.

    `AsyncGrantClient.grant` used to translate only `httpx.TimeoutException`
    and `httpx.RequestError`. Everything else -- `RuntimeError` once the pool
    is closed, `TypeError`/`UnicodeEncodeError` while encoding the
    `Authorization` header -- propagated out of the route.
    """
    for exc in (
        RuntimeError("Cannot send a request, as the client has been closed."),
        UnicodeEncodeError("ascii", "Token x", 0, 1, "ordinal not in range(128)"),
        ValueError("internal bookkeeping bug"),
    ):
        client = core.AsyncGrantClient(client=_StubAsyncClient(error=exc))
        with pytest.raises(core.GrantError) as excinfo:
            _run(client.grant(API_KEY, 30))
        assert excinfo.value.status == 502
        # The message the client sees must name no credential.
        assert API_KEY not in str(excinfo.value)
        assert type(exc).__name__ not in str(excinfo.value)


def test_a_grant_error_is_never_rewrapped_by_the_catch_all() -> None:
    """The mapping to 503/504 for real upstream faults must survive."""
    import httpx

    client = core.AsyncGrantClient(client=_StubAsyncClient(error=httpx.ConnectError("refused")))
    with pytest.raises(core.GrantError) as excinfo:
        _run(client.grant(API_KEY, 30))
    assert excinfo.value.status == 503


def test_a_pasted_non_ascii_api_key_refuses_to_start() -> None:
    """A smart quote from a document is a startup error, not a 500 per Start."""
    with pytest.raises(core.ConfigurationError) as excinfo:
        core.HostSettings.from_env({"DEEPGRAM_API_KEY": "dg_key\u2019not_real", "HOST_SHARED_SECRET": SECRET})
    message = str(excinfo.value)
    assert "DEEPGRAM_API_KEY" in message
    # A configuration error is logged; the key must never be echoed into it.
    assert "dg_key" not in message
    assert "\u2019" not in message


@pytest.mark.parametrize(
    "key",
    ["dg_key\nX-Injected: 1", "dg_key not_real", "dg_\u00a0_key", "  ", ""],
)
def test_a_key_with_whitespace_or_control_characters_refuses_to_start(key: str) -> None:
    env = {"DEEPGRAM_API_KEY": key, "HOST_SHARED_SECRET": SECRET}
    with pytest.raises(core.ConfigurationError):
        core.HostSettings.from_env(env)


def test_a_normal_deepgram_key_shape_is_accepted() -> None:
    settings = core.HostSettings.from_env({"DEEPGRAM_API_KEY": "sk_temp_9f8a7b6c5d4e3f2a1b0c",
                                           "HOST_SHARED_SECRET": SECRET})
    assert settings.deepgram_api_key == "sk_temp_9f8a7b6c5d4e3f2a1b0c"


def test_the_error_message_never_contains_the_key() -> None:
    """`check_api_key_shape` names an offset and a class, never a value."""
    key = "dg_" + "a" * 40 + "\u00e9" + "b" * 40
    with pytest.raises(core.ConfigurationError) as excinfo:
        core.check_api_key_shape(key)
    assert key not in str(excinfo.value)
    assert "position 43" in str(excinfo.value)


# ---------------------------------------------------------------------------
# B2: a redirect is not a grant
# ---------------------------------------------------------------------------

REDIRECT_PAYLOAD = {"access_token": "attacker-chosen", "expires_in": 3600}


def test_a_redirect_response_is_not_accepted_as_a_grant() -> None:
    """`follow_redirects=False` stops the key being re-sent; it must also stop
    the redirect *body* being read as a token.

    A captive portal, a `Redirect`/`RewriteRule` on a shared host, or a
    TLS-intercepting proxy that answers with JSON would otherwise mint a
    session the host never obtained from Deepgram.
    """
    for status in (301, 302, 303, 307, 308, 100, 204):
        client = core.AsyncGrantClient(
            client=_StubAsyncClient(response=_StubResponse(status, REDIRECT_PAYLOAD))
        )
        with pytest.raises(core.GrantError) as excinfo:
            _run(client.grant(API_KEY, 30))
        assert excinfo.value.status in (502, 503)
        assert "attacker-chosen" not in str(excinfo.value)


def test_2xx_responses_are_still_accepted() -> None:
    for status in (200, 201, 299):
        client = core.AsyncGrantClient(
            client=_StubAsyncClient(response=_StubResponse(status, dict(FAKE_GRANT)))
        )
        assert _run(client.grant(API_KEY, 30)) == FAKE_GRANT


def test_the_sync_transport_applies_the_same_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two transports must not diverge.

    `scripts/measure_capacity.py`, the benchmarks and these tests all drive the
    synchronous path; a grant accepted there but refused in production (or the
    reverse) means the harness validates behaviour nobody ever runs.
    """
    import httpx

    captured: Dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> _StubResponse:
        captured.update(kwargs)
        return _StubResponse(302, REDIRECT_PAYLOAD, {"Retry-After": "7"})

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(core.GrantError) as excinfo:
        core.grant_session(_settings(), 30)
    assert excinfo.value.status == 503
    assert excinfo.value.retry_after == 7, "the sync path must honor Retry-After too"
    assert captured["headers"]["Authorization"] == f"Token {API_KEY}"


def test_the_async_transport_honors_retry_after_on_every_refusal() -> None:
    client = core.AsyncGrantClient(
        client=_StubAsyncClient(
            response=_StubResponse(429, None, {"Retry-After": "12"}),
        )
    )
    with pytest.raises(core.GrantError) as excinfo:
        _run(client.grant(API_KEY, 30))
    assert (excinfo.value.status, excinfo.value.retry_after) == (503, 12)


# ---------------------------------------------------------------------------
# B3: a registry that can authenticate nobody must not start
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("content", ["", "\n\n   \n", "# doctors\n# provisioned 2026-10-12\n"])
def test_an_empty_registry_refuses_to_start(tmp_path: Path, content: str) -> None:
    path = tmp_path / "clients.txt"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(core.ConfigurationError) as excinfo:
        core.ClientRegistry.from_file(str(path))
    assert "no client entries" in str(excinfo.value)
    assert str(path) in str(excinfo.value)


def test_a_missing_registry_file_still_names_the_file_not_a_traceback(tmp_path: Path) -> None:
    with pytest.raises(core.ConfigurationError) as excinfo:
        core.ClientRegistry.from_file(str(tmp_path / "absent.txt"))
    assert "absent.txt" in str(excinfo.value)


def test_legacy_mode_needs_no_registry_file() -> None:
    """The other side of B3: refusing an empty file must not break legacy mode."""
    settings = _settings(clients_file="")
    registry = settings.build_client_registry()
    assert len(registry) == 1
    assert registry.client_ids == (core.LEGACY_CLIENT_ID,)


def test_create_app_refuses_an_empty_registry(tmp_path: Path) -> None:
    path = tmp_path / "clients.txt"
    path.write_text("# provisioned, then truncated by a half-finished copy\n", encoding="utf-8")
    with pytest.raises(core.ConfigurationError):
        app_module.create_app(_settings(clients_file=str(path)))


# ---------------------------------------------------------------------------
# B4: the client-id grammar is ASCII
# ---------------------------------------------------------------------------

VALID_IDS = ["doctor-01", "ward.7_pc-03", "A9", "a" * 64, "x", "0", "nurse-01.f2",
             "doctor-007", "client-ab12cd34"]
INVALID_IDS = [
    "",
    "a" * 65,
    "has space",
    "new\nline",
    "a\rb",
    "sl/ash",
    "..",
    ".",
    "-",
    "_",
    "\u00e9",                     # é -- a letter, but not one the grammar means
    "\u0441\u0430f\u00e9",          # Cyrillic "sa" + é: confusable with "safé"
    "\uff21\uff22\uff23",           # fullwidth ABC
    "\u4e2d\u6587",                 # CJK
    "\u2460\u2461",                 # circled digits
    "\U0001d7d8\U0001d7d9",        # mathematical double-struck digits
    "doctor\u200b01",              # zero-width space
    "caf\u00e9",
]


@pytest.mark.parametrize("client_id", VALID_IDS)
def test_valid_ids_are_accepted(client_id: str) -> None:
    assert core.is_valid_client_id(client_id) is True
    assert core.normalize_client_id(client_id) == client_id


@pytest.mark.parametrize("client_id", INVALID_IDS)
def test_confusable_or_unsafe_ids_are_refused(client_id: str) -> None:
    """An id is a rate-limit key, a revocation handle and a log field.

    A non-ASCII id is not "a different id that looks similar" in any useful
    sense: it is a homoglyph of an existing one that survives a copy-paste and
    a `grep`, which is precisely what makes revocation unsafe.
    """
    assert core.is_valid_client_id(client_id) is False
    assert core.normalize_client_id(client_id) == ""


def test_all_three_copies_of_the_grammar_agree() -> None:
    """`host/core.py`, `host/provision.py` and `medical_stt/config.py`.

    The duplication is deliberate (three deployment targets, one grammar), so
    the only defence against drift is a test that compares them on the same
    inputs. An id one accepts and another refuses is a device that can never
    connect, reported as a credential error.
    """
    from medical_stt.config import _valid_client_id

    sample = VALID_IDS + INVALID_IDS + [f"prefix-{i}" for i in range(20)]
    for client_id in sample:
        host_verdict = core.is_valid_client_id(client_id)
        assert provision._is_valid_client_id(client_id) is host_verdict, client_id
        assert _valid_client_id(client_id) is host_verdict, client_id


def test_an_invalid_registry_line_still_names_its_line_number(tmp_path: Path) -> None:
    path = tmp_path / "clients.txt"
    digest = core.hash_client_secret("x" * 32)
    path.write_text(f"doctor-01:{digest}\ncaf\u00e9:{digest}\n", encoding="utf-8")
    with pytest.raises(core.ConfigurationError) as excinfo:
        core.ClientRegistry.from_file(str(path))
    assert "line 2" in str(excinfo.value)


def test_provision_refuses_a_non_ascii_client_id(tmp_path: Path) -> None:
    out = tmp_path / "clients.txt"
    result = subprocess.run(
        [sys.executable, "-m", "host.provision", "--client-id", "caf\u00e9-01", "--out", str(out)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2
    assert "not valid" in result.stderr
    assert "ASCII" in result.stderr
    assert not out.exists()


def test_provision_never_mints_an_id_the_registry_loader_refuses(tmp_path: Path) -> None:
    """A prefix in any script must produce a loadable id, not a broken host.

    Before the fix, `--prefix "\u062f\u06a9\u062a\u0631"` (Persian "doctor") minted
    ids the registry loader rejected wholesale, so the host refused to start at
    all -- for every clinician, not just that one device.
    """
    out = tmp_path / "clients.txt"
    for prefix in ("\u062f\u06a9\u062a\u0631", "doctor 01", "caf\u00e9!", "***"):
        for index in (None, 7):
            client_id = provision.generate_client_id(prefix, index)
            assert core.is_valid_client_id(client_id), (prefix, client_id)
    result = subprocess.run(
        [sys.executable, "-m", "host.provision", "--count", "3", "--prefix", "doctor 01",
         "--start-index", "--out", str(out)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert len(core.ClientRegistry.from_file(str(out))) == 3


def test_the_registry_refuses_a_dot_only_id_written_by_hand(tmp_path: Path) -> None:
    path = tmp_path / "clients.txt"
    digest = core.hash_client_secret("x" * 32)
    path.write_text(f"..:{digest}\n", encoding="utf-8")
    with pytest.raises(core.ConfigurationError) as excinfo:
        core.ClientRegistry.from_file(str(path))
    assert "line 1" in str(excinfo.value)


# ---------------------------------------------------------------------------
# B6: duplicated registry lines must be visible, not silently resolved
# ---------------------------------------------------------------------------


def test_duplicate_registry_lines_load_with_a_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Appending stays legal (that is how `host.provision` re-issues one
    device), but "the last line wins, so the deployed secret is now dead"
    must not be silent."""
    path = tmp_path / "clients.txt"
    first = core.hash_client_secret("old-secret-value-0123456789abcd")
    second = core.hash_client_secret("new-secret-value-0123456789-abc")
    path.write_text(
        f"doctor-01:{first}\ndoctor-02:{first}\ndoctor-01:{second}\n", encoding="utf-8"
    )
    with caplog.at_level(logging.WARNING, logger="medical_stt.host"):
        registry = core.ClientRegistry.from_file(str(path))
    assert len(registry) == 2, "a duplicate collapses to one identity"
    assert registry.verify("doctor-01", "new-secret-value-0123456789-abc") is not None
    assert registry.verify("doctor-01", "old-secret-value-0123456789abcd") is None
    assert "duplicated client_id" in caplog.text
    assert "doctor-01" in caplog.text
    assert "doctor-02" not in caplog.text, "only the ids that actually collide"


def test_a_registry_without_duplicates_logs_nothing(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "clients.txt"
    digest = core.hash_client_secret("x" * 32)
    path.write_text(f"doctor-01:{digest}\ndoctor-02:{digest}\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="medical_stt.host"):
        core.ClientRegistry.from_file(str(path))
    assert "duplicated" not in caplog.text


# ---------------------------------------------------------------------------
# B8 (part): the dead single-identity helper stays dead
# ---------------------------------------------------------------------------


def test_there_is_only_one_authentication_path() -> None:
    """`core.authorize()` compared the shared secret on its own.

    It was unreachable, and an older whole-service credential check sitting
    next to the per-device one is exactly what a future edit would re-wire by
    accident -- reintroducing a single identity for a 50-clinician site.
    """
    assert not hasattr(core, "authorize")
    assert callable(core.authenticate_client)


# ---------------------------------------------------------------------------
# B5 + B8: startup warnings and per-client observability
# ---------------------------------------------------------------------------


@pytest.fixture()
def service_factory(monkeypatch: pytest.MonkeyPatch):
    """The real ASGI app with the Deepgram grant stubbed on the event loop."""

    async def fake_grant(_settings: Any, _ttl: int, _client: Any) -> Any:
        return "issued-token", 30

    monkeypatch.setattr(app_module, "grant_session_async", fake_grant)
    return app_module


def _registry_file(tmp_path: Path, ids: list, secret: str) -> Path:
    path = tmp_path / "clients.txt"
    digest = core.hash_client_secret(secret)
    path.write_text("".join(f"{client_id}:{digest}\n" for client_id in ids), encoding="utf-8")
    return path


def test_legacy_mode_says_so_once_at_startup(service_factory: Any, caplog: pytest.LogCaptureFixture) -> None:
    """The docs already warn that legacy mode is not a 50-user configuration.

    The runtime must say it too: the whole site shares one token bucket, so the
    41st clinician hears "the service is at capacity" and nobody hears "this
    deployment was never configured for a clinic".
    """
    with caplog.at_level(logging.WARNING, logger="medical_stt.host"):
        service = service_factory.create_app(_settings(clients_file=""))
    text = caplog.text
    assert "single-identity (legacy) mode" in text
    assert "host.provision" in text
    assert service.state.metrics.gauge_value("registry_clients") == 1


def test_a_provisioned_registry_does_not_warn_about_legacy_mode(
    service_factory: Any, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    registry = _registry_file(tmp_path, ["doctor-01"], SECRET)
    with caplog.at_level(logging.WARNING, logger="medical_stt.host"):
        service_factory.create_app(_settings(clients_file=str(registry)))
    assert "single-identity" not in caplog.text


def test_legacy_shedding_is_attributable_per_identity(
    service_factory: Any, tmp_path: Path
) -> None:
    """50 concurrent Starts need 50 identities -- and each one is measurable."""
    from fastapi.testclient import TestClient

    ids = [f"doctor-{index:02d}" for index in range(50)]
    registry = _registry_file(tmp_path, ids, SECRET)
    service = service_factory.create_app(
        _settings(clients_file=str(registry), allow_http=True)
    )
    client = TestClient(service)
    responses = [
        client.post(
            "/v1/session",
            headers={"Authorization": f"Bearer {SECRET}", "X-Client-Id": client_id},
            json={},
        )
        for client_id in ids
    ]
    assert [response.status_code for response in responses] == [200] * 50
    body = service.state.metrics.render()
    for client_id in ("doctor-00", "doctor-49"):
        assert f'session_requests_by_client_total{{client_id="{client_id}",outcome="ok"}} 1' in body


def test_fabricated_ids_cannot_inflate_the_per_client_metric(
    service_factory: Any, tmp_path: Path
) -> None:
    """The per-client series is labelled with the *authenticated* id only."""
    from fastapi.testclient import TestClient

    registry = _registry_file(tmp_path, ["doctor-01"], SECRET)
    service = service_factory.create_app(
        _settings(clients_file=str(registry), allow_http=True)
    )
    client = TestClient(service)
    for index in range(20):
        client.post(
            "/v1/session",
            headers={"Authorization": "Bearer nope", "X-Client-Id": f"fabricated-{index}"},
            json={},
        )
    body = service.state.metrics.render()
    assert "fabricated-" not in body
    assert 'session_requests_by_client_total{client_id="doctor-01"' not in body
    assert service.state.metrics.counter_value("auth_failures_total") == 20


# ---------------------------------------------------------------------------
# B1 + B8: the counters must reconcile for every terminal outcome
# ---------------------------------------------------------------------------


def test_every_authenticated_request_is_counted_as_success_or_failure(
    service_factory: Any,
) -> None:
    from fastapi.testclient import TestClient

    service = service_factory.create_app(_settings(allow_http=True, max_request_body_bytes=64))
    metrics = service.state.metrics
    client = TestClient(service)
    headers = {"Authorization": f"Bearer {SECRET}"}

    assert client.post("/v1/session", headers=headers, json={}).status_code == 200
    # A TTL outside the allowed range -> 422, produced no token.
    assert client.post("/v1/session", headers=headers, json={"ttl_seconds": 99999}).status_code == 422
    # A declared body over the cap -> 413, produced no token, nothing buffered.
    assert client.post(
        "/v1/session", headers=headers, content=b'{"x":"' + b"y" * 200 + b'"}'
    ).status_code == 413

    starts = metrics.counter_value("session_starts_total")
    assert starts == 3
    assert metrics.counter_value("session_success_total") == 1
    assert metrics.counter_value("session_failures_total") == starts - 1, (
        "requests that produced no token must be counted as failures: without "
        "that, an operator cannot tell 'no traffic' from 'all traffic failing'"
    )
    assert metrics.counter_value("request_body_rejections_total") == 1
    assert metrics.counter_value(
        "session_requests_by_client_total", client_id=core.LEGACY_CLIENT_ID, outcome="ok"
    ) == 1
    assert metrics.counter_value(
        "session_requests_by_client_total", client_id=core.LEGACY_CLIENT_ID, outcome="invalid_request"
    ) == 1
    assert metrics.counter_value(
        "session_requests_by_client_total", client_id=core.LEGACY_CLIENT_ID, outcome="body_too_large"
    ) == 1


def test_a_rate_limited_request_counts_as_a_failure_too(service_factory: Any) -> None:
    from fastapi.testclient import TestClient

    service = service_factory.create_app(
        _settings(allow_http=True, client_burst=1, client_rate_limit_requests=1)
    )
    metrics = service.state.metrics
    client = TestClient(service)
    headers = {"Authorization": f"Bearer {SECRET}"}

    assert client.post("/v1/session", headers=headers, json={}).status_code == 200
    refused = client.post("/v1/session", headers=headers, json={})
    assert refused.status_code == 429 and int(refused.headers["Retry-After"]) >= 1
    assert metrics.counter_value("session_starts_total") == 2
    assert metrics.counter_value("session_success_total") == 1
    assert metrics.counter_value("session_failures_total") == 1
    assert metrics.counter_value("rate_limited_total") == 1
    assert metrics.counter_value(
        "session_requests_by_client_total", client_id=core.LEGACY_CLIENT_ID, outcome="rate_limited"
    ) == 1
    # A refused request must not also spend a token it never got: the bucket
    # holds exactly the one token it was granted, refilled at 1 per window.
    assert metrics.counter_value(
        "session_requests_by_client_total", client_id=core.LEGACY_CLIENT_ID, outcome="upstream_error"
    ) == 0


def test_an_unexpected_grant_failure_is_counted_before_the_500(
    service_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one path that used to vanish from `/metrics` entirely."""
    from fastapi.testclient import TestClient

    service = service_factory.create_app(_settings(allow_http=True))
    metrics = service.state.metrics

    async def boom(_settings: Any, _ttl: int, _client: Any) -> Any:
        raise RuntimeError("an internal fault no one anticipated")

    monkeypatch.setattr(service_factory, "grant_session_async", boom)
    client = TestClient(service, raise_server_exceptions=False)
    response = client.post("/v1/session", headers={"Authorization": f"Bearer {SECRET}"}, json={})

    assert response.status_code == 500, "a genuine bug still surfaces as a 500"
    assert metrics.counter_value("token_requests_total") == 1
    assert metrics.counter_value("token_request_failures_total") == 1
    assert metrics.counter_value("session_failures_total") == 1
    assert metrics.counter_value("session_success_total") == 0
    assert 'outcome="internal_error"' in metrics.render()


def test_the_metrics_endpoint_renders_even_if_a_value_is_not_finite() -> None:
    """/metrics is read during an incident; rendering must not be a casualty."""
    metrics = metrics_module.Metrics()
    metrics.set_gauge("weird", float("inf"))
    metrics.set_gauge("weird2", float("nan"))
    metrics.observe("weird_hist", 1.0)
    body = metrics.render()
    assert "weird +Inf" in body
    assert "weird2 NaN" in body


# ---------------------------------------------------------------------------
# B7: a malformed settings.yaml value is a ConfigError, never a traceback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key, value",
    [
        ("host_timeout_seconds", "10s"),
        ("session_ttl_seconds", "30s"),
        ("reconnect_delay", "2,5"),
        ("medical_confidence_threshold", "high"),
        ("max_reconnect_attempts", "none"),
    ],
)
def test_a_malformed_number_is_a_config_error_naming_the_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, key: str, value: str
) -> None:
    from medical_stt import config as config_module

    monkeypatch.setenv("MEDICALSTT_APP_DATA_DIR", str(tmp_path))
    (tmp_path / "settings.yaml").write_text(f"host_url: https://stt.example.com\n{key}: {value}\n", "utf-8")
    with pytest.raises(config_module.ConfigError) as excinfo:
        config_module.get_settings()
    message = str(excinfo.value)
    assert key in message
    assert "must be a number" in message
    assert "settings.yaml" in message


@pytest.mark.parametrize("key, expected", [("host_timeout_seconds", 10.0), ("sample_rate", 16000)])
def test_a_key_left_blank_falls_back_to_the_documented_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, key: str, expected: Any
) -> None:
    """Blank is not wrong -- it is what an uncommented template line looks like.

    `HostSettings.from_env` in `host/core.py` treats an empty `HOST_*` value the
    same way, so both sides of the contract agree: a key present with no value
    means "use the default", and never a traceback.
    """
    from medical_stt import config as config_module

    monkeypatch.setenv("MEDICALSTT_APP_DATA_DIR", str(tmp_path))
    (tmp_path / "settings.yaml").write_text(f"host_url: https://stt.example.com\n{key}: \n", "utf-8")
    settings = config_module.get_settings()
    assert getattr(settings, key) == expected


def test_a_number_written_as_a_string_still_loads(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Only genuinely unusable values are refused."""
    from medical_stt import config as config_module

    monkeypatch.setenv("MEDICALSTT_APP_DATA_DIR", str(tmp_path))
    (tmp_path / "settings.yaml").write_text(
        'host_url: https://stt.example.com\nsession_ttl_seconds: "30"\nhost_timeout_seconds: " 9.5 "\n',
        "utf-8",
    )
    settings = config_module.get_settings()
    assert settings.session_ttl_seconds == 30
    assert settings.host_timeout_seconds == 9.5


@pytest.mark.parametrize(
    "raw, expected",
    [('"false"', False), ('"true"', True), ('"no"', False), ('"YES"', True), ("0", False), ("1", True)],
)
def test_a_quoted_boolean_does_not_invert_itself(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str, expected: bool
) -> None:
    """`bool("false")` is True, so a quoted value used to flip the setting.

    `mip_opt_out` is a data-retention choice; silently inverting it is the
    worst possible failure for a setting of that kind.
    """
    from medical_stt import config as config_module

    monkeypatch.setenv("MEDICALSTT_APP_DATA_DIR", str(tmp_path))
    (tmp_path / "settings.yaml").write_text(
        f"host_url: https://stt.example.com\noverlay_enabled: {raw}\n", "utf-8"
    )
    assert config_module.get_settings().overlay_enabled is expected


def test_an_unreadable_boolean_is_a_config_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from medical_stt import config as config_module

    monkeypatch.setenv("MEDICALSTT_APP_DATA_DIR", str(tmp_path))
    (tmp_path / "settings.yaml").write_text(
        "host_url: https://stt.example.com\nrestore_clipboard: maybe\n", "utf-8"
    )
    with pytest.raises(config_module.ConfigError) as excinfo:
        config_module.get_settings()
    assert "restore_clipboard must be true or false" in str(excinfo.value)


def test_the_shipped_template_still_loads(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """B7 must not turn the shipped `config/settings.yaml` into an error."""
    from medical_stt import config as config_module

    monkeypatch.setenv("MEDICALSTT_APP_DATA_DIR", str(tmp_path / "empty-profile"))
    settings = config_module.get_settings()
    assert settings.session_ttl_seconds == 30
    assert settings.overlay_enabled is True


def test_the_host_client_id_grammar_is_enforced_before_it_reaches_the_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from medical_stt import config as config_module

    monkeypatch.setenv("MEDICALSTT_APP_DATA_DIR", str(tmp_path))
    # Refused by the panel path before anything is written...
    with pytest.raises(config_module.ConfigError) as excinfo:
        config_module.save_host_credentials("https://stt.example.com", "x" * 40, "caf\u00e9-01")
    assert "ASCII" in str(excinfo.value)
    assert not (tmp_path / "settings.yaml").exists(), "a rejected id must not be saved"

    # ...and by validation, for an id that arrived from settings.yaml or the
    # environment instead of the panel.
    (tmp_path / "settings.yaml").write_text(
        "host_url: https://stt.example.com\nhost_client_id: caf\u00e9-01\n", "utf-8"
    )
    errors = config_module.validate_settings(config_module.get_settings())
    assert any("host_client_id" in error and "ASCII" in error for error in errors), errors
    assert not config_module._valid_client_id("caf\u00e9-01")
    assert config_module._valid_client_id("doctor-01")


# ---------------------------------------------------------------------------
# helpers used above
# ---------------------------------------------------------------------------


def test_hash_helper_is_the_one_the_registry_compares() -> None:
    digest = core.hash_client_secret(SECRET)
    assert digest == hashlib.sha256(SECRET.encode("utf-8")).hexdigest()
    registry = core.ClientRegistry({"only": digest})
    assert registry.verify("only", SECRET) is not None
    assert registry.verify("only", SECRET + "x") is None


def test_unexpected_host_fault_does_not_log_exception_payload(service_factory, monkeypatch, caplog):
    from fastapi.testclient import TestClient
    marker = 'SENSITIVE_TEST_CREDENTIAL_DO_NOT_LOG'
    service = service_factory.create_app(_settings(allow_http=True))
    async def boom(*_):
        raise RuntimeError(marker)
    monkeypatch.setattr(service_factory, 'grant_session_async', boom)
    with TestClient(service, raise_server_exceptions=False) as client:
        response = client.post('/v1/session', headers={'Authorization': f'Bearer {SECRET}'}, json={})
    assert response.status_code == 500
    assert marker not in caplog.text
    assert 'RuntimeError' in caplog.text
