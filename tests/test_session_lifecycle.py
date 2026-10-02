"""Single-instance guard and Start/Stop session lifecycle tests.

No real GUI, microphone, or network: the session is driven by a fake
`LiveMedicalSTT`.
"""
from __future__ import annotations

import threading

from medical_stt.app import SessionController
from medical_stt.app_instance import SingleInstance


# -- single instance -----------------------------------------------------


def test_second_instance_is_refused(tmp_path):
    first = SingleInstance(lock_path=tmp_path / "run.lock")
    second = SingleInstance(lock_path=tmp_path / "run.lock")

    assert first.acquire() is True
    try:
        assert second.acquire() is False
        assert second.acquired is False
    finally:
        first.release()


def test_instance_can_be_reacquired_after_release(tmp_path):
    path = tmp_path / "run.lock"
    first = SingleInstance(lock_path=path)
    assert first.acquire() is True
    first.release()

    second = SingleInstance(lock_path=path)
    assert second.acquire() is True
    second.release()


def test_context_manager_acquires_and_releases(tmp_path):
    with SingleInstance(lock_path=tmp_path / "run.lock") as instance:
        assert instance.acquired is True
    assert instance.acquired is False


# -- session controller --------------------------------------------------


class FakeSTT:
    """Stands in for LiveMedicalSTT: blocks until asked to stop."""

    def __init__(self) -> None:
        self.stopped = threading.Event()
        self.started = threading.Event()
        self.exit_code = 0

    def request_stop(self) -> None:
        self.stopped.set()

    @property
    def is_stopping(self) -> bool:
        return self.stopped.is_set()

    def run(self) -> int:
        self.started.set()
        # A real session polls this; the test needs it responsive.
        while not self.stopped.wait(0.01):
            pass
        return self.exit_code


def test_start_and_stop_runs_a_session():
    stt = FakeSTT()
    controller = SessionController(stt_factory=lambda: stt)

    controller.start()
    assert controller.is_running is True
    assert stt.started.wait(timeout=2.0)

    assert controller.stop(timeout=2.0) is True
    assert controller.is_running is False


def test_start_is_idempotent_while_running():
    stt = FakeSTT()
    controller = SessionController(stt_factory=lambda: stt)
    controller.start()
    controller.start()  # must not spawn a second session
    controller.stop(timeout=2.0)

    # Exactly one session was ever constructed.
    assert stt.started.is_set()


def test_controller_can_be_restarted_after_stop():
    instances = []

    def factory():
        stt = FakeSTT()
        instances.append(stt)
        return stt

    controller = SessionController(stt_factory=factory)
    controller.start()
    controller.stop(timeout=2.0)
    controller.start()
    controller.stop(timeout=2.0)

    assert len(instances) == 2


def test_configuration_error_from_start_propagates():
    from medical_stt.config import ConfigError

    def factory():
        raise ConfigError("host_url is not set")

    controller = SessionController(stt_factory=factory)
    try:
        controller.start()
    except ConfigError as exc:
        assert "host_url" in str(exc)
    else:  # pragma: no cover - the factory always raises
        raise AssertionError("ConfigError should have propagated")
    assert controller.is_running is False


def test_session_failure_is_recorded_not_raised():
    class FailingSTT(FakeSTT):
        def run(self) -> int:
            raise RuntimeError("microphone busy")

    controller = SessionController(stt_factory=FailingSTT)
    controller.start()
    controller.stop(timeout=2.0)

    # The failure is surfaced to the UI, not swallowed and not retried.
    deadline = threading.Event()
    for _ in range(200):
        if controller.last_error:
            break
        deadline.wait(0.01)
    assert controller.last_error == "microphone busy"


def test_stop_before_start_is_harmless():
    controller = SessionController(stt_factory=FakeSTT)
    assert controller.stop(timeout=0.1) is True
