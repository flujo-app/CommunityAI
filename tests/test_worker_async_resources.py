"""Real callback barriers and contained children; no model or accelerator loads."""

import subprocess
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_worker_resource_lifecycle import launch, wait_for

from drift.node.resource_reservations import ResourceReservationError
from drift.node.worker_supervisor import (
    WorkerPolicyError,
    WorkerReconfigurationBusyError,
    WorkerSupervisor,
    WorkerSupervisorSettings,
)


def quick(call):
    """A blocked regression cannot wedge pytest's main thread or its teardown."""
    done = threading.Event()
    outcome = []

    def run():
        try:
            outcome.append((True, call()))
        except Exception as error:
            outcome.append((False, error))
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    assert done.wait(0.75), "control operation waited for blocked resource I/O"
    ok, value = outcome[0]
    if not ok:
        raise value
    return value


@pytest.fixture
def harness():
    fixtures = []

    def make(**options):
        state = SimpleNamespace(
            acquire_gate=threading.Event(),
            release_gate=threading.Event(),
            acquired=threading.Event(),
            releasing=threading.Event(),
            events=[],
            cancellations=[],
            held={},
            children=[],
            fail_acquire=None,
            fail_release=False,
            honor_cancel=False,
            lock=threading.Lock(),
        )
        state.acquire_gate.set()
        state.release_gate.set()

        def acquire(item, cancel):
            with state.lock:
                index = len(state.cancellations) + 1
                state.cancellations.append(cancel)
                state.events.append(("acquire", index))
            state.acquired.set()
            assert state.acquire_gate.wait(8), "test did not release acquisition barrier"
            if state.fail_acquire is not None:
                raise state.fail_acquire
            if state.honor_cancel and cancel.is_set():
                raise RuntimeError("cancelled before publication")
            token = f"private-token-{index}"
            state.held[token] = item
            return token

        def release(token):
            state.events.append(("release", token))
            state.releasing.set()
            assert state.release_gate.wait(8), "test did not release cleanup barrier"
            if state.fail_release:
                raise OSError("private journal path")
            state.held.pop(token)
            state.events.append(("released", token))

        def popen(command, **kwargs):
            assert state.held
            state.events.append(("popen", len(state.cancellations)))
            child = subprocess.Popen(command, **kwargs)
            state.children.append(child)
            return child

        item = options.pop("item", launch(auto_restart=False))
        items = options.pop("items", (item,))
        supervisor = WorkerSupervisor(
            items,
            acquire_resources_cancellable=acquire,
            release_resources=release,
            popen=popen,
            stop_timeout=0.25,
            poll_period=0.01,
            **options,
        )
        state.supervisor = supervisor
        fixtures.append(state)
        return state

    yield make
    for state in fixtures:
        state.fail_release = False
        state.acquire_gate.set()
        state.release_gate.set()
        state.supervisor.shutdown()
        wait_for(lambda: all(record.resource_thread is None for record in state.supervisor._records.values()))
        state.supervisor.shutdown()
        for child in state.children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)


def test_blocked_acquisition_does_not_hold_start_status_pause_or_shutdown(harness):
    h = harness()
    h.acquire_gate.clear()
    s = h.supervisor
    assert quick(lambda: s.start_worker("worker")) is False
    assert h.acquired.wait(2)
    assert quick(lambda: s.snapshot("worker"))["resource_operation"] == "acquire"
    quick(lambda: s.pause_worker("worker"))
    assert h.cancellations[0].is_set()
    assert quick(lambda: s.snapshot("worker"))["resource_cancel_requested"]
    quick(s.shutdown)
    assert s.snapshot("worker")["operator_paused"]
    assert h.children == []
    h.acquire_gate.set()
    wait_for(lambda: h.releasing.is_set() and not h.held)
    assert h.children == []


@pytest.mark.parametrize("mutation", ["single", "batch", "policy", "restart"])
def test_pending_acquisition_rejects_mutation_without_wait_or_persistence(harness, mutation):
    h = harness(coordinated_launches=True)
    h.acquire_gate.clear()
    s = h.supervisor
    s.start_worker("worker")
    assert h.acquired.wait(2)
    persisted = []
    item = replace(s.launches[0], auto_start=False)
    actions = {
        "single": lambda: s.replace_launch(item),
        "batch": lambda: s.replace_launches((item,)),
        "policy": lambda: s.reconfigure(
            WorkerSupervisorSettings((item,), 0.25), persist=lambda: persisted.append(True)
        ),
        "restart": lambda: s.commit_configuration_restart(lambda: persisted.append(True)),
    }
    with pytest.raises(WorkerReconfigurationBusyError):
        quick(actions[mutation])
    assert persisted == [] and h.children == []


def test_pause_start_cannot_reuse_cancelled_admission_or_overlap_late_release(harness):
    h = harness()
    h.acquire_gate.clear()
    h.release_gate.clear()
    s = h.supervisor
    s.start_worker("worker")
    assert h.acquired.wait(2)
    for _ in range(30):
        assert quick(lambda: s.start_worker("worker")) is False
    assert len(h.cancellations) == 1
    quick(lambda: s.pause_worker("worker"))
    quick(lambda: s.start_worker("worker"))
    h.acquire_gate.set()
    assert h.releasing.wait(2)
    assert h.children == [] and len(h.cancellations) == 1
    assert quick(lambda: s.snapshot("worker"))["resource_operation"] == "release"
    h.release_gate.set()
    wait_for(lambda: len(h.children) == 1)
    assert len(h.cancellations) == 2 and h.cancellations[0].is_set() and not h.cancellations[1].is_set()
    assert h.events.index(("released", "private-token-1")) < h.events.index(("acquire", 2))
    assert ("popen", 1) not in h.events


def test_cancelled_acquire_without_token_can_honor_new_start(harness):
    h = harness()
    h.honor_cancel = True
    h.acquire_gate.clear()
    s = h.supervisor
    s.start_worker("worker")
    assert h.acquired.wait(2)
    s.pause_worker("worker")
    s.start_worker("worker")
    h.acquire_gate.set()
    wait_for(lambda: len(h.children) == 1)
    assert len(h.cancellations) == 2
    assert ("release", "private-token-1") not in h.events


@pytest.mark.parametrize("change", ["lease", "schedule", "launch_identity"])
def test_completion_rechecks_live_gates_and_exact_launch_identity(harness, change):
    allowed = [True]
    item = launch(auto_restart=False, placement_available=lambda: allowed[0])
    h = harness(item=item, schedule_allowed=lambda: allowed[0] if change == "schedule" else True)
    h.acquire_gate.clear()
    s = h.supervisor
    s.start_worker("worker")
    assert h.acquired.wait(2)
    if change == "launch_identity":
        # Callback fields compare=False, so equality is insufficient binding.
        replacement = replace(item, placement_available=lambda: True)
        assert replacement == item and replacement is not item
        with s._lock:
            s._record("worker").launch = replacement
    else:
        allowed[0] = False
    h.acquire_gate.set()
    assert h.releasing.wait(2)
    wait_for(lambda: not h.held)
    assert h.children == []


def test_blocked_release_preserves_token_and_controls_after_verified_child_cleanup(harness):
    h = harness()
    s = h.supervisor
    s.start_worker("worker")
    wait_for(lambda: len(h.children) == 1)
    h.release_gate.clear()
    quick(lambda: s.pause_worker("worker"))
    assert h.releasing.wait(2) and h.children[0].poll() is not None
    snapshot = quick(lambda: s.snapshot("worker"))
    assert snapshot["cleanup_pending"] and snapshot["resource_operation"] == "release"
    assert "release is incomplete" in snapshot["resource_reason"]
    assert h.held
    quick(s.shutdown)
    assert h.held
    h.release_gate.set()
    wait_for(lambda: not h.held)


def test_failed_release_is_retained_and_pause_retries_same_token(harness):
    h = harness()
    h.fail_release = True
    s = h.supervisor
    s.start_worker("worker")
    wait_for(lambda: len(h.children) == 1)
    try:
        s.pause_worker("worker")
    except RuntimeError:
        pass  # Immediate completed failure and still-pending release are both legal.
    wait_for(lambda: s.snapshot("worker")["resource_operation"] is None)
    assert h.held and s.snapshot("worker")["cleanup_pending"]
    with pytest.raises(WorkerReconfigurationBusyError):
        s.replace_launch(replace(s.launches[0], auto_start=False))
    h.fail_release = False
    quick(lambda: s.pause_worker("worker"))
    wait_for(lambda: not h.held)
    assert h.events.count(("release", "private-token-1")) == 2


@pytest.mark.parametrize("capacity", [False, True])
def test_async_error_reason_is_fixed_and_no_token_is_invented(harness, capacity):
    h = harness()
    h.fail_acquire = (
        ResourceReservationError("private-path-token", category="capacity")
        if capacity
        else RuntimeError("private-path-token")
    )
    s = h.supervisor
    s.start_worker("worker")
    wait_for(lambda: s.snapshot("worker")["resource_operation"] is None)
    snapshot = s.snapshot("worker")
    assert ("shared host memory" in snapshot["resource_reason"]) == capacity
    assert "private-path-token" not in str(snapshot)
    assert h.children == [] and h.held == {} and not h.releasing.is_set()


def test_persist_master_pause_keeps_operation_and_denies_late_spawn(harness):
    h = harness()
    h.acquire_gate.clear()
    s = h.supervisor
    s.start_worker("worker")
    assert h.acquired.wait(2)
    with pytest.raises(WorkerReconfigurationBusyError):
        quick(lambda: s.persist_sharing_disabled(lambda: pytest.fail("not paused")))
    s.pause_worker("worker")
    ticket = s._record("worker").resource_operation
    stored = []
    quick(lambda: s.persist_sharing_disabled(lambda: stored.append(True)))
    assert stored == [True] and s._record("worker").resource_operation is ticket
    assert not s.snapshot("worker")["policy_admitted"]
    with pytest.raises(WorkerPolicyError):
        s.start_worker("worker")
    h.acquire_gate.set()
    wait_for(lambda: h.releasing.is_set() and not h.held)
    assert h.children == []
    wait_for(lambda: s._record("worker").resource_thread is None)
    s.reconfigure(WorkerSupervisorSettings(s.launches, 0.25), persist=lambda: None)
    assert s.snapshot("worker")["policy_admitted"]


def test_failed_disable_persistence_does_not_mask_old_policy(harness):
    h = harness()
    s = h.supervisor
    s.pause_worker("worker")
    with pytest.raises(OSError):
        s.persist_sharing_disabled(lambda: (_ for _ in ()).throw(OSError("write failed")))
    assert s.snapshot("worker")["policy_admitted"]


def test_async_hook_requires_release_and_valid_callable():
    with pytest.raises(ValueError):
        WorkerSupervisor((launch(),), acquire_resources_cancellable=True)
    s = WorkerSupervisor((launch(),), acquire_resources_cancellable=lambda item, cancel: pytest.fail("no release hook"))
    try:
        with pytest.raises(WorkerPolicyError):
            s.start_worker("worker")
    finally:
        s.shutdown()


def test_independent_workers_can_queue_bounded_admission_concurrently(harness):
    h = harness(items=(launch("one", auto_restart=False), launch("two", auto_restart=False)))
    h.acquire_gate.clear()
    s = h.supervisor
    quick(lambda: s.start_worker("one"))
    assert h.acquired.wait(2)
    quick(lambda: s.start_worker("two"))
    wait_for(lambda: len(h.cancellations) == 2)
    runners = tuple(record.resource_thread for record in s._records.values())
    for _ in range(20):
        s.start_worker("one")
        s.start_worker("two")
    assert tuple(record.resource_thread for record in s._records.values()) == runners
    assert len(h.cancellations) == 2
    quick(s.shutdown)
    assert all(cancel.is_set() for cancel in h.cancellations)
    h.acquire_gate.set()
    wait_for(lambda: all(record.resource_thread is None for record in s._records.values()))
    assert not h.children and not h.held


def test_batch_waits_for_every_old_release_and_retries_whole_set(harness):
    items = (launch("one", auto_restart=False), launch("two", auto_restart=False))
    h = harness(items=items, coordinated_launches=True)
    s = h.supervisor
    s.start_service()
    s.start_worker("one")
    s.start_worker("two")
    wait_for(lambda: len(h.children) == 2)
    wait_for(lambda: all(s.snapshot(item.worker_id)["pid"] is not None for item in items))
    h.release_gate.clear()
    new = tuple(replace(item, model_id="replacement") for item in items)
    with pytest.raises(RuntimeError, match="cleanup is incomplete"):
        quick(lambda: s.replace_launches(new, preserve_start_intent=True))
    assert s.launches == items and all(child.poll() is not None for child in h.children)
    assert len(h.cancellations) == 2 and len(h.held) == 2
    h.release_gate.set()
    wait_for(lambda: not h.held)
    wait_for(lambda: all(record.resource_thread is None for record in s._records.values()))
    with pytest.raises(WorkerReconfigurationBusyError, match="every pending"):
        s.replace_launches(new[:1], preserve_start_intent=True)
    s.replace_launches(new, preserve_start_intent=True)
    wait_for(lambda: len(h.children) == 4)
    assert s.launches == new
    first_new = h.events.index(("acquire", 3))
    assert all(h.events.index(("released", f"private-token-{index}")) < first_new for index in (1, 2))


def test_natural_exit_releases_old_generation_before_automatic_restart(harness):
    h = harness(item=launch(auto_start=True, auto_restart=True))
    s = h.supervisor
    s.start_service()
    wait_for(lambda: len(h.children) == 1)
    h.children[0].terminate()
    h.children[0].wait(timeout=3)
    wait_for(lambda: len(h.children) == 2)
    assert h.events.index(("released", "private-token-1")) < h.events.index(("acquire", 2))


def test_uncertain_popen_keeps_async_token_instead_of_releasing(harness):
    h = harness()
    s = h.supervisor
    original = s._popen

    def lost_handle(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("created a child but lost its handle")

    s._popen = lost_handle
    s.start_worker("worker")
    wait_for(lambda: s._record("worker").resource_thread is None)
    assert len(h.children) == 1 and h.held and not h.releasing.is_set()
    assert "creation is uncertain" in s.snapshot("worker")["resource_reason"]
    quick(s.shutdown)
    assert h.held and not h.releasing.is_set()


@pytest.mark.parametrize("operation", ["acquire", "release"])
def test_resource_thread_start_failure_is_fixed_retryable_and_keeps_existing_token(harness, monkeypatch, operation):
    h = harness()
    s = h.supervisor
    if operation == "release":
        s.start_worker("worker")
        wait_for(lambda: len(h.children) == 1 and s._record("worker").resource_thread is None)
    original = threading.Thread.start

    def unavailable(thread):
        if thread.name.startswith("drift-worker-resource-"):
            raise RuntimeError("private backend failure")
        return original(thread)

    monkeypatch.setattr(threading.Thread, "start", unavailable)
    if operation == "acquire":
        assert s.start_worker("worker") is False
        assert not h.held and not h.acquired.is_set()
    else:
        with pytest.raises(RuntimeError):
            s.pause_worker("worker")
        assert h.held and not h.releasing.is_set()
    record = s._record("worker")
    assert record.resource_thread is None and record.resource_operation is None and not record.resource_operation_active
    assert "private backend" not in str(s.snapshot("worker"))
    monkeypatch.setattr(threading.Thread, "start", original)
    if operation == "acquire":
        s.start_worker("worker")
        wait_for(lambda: len(h.children) == 1)
    else:
        s.pause_worker("worker")
        wait_for(lambda: not h.held)
