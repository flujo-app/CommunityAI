import sys
import time

from drift.node.worker_supervisor import WorkerLaunch, WorkerState, WorkerSupervisor


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
