"""Blocked birth cannot hold controls or abandon generation ownership."""

import threading
from dataclasses import replace

import pytest
from test_worker_async_resources import harness, quick
from test_worker_resource_lifecycle import wait_for

from drift.node.worker_supervisor import WorkerReconfigurationBusyError


@pytest.mark.parametrize("action", ["pause", "shutdown", "pause_then_start"])
def test_pending_birth_keeps_controls_responsive_and_cleans_before_restart(harness, action):
    h = harness(coordinated_launches=True)
    supervisor = h.supervisor
    entered, unblock = threading.Event(), threading.Event()
    original = supervisor._popen

    def blocked_spawn(*args, **kwargs):
        entered.set()
        assert unblock.wait(8), "birth barrier was not released"
        return original(*args, **kwargs)

    supervisor._popen = blocked_spawn
    try:
        quick(lambda: supervisor.start_worker("worker"))
        assert entered.wait(2)
        snapshot = quick(lambda: supervisor.snapshot("worker"))
        assert snapshot["resource_operation"] == "spawn"
        assert snapshot["pid"] is None and len(h.held) == 1
        with pytest.raises(WorkerReconfigurationBusyError):
            quick(lambda: supervisor.replace_launch(replace(supervisor.launches[0], auto_start=False)))
        quick(lambda: supervisor.pause_worker("worker"))
        assert quick(lambda: supervisor.snapshot("worker"))["resource_cancel_requested"]
        if action == "shutdown":
            quick(supervisor.shutdown)
            assert quick(lambda: supervisor.drain_resource_operations(timeout=0)) is False
        elif action == "pause_then_start":
            quick(lambda: supervisor.start_worker("worker"))
        assert h.held and not h.children
        unblock.set()
        if action == "pause_then_start":
            wait_for(lambda: supervisor.snapshot("worker")["pid"] is not None)
            assert len(h.children) == 2
            assert h.children[0].poll() is not None
            assert h.events.index(("released", "private-token-1")) < h.events.index(("acquire", 2))
            assert supervisor.snapshot("worker")["model_ready"] is False
        else:
            wait_for(lambda: not h.held)
            assert all(child.poll() is not None for child in h.children)
            assert quick(lambda: supervisor.snapshot("worker"))["pid"] is None
            if action == "shutdown":
                assert supervisor.drain_resource_operations(timeout=2)
    finally:
        unblock.set()


def test_birth_without_a_returned_handle_retains_claim_after_pause(harness):
    h = harness()
    entered, unblock = threading.Event(), threading.Event()

    def uncertain_spawn(*args, **kwargs):
        entered.set()
        assert unblock.wait(8)
        raise OSError("private native failure")

    h.supervisor._popen = uncertain_spawn
    try:
        h.supervisor.start_worker("worker")
        assert entered.wait(2)
        quick(lambda: h.supervisor.pause_worker("worker"))
        unblock.set()
        wait_for(lambda: h.supervisor.snapshot("worker")["resource_operation"] is None)
        status = h.supervisor.snapshot("worker")
        assert status["cleanup_pending"] and not status["model_ready"]
        assert h.held and not h.releasing.is_set()
        assert "private native failure" not in str(status)
        quick(h.supervisor.shutdown)
        assert not h.supervisor.drain_resource_operations(timeout=0)
    finally:
        unblock.set()


def test_log_thread_failure_keeps_spawn_owned_through_certified_cleanup(harness, monkeypatch):
    h = harness()
    original = threading.Thread.start

    def start(thread):
        if thread.name.startswith("drift-worker-log-"):
            raise RuntimeError("controlled log runner failure")
        return original(thread)

    monkeypatch.setattr(threading.Thread, "start", start)
    h.supervisor.start_worker("worker")
    wait_for(lambda: bool(h.children) and not h.held)
    wait_for(lambda: h.supervisor._record("worker").resource_thread is None)
    assert h.children[0].poll() is not None
    assert h.supervisor.snapshot("worker")["resource_operation"] is None
    assert not h.supervisor.snapshot("worker")["cleanup_pending"]
    quick(h.supervisor.shutdown)
    assert h.supervisor.drain_resource_operations(timeout=0)
