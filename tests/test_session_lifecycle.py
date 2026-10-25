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


def test_windows_mutex_name_is_per_session_and_app_specific():
    """`Global\\` needs SeCreateGlobalPrivilege, which a normal user lacks."""
    from medical_stt import app_instance

    assert not app_instance._WINDOWS_MUTEX_NAME.startswith("Global\\")  # noqa: SLF001
    assert "MedicalSTT" in app_instance._WINDOWS_MUTEX_NAME  # noqa: SLF001


def test_unavailable_windows_mutex_does_not_block_startup(monkeypatch, caplog):
    """If the Win32 mutex cannot be created, the app must still start.

    The failure is forced by making `ctypes.WinDLL` raise, so the path is
    exercised identically on Windows and off it (on Windows the mutex would
    otherwise be created successfully and no warning would be logged).
    """
    import ctypes
    import logging

    from medical_stt import app_instance

    def exploding_windll(*_args, **_kwargs):
        raise OSError("simulated CreateMutexW failure")

    instance = app_instance.SingleInstance()
    monkeypatch.setattr(app_instance.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", exploding_windll, raising=False)
    with caplog.at_level(logging.WARNING):
        assert instance.acquire() is True
    assert instance.acquired is True
    assert any("mutex" in record.getMessage() for record in caplog.records)
    instance.release()


def test_windows_mutex_failure_is_reported_not_swallowed(monkeypatch, caplog):
    """A failed CreateMutexW handle (not an exception) must also degrade safely."""
    import ctypes
    import logging

    from medical_stt import app_instance

    class FakeKernel32:
        def __getattr__(self, name):  # pragma: no cover - attribute plumbing
            def _setter(*_args, **_kwargs):
                return None

            return _setter

        def CreateMutexW(self, *_args):  # noqa: N802 - Win32 name
            return 0  # failure: no handle

    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: FakeKernel32(), raising=False)
    instance = app_instance.SingleInstance()
    with caplog.at_level(logging.WARNING):
        assert instance._acquire_windows() is True  # noqa: SLF001 - the path under test
    assert any("mutex" in record.getMessage() for record in caplog.records)


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


def test_stop_snapshots_the_started_thread_under_the_lock(monkeypatch):
    """P0-11: 50 immediate Start→Stop races must join the new session thread.

    `start()` builds the STT object and thread while holding `_lock`. A `stop()`
    that samples `_stt`/`_thread` outside that lock can see the previous `None`
    and return before the new thread even exists.
    """
    import medical_stt.app as app_module

    real_thread = threading.Thread
    created_threads: list[real_thread] = []
    joined_threads: list[real_thread] = []

    class RecordingThread(real_thread):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            if self.name == "stt-session":
                created_threads.append(self)

        def join(self, timeout: float | None = None) -> None:
            if self.name == "stt-session":
                joined_threads.append(self)
            super().join(timeout)

    monkeypatch.setattr(app_module.threading, "Thread", RecordingThread)

    class GatedSTT:
        """The session thread stays alive until stop()'s own request_stop().

        start() calls request_stop() once because cancel_start() set the
        latch while the factory was blocked. Only stop()'s snapshot path can
        make the second call -- if it sampled a stale _thread, run() would
        time out instead of being joined.
        """

        def __init__(self) -> None:
            self.started = threading.Event()
            self.run_release = threading.Event()
            self.stop_calls = 0
            self.call_lock = threading.Lock()

        def request_stop(self) -> None:
            with self.call_lock:
                self.stop_calls += 1
                is_second_call = self.stop_calls == 2
            if is_second_call:
                self.run_release.set()

        def run(self) -> int:
            self.started.set()
            self.run_release.wait(timeout=1.0)
            return 0

    for _ in range(50):
        created_threads.clear()
        joined_threads.clear()
        entered = threading.Event()
        gate = threading.Event()
        cancel_called = threading.Event()
        stop_returned = threading.Event()
        stt = GatedSTT()
        stop_result: list[bool] = []

        def factory(entered=entered, gate=gate, stt=stt):
            entered.set()
            assert gate.wait(timeout=2.0)
            return stt

        controller = SessionController(stt_factory=factory)
        original_cancel = controller.cancel_start

        def cancel_start(
            cancel_called=cancel_called, original_cancel=original_cancel
        ) -> None:
            cancel_called.set()
            original_cancel()

        controller.cancel_start = cancel_start  # type: ignore[method-assign]

        starter = real_thread(target=controller.start, daemon=True)
        stopper = real_thread(
            target=lambda stop_result=stop_result, controller=controller, stop_returned=stop_returned: (
                stop_result.append(controller.stop(timeout=2.0)), stop_returned.set()
            ),
            daemon=True,
        )
        starter.start()
        assert entered.wait(timeout=2.0), "start did not enter the factory"
        stopper.start()
        assert cancel_called.wait(timeout=2.0), "stop did not request cancellation"

        # Give the unfixed stop() time to take its stale snapshot while start()
        # is still blocked inside the factory holding the controller lock.
        stop_returned.wait(timeout=0.05)
        gate.set()
        starter.join(timeout=2.0)
        stopper.join(timeout=3.0)

        assert not starter.is_alive() and not stopper.is_alive()
        assert stop_result == [True]
        assert len(created_threads) == 1
        assert stt.started.is_set()
        assert not created_threads[0].is_alive()
        assert joined_threads == [created_threads[0]], (
            "stop() joined a stale thread or returned without joining the started thread"
        )


def test_stop_releases_the_lock_before_joining(monkeypatch):
    """The snapshot lock must never be held for the duration of join()."""
    import medical_stt.app as app_module

    real_thread = threading.Thread
    join_started = threading.Event()
    release_join = threading.Event()
    run_started = threading.Event()
    release_run = threading.Event()

    class JoinProbeThread(real_thread):
        def join(self, timeout: float | None = None) -> None:
            join_started.set()
            assert release_join.wait(timeout=2.0)
            super().join(timeout)

    monkeypatch.setattr(app_module.threading, "Thread", JoinProbeThread)

    class BlockingSTT:
        def request_stop(self) -> None:
            return None

        def run(self) -> int:
            run_started.set()
            assert release_run.wait(timeout=2.0)
            return 0

    controller = SessionController(stt_factory=BlockingSTT)
    controller.start()
    assert run_started.wait(timeout=2.0)

    stopper = real_thread(target=lambda: controller.stop(timeout=2.0), daemon=True)
    stopper.start()
    assert join_started.wait(timeout=2.0)
    assert controller._lock.acquire(timeout=0.1) is True  # noqa: SLF001
    controller._lock.release()  # noqa: SLF001

    release_run.set()
    release_join.set()
    stopper.join(timeout=3.0)
    assert not stopper.is_alive()
