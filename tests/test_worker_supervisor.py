import sys
import threading
import time

import pytest

from drift.node.worker_supervisor import WorkerLaunch, WorkerPolicyError, WorkerState, WorkerSupervisor


def _wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


def test_supervisor_starts_and_pauses_an_isolated_worker_process():
    launch = WorkerLaunch(
        "worker",
        "model",
        (
            sys.executable,
            "-c",
            "import os,time; print(os.environ['DRIFT_WORKER_TEST_VALUE'], flush=True); time.sleep(30)",
        ),
        auto_restart=False,
        environment=(("DRIFT_WORKER_TEST_VALUE", "configured"),),
    )
    supervisor = WorkerSupervisor([launch], stop_timeout=2, poll_period=0.01)
    supervisor.start_service()

    assert supervisor.start_worker("WORKER") is True
    _wait_for(lambda: supervisor.snapshot("worker")["state"] == WorkerState.RUNNING.value)
    assert supervisor.snapshot("worker")["pid"] is not None
    _wait_for(lambda: supervisor.snapshot("worker")["recent_logs"] == ["configured"])

    assert supervisor.pause_worker("worker") is True
    snapshot = supervisor.snapshot("worker")
    assert snapshot["state"] == WorkerState.PAUSED.value
    assert snapshot["pid"] is None
    assert snapshot["recent_logs"] == ["configured"]
    supervisor.shutdown()


def test_crashed_worker_restarts_without_affecting_supervisor():
    launch = WorkerLaunch(
        "worker",
        "model",
        (sys.executable, "-c", "raise SystemExit(7)"),
        auto_start=True,
        auto_restart=True,
        restart_backoff=0.02,
    )
    supervisor = WorkerSupervisor([launch], stop_timeout=2, poll_period=0.01)
    supervisor.start_service()

    _wait_for(lambda: supervisor.snapshot("worker")["restart_count"] >= 1)
    assert supervisor.snapshot("worker")["desired_running"] is True
    supervisor.pause_worker("worker")
    assert supervisor.snapshot("worker")["state"] == WorkerState.PAUSED.value
    supervisor.shutdown()


def test_schedule_defers_auto_start_suspends_a_running_worker_and_resumes():
    schedule = {"allowed": False}
    launch = WorkerLaunch(
        "scheduled-worker",
        "model",
        (sys.executable, "-c", "import time; time.sleep(30)"),
        auto_start=True,
        auto_restart=False,
    )
    supervisor = WorkerSupervisor(
        [launch],
        stop_timeout=2,
        poll_period=0.01,
        schedule_allowed=lambda: schedule["allowed"],
    )
    supervisor.start_service()

    snapshot = supervisor.snapshot("scheduled-worker")
    assert snapshot["state"] == WorkerState.PAUSED.value
    assert snapshot["desired_running"] is True
    assert snapshot["schedule_admitted"] is False
    assert snapshot["schedule_suspended"] is True
    assert snapshot["schedule_reason"] == "outside the configured contribution schedule"

    schedule["allowed"] = True
    _wait_for(lambda: supervisor.snapshot("scheduled-worker")["state"] == WorkerState.RUNNING.value)

    schedule["allowed"] = False
    _wait_for(lambda: supervisor.snapshot("scheduled-worker")["state"] == WorkerState.PAUSED.value)
    snapshot = supervisor.snapshot("scheduled-worker")
    assert snapshot["pid"] is None
    assert snapshot["desired_running"] is True
    assert snapshot["schedule_suspended"] is True

    schedule["allowed"] = True
    _wait_for(lambda: supervisor.snapshot("scheduled-worker")["state"] == WorkerState.RUNNING.value)
    assert supervisor.snapshot("scheduled-worker")["restart_count"] == 1

    supervisor.pause_worker("scheduled-worker")
    time.sleep(0.05)
    assert supervisor.snapshot("scheduled-worker")["desired_running"] is False
    assert supervisor.snapshot("scheduled-worker")["state"] == WorkerState.PAUSED.value
    supervisor.shutdown()


def test_schedule_suspends_multiple_workers_concurrently(monkeypatch):
    schedule = {"allowed": True}
    launches = [
        WorkerLaunch(
            f"scheduled-worker-{index}",
            "model",
            (sys.executable, "-c", "import time; time.sleep(30)"),
            auto_start=True,
            auto_restart=False,
        )
        for index in range(2)
    ]
    supervisor = WorkerSupervisor(
        launches,
        stop_timeout=2,
        poll_period=0.01,
        schedule_allowed=lambda: schedule["allowed"],
    )
    supervisor.start_service()
    _wait_for(lambda: all(snapshot["state"] == WorkerState.RUNNING.value for snapshot in supervisor.snapshots()))

    original_terminate = supervisor._terminate
    termination_gate = threading.Barrier(len(launches))

    def synchronized_terminate(process):
        termination_gate.wait(timeout=1)
        return original_terminate(process)

    monkeypatch.setattr(supervisor, "_terminate", synchronized_terminate)
    schedule["allowed"] = False

    _wait_for(lambda: all(snapshot["state"] == WorkerState.PAUSED.value for snapshot in supervisor.snapshots()))
    assert all(snapshot["schedule_suspended"] is True for snapshot in supervisor.snapshots())
    supervisor.shutdown()


def test_closed_schedule_does_not_resume_a_previously_crashed_worker():
    schedule = {"allowed": True}
    launch = WorkerLaunch(
        "scheduled-worker",
        "model",
        (sys.executable, "-c", "raise SystemExit(7)"),
        auto_start=True,
        auto_restart=False,
    )
    supervisor = WorkerSupervisor(
        [launch],
        stop_timeout=2,
        poll_period=0.01,
        schedule_allowed=lambda: schedule["allowed"],
    )
    supervisor.start_service()
    _wait_for(lambda: supervisor.snapshot("scheduled-worker")["state"] == WorkerState.CRASHED.value)

    schedule["allowed"] = False
    time.sleep(0.05)
    snapshot = supervisor.snapshot("scheduled-worker")
    assert snapshot["state"] == WorkerState.CRASHED.value
    assert snapshot["schedule_suspended"] is False

    schedule["allowed"] = True
    time.sleep(0.05)
    snapshot = supervisor.snapshot("scheduled-worker")
    assert snapshot["state"] == WorkerState.CRASHED.value
    assert snapshot["restart_count"] == 0
    supervisor.shutdown()


def test_manual_start_fails_closed_outside_contribution_schedule():
    launch = WorkerLaunch(
        "scheduled-worker",
        "model",
        (sys.executable, "-c", "import time; time.sleep(30)"),
    )
    supervisor = WorkerSupervisor([launch], schedule_allowed=lambda: False)

    with pytest.raises(WorkerPolicyError, match="outside the configured contribution schedule"):
        supervisor.start_worker("scheduled-worker")

    snapshot = supervisor.snapshot("scheduled-worker")
    assert snapshot["desired_running"] is False
    assert snapshot["pid"] is None
    assert snapshot["schedule_admitted"] is False
    supervisor.shutdown()
