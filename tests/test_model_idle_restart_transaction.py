import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from drift.node.model_manager import ModelDescriptor, ModelManager, ModelManagerClosedError, ModelRuntime


def _manager(close=None):
    manager = ModelManager()
    manager.register(ModelDescriptor("model"), lambda: ModelRuntime(object(), object(), close=close))
    return manager


def test_active_inference_rejects_commit_without_persistence_or_cancellation():
    close = Mock()
    manager = _manager(close)
    lease = manager.load("model")
    persist = Mock()

    assert manager.commit_idle_restart(persist) is False
    persist.assert_not_called()
    close.assert_not_called()
    assert manager.snapshots()[0].active_requests == 1
    with manager.load("model") as concurrent:
        assert concurrent.runtime is lease.runtime
    lease.release()

    assert manager.commit_idle_restart(persist) is True
    persist.assert_called_once_with()
    close.assert_not_called()
    with pytest.raises(ModelManagerClosedError):
        manager.load("model")
    manager.shutdown()
    close.assert_called_once_with()


@pytest.mark.parametrize("stopping", ["closed", "draining"])
def test_stopping_manager_never_persists(stopping):
    manager = _manager()
    if stopping == "closed":
        manager.shutdown()
    else:
        assert manager.begin_idle_restart() is True
    persist = Mock()
    assert manager.commit_idle_restart(persist) is False
    persist.assert_not_called()


def test_failed_persistence_preserves_admission_and_retry():
    manager = _manager()
    failure = OSError("disk full")
    persist = Mock(side_effect=failure)
    with pytest.raises(OSError) as error:
        manager.commit_idle_restart(persist)
    assert error.value is failure
    with manager.load("model"):
        assert manager.snapshots()[0].active_requests == 1
    assert manager.commit_idle_restart(Mock()) is True


@pytest.mark.parametrize("operation", ["loading", "unloading"])
def test_inflight_runtime_transition_rejects_commit(operation):
    entered = threading.Event()
    finish = threading.Event()

    def blocked():
        entered.set()
        assert finish.wait(5)

    def loader():
        if operation == "loading":
            blocked()
        return ModelRuntime(object(), object(), close=blocked if operation == "unloading" else None)

    manager = ModelManager()
    manager.register(ModelDescriptor("model"), loader)
    if operation == "unloading":
        manager.load("model").release()
    persist = Mock()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(manager.load if operation == "loading" else manager.unload, "model")
        try:
            assert entered.wait(5)
            assert manager.commit_idle_restart(persist) is False
            persist.assert_not_called()
        finally:
            finish.set()
        result = future.result(timeout=5)
        if operation == "loading":
            result.release()
    assert manager.commit_idle_restart(persist) is True
    persist.assert_called_once_with()


@pytest.mark.parametrize("persist_fails", [False, True])
def test_admission_cannot_pass_persistence_transaction(persist_fails):
    manager = _manager()
    # Exercise the ready-runtime fast path, as well as lazy loading above.
    manager.load("model").release()
    entered = threading.Event()
    finish = threading.Event()
    admission_attempted = threading.Event()
    admission_finished = threading.Event()

    def persist():
        entered.set()
        assert finish.wait(5)
        if persist_fails:
            raise OSError("disk full")

    def admit():
        admission_attempted.set()
        try:
            return manager.load("model")
        finally:
            admission_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        commit = pool.submit(manager.commit_idle_restart, persist)
        try:
            assert entered.wait(5)
            admission = pool.submit(admit)
            assert admission_attempted.wait(5)
            assert not admission_finished.wait(0.05)
        finally:
            finish.set()
        if persist_fails:
            with pytest.raises(OSError, match="disk full"):
                commit.result(timeout=5)
            admission.result(timeout=5).release()
            assert manager.begin_idle_restart() is True
        else:
            assert commit.result(timeout=5) is True
            with pytest.raises(ModelManagerClosedError):
                admission.result(timeout=5)


def test_begin_idle_restart_preserves_existing_idle_only_behavior():
    manager = _manager()
    lease = manager.load("model")
    assert manager.begin_idle_restart() is False
    lease.release()
    assert manager.begin_idle_restart() is True
    assert manager.begin_idle_restart() is False
    with pytest.raises(ModelManagerClosedError):
        manager.load("model")
