"""Atomic supervisor gate for revision-bound worker-selection restarts."""

import threading
import time
from dataclasses import replace
from unittest.mock import Mock

import pytest

from drift.node.worker_supervisor import (
    WorkerLaunch,
    WorkerReconfigurationBusyError,
    WorkerSupervisor,
    WorkerSupervisorSettings,
)


def _launch(*, auto_start=False, device=None):
    return WorkerLaunch("worker", "model", ("worker-fixture",), auto_start=auto_start, device=device)


def _paused_supervisor(*, popen=None):
    popen = Mock() if popen is None else popen
    supervisor = WorkerSupervisor([_launch()], popen=popen, poll_period=0.01, stop_timeout=1)
    supervisor.start_service()
    supervisor.pause_worker("worker")
    return supervisor


def _wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not reached before timeout")


def _process():
    process = Mock(pid=424242, stdout=None)
    process.poll.return_value = None
    process.wait.return_value = 0
    return process


def test_empty_supervisor_can_commit_and_latches_restart():
    supervisor = WorkerSupervisor([])
    persisted = Mock()
    try:
        supervisor.commit_configuration_restart(persisted)
        persisted.assert_called_once_with()
        assert supervisor.configuration_restart_pending
        with pytest.raises(WorkerReconfigurationBusyError, match="restart is pending"):
            supervisor.start_service()
    finally:
        supervisor.shutdown()


def test_commit_first_blocks_every_launch_and_reconfiguration_path():
    popen = Mock()
    supervisor = _paused_supervisor(popen=popen)
    replacement = replace(_launch(), model_id="replacement")
    settings = WorkerSupervisorSettings((replacement,), stop_timeout=1)
    try:
        supervisor.commit_configuration_restart(lambda: None)
        assert supervisor.configuration_restart_pending
        assert supervisor.pause_worker("worker") is False
        assert supervisor.pause_worker_for_reconfiguration("worker") is False
        for action in (
            lambda: supervisor.start_worker("worker"),
            lambda: supervisor.restart_worker("worker"),
            supervisor.start_service,
            lambda: supervisor.replace_launch(replacement, start=True),
            lambda: supervisor.reconfigure(settings, persist=lambda: None),
            lambda: supervisor.commit_configuration_restart(lambda: None),
        ):
            with pytest.raises(WorkerReconfigurationBusyError, match="restart is pending"):
                action()
        popen.assert_not_called()
        snapshot = supervisor.snapshot("worker")
        assert snapshot["operator_paused"] and not snapshot["desired_running"] and snapshot["pid"] is None
    finally:
        supervisor.shutdown()


def test_persistence_failure_leaves_latch_open_for_normal_start(monkeypatch):
    process = _process()
    popen = Mock(return_value=process)
    supervisor = _paused_supervisor(popen=popen)
    monkeypatch.setattr(supervisor, "_kill_linux_worker_group", lambda ignored: None)

    class WriteFailed(Exception):
        pass

    def fail():
        raise WriteFailed("fixture write failed")

    try:
        with pytest.raises(WriteFailed, match="fixture write failed"):
            supervisor.commit_configuration_restart(fail)
        assert not supervisor.configuration_restart_pending
        assert supervisor.start_worker("worker") is True
        popen.assert_called_once()
        supervisor.pause_worker("worker")
    finally:
        supervisor.shutdown()


def test_start_wins_lock_race_and_prevents_commit(monkeypatch):
    entered_popen = threading.Event()
    release_popen = threading.Event()
    process = _process()

    def popen(*args, **kwargs):
        entered_popen.set()
        assert release_popen.wait(2)
        return process

    supervisor = _paused_supervisor(popen=popen)
    monkeypatch.setattr(supervisor, "_kill_linux_worker_group", lambda ignored: None)
    start_errors = []
    commit_errors = []
    persisted = Mock()

    def start():
        try:
            supervisor.start_worker("worker")
        except BaseException as exc:  # pragma: no cover - asserted below
            start_errors.append(exc)

    def commit():
        try:
            supervisor.commit_configuration_restart(persisted)
        except BaseException as exc:
            commit_errors.append(exc)

    start_thread = threading.Thread(target=start)
    commit_thread = threading.Thread(target=commit)
    try:
        start_thread.start()
        assert entered_popen.wait(2)
        commit_thread.start()
        release_popen.set()
        start_thread.join(2)
        commit_thread.join(2)
        assert not start_thread.is_alive() and not commit_thread.is_alive()
        assert start_errors == []
        assert len(commit_errors) == 1
        assert isinstance(commit_errors[0], WorkerReconfigurationBusyError)
        persisted.assert_not_called()
        assert not supervisor.configuration_restart_pending
        supervisor.pause_worker("worker")
    finally:
        release_popen.set()
        start_thread.join(2)
        commit_thread.join(2)
        supervisor.shutdown()


def test_commit_wins_lock_race_and_blocks_waiting_start():
    entered_persist = threading.Event()
    release_persist = threading.Event()
    popen = Mock()
    supervisor = _paused_supervisor(popen=popen)
    commit_errors = []
    start_errors = []

    def persist():
        entered_persist.set()
        assert release_persist.wait(2)

    def commit():
        try:
            supervisor.commit_configuration_restart(persist)
        except BaseException as exc:  # pragma: no cover - asserted below
            commit_errors.append(exc)

    def start():
        try:
            supervisor.start_worker("worker")
        except BaseException as exc:
            start_errors.append(exc)

    commit_thread = threading.Thread(target=commit)
    start_thread = threading.Thread(target=start)
    try:
        commit_thread.start()
        assert entered_persist.wait(2)
        start_thread.start()
        release_persist.set()
        commit_thread.join(2)
        start_thread.join(2)
        assert not commit_thread.is_alive() and not start_thread.is_alive()
        assert commit_errors == []
        assert len(start_errors) == 1
        assert isinstance(start_errors[0], WorkerReconfigurationBusyError)
        assert supervisor.configuration_restart_pending
        popen.assert_not_called()
    finally:
        release_persist.set()
        commit_thread.join(2)
        start_thread.join(2)
        supervisor.shutdown()


@pytest.mark.parametrize("kind", ["schedule", "resource"])
def test_suspended_running_intent_must_be_explicitly_paused_before_commit(kind):
    allowed = kind != "schedule"
    available = kind != "resource"
    launch = _launch(auto_start=True, device="cuda:0" if kind == "resource" else None)
    supervisor = WorkerSupervisor(
        [launch],
        poll_period=0.01,
        schedule_allowed=lambda: allowed,
        device_available=lambda worker_id: available,
        popen=Mock(),
    )
    persisted = Mock()
    try:
        supervisor.start_service()
        snapshot = supervisor.snapshot("worker")
        assert snapshot[f"{kind}_suspended"] and snapshot["desired_running"]
        with pytest.raises(WorkerReconfigurationBusyError, match="pause all contribution workers"):
            supervisor.commit_configuration_restart(persisted)
        persisted.assert_not_called()
        supervisor.pause_worker("worker")
        supervisor.commit_configuration_restart(persisted)
        persisted.assert_called_once_with()
    finally:
        supervisor.shutdown()


def test_stopping_worker_cannot_commit_even_after_operator_pause(monkeypatch):
    allowed = True
    wait_started = threading.Event()
    release_wait = threading.Event()
    exited = threading.Event()
    process = Mock(pid=424242, stdout=None)
    process.poll.side_effect = lambda: 0 if exited.is_set() else None

    def wait(timeout=None):
        wait_started.set()
        assert release_wait.wait(2)
        exited.set()
        return 0

    process.wait.side_effect = wait
    supervisor = WorkerSupervisor(
        [_launch(auto_start=True)],
        poll_period=0.01,
        schedule_allowed=lambda: allowed,
        popen=Mock(return_value=process),
        stop_timeout=1,
    )
    monkeypatch.setattr(supervisor, "_kill_linux_worker_group", lambda ignored: None)
    pause_errors = []

    def pause():
        try:
            supervisor.pause_worker("worker")
        except BaseException as exc:  # pragma: no cover - asserted below
            pause_errors.append(exc)

    pause_thread = threading.Thread(target=pause)
    try:
        supervisor.start_service()
        allowed = False
        assert wait_started.wait(2)
        pause_thread.start()
        _wait_for(lambda: supervisor.snapshot("worker")["operator_paused"])
        snapshot = supervisor.snapshot("worker")
        assert snapshot["state"] == "stopping" and not snapshot["desired_running"]
        with pytest.raises(WorkerReconfigurationBusyError, match="pause all contribution workers"):
            supervisor.commit_configuration_restart(lambda: None)
    finally:
        release_wait.set()
        pause_thread.join(3)
        assert pause_errors == []
        supervisor.shutdown()
