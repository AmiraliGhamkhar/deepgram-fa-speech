"""Regression tests for the audit remediation (AUDIT/REPORT.md findings).

Each test pins one finding's fix so the defect cannot return silently:

* 07-F1  the host client refuses redirects instead of re-sending the
         `Authorization` header to a redirect target;
* 01-F1  `POST /v1/session` aborts a chunked oversized body while
         streaming instead of buffering it first;
* 01-F5  `session_starts_total` counts only authenticated callers;
* 01-F7  `/metrics` token comparison never 500s on non-ASCII input;
* 14-F1  the provisioner cannot hang on a colliding prefix;
* 14-F3  `--client-id` is validated before anything is written;
* 14-F4  re-provisioning warns about ids it replaces;
* 04-F1  a send racing `stop()` is dropped silently, not recorded as an
         error;
* 08-F1  an upstream `Retry-After` is never shortened below the backoff;
* 11-F1  `Settings.as_dict()` never serializes the shared secret;
* 15-F3  a committed `*.secret` file fails the credential scan.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import secrets
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Tuple

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOST_DIR = ROOT / "host"
if str(HOST_DIR) not in sys.path:
    sys.path.insert(0, str(HOST_DIR))

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

os.environ.setdefault("DEEPGRAM_API_KEY", "dg_fake_host_side_only")
os.environ.setdefault("HOST_SHARED_SECRET", "s" * 40)
os.environ.setdefault("HOST_ALLOW_HTTP", "1")

import core  # type: ignore[import-not-found]  # noqa: E402
import provision  # type: ignore[import-not-found]  # noqa: E402


# -- 07-F1: the client never follows redirects -----------------------------


class _RedirectOpener:
    """Simulates stdlib's redirect-following behaviour for the handler seam."""

    def __init__(self, code: int, location: str) -> None:
        self.code = code
        self.location = location

    def __call__(self, request: urllib.request.Request, timeout: float) -> bytes:
        raise urllib.error.HTTPError(
            request.full_url, self.code, "moved", {"Location": self.location}, None
        )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise RuntimeError("redirect attempted")


def test_opener_refuses_every_redirect():
    """The shipping opener treats any 3xx as a hard error, never a hop."""
    from medical_stt.host_client import _NoRedirectHandler

    handler = _NoRedirectHandler()
    for code in (301, 302, 303, 307, 308):
        request = urllib.request.Request("https://stt.example.com/v1/session")
        with pytest.raises(RuntimeError):
            handler.redirect_request(
                request,
                fp=None,
                code=code,
                msg="moved",
                headers={"Location": "https://evil.example.com/v1/session"},
                newurl="https://evil.example.com/v1/session",
            )


def test_redirect_error_is_classified_as_config_and_never_leaks_the_secret():
    from medical_stt.host_client import HostSessionClient
    from medical_stt.stt.base import ErrorCategory, ProviderError

    secret = "the-shared-secret-value"

    def handler(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 302, "found", {"Location": "https://evil.example.com"}, None
        )

    client = HostSessionClient("https://stt.example.com", secret, opener=handler)
    with pytest.raises(ProviderError) as excinfo:
        client.fetch_session()
    assert excinfo.value.category is ErrorCategory.CONFIG
    rendered = f"{excinfo.value} {excinfo.value.__cause__ or ''}"
    assert secret not in rendered
    assert "evil.example.com" not in rendered, "the redirect target must not be echoed"


def test_a_3xx_httperror_is_config_without_following():
    """307/308 surface as plain HTTPError (no auto-handler) -> CONFIG."""
    from medical_stt.host_client import HostSessionClient
    from medical_stt.stt.base import ErrorCategory, ProviderError

    def handler(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 308, "permanent redirect",
            {"Location": "https://evil.example.com"}, None,
        )

    client = HostSessionClient("https://stt.example.com", "secret", opener=handler)
    with pytest.raises(ProviderError) as excinfo:
        client.fetch_session()
    assert excinfo.value.category is ErrorCategory.CONFIG


def test_host_grant_client_never_follows_a_redirect():
    """The grant request carries the Deepgram API key in an Authorization header.

    `httpx` defaults to `follow_redirects=False`; this pins that the host keeps
    saying so explicitly. A silent library default change -- or a well-meaning
    "behave like a browser" edit -- would hand the long-lived API key to
    whatever host a 302 names, which is the exact failure the client-side
    `_NoRedirectHandler` above exists to prevent.
    """
    client = core.AsyncGrantClient()
    try:
        # httpx exposes this publicly from 0.28 and privately before it; the
        # supported range is >=0.27,<1.0, so read whichever exists. A rename
        # that leaves neither is worth a loud failure here.
        http_client = client._client  # noqa: SLF001 - the property under test
        follow = getattr(http_client, "follow_redirects", None)
        if follow is None:
            follow = http_client._follow_redirects  # noqa: SLF001
        assert follow is False
    finally:
        asyncio.run(client.aclose())


# -- 01-F1: the body cap applies while streaming ---------------------------

SECRET = "s" * 40


def _settings(tmp_path: Path, **overrides: str) -> "core.HostSettings":
    env = {
        "DEEPGRAM_API_KEY": "dg_fake_host_side_only",
        "HOST_SHARED_SECRET": SECRET,
        "HOST_ALLOW_HTTP": "1",
    }
    env.update(overrides)
    return core.HostSettings.from_env(env)


def _build_app(monkeypatch, settings: "core.HostSettings"):
    app_module = importlib.import_module("app")
    core_module = importlib.import_module("core")

    async def stub_grant(_self, _api_key, _ttl):
        return {"access_token": "issued-token", "expires_in": 30}

    monkeypatch.setattr(core_module.AsyncGrantClient, "grant", stub_grant)
    return app_module.create_app(settings)


async def _post_raw(
    app: Any, *, headers: Dict[str, str], content: bytes
) -> Tuple[int, Dict[str, Any]]:
    transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 51000))
    async with httpx.AsyncClient(transport=transport, base_url="https://stt.example.com") as http:
        response = await http.post(
            "/v1/session",
            headers={"Authorization": f"Bearer {SECRET}", **headers},
            content=content,
        )
        try:
            body = response.json()
        except ValueError:
            body = {}
        return response.status_code, body


def test_chunked_oversized_body_is_aborted_at_the_cap(monkeypatch, tmp_path):
    """A body with no Content-Length must still be capped.

    `httpx` sends `Transfer-Encoding: chunked` when the request is built
    from a byte *generator*, so the route can only enforce the cap while
    reading. The buffer-then-check implementation this test replaces
    returned 200 for this request and buffered all of it.
    """
    app = _build_app(monkeypatch, _settings(tmp_path, HOST_MAX_REQUEST_BODY_BYTES="256"))

    async def big_body():
        yield b'{"ttl_seconds":30,"pad":"'
        yield b"A" * 4096
        yield b'"}'

    async def run():
        transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 51000))
        async with httpx.AsyncClient(transport=transport, base_url="https://stt.example.com") as http:
            response = await http.post(
                "/v1/session",
                headers={"Authorization": f"Bearer {SECRET}"},
                content=big_body(),
            )
            try:
                body = response.json()
            except ValueError:
                body = {}
            return response.status_code, body

    status, body = asyncio.run(run())
    assert status == 413, body
    assert body.get("detail") == "request body too large"


def test_oversized_declared_body_is_refused_before_buffering(monkeypatch, tmp_path):
    app = _build_app(monkeypatch, _settings(tmp_path, HOST_MAX_REQUEST_BODY_BYTES="256"))
    status, _ = asyncio.run(
        _post_raw(app, headers={"Content-Length": "999999"}, content=b'{"ttl_seconds":30}')
    )
    assert status == 413


def test_small_bodies_still_work_after_the_streaming_change(monkeypatch, tmp_path):
    app = _build_app(monkeypatch, _settings(tmp_path))
    status, body = asyncio.run(
        _post_raw(app, headers={"Content-Type": "application/json"}, content=b'{"ttl_seconds": 45}')
    )
    assert status == 200
    assert body["access_token"] == "issued-token"


# -- 01-F5: the session-start counter excludes brute-force noise ------------


def test_session_starts_counter_excludes_failed_auth(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    app = _build_app(monkeypatch, _settings(tmp_path))
    client = TestClient(app, base_url="https://stt.example.com")

    for _ in range(3):
        client.post("/v1/session", headers={"Authorization": "Bearer wrong"}, json={})
    assert app.state.metrics.counter_value("session_starts_total") == 0

    response = client.post(
        "/v1/session", headers={"Authorization": f"Bearer {SECRET}"}, json={}
    )
    assert response.status_code == 200
    assert app.state.metrics.counter_value("session_starts_total") == 1


# -- 01-F7: non-ASCII bearer tokens get 401, never 500 ----------------------


def test_metrics_token_comparison_handles_non_ascii(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app import _token_matches

    assert _token_matches("Bearer caf\u00e9-token", "ascii-expected") is False
    assert _token_matches("Bearer \U0001f3a3", "x") is False
    assert _token_matches("Bearer ok", "ok") is True

    app = _build_app(monkeypatch, _settings(tmp_path, HOST_METRICS_ADMIN_TOKEN="admintoken"))
    client = TestClient(app, base_url="https://stt.example.com")
    # The ASGI header cannot carry non-ASCII directly, so the raw bytes are
    # compared through the helper -- the guarantee under test is that the
    # comparison itself never raises, which is what used to 500 on /metrics.
    assert client.get(
        "/metrics", headers={"Authorization": "Bearer admintoken"}
    ).status_code == 200
    assert client.get("/metrics").status_code == 401


# -- 14-F1 / 14-F3 / 14-F4: provisioner CLI ---------------------------------


def test_provision_refuses_an_over_long_prefix(monkeypatch, tmp_path):
    out = tmp_path / "clients.txt"
    result = subprocess.run(
        [sys.executable, "-m", "host.provision", "--count", "2",
         "--prefix", "x" * 62, "--out", str(out)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "too long" in result.stderr
    assert not out.exists(), "nothing may be written on a refused invocation"


def test_provision_refuses_an_invalid_client_id(monkeypatch, tmp_path):
    out = tmp_path / "clients.txt"
    result = subprocess.run(
        [sys.executable, "-m", "host.provision", "--client-id", "Dr. Smith (cardio)",
         "--out", str(out)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "not valid" in result.stderr
    assert not out.exists()


def test_provision_accepts_ids_the_registry_loader_accepts(monkeypatch, tmp_path):
    """Round-trip: every id the tool mints must load in `ClientRegistry`."""
    out = tmp_path / "clients.txt"
    result = subprocess.run(
        [sys.executable, "-m", "host.provision", "--count", "5", "--prefix", "doctor",
         "--start-index", "--out", str(out)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    registry = core.ClientRegistry.from_file(str(out))
    assert len(registry) == 5


def test_provision_warns_when_reprovisioning_an_existing_id(monkeypatch, tmp_path):
    out = tmp_path / "clients.txt"
    first = subprocess.run(
        [sys.executable, "-m", "host.provision", "--client-id", "doctor-01", "--out", str(out)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=30,
    )
    assert first.returncode == 0, first.stderr
    assert "warning" not in first.stderr

    second = subprocess.run(
        [sys.executable, "-m", "host.provision", "--client-id", "doctor-01", "--out", str(out)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=30,
    )
    assert second.returncode == 0, second.stderr
    assert "no longer work" in second.stderr
    # The duplicate line must still be loadable (later entry wins).
    assert len(core.ClientRegistry.from_file(str(out))) == 1


def test_provision_hang_guard_bounds_collision_retries(tmp_path):
    """The CLI refuses any invocation that could collide/hang."""
    # generate_client_id truncates at 64 chars, so a 62-char prefix with
    # --start-index would mint identical ids and loop forever. The CLI
    # prefix guard (MAX_PREFIX_LENGTH) is what makes the loop safe. The
    # budget is the *default* suffix: "-" plus token_hex(4) is 9 characters,
    # not the 5 that --start-index's 3-digit index needs.
    assert provision.MAX_PREFIX_LENGTH + 1 + len(secrets.token_hex(4)) <= 64
    out = tmp_path / "clients.txt"
    result = subprocess.run(
        [sys.executable, "-m", "host.provision", "--count", "3",
         "--prefix", "x" * provision.MAX_PREFIX_LENGTH, "--start-index",
         "--out", str(out)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=30,
    )
    # The longest *legal* prefix must still work (boundary check).
    assert result.returncode == 0, result.stderr
    assert len(core.ClientRegistry.from_file(str(out))) == 3


def test_provision_keeps_full_suffix_entropy_at_the_longest_legal_prefix():
    """The prefix bound must leave the whole random suffix intact.

    With MAX_PREFIX_LENGTH at 59 the default `token_hex(4)` suffix (8 hex
    characters) plus its separator needed 9, so `generate_client_id`'s
    `[:64]` truncation left only 4 hex digits -- 65536 possible ids instead
    of 4.3e9. Ids then collide often enough that a large `--count` burns the
    MAX_DEDUP_ATTEMPTS budget, and the constant's own comment described a
    5-character suffix that the code does not produce.
    """
    prefix = "x" * provision.MAX_PREFIX_LENGTH
    ids = {provision.generate_client_id(prefix) for _ in range(4000)}
    assert len(ids) == 4000, f"suffix entropy lost: {4000 - len(ids)} collisions in 4000 draws"
    for client_id in ids:
        assert len(client_id) <= 64
        assert client_id.rsplit("-", 1)[1] == client_id.rsplit("-", 1)[1][:8]
        assert len(client_id.rsplit("-", 1)[1]) == 8, "the random suffix was truncated"


def test_provision_at_the_old_bound_would_have_collided():
    """Pins the number the guard exists to protect, so it cannot drift back."""
    prefix = "x" * (provision.MAX_PREFIX_LENGTH + 4)  # the old 59
    ids = {provision.generate_client_id(prefix) for _ in range(4000)}
    assert len(ids) < 4000, (
        "a prefix this long now retains full entropy; MAX_PREFIX_LENGTH may be "
        "too conservative, or generate_client_id stopped truncating"
    )


# -- 04-F1: a send racing stop() is dropped, not an error -------------------


def test_send_audio_racing_stop_is_not_an_error():
    from medical_stt.stt.deepgram_provider import DeepgramProvider
    from medical_stt.stt.base import ErrorCategory, ProviderError

    from medical_stt.config import Settings

    class FlakyConnection:
        def send_media(self, chunk: bytes) -> None:
            raise OSError("socket closed")

    provider = DeepgramProvider(
        Settings(host_url="https://stt.example.com", host_secret="secret"),
        token_provider=lambda: "token",
    )
    provider._connection = FlakyConnection()  # noqa: SLF001 - test seam

    # A send that fails without a racing stop is a real error...
    with pytest.raises(ProviderError) as excinfo:
        provider.send_audio(b"\x00\x01")
    assert excinfo.value.category is ErrorCategory.NETWORK

    # ...but the same failure after stop() bumped the generation is dropped.
    provider.stop()
    provider.send_audio(b"\x00\x02")  # must not raise


# -- 08-F1: Retry-After is a floor against the backoff, not a clamp ---------


def test_retry_after_is_never_shortened_below_the_backoff():
    from medical_stt.stt.base import ErrorCategory, ProviderError
    from medical_stt.stt.reconnect import ReconnectPolicy, decide

    policy = ReconnectPolicy(base_delay=8.0, max_delay=60.0, jitter=0.0)
    decision = decide(
        policy, ProviderError(ErrorCategory.RATE_LIMIT, "slow"), attempt=1, retry_after=2.0
    )
    assert decision.honored_retry_after is True
    assert decision.delay_seconds == 8.0, (
        "a 2s hint must not shorten the 8s backoff into a hot retry loop"
    )


def test_retry_after_still_wins_over_a_smaller_backoff():
    from medical_stt.stt.base import ErrorCategory, ProviderError
    from medical_stt.stt.reconnect import ReconnectPolicy, decide

    policy = ReconnectPolicy(base_delay=2.0, max_delay=30.0, jitter=0.0)
    decision = decide(
        policy, ProviderError(ErrorCategory.RATE_LIMIT, "slow"), attempt=1, retry_after=9.0
    )
    assert decision.delay_seconds == 9.0


# -- 11-F1: as_dict() never serializes the secret ---------------------------


def test_settings_as_dict_excludes_the_secret():
    from medical_stt.config import Settings

    settings = Settings(host_url="https://stt.example.com", host_secret="topsecret")
    dumped = settings.as_dict()
    assert "host_secret" not in dumped
    assert "topsecret" not in json.dumps(dumped)


def test_settings_as_dict_keys_are_unchanged_otherwise():
    from medical_stt.config import Settings

    dumped = Settings().as_dict()
    for key in ("model", "language", "host_url", "host_client_id", "session_ttl_seconds"):
        assert key in dumped


# -- 15-F3: a committed *.secret file fails the scan ------------------------


def _load_scanner():
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("scan_secrets", ROOT / "scripts" / "scan_secrets.py")
    assert spec and spec.loader
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def test_scanner_finds_a_committed_secret_file(tmp_path):
    module = _load_scanner()
    (tmp_path / "doctor-01.secret").write_text("not-even-a-key-pattern\n", encoding="utf-8")
    findings = module.scan_tree(tmp_path)
    assert [f.pattern for f in findings] == ["secret_file"]
    assert "doctor-01.secret" in findings[0].path


def test_scanner_filename_rule_is_case_insensitive(tmp_path):
    """Any *.secret path is a finding regardless of case."""
    module = _load_scanner()
    assert module.SECRET_FILENAME_SUFFIXES == (".secret",)
    probe = tmp_path / "Doctor-01.SECRET"
    probe.write_text("irrelevant\n", encoding="utf-8")
    findings = module.scan_tree(tmp_path)
    assert [f.pattern for f in findings] == ["secret_file"]


# -- degraded recovery (05-F1 companion) ------------------------------------


def test_queue_degraded_requires_sustained_recovery():
    from medical_stt.audio import BoundedAudioQueue

    q = BoundedAudioQueue(maxsize=1)
    assert q.put_nowait(b"fill") is True  # the one slot
    for _ in range(3):
        assert q.put_nowait(b"x") is False  # drop streak begins
    assert q.stats().degraded is True

    # One good send is not recovery.
    assert q.get(timeout=0.1) == b"fill"  # make room
    assert q.put_nowait(b"y") is True
    assert q.stats().degraded is True
    # A sustained run of successful sends clears it.
    for _ in range(2):
        assert q.get(timeout=0.1)
        assert q.put_nowait(b"y") is True
    assert q.stats().degraded is False


def test_queue_drain_resets_the_degraded_streak():
    from medical_stt.audio import BoundedAudioQueue

    q = BoundedAudioQueue(maxsize=1)
    for _ in range(4):
        q.put_nowait(b"x")
    assert q.stats().degraded is True
    q.drain()
    assert q.stats().degraded is False
    assert q.put_nowait(b"y") is True


# -- malformed-word logging (04-F3) -----------------------------------------


def test_malformed_words_are_reported_through_the_callback():
    from medical_stt.stt.deepgram_provider import transcript_event_from_result

    word = SimpleNamespace(word="دوز", punctuated_word=None, start=None, end=None, confidence=0.5)
    alternative = SimpleNamespace(transcript="دوز", confidence=0.9, words=[word])
    message = SimpleNamespace(
        channel=SimpleNamespace(alternatives=[alternative]), is_final=True, speech_final=True
    )
    seen: list[int] = []
    transcript_event_from_result(message, on_malformed_words=seen.append)
    assert seen == [1]


def test_malformed_word_callback_not_fired_for_complete_words():
    from medical_stt.stt.deepgram_provider import transcript_event_from_result

    word = SimpleNamespace(word="دوز", punctuated_word="دوز", start=1.0, end=1.5, confidence=0.5)
    alternative = SimpleNamespace(transcript="دوز", confidence=0.9, words=[word])
    message = SimpleNamespace(
        channel=SimpleNamespace(alternatives=[alternative]), is_final=True, speech_final=True
    )
    seen: list[int] = []
    transcript_event_from_result(message, on_malformed_words=seen.append)
    assert seen == []
