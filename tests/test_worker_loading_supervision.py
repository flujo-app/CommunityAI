"""Generation-bound acknowledgements with real contained children and I/O barriers."""

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_worker_async_resources import quick
from test_worker_resource_lifecycle import launch, wait_for

from drift.node import worker_loading, worker_supervisor
from drift.node.worker_loading_identity import resolve_loading_worker_pid
from drift.node.worker_supervisor import WorkerSupervisor, WorkerSupervisorSettings


@pytest.fixture
def harness(tmp_path):
    fixtures = []

    def make(*, asynchronous=False, item=None, binding_hook=True):
        h = SimpleNamespace(children=[], held={}, bindings={}, environments=[], barriers=[], sequence=0)

        def acquire(item, cancel=None):
            h.sequence += 1
            token = f"private-generation-{h.sequence}"
            h.bindings[token] = worker_loading.create_loading_binding(
                tmp_path / f"loading-{len(fixtures)}", token, "sha256:" + "b" * 64
            )
            h.held[token] = item
            return token

        def release(token):
            assert all(child.poll() is not None for child in h.children)
            h.held.pop(token)

        def popen(command, **kwargs):
            assert h.held
            h.environments.append(kwargs["env"].copy())
            process = subprocess.Popen(command, **kwargs)
            h.children.append(process)
            return process

        options = {"acquire_resources_cancellable" if asynchronous else "acquire_resources": acquire}
        if binding_hook:
            options["loading_binding_for_token"] = h.bindings.__getitem__
        h.supervisor = WorkerSupervisor(
            (item or launch(auto_restart=True),),
            release_resources=release,
            popen=popen,
            poll_period=0.01,
            stop_timeout=0.5,
            **options,
        )
        fixtures.append(h)
        return h

    yield make
    for h in fixtures:
        for barrier in h.barriers:
            barrier.set()
        h.supervisor.shutdown()
        wait_for(lambda: h.supervisor._record("worker").resource_thread is None)
        wait_for(lambda: h.supervisor._record("worker").loading_thread is None)
        h.supervisor.shutdown()
        for process in h.children:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)


def start(h):
    h.supervisor.start_worker("worker")
    wait_for(lambda: bool(h.children))
    process = h.children[-1]
    identities = []

    def identified():
        identity = resolve_loading_worker_pid(process, h.supervisor.launches[0].command)
        if identity is not None:
            identities.append(identity)
        return identity is not None

    wait_for(identified)
    token = h.supervisor._record("worker").resource_token
    return h.bindings[token], identities[-1].pid


def publish(binding, expected_pid, state, **overrides):
    owner = {**binding._payload(), "pid": expected_pid}
    if not binding._path("owner").exists():
        worker_loading._exclusive(binding._path("owner"), owner)
    worker_loading._replace(binding._path("status"), {**owner, "state": state, **overrides})


@pytest.mark.parametrize("asynchronous", [False, True])
def test_running_is_not_ready_and_ready_retains_reservation(harness, asynchronous):
    h = harness(asynchronous=asynchronous)
    binding, pid = start(h)
    s = h.supervisor
    assert s.snapshot("worker")["state"] == "running"
    assert s.snapshot("worker")["load_state"] == "waiting"
    assert not s.snapshot("worker")["model_ready"]
    publish(binding, pid, "loading")
    wait_for(lambda: s.snapshot("worker")["load_state"] == "loading")
    publish(binding, pid, "ready")
    wait_for(lambda: s.snapshot("worker")["model_ready"])
    assert binding.token in h.held
    assert len(h.children) == 1
    s.pause_worker("worker")
    assert not s.snapshot("worker")["model_ready"]
    wait_for(lambda: not h.held)


@pytest.mark.parametrize("bad", ["pid", "nonce", "digest", "malformed", "failed"])
def test_invalid_or_failed_ack_stops_contained_child_without_restart(harness, bad):
    h = harness(asynchronous=True)
    binding, pid = start(h)
    s = h.supervisor
    s.start_service()
    if bad == "malformed":
        binding._path("status").write_text("not json")
    else:
        changes = {
            "pid": {"pid": pid + 1000000},
            "nonce": {"nonce": "c" * 32},
            "digest": {"binding_digest": "sha256:" + "c" * 64},
        }.get(bad, {})
        publish(binding, pid, "failed" if bad == "failed" else "ready", **changes)
    wait_for(lambda: s.snapshot("worker")["load_state"] == "failed")
    wait_for(lambda: not h.held)
    assert h.children[0].poll() is not None
    assert not s.snapshot("worker")["desired_running"]
    assert not s.snapshot("worker")["model_ready"]
    assert s.snapshot("worker")["resource_reason"] == worker_supervisor._LOADING_FAILED
    time.sleep(0.05)  # Several actual monitor cycles with auto_restart enabled.
    assert len(h.children) == 1
    s.start_worker("worker")
    wait_for(lambda: len(h.children) == 2)
    assert s.snapshot("worker")["load_state"] == "waiting"
    assert h.environments[0]["DRIFT_INTERNAL_LOADING_TOKEN"] != h.environments[1]["DRIFT_INTERNAL_LOADING_TOKEN"]


@pytest.mark.parametrize("action", ["pause", "shutdown", "replace", "policy"])
def test_blocked_read_never_blocks_controls_or_marks_cancelled_generation_ready(harness, monkeypatch, action):
    entered, unblock = threading.Event(), threading.Event()

    def blocked(binding, *, expected_pid):
        entered.set()
        assert unblock.wait(8)
        return "ready"

    monkeypatch.setattr(worker_supervisor, "read_loading_status", blocked)
    h = harness()
    h.barriers.append(unblock)
    start(h)
    s = h.supervisor
    assert entered.wait(3)
    assert not quick(lambda: s.snapshot("worker"))["model_ready"]
    if action == "pause":
        quick(lambda: s.pause_worker("worker"))
    elif action == "shutdown":
        quick(s.shutdown)
    elif action == "policy":
        quick(lambda: s.pause_worker("worker"))
        persisted = []
        quick(lambda: s.reconfigure(WorkerSupervisorSettings(s.launches, 0.5), persist=lambda: persisted.append(True)))
        assert persisted == [True]
    else:
        quick(lambda: s.pause_worker("worker"))
        quick(lambda: s.replace_launch(replace(s.launches[0], model_id="replacement", auto_start=False)))
    assert not s.snapshot("worker")["model_ready"]
    unblock.set()
    wait_for(lambda: s._record("worker").loading_thread is None)
    assert not s.snapshot("worker")["model_ready"]
    assert not h.held


@pytest.mark.parametrize("stale_status", ["ready", "failed"])
def test_late_old_read_cannot_mark_new_generation_ready_and_runner_is_bounded(harness, monkeypatch, stale_status):
    entered, unblock = threading.Event(), threading.Event()
    calls = []

    def blocked(binding, *, expected_pid):
        calls.append(binding.token)
        if len(calls) == 1:
            entered.set()
            assert unblock.wait(8)
            return stale_status
        return None

    monkeypatch.setattr(worker_supervisor, "read_loading_status", blocked)
    h = harness()
    h.barriers.append(unblock)
    start(h)
    s = h.supervisor
    assert entered.wait(3)
    runner = s._record("worker").loading_thread
    for _ in range(3):
        quick(lambda: s.pause_worker("worker"))
        quick(lambda: s.start_worker("worker"))
        assert s._record("worker").loading_thread is runner
        assert not s.snapshot("worker")["model_ready"]
    unblock.set()
    wait_for(lambda: len(calls) >= 2)
    assert calls[0] != calls[-1]
    assert s.snapshot("worker")["load_state"] == "waiting"
    assert not s.snapshot("worker")["model_ready"]
    assert len(h.children) == 4


@pytest.mark.parametrize("next_state", [None, "loading", "waiting"])
def test_ready_evidence_cannot_disappear_or_regress(harness, next_state):
    h = harness()
    binding, pid = start(h)
    publish(binding, pid, "ready")
    wait_for(lambda: h.supervisor.snapshot("worker")["model_ready"])
    if next_state is None:
        binding._path("status").unlink()
    else:
        publish(binding, pid, next_state)
    wait_for(lambda: h.supervisor.snapshot("worker")["load_state"] == "failed")
    wait_for(lambda: not h.held)


def test_failed_exit_before_status_read_is_latched_and_explicit_start_retries(harness, monkeypatch):
    entered, unblock = threading.Event(), threading.Event()

    def blocked(binding, *, expected_pid):
        entered.set()
        assert unblock.wait(8)
        return None

    monkeypatch.setattr(worker_supervisor, "read_loading_status", blocked)
    h = harness()
    h.barriers.append(unblock)
    start(h)
    s = h.supervisor
    assert entered.wait(3)
    # Popen.kill cannot choose an exit code; a contained real child watches a file.
    s.pause_worker("worker")
    signal_path = str(h.bindings[next(iter(h.bindings))]._path("exit"))
    command = (
        sys.executable,
        "-c",
        "import os,time,sys; p=sys.argv[1]; " "exec('while not os.path.exists(p): time.sleep(.01)'); sys.exit(74)",
        signal_path,
    )
    s.replace_launch(replace(s.launches[0], command=command, auto_start=False))
    s.start_worker("worker")
    s.start_service()
    with open(signal_path, "w") as stream:
        stream.write("exit")
    wait_for(lambda: s.snapshot("worker")["load_state"] == "failed")
    wait_for(lambda: not h.held)
    assert s.snapshot("worker")["desired_running"] is False
    count = len(h.children)
    time.sleep(0.05)
    assert len(h.children) == count
    os.unlink(signal_path)
    s.start_worker("worker")
    assert len(h.children) == count + 1
    assert s.snapshot("worker")["load_state"] == "waiting"


def test_inherited_protocol_is_scrubbed_and_private_binding_redacted(harness, monkeypatch):
    monkeypatch.setenv("DRIFT_INTERNAL_LOADING_UNKNOWN", "stale-private-value")
    monkeypatch.setenv("drift_internal_loading_nonce", "stale-private-nonce")
    item = replace(
        launch(),
        command=(
            sys.executable,
            "-c",
            "import os,time; print(os.environ['DRIFT_INTERNAL_LOADING_TOKEN'],flush=True); time.sleep(60)",
        ),
        environment=(("DRIFT_INTERNAL_LOADING_DIR", "stale-private-directory"),),
    )
    h = harness(item=item)
    start(h)
    environment = h.environments[0]
    assert "DRIFT_INTERNAL_LOADING_UNKNOWN" not in environment
    assert environment["DRIFT_INTERNAL_LOADING_DIR"] != "stale-private-directory"
    assert environment["DRIFT_INTERNAL_LOADING_NONCE"] != "stale-private-nonce"
    wait_for(lambda: bool(h.supervisor.snapshot("worker")["recent_logs"]))
    assert "private-generation" not in json.dumps(h.supervisor.snapshot("worker"))
    assert "[private loading binding]" in h.supervisor.snapshot("worker")["recent_logs"][-1]


def test_missing_binding_fails_before_popen_and_does_not_leak_error(harness):
    h = harness()

    def missing(token):
        raise OSError("private path and nonce")

    h.supervisor._loading_binding_for_token = missing
    assert not h.supervisor.start_worker("worker")
    assert not h.children and not h.held
    snapshot = h.supervisor.snapshot("worker")
    assert snapshot["load_state"] == "failed" and not snapshot["model_ready"]
    assert "private path" not in json.dumps(snapshot)


def test_legacy_without_hook_remains_running_without_claiming_readiness(harness):
    h = harness(binding_hook=False)
    start(h)
    snapshot = h.supervisor.snapshot("worker")
    assert snapshot["state"] == "running"
    assert snapshot["load_state"] is None and not snapshot["model_ready"]
    assert h.supervisor._record("worker").loading_thread is None


def test_identity_change_after_status_read_fails_closed(harness, monkeypatch):
    real_resolve = resolve_loading_worker_pid
    calls = []

    def replaced(process, command, *, expected_identity=None):
        identity = real_resolve(process, command, expected_identity=expected_identity)
        if identity is not None:
            calls.append(identity)
            if len(calls) > 1:
                return replace(identity, creation_time=identity.creation_time + 1)
        return identity

    monkeypatch.setattr(worker_supervisor, "resolve_loading_worker_pid", replaced)
    monkeypatch.setattr(worker_supervisor, "read_loading_status", lambda *args, **kwargs: "ready")
    h = harness()
    h.supervisor.start_worker("worker")
    wait_for(lambda: h.supervisor.snapshot("worker")["load_state"] == "failed")
    wait_for(lambda: not h.held)
    assert not h.supervisor.snapshot("worker")["model_ready"]


@pytest.mark.parametrize("runner", ["log", "loading"])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_observer_thread_start_failure_cleans_child_and_requires_explicit_retry(
    harness, monkeypatch, runner, asynchronous
):
    original = threading.Thread.start

    def fail_observer(thread):
        if thread.name.startswith(f"drift-worker-{runner}-"):
            raise OSError("private operating system detail")
        return original(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_observer)
    h = harness(asynchronous=asynchronous)
    h.supervisor.start_worker("worker")
    wait_for(lambda: bool(h.children))
    wait_for(lambda: not h.held)
    snapshot = h.supervisor.snapshot("worker")
    assert snapshot["load_state"] == "failed"
    assert not snapshot["desired_running"] and not snapshot["model_ready"]
    assert h.supervisor._record("worker").loading_thread is None
    assert h.children[0].poll() is not None
    assert "private operating" not in json.dumps(snapshot)


def test_loading_failure_keeps_reservation_when_containment_cleanup_is_uncertain(harness, monkeypatch):
    h = harness()
    binding, pid = start(h)
    s = h.supervisor
    original = s._terminate_launch_tree

    def incomplete(process):
        raise OSError("private containment failure")

    monkeypatch.setattr(s, "_terminate_launch_tree", incomplete)
    publish(binding, pid, "failed")
    wait_for(lambda: s.snapshot("worker")["load_state"] == "failed")
    wait_for(lambda: s._record("worker").suspension_stop_thread is None)
    assert s.snapshot("worker")["cleanup_pending"]
    assert binding.token in h.held and h.children[0].poll() is None
    assert not s.snapshot("worker")["model_ready"]
    monkeypatch.setattr(s, "_terminate_launch_tree", original)
    s.pause_worker("worker")
    assert not h.held


def test_current_lease_gate_can_withhold_previously_acknowledged_readiness(harness):
    allowed = [True]
    h = harness(item=launch(placement_available=lambda: allowed[0]))
    binding, pid = start(h)
    publish(binding, pid, "ready")
    wait_for(lambda: h.supervisor.snapshot("worker")["model_ready"])
    allowed[0] = False
    snapshot = h.supervisor.snapshot("worker")
    assert snapshot["state"] == "running" and snapshot["load_state"] == "ready"
    assert not snapshot["model_ready"]


@pytest.mark.parametrize("first", ["acknowledgement", "exit"])
@pytest.mark.parametrize("pause", [False, True])
def test_memory_rejection_preserves_budget_guidance_in_both_observation_orders(
    harness, monkeypatch, tmp_path, first, pause
):
    entered, unblock = threading.Event(), threading.Event()
    original_read = worker_loading.read_loading_status

    def held_read(binding, *, expected_pid):
        entered.set()
        assert unblock.wait(8)
        return original_read(binding, expected_pid=expected_pid)

    signal_path = str(tmp_path / "exit78")
    command = (
        sys.executable,
        "-c",
        "import os,time,sys; p=sys.argv[1]; " "exec('while not os.path.exists(p): time.sleep(.01)'); sys.exit(78)",
        signal_path,
    )
    if first == "exit":
        monkeypatch.setattr(worker_supervisor, "read_loading_status", held_read)
    h = harness(item=replace(launch(auto_restart=True), command=command))
    h.barriers.append(unblock)
    binding, pid = start(h)
    s = h.supervisor
    s.start_service()
    if first == "exit":
        assert entered.wait(3)
    publish(binding, pid, "memory_rejected")
    if first == "exit":
        with open(signal_path, "w") as stream:
            stream.write("exit")
        wait_for(lambda: h.children[0].poll() == 78)
    wait_for(lambda: not h.held)
    if first == "acknowledgement":
        assert h.children[0].poll() != 78  # Observer stopped the still-live child.
    if pause:
        s.pause_worker("worker")
    snapshot = s.snapshot("worker")
    assert (
        snapshot["resource_reason"]
        == "selected blocks exceed the VRAM budget; increase VRAM or contribute fewer blocks"
    )
    assert snapshot["load_state"] is None and not snapshot["model_ready"]
    assert snapshot["desired_running"] is not pause
    assert not s._record("worker").loading_failed
    unblock.set()
    wait_for(lambda: s._record("worker").loading_thread is None)
    assert len(h.children) == 1


@pytest.mark.parametrize("recovery", ["pause", "shutdown"])
@pytest.mark.parametrize("stop_failure", ["construct", "start"])
def test_log_and_cleanup_runner_exhaustion_retains_child_and_allows_later_cleanup(
    harness, monkeypatch, recovery, stop_failure
):
    thread_class, original_start = threading.Thread, threading.Thread.start

    def exhausted_start(thread):
        if thread.name.startswith("drift-worker-log-") or (
            stop_failure == "start" and thread.name.startswith("drift-worker-policy-stop-")
        ):
            raise OSError("private thread exhaustion detail")
        return original_start(thread)

    def exhausted_factory(*args, **kwargs):
        if stop_failure == "construct" and kwargs.get("name", "").startswith("drift-worker-policy-stop-"):
            raise OSError("private thread construction detail")
        return thread_class(*args, **kwargs)

    h = harness()
    s = h.supervisor
    with monkeypatch.context() as patch:
        patch.setattr(thread_class, "start", exhausted_start)
        patch.setattr(threading, "Thread", exhausted_factory)
        assert s.start_worker("worker") is False
        snapshot = s.snapshot("worker")
        assert h.children[0].poll() is None and h.held
        assert snapshot["cleanup_pending"] and snapshot["load_state"] == "failed"
        assert not snapshot["model_ready"] and not snapshot["desired_running"]
        assert s._record("worker").suspension_stop_thread is None
        assert "private thread" not in json.dumps(snapshot)
    if recovery == "pause":
        s.pause_worker("worker")
    else:
        s.shutdown()
    assert h.children[0].poll() is not None and not h.held
    assert not s.snapshot("worker")["cleanup_pending"]
