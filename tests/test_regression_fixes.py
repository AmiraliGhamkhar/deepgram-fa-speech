"""Regression pins for the audit follow-up batch (runner, census, DPAPI,
single-instance, data-dir and host terminal-log fixes).

Every test here is logic-level: no Windows, microphone, network or
credentials are required.
"""
from __future__ import annotations

import logging
import threading
import time

import pytest

from medical_stt.processing.negation import census_fully_negated_sources
from medical_stt.stt.base import ErrorCategory, ProviderError, STTProvider, TranscriptEvent
from medical_stt.stt.runner import stream_audio


# -- C6: diagnostic runner ---------------------------------------------------


class _RunnerProvider(STTProvider):
    """Provider whose listener blocks until `stop()` releases it."""

    def __init__(self, *, error: ProviderError | None = None, slow_stop: bool = False) -> None:
        self.released = threading.Event()
        self.error_sent = threading.Event()
        self.audio: list[bytes] = []
        self.aborted = False
        self._error = error
        self._slow_stop = slow_stop

    def validate_config(self) -> None:
        return None

    def wait_ready(self, timeout: float) -> bool:
        if self._error is not None:
            return self.error_sent.wait(timeout)
        return True

    def start(self, on_transcript, on_error) -> None:  # noqa: ANN001 - provider protocol
        if self._error is not None:
            on_error(self._error)
            self.error_sent.set()
        else:
            on_transcript(TranscriptEvent("final", True))
        self.released.wait(10)

    def send_audio(self, chunk: bytes) -> None:
        self.audio.append(chunk)

    def stop(self) -> None:
        self.released.set()
        if self._slow_stop:
            # The listener can finish (all finals were delivered in start)
            # while this cleanup keeps running past the stop budget.
            time.sleep(3.0)

    def abort(self) -> None:
        self.aborted = True
        self.released.set()


def test_runner_slow_stop_after_finals_does_not_raise_timeout():
    provider = _RunnerProvider(slow_stop=True)
    transcripts: list[TranscriptEvent] = []
    stream_audio(
        provider, [b"one"], transcripts.append, lambda _e: None,
        bytes_per_second=100000, join_timeout=2.0,
    )
    assert [t.text for t in transcripts] == ["final"]
    assert provider.aborted is False


def test_runner_ignores_shutdown_errors_mid_stream():
    provider = _RunnerProvider(
        error=ProviderError(ErrorCategory.SHUTDOWN, "deliberate shutdown")
    )
    delivered: list[ProviderError] = []
    stream_audio(
        provider, [b"one"], lambda _e: None, delivered.append,
        bytes_per_second=100000, join_timeout=2.0,
    )
    assert provider.audio == [b"one"], "a SHUTDOWN notice must not abort streaming"
    assert len(delivered) == 1


def test_runner_does_not_double_surface_a_callback_delivered_error():
    provider = _RunnerProvider(
        error=ProviderError(ErrorCategory.NETWORK, "connection reset")
    )
    delivered: list[ProviderError] = []
    # No raise: the error already reached the caller through on_error.
    stream_audio(
        provider, [b"one"], lambda _e: None, delivered.append,
        bytes_per_second=100000, join_timeout=2.0,
    )
    assert len(delivered) == 1
    assert provider.audio == [], "streaming stops on a real error, without re-raising it"


# -- C6: negation census -----------------------------------------------------


def test_negation_census_warns_about_sources_that_can_never_fire(caplog):
    with caplog.at_level(logging.WARNING, logger="medical_stt.processing.negation"):
        assert census_fully_negated_sources(["بیوپسی", "ندارد", "کاتتر"]) == 1
    assert "negation" in caplog.text
    assert census_fully_negated_sources(["بیوپسی"]) == 0


# -- D2: DPAPI empty input + library caching -----------------------------------


def test_dpapi_empty_payload_is_a_dpapi_error():
    from medical_stt.security import dpapi

    with pytest.raises(dpapi.DPAPIError, match="empty payload"):
        dpapi.protect(b"")
    with pytest.raises(dpapi.DPAPIError, match="empty payload"):
        dpapi.unprotect(b"")
    with pytest.raises(dpapi.DPAPIError, match="empty payload"):
        dpapi._make_blob(b"")


def test_dpapi_libraries_are_loaded_once(monkeypatch):
    import ctypes
    from types import SimpleNamespace

    from medical_stt.security import dpapi

    class _FakeFunc:
        pass

    def fake_lib():
        return SimpleNamespace(
            CryptProtectData=_FakeFunc(),
            CryptUnprotectData=_FakeFunc(),
            LocalFree=_FakeFunc(),
        )

    loads: list[str] = []
    monkeypatch.setattr(
        ctypes, "WinDLL", lambda name, **kw: loads.append(name) or fake_lib(), raising=False
    )
    monkeypatch.setattr(dpapi, "_LIBRARY_CACHE", None)
    first = dpapi._libraries()
    second = dpapi._libraries()
    assert first is second or first == second
    assert first[0] is second[0], "prototype setup must run once per process"
    assert loads == ["crypt32", "kernel32"]


# -- D3: single-instance fail-open scope ----------------------------------------


def test_squatted_mutex_name_refuses_to_start(monkeypatch):
    import ctypes

    from medical_stt import app_instance

    class _FakeKernel32:
        def __init__(self):
            # Plain function objects: the production code assigns
            # `.argtypes`/`.restype` to them, which a bound method refuses.
            self.CreateMutexW = lambda *_args: 0  # noqa: N802 - Win32 name
            self.CloseHandle = lambda *_args: True  # noqa: N802 - Win32 name

    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: _FakeKernel32(), raising=False)
    monkeypatch.setattr(app_instance, "_last_error", lambda: 5)  # ERROR_ACCESS_DENIED
    instance = app_instance.SingleInstance()
    assert instance._acquire_windows() is False
    assert instance.acquired is False


def test_unwritable_file_lock_dir_degrades_instead_of_crashing(tmp_path):
    from medical_stt import app_instance

    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    instance = app_instance.SingleInstance(lock_path=blocker / "run.lock")
    assert instance._acquire_file() is True
    assert instance.acquired is True
    instance.release()


# -- D1: data-dir + env-secret hardening ------------------------------------------


def test_data_dir_override_inside_install_dir_is_refused(monkeypatch):
    from pathlib import Path

    from medical_stt import paths

    inside = Path(paths.__file__).resolve().parent / "sneaky-data"
    monkeypatch.setenv(paths.APP_DATA_DIR_ENV, str(inside))
    with pytest.raises(ValueError, match="install"):
        paths.app_data_dir()


def test_data_dir_override_in_windows_temp_is_refused(monkeypatch):
    import tempfile
    from pathlib import Path

    from medical_stt import paths

    monkeypatch.setattr(paths, "_IS_WINDOWS", True)
    monkeypatch.setenv(paths.APP_DATA_DIR_ENV, str(Path(tempfile.gettempdir()) / "medicalstt-x"))
    with pytest.raises(ValueError, match="temp"):
        paths.app_data_dir()


def test_data_dir_override_is_resolved_absolute(monkeypatch, tmp_path):
    from medical_stt import paths

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(paths.APP_DATA_DIR_ENV, "relative-data")
    resolved = paths.app_data_dir()
    assert resolved.is_absolute()
    assert resolved == (tmp_path / "relative-data")


def test_data_dir_is_logged_once_when_ensured(monkeypatch, tmp_path, caplog):
    from medical_stt import paths

    monkeypatch.setenv(paths.APP_DATA_DIR_ENV, str(tmp_path / "data"))
    monkeypatch.setattr(paths, "_DATA_DIR_LOGGED", False, raising=False)
    with caplog.at_level(logging.INFO, logger="medical_stt.paths"):
        paths.ensure_app_data_dir()
        paths.ensure_app_data_dir()
    mentions = [r for r in caplog.records if str(tmp_path / "data") in r.getMessage()]
    assert len(mentions) == 1


def test_env_host_secret_shorter_than_24_chars_is_refused(monkeypatch):
    from medical_stt import config

    monkeypatch.setenv("MEDICALSTT_HOST_SECRET", "short")
    with pytest.raises(config.ConfigError, match="24"):
        config.load_host_secret()
    monkeypatch.setenv("MEDICALSTT_HOST_SECRET", "x" * 24)
    assert config.load_host_secret() == "x" * 24
