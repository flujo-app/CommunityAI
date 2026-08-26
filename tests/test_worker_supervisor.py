import sys
import threading
import time
from types import SimpleNamespace

import pytest

from drift.node.worker_supervisor import (
    SystemBandwidthMonitor,
    WorkerLaunch,
    WorkerPolicyError,
    WorkerReconfigurationBusyError,
    WorkerState,
    WorkerSupervisor,
    WorkerSupervisorSettings,
)


def _wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


def test_supervisor_reconfigures_only_after_persistence_and_while_idle():
    initial = WorkerLaunch(
        "worker",
        "model",
        (sys.executable, "-c", "import time; time.sleep(30)"),
        policy_admitted=False,
        policy_reason="sharing disabled",
    )
    updated = WorkerLaunch(
        "worker",
        "model",
        initial.command,
        policy_admitted=True,
        max_disk_bytes=10,
    )
    supervisor = WorkerSupervisor([initial])
    persisted = []
    supervisor.reconfigure(
        WorkerSupervisorSettings((updated,), stop_timeout=3),
        persist=lambda: persisted.append(True),
    )
    assert persisted == [True]
    assert supervisor.launches == (updated,)

    with pytest.raises(OSError, match="write failed"):
        supervisor.reconfigure(
            WorkerSupervisorSettings((initial,), stop_timeout=4),
            persist=lambda: (_ for _ in ()).throw(OSError("write failed")),
        )
    assert supervisor.launches == (updated,)
    supervisor.shutdown()


def test_supervisor_reconfiguration_refuses_running_intent_before_persistence():
    launch = WorkerLaunch(
        "worker",
        "model",
        (sys.executable, "-c", "import time; time.sleep(30)"),
        auto_restart=False,
    )
    supervisor = WorkerSupervisor([launch], stop_timeout=2, poll_period=0.01)
    supervisor.start_service()
    supervisor.start_worker("worker")
    persisted = []

    with pytest.raises(WorkerReconfigurationBusyError, match="pause all"):
        supervisor.reconfigure(
            WorkerSupervisorSettings((launch,), stop_timeout=3),
            persist=lambda: persisted.append(True),
        )

    assert persisted == []
    assert supervisor.snapshot("worker")["desired_running"] is True
    supervisor.pause_worker("worker")
    supervisor.shutdown()


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


def test_vram_pool_defers_second_worker_until_first_releases_reservation():
    command = (sys.executable, "-c", "import time; time.sleep(30)")
    launches = [
        WorkerLaunch(
            f"gpu-worker-{index}",
            "model",
            command,
            auto_start=True,
            auto_restart=False,
            max_vram_bytes=60,
            vram_device="cuda:0",
            vram_pool_bytes=100,
        )
        for index in range(2)
    ]
    supervisor = WorkerSupervisor(launches, stop_timeout=2, poll_period=0.01)
    supervisor.start_service()

    _wait_for(lambda: supervisor.snapshot("gpu-worker-0")["state"] == WorkerState.RUNNING.value)
    blocked = supervisor.snapshot("gpu-worker-1")
    assert blocked["state"] == WorkerState.PAUSED.value
    assert blocked["desired_running"] is True
    assert blocked["resource_admitted"] is False
    assert blocked["resource_suspended"] is True
    assert blocked["resource_reason"] == "VRAM budget is already reserved on cuda:0"

    supervisor.pause_worker("gpu-worker-0")
    _wait_for(lambda: supervisor.snapshot("gpu-worker-1")["state"] == WorkerState.RUNNING.value)
    resumed = supervisor.snapshot("gpu-worker-1")
    assert resumed["resource_admitted"] is True
    assert resumed["resource_suspended"] is False
    assert resumed["max_vram_bytes"] == 60
    assert resumed["vram_pool_bytes"] == 100
    supervisor.shutdown()


def test_system_bandwidth_monitor_uses_bounded_aggregate_samples():
    samples = iter(
        [
            SimpleNamespace(bytes_sent=1_000_000, bytes_recv=2_000_000),
            SimpleNamespace(bytes_sent=1_500_000, bytes_recv=2_500_000),
            SimpleNamespace(bytes_sent=10, bytes_recv=20),
        ]
    )
    times = iter([10.0, 11.0, 12.0])
    monitor = SystemBandwidthMonitor(counters=lambda: next(samples), clock=lambda: next(times))

    assert monitor() == 8.0
    assert monitor() == 0


@pytest.mark.parametrize(
    ("limit_field", "provider_field", "over_limit", "reason", "current_field"),
    [
        ("max_bandwidth_mbps", "bandwidth_mbps", 12.0, "bandwidth usage", "current_bandwidth_mbps"),
        ("max_power_watts", "power_watts", 125.0, "power usage", "current_power_watts"),
    ],
)
def test_measured_budget_suspends_and_resumes_worker(
    limit_field,
    provider_field,
    over_limit,
    reason,
    current_field,
):
    usage = {"value": 5.0}
    launch = WorkerLaunch(
        "metered-worker",
        "model",
        (sys.executable, "-c", "import time; time.sleep(30)"),
        auto_start=True,
        auto_restart=False,
        **{limit_field: 10.0},
    )
    supervisor = WorkerSupervisor(
        [launch],
        stop_timeout=2,
        poll_period=0.01,
        **{provider_field: lambda *_args: usage["value"]},
    )
    supervisor.start_service()
    _wait_for(lambda: supervisor.snapshot("metered-worker")["state"] == WorkerState.RUNNING.value)

    usage["value"] = over_limit
    _wait_for(lambda: supervisor.snapshot("metered-worker")["state"] == WorkerState.PAUSED.value)
    suspended = supervisor.snapshot("metered-worker")
    assert suspended["desired_running"] is True
    assert suspended["resource_admitted"] is False
    assert suspended["resource_suspended"] is True
    assert reason in suspended["resource_reason"]
    assert suspended[current_field] == over_limit

    usage["value"] = 5.0
    _wait_for(lambda: supervisor.snapshot("metered-worker")["state"] == WorkerState.RUNNING.value)
    resumed = supervisor.snapshot("metered-worker")
    assert resumed["resource_admitted"] is True
    assert resumed["resource_suspended"] is False
    assert resumed["restart_count"] == 1

    supervisor.pause_worker("metered-worker")
    supervisor.shutdown()


def test_power_budget_is_scoped_to_each_workers_selected_device():
    command = (sys.executable, "-c", "import time; time.sleep(30)")
    launches = [
        WorkerLaunch(
            f"gpu-worker-{index}",
            "model",
            command,
            auto_start=True,
            auto_restart=False,
            max_power_watts=150.0,
        )
        for index in range(2)
    ]
    usage = {"gpu-worker-0": 100.0, "gpu-worker-1": 125.0}
    supervisor = WorkerSupervisor(
        launches,
        stop_timeout=2,
        poll_period=0.01,
        power_watts=lambda worker_id: usage[worker_id],
    )
    supervisor.start_service()
    _wait_for(lambda: all(snapshot["state"] == WorkerState.RUNNING.value for snapshot in supervisor.snapshots()))

    usage["gpu-worker-0"] = 200.0
    _wait_for(lambda: supervisor.snapshot("gpu-worker-0")["state"] == WorkerState.PAUSED.value)

    first = supervisor.snapshot("gpu-worker-0")
    second = supervisor.snapshot("gpu-worker-1")
    assert first["resource_suspended"] is True
    assert first["current_power_watts"] == 200.0
    assert second["state"] == WorkerState.RUNNING.value
    assert second["resource_admitted"] is True
    assert second["resource_suspended"] is False
    assert second["current_power_watts"] == 125.0

    usage["gpu-worker-0"] = 100.0
    _wait_for(lambda: supervisor.snapshot("gpu-worker-0")["state"] == WorkerState.RUNNING.value)
    supervisor.shutdown()


@pytest.mark.parametrize(
    ("limit_field", "reason"),
    [
        ("max_bandwidth_mbps", "bandwidth telemetry is unavailable"),
        ("max_power_watts", "power telemetry is unavailable"),
    ],
)
def test_measured_budget_fails_closed_when_telemetry_is_unavailable(limit_field, reason):
    launch = WorkerLaunch(
        "metered-worker",
        "model",
        (sys.executable, "-c", "import time; time.sleep(30)"),
        **{limit_field: 10.0},
    )
    supervisor = WorkerSupervisor([launch])

    with pytest.raises(WorkerPolicyError, match=reason):
        supervisor.start_worker("metered-worker")
    with pytest.raises(WorkerPolicyError, match=reason):
        supervisor.restart_worker("metered-worker")

    snapshot = supervisor.snapshot("metered-worker")
    assert snapshot["desired_running"] is False
    assert snapshot["resource_admitted"] is False
    assert snapshot["resource_suspended"] is False
    assert snapshot["pid"] is None
    supervisor.shutdown()


@pytest.mark.parametrize("value", [None, True, -1, float("nan"), "invalid"])
def test_measured_budget_rejects_invalid_provider_values(value):
    launch = WorkerLaunch(
        "metered-worker",
        "model",
        (sys.executable, "-c", "import time; time.sleep(30)"),
        max_power_watts=10.0,
    )
    supervisor = WorkerSupervisor([launch], power_watts=lambda _worker_id: value)

    with pytest.raises(WorkerPolicyError, match="power telemetry is unavailable"):
        supervisor.start_worker("metered-worker")

    assert supervisor.snapshot("metered-worker")["current_power_watts"] is None
    supervisor.shutdown()


def test_closed_schedule_remains_authoritative_while_resource_is_over_budget():
    schedule = {"allowed": False}
    usage = {"power": 20.0}
    launch = WorkerLaunch(
        "metered-worker",
        "model",
        (sys.executable, "-c", "import time; time.sleep(30)"),
        auto_start=True,
        auto_restart=False,
        max_power_watts=10.0,
    )
    supervisor = WorkerSupervisor(
        [launch],
        stop_timeout=2,
        poll_period=0.01,
        schedule_allowed=lambda: schedule["allowed"],
        power_watts=lambda _worker_id: usage["power"],
    )
    supervisor.start_service()

    snapshot = supervisor.snapshot("metered-worker")
    assert snapshot["schedule_suspended"] is True
    assert snapshot["resource_suspended"] is False
    assert snapshot["pid"] is None

    schedule["allowed"] = True
    _wait_for(lambda: supervisor.snapshot("metered-worker")["resource_suspended"] is True)
    blocked = supervisor.snapshot("metered-worker")
    assert blocked["schedule_suspended"] is False
    assert blocked["state"] == WorkerState.PAUSED.value

    usage["power"] = 5.0
    _wait_for(lambda: supervisor.snapshot("metered-worker")["state"] == WorkerState.RUNNING.value)
    supervisor.pause_worker("metered-worker")
    supervisor.shutdown()
