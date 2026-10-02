"""Local secret storage tests.

DPAPI itself cannot run in CI (Windows-only), so these tests use a fake
protector and verify the *behaviour* that matters: the plaintext secret is
never written to disk, a stored secret round-trips, and a corrupt or
foreign file fails with a clear error instead of a traceback.
"""
from __future__ import annotations

import pytest

from medical_stt.security import SecretStore, SecretStoreError
from medical_stt.security.dpapi import APP_ENTROPY, DPAPIError, DPAPINotAvailable, is_available

SECRET = "shared-secret-not-real-0123456789"


class FakeProtector:
    """Reversible 'encryption' that also proves no plaintext hits disk."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def protect(self, data: bytes) -> bytes:
        self.calls.append("protect")
        return b"FAKEBLOB" + bytes(reversed(data))

    def unprotect(self, blob: bytes) -> bytes:
        self.calls.append("unprotect")
        if not blob.startswith(b"FAKEBLOB"):
            raise DPAPIError("CryptUnprotectData failed (Win32 error 5)")
        return bytes(reversed(blob[len(b"FAKEBLOB"):]))


def test_secret_round_trips(tmp_path):
    store = SecretStore(tmp_path / "host_secret.dpapi", FakeProtector())
    store.save(SECRET)
    assert SecretStore(tmp_path / "host_secret.dpapi", FakeProtector()).load() == SECRET


def test_plaintext_secret_is_never_written_to_disk(tmp_path):
    path = tmp_path / "host_secret.dpapi"
    SecretStore(path, FakeProtector()).save(SECRET)
    assert SECRET.encode() not in path.read_bytes()


def test_no_secret_stored_yet_returns_none(tmp_path):
    assert SecretStore(tmp_path / "missing.dpapi", FakeProtector()).load() is None


def test_empty_secret_is_refused(tmp_path):
    with pytest.raises(SecretStoreError):
        SecretStore(tmp_path / "host_secret.dpapi", FakeProtector()).save("")


def test_foreign_file_is_reported_clearly(tmp_path):
    path = tmp_path / "host_secret.dpapi"
    path.write_bytes(b"just some unrelated bytes")
    with pytest.raises(SecretStoreError) as excinfo:
        SecretStore(path, FakeProtector()).load()
    assert "not a Medical STT secret file" in str(excinfo.value)


def test_blob_from_another_user_is_reported_clearly(tmp_path):
    path = tmp_path / "host_secret.dpapi"
    SecretStore(path, FakeProtector()).save(SECRET)

    class WrongProtector:
        def protect(self, data: bytes) -> bytes:  # pragma: no cover - unused
            return b""

        def unprotect(self, blob: bytes) -> bytes:
            raise DPAPIError("CryptUnprotectData failed ... re-enter the shared secret")

    with pytest.raises(SecretStoreError) as excinfo:
        SecretStore(path, WrongProtector()).load()
    assert "re-enter the shared secret" in str(excinfo.value)


def test_save_replaces_previous_value(tmp_path):
    path = tmp_path / "host_secret.dpapi"
    store = SecretStore(path, FakeProtector())
    store.save("first-secret-value")
    store.save(SECRET)
    assert store.load() == SECRET


def test_clear_removes_the_file(tmp_path):
    path = tmp_path / "host_secret.dpapi"
    store = SecretStore(path, FakeProtector())
    store.save(SECRET)
    assert store.clear() is True
    assert store.clear() is False
    assert store.load() is None


def test_no_temporary_secret_files_are_left_behind(tmp_path):
    SecretStore(tmp_path / "host_secret.dpapi", FakeProtector()).save(SECRET)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["host_secret.dpapi"]


def test_app_entropy_is_application_specific():
    assert b"MedicalSTT" in APP_ENTROPY


@pytest.mark.skipif(is_available(), reason="DPAPI is unavailable off Windows")
def test_dpapi_refuses_to_run_off_windows():
    from medical_stt.security.dpapi import protect, unprotect

    with pytest.raises(DPAPINotAvailable):
        protect(b"x")
    with pytest.raises(DPAPINotAvailable):
        unprotect(b"x")
