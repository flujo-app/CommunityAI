"""Joint launch transitions with real local children and synthetic GPU claims.

The children only sleep. These tests establish process ordering, admission and
operator-intent behavior; they do not qualify any GPU runtime or hardware.
"""

import os
import subprocess
import sys
import threading
import time
from dataclasses import replace

import psutil
import pytest

from drift.node.worker_supervisor import (
    WorkerLaunch,
    WorkerNotFoundError,
    WorkerPolicyError,
    WorkerReconfigurationBusyError,
    WorkerSupervisor,
    WorkerSupervisorSettings,
)


def _wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("joint transition did not reach the expected state")


def _automatic(worker_id, start, *, device=0, manifest="a", auto_start=True, pool=100, reservation=60):
    block_range = f"{start}:{start + 1}"
    binding = {
        "placement_manifest_digest": "sha256:" + manifest * 64,
        "placement_artifact_bytes": 1234,
        "placement_artifact_set_digest": "b" * 64,
        "placement_cache_root": os.path.realpath(sys.prefix),
    }
    command = (
        sys.executable,
        "-m",
        "drift.cli",
        "server",
        "org/model",
        "--block_indices",
        block_range,
        "--expected_block_indices",
        block_range,
        "--expected_manifest_digest",
        binding["placement_manifest_digest"],
        "--expected_artifact_bytes",
        str(binding["placement_artifact_bytes"]),
        "--expected_artifact_set_digest",
        binding["placement_artifact_set_digest"],
        "--expected_cache_root",
        binding["placement_cache_root"],
        "--cache_dir",
        binding["placement_cache_root"],
    )
    return WorkerLaunch(
        worker_id,
        "model",
        command,
        auto_start=auto_start,
        restart_backoff=0.01,
        automatic=True,
        block_indices=block_range,
        placement_reason="synthetic joint transition fixture",
        intent_published=True,
        remote_acknowledged=True,
        device=f"cuda:{device}",
        vram_device=f"cuda:{device}",
        max_vram_bytes=reservation,
        vram_pool_bytes=pool,
        **binding,
    )


def _manual(worker_id="spectator", *, device=1, reservation=50, pool=100):
    return WorkerLaunch(
        worker_id,
        "manual-model",
        (sys.executable, "-c", "import time; time.sleep(60)"),
        device=f"cuda:{device}",
        vram_device=f"cuda:{device}",
        max_vram_bytes=reservation,
        vram_pool_bytes=pool,
    )


def _launch_map(supervisor):
    return {launch.worker_id.casefold(): launch for launch in supervisor.launches}


class _Children:
    def __init__(self):
        self.processes = []
        self.commands = []
        self.spawned_at = []
        self.before_spawn = None
        self.program = "import time; time.sleep(60)"

    def popen(self, command, **kwargs):
        if self.before_spawn is not None:
            self.before_spawn(tuple(command))
        process = subprocess.Popen(
            (sys.executable, "-c", self.program),
            **kwargs,
        )
        self.commands.append(tuple(command))
        self.processes.append(process)
        self.spawned_at.append(time.monotonic())
        return process


@pytest.fixture
def make_supervisor():
    supervisors, children = [], []

    def make(launches, *, coordinated_launches=True, schedule_allowed=None):
        spawned = _Children()
        supervisor = WorkerSupervisor(
            launches,
            popen=spawned.popen,
            poll_period=0.01,
            stop_timeout=1,
            coordinated_launches=coordinated_launches,
            schedule_allowed=schedule_allowed,
        )
        supervisors.append(supervisor)
        children.append(spawned)
        return supervisor, spawned

    yield make
    for supervisor in supervisors:
        supervisor.shutdown()
    # Keep a failed regression assertion from leaving its real fixture children.
    for spawned in children:
        for process in spawned.processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def _background(callback):
    result = {"value": None, "error": None}
    done = threading.Event()

    def run():
        try:
            result["value"] = callback()
        except Exception as exc:
            result["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, done, result


def _join(background):
    thread, done, result = background
    assert done.wait(5), "joint transition thread exceeded the test timeout"
    thread.join(timeout=1)
    assert not thread.is_alive()
    return result


def test_coordinated_constructor_rejects_overlapping_initial_ranges_before_start():
    children = _Children()
    with pytest.raises(ValueError, match="overlap"):
        WorkerSupervisor(
            [_automatic("first", 0), _automatic("second", 0, device=1)],
            popen=children.popen,
            coordinated_launches=True,
        )
    assert not children.processes


def test_eight_card_range_swap_retires_every_old_process_before_install_or_spawn(make_supervisor, monkeypatch):
    initial = tuple(_automatic(f"gpu-{index}", index, device=index) for index in range(8))
    replacements = tuple(_automatic(f"gpu-{index}", (index + 1) % 8, device=index) for index in range(8))
    supervisor, children = make_supervisor(initial)
    supervisor.start_service()
    old_processes = tuple(children.processes)
    assert len(old_processes) == 8
    expected = {launch.worker_id: launch for launch in replacements}
    original_cleanup = supervisor._terminate_launch_tree
    cleaned = []

    def cleanup(process):
        assert supervisor.launches == initial, "a launch changed before all old children retired"
        result = original_cleanup(process)
        cleaned.append(process.pid)
        return result

    def before_spawn(command):
        assert all(process.poll() is not None for process in old_processes)
        assert len(cleaned) == 8
        assert _launch_map(supervisor) == expected

    def forbidden_start(worker_id):
        raise AssertionError("batch transitions must not call public start_worker")

    monkeypatch.setattr(supervisor, "_terminate_launch_tree", cleanup)
    monkeypatch.setattr(supervisor, "start_worker", forbidden_start)
    children.before_spawn = before_spawn
    assert supervisor.replace_launches(replacements, start=True)
    assert len(children.processes) == 16
    snapshots = supervisor.snapshots()
    assert all(row["state"] == "running" and row["desired_running"] for row in snapshots)
    assert {row["pid"] for row in snapshots}.isdisjoint({process.pid for process in old_processes})
    assert [row["block_indices"] for row in snapshots] == [f"{(i + 1) % 8}:{(i + 1) % 8 + 1}" for i in range(8)]


def test_subset_transition_keeps_unaffected_child_and_launch(make_supervisor):
    first, second = _automatic("first", 0), _automatic("second", 1, device=1)
    supervisor, children = make_supervisor((first, second))
    supervisor.start_service()
    old_first, old_second = children.processes
    replacement = _automatic("FIRST", 8)
    assert supervisor.replace_launches([replacement], start=True)
    assert _launch_map(supervisor) == {"first": replacement, "second": second}
    assert old_first.poll() is not None
    assert old_second.poll() is None
    assert supervisor.snapshot("second")["pid"] == old_second.pid
    assert len(children.processes) == 3


@pytest.mark.parametrize("invalid", ["duplicate", "unknown", "oversized", "not_launch"])
def test_invalid_batch_rejects_before_stopping_any_child(make_supervisor, invalid):
    first, second = _automatic("first", 0), _automatic("second", 1, device=1)
    supervisor, children = make_supervisor((first, second))
    supervisor.start_service()
    proposed = {
        "duplicate": [first, replace(first, worker_id="FIRST")],
        "unknown": [_automatic("unknown", 8)],
        "oversized": [_automatic(f"card-{index}", index, device=index % 16) for index in range(17)],
        "not_launch": [object()],
    }[invalid]
    with pytest.raises((ValueError, TypeError, WorkerNotFoundError)):
        supervisor.replace_launches(proposed, start=True)
    assert supervisor.launches == (first, second)
    assert len(children.processes) == 2
    assert all(process.poll() is None for process in children.processes)
    assert all(row["desired_running"] for row in supervisor.snapshots())


def test_final_map_rejects_overlap_with_unaffected_worker_before_cleanup(make_supervisor):
    initial = (_automatic("first", 0), _automatic("second", 1, device=1))
    supervisor, children = make_supervisor(initial)
    supervisor.start_service()
    with pytest.raises(ValueError):
        supervisor.replace_launches([_automatic("first", 1)], start=True)
    assert supervisor.launches == initial
    assert all(process.poll() is None for process in children.processes)


def test_final_map_rejects_new_conflict_between_batch_members(make_supervisor):
    initial = (_automatic("first", 0), _automatic("second", 1, device=1))
    supervisor, children = make_supervisor(initial)
    supervisor.start_service()
    with pytest.raises(ValueError):
        supervisor.replace_launches([_automatic("first", 8), _automatic("second", 8, device=1)])
    assert supervisor.launches == initial
    assert all(process.poll() is None for process in children.processes)


@pytest.mark.parametrize("violation", ["overlap", "noncanonical_range"])
def test_single_replacement_cannot_bypass_joint_range_validation(make_supervisor, violation):
    initial = (_automatic("first", 0, auto_start=False), _automatic("second", 1, device=1))
    supervisor, children = make_supervisor(initial)
    supervisor.start_service()
    retained = children.processes[0]
    replacement = _automatic("first", 1)
    if violation == "noncanonical_range":
        command = list(replacement.command)
        command[command.index("--block_indices") + 1] = "0:01"
        command[command.index("--expected_block_indices") + 1] = "0:01"
        replacement = replace(replacement, block_indices="0:01", command=tuple(command))
    with pytest.raises(ValueError, match="overlap|canonical"):
        supervisor.replace_launch(replacement, start=True)
    assert supervisor.launches == initial
    assert len(children.processes) == 1 and retained.poll() is None
    assert supervisor.snapshot("first")["state"] == "paused"
    assert not supervisor.snapshot("first")["desired_running"]


@pytest.mark.parametrize("violation", ["overlap", "noncanonical_range"])
def test_policy_reconfigure_cannot_persist_a_joint_range_validation_bypass(make_supervisor, violation):
    initial = (_automatic("first", 0, auto_start=False), _automatic("second", 1, device=1, auto_start=False))
    supervisor, children = make_supervisor(initial)
    supervisor.start_service()
    replacement = _automatic("first", 1, auto_start=False)
    if violation == "noncanonical_range":
        command = list(replacement.command)
        command[command.index("--block_indices") + 1] = "0:01"
        command[command.index("--expected_block_indices") + 1] = "0:01"
        replacement = replace(replacement, block_indices="0:01", command=tuple(command))
    settings = WorkerSupervisorSettings((replacement, initial[1]), stop_timeout=3)
    persisted = []
    with pytest.raises(ValueError, match="overlap|canonical"):
        supervisor.reconfigure(settings, persist=lambda: persisted.append(True))
    assert not persisted
    assert supervisor.launches == initial
    assert supervisor._stop_timeout == 1
    assert not children.processes


@pytest.mark.parametrize("block_range", ["-1:1", "1:1", "2:1", "0:513", "0:1:2", "0:01", " 0:1"])
def test_invalid_exact_range_rejects_before_process_changes(make_supervisor, block_range):
    initial = _automatic("first", 0)
    supervisor, children = make_supervisor([initial])
    supervisor.start_service()
    command = list(initial.command)
    command[command.index("--block_indices") + 1] = block_range
    command[command.index("--expected_block_indices") + 1] = block_range
    malformed = replace(initial, block_indices=block_range, command=tuple(command))
    with pytest.raises(ValueError):
        supervisor.replace_launches([malformed], start=True)
    assert supervisor.launches == (initial,)
    assert len(children.processes) == 1 and children.processes[0].poll() is None


def test_final_map_allows_equal_ranges_for_different_manifests(make_supervisor):
    first, second = _automatic("first", 0), _automatic("second", 1, device=1)
    supervisor, children = make_supervisor((first, second))
    supervisor.start_service()
    replacement = _automatic("first", 1, manifest="c")
    assert supervisor.replace_launches([replacement], start=True)
    assert _launch_map(supervisor)["first"] == replacement
    assert len(children.processes) == 3


def test_final_map_checks_vram_pool_against_unaffected_manual_worker(make_supervisor):
    first = _automatic("first", 0, reservation=40)
    spectator = _manual(device=0, reservation=40)
    supervisor, children = make_supervisor((first, spectator))
    supervisor.start_service()
    with pytest.raises(ValueError):
        supervisor.replace_launches([replace(first, vram_pool_bytes=200)], start=True)
    assert supervisor.launches == (first, spectator)
    assert len(children.processes) == 1 and children.processes[0].poll() is None


@pytest.mark.parametrize("retry_superset", [False, True])
def test_cleanup_failure_retains_old_launches_reservation_and_requires_complete_retry(
    make_supervisor, monkeypatch, retry_superset
):
    first = _automatic("first", 0)
    second = _automatic("second", 1, device=1, reservation=75)
    spectator = _manual()
    initial = (first, second, spectator)
    replacements = (_automatic("first", 1), _automatic("second", 0, device=1, reservation=60))
    supervisor, children = make_supervisor(initial)
    supervisor.start_service()
    old_first, old_second = tuple(children.processes)
    original_cleanup = supervisor._terminate_launch_tree

    def fail_second(process):
        if process is old_second:
            raise RuntimeError("controlled cleanup failure")
        return original_cleanup(process)

    monkeypatch.setattr(supervisor, "_terminate_launch_tree", fail_second)
    with pytest.raises(RuntimeError):
        supervisor.replace_launches(replacements, start=True)
    assert supervisor.launches == initial
    assert old_first.poll() is not None and old_second.poll() is None
    assert len(children.processes) == 2
    assert supervisor.launch_transition_status == {
        "state": "cleanup_failed",
        "worker_ids": ["first", "second"],
        "closed": False,
    }
    failed = supervisor.snapshot("second")
    assert failed["pid"] == old_second.pid and failed["max_vram_bytes"] == 75
    assert not supervisor.snapshot("spectator")["resource_admitted"]
    for action in (
        lambda: supervisor.start_worker("spectator"),
        lambda: supervisor.restart_worker("first"),
        lambda: supervisor.replace_launch(spectator),
        lambda: supervisor.replace_launches([replacements[0]], start=True),
    ):
        with pytest.raises(WorkerReconfigurationBusyError):
            action()
    assert supervisor.launches == initial and len(children.processes) == 2
    monkeypatch.setattr(supervisor, "_terminate_launch_tree", original_cleanup)

    def all_old_stopped(command):
        assert old_first.poll() is not None and old_second.poll() is not None

    children.before_spawn = all_old_stopped
    retry = replacements + (spectator,) if retry_superset else replacements
    assert supervisor.replace_launches(retry, start=True)
    assert _launch_map(supervisor) == {"first": replacements[0], "second": replacements[1], "spectator": spectator}
    assert len(children.processes) == 4
    assert supervisor.launch_transition_status == {"state": "idle", "worker_ids": [], "closed": False}


def test_operator_pause_during_cleanup_survives_requested_batch_start(make_supervisor, monkeypatch):
    initial = (_automatic("first", 0), _automatic("second", 1, device=1))
    replacements = (_automatic("first", 1), _automatic("second", 0, device=1))
    supervisor, children = make_supervisor(initial)
    supervisor.start_service()
    entered, release = threading.Event(), threading.Event()
    original_cleanup = supervisor._terminate_launch_tree

    def blocked_cleanup(process):
        entered.set()
        assert release.wait(5), "test did not release batch cleanup"
        return original_cleanup(process)

    monkeypatch.setattr(supervisor, "_terminate_launch_tree", blocked_cleanup)
    transition = _background(lambda: supervisor.replace_launches(replacements, start=True))
    pause = None
    try:
        assert entered.wait(5)
        pause = _background(lambda: supervisor.pause_worker("first"))
        _wait_for(lambda: supervisor.snapshot("first")["operator_paused"])
    finally:
        release.set()
        outcome = _join(transition)
        pause_outcome = _join(pause) if pause is not None else None
    assert outcome["error"] is None
    assert pause_outcome is not None and pause_outcome["error"] is None
    first, second = supervisor.snapshot("first"), supervisor.snapshot("second")
    assert first["operator_paused"] and not first["desired_running"] and first["pid"] is None
    assert second["desired_running"] and second["pid"] is not None
    assert len(children.processes) == 3


def test_transition_blocks_interleaved_launch_and_configuration_operations(make_supervisor, monkeypatch):
    initial = (_automatic("first", 0), _automatic("second", 1, device=1), _manual(device=2))
    replacements = (_automatic("first", 1), _automatic("second", 0, device=1))
    supervisor, children = make_supervisor(initial)
    supervisor.start_service()
    first_process, second_process = tuple(children.processes)
    entered, release = threading.Event(), threading.Event()
    original_cleanup = supervisor._terminate_launch_tree
    persisted = []

    def blocked_second(process):
        if process is second_process:
            entered.set()
            assert release.wait(5), "test did not release batch cleanup"
        return original_cleanup(process)

    monkeypatch.setattr(supervisor, "_terminate_launch_tree", blocked_second)
    transition = _background(lambda: supervisor.replace_launches(replacements, start=True))
    try:
        assert entered.wait(5)
        assert first_process.poll() is not None
        assert supervisor.launch_transition_status == {
            "state": "stopping",
            "worker_ids": ["first", "second"],
            "closed": False,
        }
        settings = WorkerSupervisorSettings(initial, stop_timeout=1)
        actions = (
            lambda: supervisor.start_worker("spectator"),
            lambda: supervisor.restart_worker("spectator"),
            lambda: supervisor.start_service(),
            lambda: supervisor.replace_launch(initial[2]),
            lambda: supervisor.replace_launches([initial[2]]),
            lambda: supervisor.reconfigure(settings, persist=lambda: persisted.append("policy")),
            lambda: supervisor.commit_configuration_restart(lambda: persisted.append("restart")),
        )
        for action in actions:
            with pytest.raises(WorkerReconfigurationBusyError):
                action()
        # The monitor must remain alive without restarting the already stopped
        # first worker while the second worker is still cleaning up.
        assert not release.wait(0.05)
        assert supervisor._monitor.is_alive()
        assert supervisor.launches == initial
        assert len(children.processes) == 2 and not persisted
        assert not supervisor.snapshot("spectator")["desired_running"]
    finally:
        release.set()
        outcome = _join(transition)
    assert outcome["error"] is None
    assert len(children.processes) == 4


def test_shutdown_during_cleanup_never_installs_or_starts_replacements(make_supervisor, monkeypatch):
    initial = (_automatic("first", 0), _automatic("second", 1, device=1))
    replacements = (_automatic("first", 1), _automatic("second", 0, device=1))
    supervisor, children = make_supervisor(initial)
    supervisor.start_service()
    entered, release = threading.Event(), threading.Event()
    original_cleanup = supervisor._terminate_launch_tree

    def blocked_cleanup(process):
        entered.set()
        assert release.wait(5), "test did not release batch cleanup"
        return original_cleanup(process)

    monkeypatch.setattr(supervisor, "_terminate_launch_tree", blocked_cleanup)
    transition = _background(lambda: supervisor.replace_launches(replacements, start=True))
    stopping = None
    try:
        assert entered.wait(5)
        stopping = _background(supervisor.shutdown)
        assert supervisor._stop.wait(5)
    finally:
        release.set()
        outcome = _join(transition)
        stopped = _join(stopping) if stopping is not None else None
    assert outcome["error"] is None or isinstance(outcome["error"], RuntimeError)
    assert stopped is not None and stopped["error"] is None
    assert supervisor.launches == initial
    assert len(children.processes) == 2
    assert all(process.poll() is not None for process in children.processes)


def test_explicit_paused_transition_preserves_pause_without_spawning(make_supervisor):
    initial = (_automatic("first", 0), _automatic("second", 1, device=1))
    replacements = (_automatic("first", 1), _automatic("second", 0, device=1))
    supervisor, children = make_supervisor(initial)
    supervisor.start_service()
    supervisor.pause_worker("first")
    assert supervisor.replace_launches(replacements, start=False)
    assert len(children.processes) == 2
    assert all(process.poll() is not None for process in children.processes)
    assert supervisor.snapshot("first")["operator_paused"]
    assert all(not row["desired_running"] and row["pid"] is None for row in supervisor.snapshots())


def test_joint_transition_requires_containment_opt_in(make_supervisor):
    launch = _automatic("first", 0)
    supervisor, children = make_supervisor([launch], coordinated_launches=False)
    with pytest.raises(WorkerReconfigurationBusyError):
        supervisor.replace_launches([_automatic("first", 1)], start=True)
    assert supervisor.launches == (launch,)
    assert not children.processes


@pytest.mark.parametrize("start", [0, 1, "yes"])
def test_start_intent_requires_boolean_or_none_before_process_changes(make_supervisor, start):
    launch = _automatic("first", 0)
    supervisor, children = make_supervisor([launch])
    supervisor.start_service()
    with pytest.raises((ValueError, TypeError)):
        supervisor.replace_launches([_automatic("first", 1)], start=start)
    assert supervisor.launches == (launch,)
    assert len(children.processes) == 1 and children.processes[0].poll() is None


def test_contained_natural_exit_does_not_restart_when_auto_restart_is_disabled(make_supervisor):
    launch = replace(_automatic("first", 0), auto_restart=False)
    supervisor, children = make_supervisor([launch])
    children.program = "raise SystemExit(3)"
    supervisor.start_service()

    def cleaned_exit():
        status = supervisor.snapshot("first")
        return status["last_exit_code"] == 3 and status["pid"] is None

    _wait_for(cleaned_exit)
    threading.Event().wait(0.15)
    assert len(children.processes) == 1
    assert supervisor._monitor.is_alive()
    assert supervisor.snapshot("first")["state"] == "crashed"


def test_contained_natural_exit_obeys_restart_backoff(make_supervisor):
    launch = replace(_automatic("first", 0), restart_backoff=0.5)
    supervisor, children = make_supervisor([launch])
    children.program = "raise SystemExit(3)"
    supervisor.start_service()

    def cleaned_exit():
        status = supervisor.snapshot("first")
        return status["last_exit_code"] == 3 and status["pid"] is None

    _wait_for(cleaned_exit)
    observed_exit = time.monotonic()
    threading.Event().wait(0.15)
    assert len(children.processes) == 1
    _wait_for(lambda: len(children.processes) >= 2)
    assert children.spawned_at[1] - observed_exit >= 0.3
    assert supervisor._monitor.is_alive()


def _parent_that_spawns_child(pid_file):
    child = (
        "import os,time; from pathlib import Path; "
        f"Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    return "import subprocess,sys; " f"subprocess.Popen([sys.executable,'-c',{child!r}]); raise SystemExit(17)"


def _read_child_pid(pid_file):
    def ready():
        return pid_file.exists() and pid_file.read_text().isdigit()

    _wait_for(ready)
    return int(pid_file.read_text())


def _pid_running(pid):
    try:
        return psutil.Process(pid).is_running() and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def test_parent_exit_does_not_release_live_descendant_before_joint_cleanup(make_supervisor, tmp_path):
    launch = _automatic("first", 0)
    supervisor, children = make_supervisor([launch])
    pid_file = tmp_path / "grandchild.pid"
    children.program = _parent_that_spawns_child(pid_file)
    supervisor.start_worker("first")
    parent = children.processes[0]
    grandchild = _read_child_pid(pid_file)
    assert parent.wait(timeout=5) == 17
    assert _pid_running(grandchild), "fixture must retain a child after its parent exits"
    replacement = _automatic("first", 1)
    assert supervisor.replace_launches([replacement], start=False)
    _wait_for(lambda: not _pid_running(grandchild))
    assert supervisor.launches == (replacement,)
    assert supervisor.snapshot("first")["pid"] is None
    assert not supervisor._process_containments


def test_immediate_start_after_parent_exit_waits_for_descendant_cleanup(make_supervisor, monkeypatch, tmp_path):
    launch = replace(_automatic("first", 0, reservation=75), auto_restart=False)
    supervisor, children = make_supervisor([launch, _manual(device=0)])
    pid_file = tmp_path / "grandchild.pid"
    children.program = _parent_that_spawns_child(pid_file)
    supervisor.start_worker("first")
    parent = children.processes[0]
    grandchild = _read_child_pid(pid_file)
    assert parent.wait(timeout=5) == 17
    entered, release = threading.Event(), threading.Event()
    original_cleanup = supervisor._terminate_launch_tree

    def blocked_cleanup(process):
        entered.set()
        assert release.wait(5), "test did not release natural-exit cleanup"
        return original_cleanup(process)

    children.program = "import time; time.sleep(60)"

    def old_tree_absent_before_spawn(command):
        assert parent.poll() is not None
        assert not _pid_running(grandchild), "explicit Start cannot outlive the cleanup barrier"

    children.before_spawn = old_tree_absent_before_spawn
    monkeypatch.setattr(supervisor, "_terminate_launch_tree", blocked_cleanup)
    try:
        assert supervisor.start_worker("first") is False
        assert entered.wait(5)
        assert len(children.processes) == 1
        assert supervisor.snapshot("first")["pid"] == parent.pid
        assert supervisor.snapshot("first")["cleanup_pending"]
        assert not supervisor.snapshot("spectator")["resource_admitted"]
        assert _pid_running(grandchild)
    finally:
        release.set()
        _wait_for(lambda: len(children.processes) == 2)
    _wait_for(lambda: not _pid_running(grandchild))
    assert len(children.processes) == 2
    status = supervisor.snapshot("first")
    assert status["pid"] == children.processes[1].pid and status["desired_running"]
    assert not status["auto_restart"]


def test_later_pause_cancels_explicit_start_waiting_for_descendant_cleanup(make_supervisor, monkeypatch, tmp_path):
    launch = replace(_automatic("first", 0), auto_restart=False)
    supervisor, children = make_supervisor([launch])
    pid_file = tmp_path / "grandchild.pid"
    children.program = _parent_that_spawns_child(pid_file)
    supervisor.start_worker("first")
    parent = children.processes[0]
    grandchild = _read_child_pid(pid_file)
    assert parent.wait(timeout=5) == 17
    entered, release = threading.Event(), threading.Event()
    original_cleanup = supervisor._terminate_launch_tree

    def blocked_cleanup(process):
        entered.set()
        assert release.wait(5), "test did not release pending Start cleanup"
        return original_cleanup(process)

    children.program = "import time; time.sleep(60)"
    monkeypatch.setattr(supervisor, "_terminate_launch_tree", blocked_cleanup)
    pause = None
    try:
        assert supervisor.start_worker("first") is False
        assert entered.wait(5)
        pause = _background(lambda: supervisor.pause_worker("first"))
        _wait_for(lambda: supervisor.snapshot("first")["operator_paused"])
    finally:
        release.set()
        outcome = _join(pause) if pause is not None else None
        _wait_for(lambda: supervisor.snapshot("first")["pid"] is None)
    assert outcome is not None and outcome["error"] is None
    assert not _pid_running(grandchild)
    assert len(children.processes) == 1
    status = supervisor.snapshot("first")
    assert status["operator_paused"] and not status["desired_running"]


def test_sibling_start_after_parent_exit_cannot_spend_its_unreleased_reservation(make_supervisor, tmp_path):
    launch = _automatic("first", 0, reservation=75)
    supervisor, children = make_supervisor([launch, _manual(device=0)])
    pid_file = tmp_path / "grandchild.pid"
    children.program = _parent_that_spawns_child(pid_file)
    supervisor.start_worker("first")
    parent = children.processes[0]
    grandchild = _read_child_pid(pid_file)
    assert parent.wait(timeout=5) == 17
    # Do not read a snapshot or start the monitor first: sibling admission must
    # preserve the old claim even before any refresh notices its dead parent.
    with pytest.raises(WorkerPolicyError, match="VRAM"):
        supervisor.start_worker("spectator")
    assert len(children.processes) == 1 and _pid_running(grandchild)
    assert supervisor.replace_launches([_automatic("first", 1, reservation=40)], start=False)
    _wait_for(lambda: not _pid_running(grandchild))
    children.program = "import time; time.sleep(60)"
    assert supervisor.start_worker("spectator")
    assert len(children.processes) == 2


def test_replacement_after_natural_cleanup_failure_still_resumes_after_schedule_pause(make_supervisor, monkeypatch):
    schedule = {"allowed": True}
    launch = _automatic("first", 0)
    supervisor, children = make_supervisor([launch], schedule_allowed=lambda: schedule["allowed"])
    children.program = "raise SystemExit(3)"
    original_cleanup = supervisor._terminate_launch_tree

    def failed_cleanup(process):
        raise OSError("controlled natural-exit cleanup failure")

    monkeypatch.setattr(supervisor, "_terminate_launch_tree", failed_cleanup)
    supervisor.start_service()

    def cleanup_failed():
        status = supervisor.snapshot("first")
        return status["cleanup_pending"] and status["state"] == "crashed"

    _wait_for(cleanup_failed)
    children.program = "import time; time.sleep(60)"
    monkeypatch.setattr(supervisor, "_terminate_launch_tree", original_cleanup)
    replacement = replace(_automatic("first", 1), auto_restart=False)
    assert supervisor.replace_launches([replacement], start=True)
    assert len(children.processes) == 2
    assert supervisor.snapshot("first")["state"] == "running"
    schedule["allowed"] = False

    def schedule_paused():
        status = supervisor.snapshot("first")
        return status["pid"] is None and status["schedule_suspended"]

    _wait_for(schedule_paused)
    schedule["allowed"] = True
    _wait_for(lambda: len(children.processes) == 3)
    status = supervisor.snapshot("first")
    assert status["state"] == "running" and status["desired_running"]
    assert not status["operator_paused"]


def test_operator_pause_started_before_batch_has_one_cleanup_owner(make_supervisor, monkeypatch):
    initial = (_automatic("first", 0), _automatic("second", 1, device=1))
    replacements = (_automatic("first", 1), _automatic("second", 0, device=1))
    supervisor, children = make_supervisor(initial)
    supervisor.start_service()
    old_first, old_second = tuple(children.processes)
    entered, release = threading.Event(), threading.Event()
    calls = []
    original_cleanup = supervisor._terminate_launch_tree

    def blocked_first(process):
        calls.append(process.pid)
        if process is old_first:
            entered.set()
            assert release.wait(5), "test did not release ordinary pause cleanup"
        return original_cleanup(process)

    monkeypatch.setattr(supervisor, "_terminate_launch_tree", blocked_first)
    pause = _background(lambda: supervisor.pause_worker("first"))
    transition = None
    try:
        assert entered.wait(5)
        transition = _background(lambda: supervisor.replace_launches(replacements, start=True))
        _wait_for(lambda: supervisor.launch_transition_status["state"] == "stopping")
        assert calls.count(old_first.pid) == 1
    finally:
        release.set()
        paused = _join(pause)
        outcome = _join(transition) if transition is not None else None
    assert paused["error"] is None
    assert outcome is not None and outcome["error"] is None
    assert calls.count(old_first.pid) == calls.count(old_second.pid) == 1
    assert supervisor.snapshot("first")["operator_paused"]
    assert supervisor.snapshot("first")["pid"] is None
    assert supervisor.snapshot("second")["pid"] is not None
    assert len(children.processes) == 3


def test_stale_stop_thread_timeout_preserves_verified_cleanup_and_allows_same_set_retry(make_supervisor, monkeypatch):
    schedule = {"allowed": True}
    initial = (_automatic("first", 0), _automatic("second", 1, device=1))
    replacements = (_automatic("first", 1), _automatic("second", 0, device=1))
    supervisor, children = make_supervisor(initial, schedule_allowed=lambda: schedule["allowed"])
    supervisor.start_service()
    old_first, old_second = tuple(children.processes)
    entered, release = threading.Event(), threading.Event()
    original_cleanup = supervisor._terminate_launch_tree
    cleanup_calls = []

    def blocked_first(process):
        cleanup_calls.append(process.pid)
        if process is old_first:
            entered.set()
            assert release.wait(5), "test did not release preexisting policy cleanup"
        return original_cleanup(process)

    monkeypatch.setattr(supervisor, "_terminate_launch_tree", blocked_first)
    schedule["allowed"] = False
    try:
        assert entered.wait(5)
        with supervisor._lock:
            stop_thread = supervisor._records["first"].suspension_stop_thread
        assert stop_thread is not None
        real_join, real_is_alive = stop_thread.join, stop_thread.is_alive
        observations = []

        def complete_cleanup_before_timeout_check(timeout=None):
            release.set()
            real_join(timeout=5)
            assert not real_is_alive(), "real policy cleanup did not finish"

        def stale_is_alive():
            # Model is_alive() returning an observation taken just before the
            # cleanup thread completed. The exception handler must recheck the
            # record under its lock instead of resurrecting cleanup_pending.
            observations.append(True)
            return True

        monkeypatch.setattr(stop_thread, "join", complete_cleanup_before_timeout_check)
        monkeypatch.setattr(stop_thread, "is_alive", stale_is_alive)
        with pytest.raises(RuntimeError, match="transition cleanup is incomplete"):
            supervisor.replace_launches(replacements, start=True)
        assert observations == [True]
    finally:
        release.set()

    assert supervisor.launch_transition_status["state"] == "cleanup_failed"
    assert supervisor.launches == initial
    assert old_first.poll() is not None and old_second.poll() is not None
    first_status = supervisor.snapshot("first")
    assert first_status["pid"] is None and not first_status["cleanup_pending"]
    assert first_status["state"] == "paused" and first_status["last_error"] is None
    assert not supervisor._process_containments
    assert len(children.processes) == 2

    schedule["allowed"] = True
    assert supervisor.replace_launches(replacements, start=True)
    assert supervisor.launch_transition_status["state"] == "idle"
    assert supervisor.launches == replacements
    assert len(children.processes) == 4
    assert cleanup_calls.count(old_first.pid) == cleanup_calls.count(old_second.pid) == 1
    assert all(supervisor.snapshot(worker)["state"] == "running" for worker in ("first", "second"))


def test_shutdown_retries_cleanup_failure_from_inflight_batch(make_supervisor, monkeypatch):
    initial = _automatic("first", 0)
    supervisor, children = make_supervisor([initial])
    supervisor.start_service()
    parent = children.processes[0]
    entered, release = threading.Event(), threading.Event()
    calls = []
    original_cleanup = supervisor._terminate_launch_tree

    def fail_first_cleanup(process):
        calls.append(process.pid)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5), "test did not release failing batch cleanup"
            raise OSError("controlled first cleanup failure")
        return original_cleanup(process)

    monkeypatch.setattr(supervisor, "_terminate_launch_tree", fail_first_cleanup)
    transition = _background(lambda: supervisor.replace_launches([_automatic("first", 1)], start=True))
    stopping = None
    try:
        assert entered.wait(5)
        stopping = _background(supervisor.shutdown)
        assert supervisor._stop.wait(5)
    finally:
        release.set()
        outcome = _join(transition)
        stopped = _join(stopping) if stopping is not None else None
    assert isinstance(outcome["error"], RuntimeError)
    assert stopped is not None and stopped["error"] is None
    assert supervisor.launches == (initial,)
    assert calls.count(parent.pid) == 2
    assert len(children.processes) == 1 and parent.poll() is not None
    assert not supervisor._process_containments


def test_failed_containment_query_after_parent_exit_retains_reservation_until_retry(
    make_supervisor, monkeypatch, tmp_path
):
    launch = _automatic("first", 0, reservation=75)
    spectator = _manual(device=0)
    supervisor, children = make_supervisor([launch, spectator])
    pid_file = tmp_path / "grandchild.pid"
    children.program = _parent_that_spawns_child(pid_file)
    supervisor.start_worker("first")
    parent = children.processes[0]
    grandchild = _read_child_pid(pid_file)
    assert parent.wait(timeout=5) == 17
    containment = supervisor._process_containments[id(parent)]
    query = containment.has_members

    def failed_query():
        raise OSError("controlled containment query failure")

    monkeypatch.setattr(containment, "has_members", failed_query)
    replacement = _automatic("first", 1, reservation=40)
    with pytest.raises(RuntimeError):
        supervisor.replace_launches([replacement], start=False)
    assert supervisor.launches == (launch, spectator)
    assert supervisor.launch_transition_status["state"] == "cleanup_failed"
    assert supervisor.snapshot("first")["max_vram_bytes"] == 75
    assert not supervisor.snapshot("spectator")["resource_admitted"]
    assert _pid_running(grandchild)
    monkeypatch.setattr(containment, "has_members", query)
    assert supervisor.replace_launches([replacement], start=False)
    _wait_for(lambda: not _pid_running(grandchild))
    assert supervisor.snapshot("spectator")["resource_admitted"]
    assert not supervisor._process_containments


@pytest.mark.parametrize("failure_stage", ["attach", "resume"])
def test_containment_setup_failure_reaps_suspended_child_without_retry_or_public_details(
    make_supervisor, monkeypatch, failure_stage
):
    from drift.node import edge_supervisor

    create_containment = edge_supervisor._new_containment
    closed = []

    def broken_containment():
        containment = create_containment()
        close = containment.close

        def fail(process):
            raise OSError("private fixture device GPU-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

        def close_and_record():
            closed.append(True)
            close()

        monkeypatch.setattr(containment, failure_stage, fail)
        monkeypatch.setattr(containment, "close", close_and_record)
        return containment

    monkeypatch.setattr(edge_supervisor, "_new_containment", broken_containment)
    launch = replace(_automatic("first", 0), auto_restart=False)
    supervisor, children = make_supervisor([launch])
    supervisor.start_service()
    _wait_for(lambda: children.processes and children.processes[0].poll() is not None)
    status = supervisor.snapshot("first")
    assert status["state"] == "crashed" and status["pid"] is None
    assert "private fixture" not in status["last_error"] and "GPU-" not in status["last_error"]
    assert closed and not supervisor._process_containments
    threading.Event().wait(0.05)
    assert len(children.processes) == 1


@pytest.mark.parametrize("cleanup_path", ["pause", "shutdown"])
def test_cleanup_timeout_does_not_publish_private_process_arguments(make_supervisor, monkeypatch, caplog, cleanup_path):
    launch = replace(_automatic("first", 0), auto_restart=False)
    supervisor, children = make_supervisor([launch])
    supervisor.start_service()
    original_cleanup = supervisor._terminate_launch_tree
    caplog.set_level("ERROR")

    def private_timeout(process):
        raise subprocess.TimeoutExpired(["node", "--token", "private-test-value"], 1)

    monkeypatch.setattr(supervisor, "_terminate_launch_tree", private_timeout)
    try:
        error_message = ""
        if cleanup_path == "pause":
            with pytest.raises(RuntimeError) as stopped:
                supervisor.pause_worker("first")
            error_message = str(stopped.value)
        else:
            supervisor.shutdown()
        status = supervisor.snapshot("first")
        public_output = "\n".join((status["last_error"] or "", error_message, caplog.text))
        assert "private-test-value" not in public_output
        assert "--token" not in public_output
        assert status["cleanup_pending"] and status["pid"] == children.processes[0].pid
        assert children.processes[0].poll() is None
    finally:
        monkeypatch.setattr(supervisor, "_terminate_launch_tree", original_cleanup)
        supervisor.shutdown()
    assert children.processes[0].poll() is not None
