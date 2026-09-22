"""Lifetime reservations with real contained sleeping children, no GPU/model."""

import subprocess
import sys
import time
from dataclasses import replace

import psutil
import pytest

from drift.node import edge_supervisor
from drift.node.placement_resources import WorkerResourceClaim
from drift.node.worker_supervisor import (
    WorkerLaunch,
    WorkerPolicyError,
    WorkerReconfigurationBusyError,
    WorkerSupervisor,
    WorkerSupervisorSettings,
)


def wait_for(predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("resource lifecycle did not reach the expected state")


def launch(worker="worker", **kwargs):
    return WorkerLaunch(
        worker,
        "model",
        (sys.executable, "-c", "import time; time.sleep(60)", "communityai-resource-fixture", worker),
        resource_claim=WorkerResourceClaim("template", worker, 10, 20),
        max_host_memory_bytes=100,
        max_disk_bytes=100,
        restart_backoff=0.01,
        **kwargs,
    )


class Reservations:
    def __init__(self):
        self.events = []
        self.held = {}
        self.sequence = 0
        self.fail_acquire = False
        self.fail_release = False
        self.on_acquire = None
        self.on_release = None

    def acquire(self, item):
        self.events.append(("acquire", item.worker_id))
        if self.fail_acquire:
            raise OSError("private journal path and credential")
        self.sequence += 1
        token = f"private-generation-{self.sequence}"
        self.held[token] = item
        if self.on_acquire is not None:
            self.on_acquire(item)
        return token

    def release(self, token):
        self.events.append(("release", token))
        if self.fail_release:
            raise OSError("private journal path and credential")
        if self.on_release is not None:
            self.on_release(token)
        self.held.pop(token, None)


@pytest.fixture
def make_supervisor():
    fixtures = []

    def make(launches=None, **kwargs):
        reservations = Reservations()
        children = []

        def popen(command, **options):
            assert any(item.worker_id == command[-1] for item in reservations.held.values())
            reservations.events.append(("popen", command[-1]))
            process = subprocess.Popen(command, **options)
            children.append(process)
            return process

        supervisor = WorkerSupervisor(
            (launch(),) if launches is None else launches,
            acquire_resources=reservations.acquire,
            release_resources=reservations.release,
            popen=popen,
            stop_timeout=0.5,
            poll_period=0.01,
            **kwargs,
        )
        original_terminate = supervisor._terminate_launch_tree
        fixtures.append((supervisor, reservations, children, original_terminate))
        return supervisor, reservations, children

    yield make
    for supervisor, reservations, children, original in fixtures:
        reservations.fail_release = False
        reservations.on_release = None
        supervisor._terminate_launch_tree = original
        supervisor.shutdown()
        # Deliberately lost Popen handles are recovered only by this fixture,
        # never by the supervisor or by guessing a PID in production.
        for process in children:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize("coordinated", [False, True])
def test_reservation_precedes_spawn_and_survives_until_verified_containment_cleanup(make_supervisor, coordinated):
    supervisor, reservations, children = make_supervisor(coordinated_launches=coordinated)
    assert supervisor.start_worker("worker")
    token = supervisor._record("worker").resource_token
    assert token in reservations.held
    assert id(children[0]) in supervisor._process_containments
    assert not supervisor.start_worker("worker")
    assert reservations.sequence == 1

    def after_cleanup(value):
        assert value == token
        assert children[0].poll() is not None
        assert id(children[0]) not in supervisor._process_containments
        assert children[0] in supervisor._verified_stops

    reservations.on_release = after_cleanup
    supervisor.pause_worker("worker")
    assert not reservations.held
    assert supervisor._record("worker").resource_token is None
    reservations.on_release = None
    assert supervisor.start_worker("worker")
    assert supervisor._record("worker").resource_token != token
    assert reservations.sequence == 2


@pytest.mark.parametrize("gate", ["schedule", "device", "lease"])
def test_closed_gate_never_acquires_or_spawns(make_supervisor, gate):
    item = launch(
        device_available=(lambda: False) if gate == "device" else None,
        placement_available=(lambda: False) if gate == "lease" else None,
    )
    supervisor, reservations, children = make_supervisor(
        [item], schedule_allowed=(lambda: False) if gate == "schedule" else None
    )
    with pytest.raises(WorkerPolicyError):
        supervisor.start_worker("worker")
    assert not reservations.events and not children


@pytest.mark.parametrize("acquire,release", [(None, None), (lambda item: "token", None), (None, lambda token: None)])
def test_claim_without_both_hooks_fails_closed(acquire, release):
    supervisor = WorkerSupervisor([launch()], acquire_resources=acquire, release_resources=release)
    try:
        with pytest.raises(WorkerPolicyError, match="aggregate resource reservation"):
            supervisor.start_worker("worker")
        assert supervisor.snapshot("worker")["pid"] is None
    finally:
        supervisor.shutdown()


@pytest.mark.parametrize("hook", ["acquire_resources", "release_resources"])
def test_noncallable_hook_rejected(hook):
    with pytest.raises(ValueError, match="hooks"):
        WorkerSupervisor([launch()], **{hook: 1})


@pytest.mark.parametrize("budget", [None, True, 0, -1, 1.5, 2**63])
def test_resource_claim_requires_positive_bounded_host_ceiling(budget):
    with pytest.raises(ValueError, match="host memory|max_host_memory"):
        replace(launch(), max_host_memory_bytes=budget)


def test_claim_is_immutable_comparison_input_and_cannot_name_another_worker():
    item = launch()
    assert item != replace(item, resource_claim=replace(item.resource_claim, persistent_host_bytes=11))
    with pytest.raises(ValueError, match="matching worker"):
        replace(item, resource_claim=replace(item.resource_claim, worker_id="other"))


def test_acquisition_failure_is_sanitized_and_can_retry_without_popen(make_supervisor, caplog):
    supervisor, reservations, children = make_supervisor()
    reservations.fail_acquire = True
    assert not supervisor.start_worker("worker")
    status = supervisor.snapshot("worker")
    assert not status["resource_admitted"]
    assert "aggregate resource reservation" in status["last_error"]
    assert "private" not in repr(status) + caplog.text
    assert not children and not reservations.held
    assert not any(event[0] == "release" for event in reservations.events)
    reservations.fail_acquire = False
    assert supervisor.start_worker("worker")


@pytest.mark.parametrize("kind", ["capacity", "unavailable", "spoof"])
def test_only_typed_capacity_error_selects_fixed_capacity_reason(make_supervisor, caplog, kind):
    from drift.node.resource_reservations import ResourceReservationError

    supervisor, reservations, children = make_supervisor()
    if kind == "spoof":
        error = RuntimeError("private raw journal and token")
        error.category = "capacity"
    else:
        error = ResourceReservationError("private raw journal and token", category=kind)

    def fail(_):
        raise error

    supervisor._acquire_resources = fail
    assert not supervisor.start_worker("worker")
    status = supervisor.snapshot("worker")
    expected = (
        "shared host memory or cache storage is unavailable for this worker"
        if kind == "capacity"
        else "worker is waiting for an aggregate resource reservation"
    )
    assert status["resource_reason"] == status["last_error"] == expected
    assert "private raw" not in repr(status) + caplog.text
    assert not children and not reservations.held


def test_acquire_postwrite_exception_never_rolls_back_an_unknown_manager_token(make_supervisor):
    supervisor, reservations, children = make_supervisor()

    def uncertain_write(_):
        reservations.fail_acquire = True  # Emulate manager quarantine on following attempts.
        raise OSError("private durable journal uncertainty")

    reservations.on_acquire = uncertain_write
    assert not supervisor.start_worker("worker")
    assert len(reservations.held) == 1 and not children
    assert supervisor._record("worker").resource_token is None
    assert not supervisor.start_worker("worker")
    supervisor.shutdown()
    assert len(reservations.held) == 1
    assert not any(event[0] == "release" for event in reservations.events)


@pytest.mark.parametrize("token", [None, "", True, "x" * 129])
def test_invalid_token_keeps_supervisor_uncertain_without_spawning(make_supervisor, token):
    supervisor, reservations, children = make_supervisor()
    supervisor._acquire_resources = lambda item: token
    assert not supervisor.start_worker("worker")
    assert supervisor.snapshot("worker")["cleanup_pending"]
    assert not children
    with pytest.raises(RuntimeError, match="uncertain"):
        supervisor.pause_worker("worker")
    assert not any(event[0] == "release" for event in reservations.events)


@pytest.mark.parametrize("create_before_raise", [False, True])
def test_popen_exception_without_handle_retains_generation_without_guessing_cleanup(
    make_supervisor, create_before_raise
):
    supervisor, reservations, children = make_supervisor()
    original = supervisor._popen

    def uncertain(command, **kwargs):
        if create_before_raise:
            original(command, **kwargs)
        raise OSError("private process command")

    supervisor._popen = uncertain
    assert not supervisor.start_worker("worker")
    assert len(reservations.held) == 1
    token = supervisor._record("worker").resource_token
    assert token in reservations.held
    assert supervisor.snapshot("worker")["cleanup_pending"]
    assert not supervisor.start_worker("worker")
    supervisor.shutdown()
    assert token in reservations.held
    assert not any(event[0] == "release" for event in reservations.events)


@pytest.mark.parametrize("change", ["lease", "schedule", "pause", "shutdown"])
def test_admission_rechecked_after_acquire_releases_when_no_spawn_occurs(make_supervisor, change):
    permitted = [True]
    item = launch(placement_available=lambda: permitted[0])
    supervisor, reservations, children = make_supervisor(
        [item], schedule_allowed=lambda: permitted[0] if change == "schedule" else True
    )

    def change_admission(_):
        if change in ("lease", "schedule"):
            permitted[0] = False
        elif change == "pause":
            supervisor.pause_worker("worker")
        else:
            supervisor.shutdown()

    reservations.on_acquire = change_admission
    assert not supervisor.start_worker("worker")
    assert not children and not reservations.held
    assert [event[0] for event in reservations.events] == ["acquire", "release"]


def test_reentrant_start_and_reconfiguration_cannot_cross_resource_acquire(make_supervisor):
    supervisor, reservations, children = make_supervisor(coordinated_launches=True)

    def reenter(item):
        with pytest.raises(WorkerReconfigurationBusyError):
            supervisor.start_worker("worker")
        with pytest.raises(WorkerReconfigurationBusyError):
            supervisor.replace_launches([item])

    reservations.on_acquire = reenter
    assert supervisor.start_worker("worker")
    assert len(children) == reservations.sequence == 1


def test_attach_failure_releases_only_after_actual_tree_cleanup(make_supervisor, monkeypatch):
    supervisor, reservations, children = make_supervisor()
    factory = edge_supervisor._new_containment

    class FailingAttach:
        def __init__(self):
            self.inner = factory()

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def attach(self, process):
            self.inner.attach(process)
            raise RuntimeError("private containment failure")

    monkeypatch.setattr(edge_supervisor, "_new_containment", FailingAttach)
    assert not supervisor.start_worker("worker")
    assert children[0].poll() is not None
    assert children[0] in supervisor._verified_stops
    assert not reservations.held


def test_uncertain_stop_retains_generation_and_retry_verifies_before_release(make_supervisor):
    supervisor, reservations, children = make_supervisor()
    assert supervisor.start_worker("worker")
    original = supervisor._terminate_launch_tree

    def fail(_):
        raise RuntimeError("private descendant failure")

    supervisor._terminate_launch_tree = fail
    with pytest.raises(RuntimeError, match="cleanup is incomplete"):
        supervisor.pause_worker("worker")
    assert len(reservations.held) == 1 and children[0].poll() is None
    assert not supervisor.start_worker("worker")
    supervisor._terminate_launch_tree = original
    supervisor.pause_worker("worker")
    assert not reservations.held and children[0].poll() is not None


def test_release_failure_blocks_start_and_mutation_after_child_is_gone(make_supervisor, caplog):
    supervisor, reservations, children = make_supervisor()
    assert supervisor.start_worker("worker")
    reservations.fail_release = True
    with pytest.raises(RuntimeError, match="resource release"):
        supervisor.pause_worker("worker")
    token = supervisor._record("worker").resource_token
    assert children[0].poll() is not None and token in reservations.held
    assert supervisor.snapshot("worker")["resource_reason"] == "worker resource release is incomplete; retry cleanup"
    assert "private journal" not in repr(supervisor.snapshot("worker")) + caplog.text
    assert not supervisor.start_worker("worker")
    assert reservations.sequence == 1
    with pytest.raises(WorkerReconfigurationBusyError):
        supervisor.replace_launch(launch())
    with pytest.raises(WorkerReconfigurationBusyError):
        supervisor.reconfigure(WorkerSupervisorSettings((launch(),), 0.5), persist=lambda: None)
    reservations.fail_release = False
    supervisor.pause_worker("worker")
    assert not reservations.held
    assert supervisor.start_worker("worker")
    assert supervisor._record("worker").resource_token != token


def test_shutdown_retries_pending_release_even_without_a_process(make_supervisor):
    supervisor, reservations, children = make_supervisor()
    supervisor.start_worker("worker")
    reservations.fail_release = True
    supervisor.shutdown()
    assert len(reservations.held) == 1 and children[0].poll() is not None
    reservations.fail_release = False
    supervisor.shutdown()
    assert not reservations.held


def test_release_acknowledgement_failure_retries_same_token_idempotently(make_supervisor):
    supervisor, reservations, children = make_supervisor()
    supervisor.start_worker("worker")
    token = supervisor._record("worker").resource_token
    original = reservations.release
    fail_once = [True]

    def uncertain_release(value):
        original(value)
        if fail_once[0]:
            fail_once[0] = False
            raise OSError("private journal acknowledgement failure")

    supervisor._release_resources = uncertain_release
    with pytest.raises(RuntimeError, match="resource release"):
        supervisor.pause_worker("worker")
    assert children[0].poll() is not None and not reservations.held
    assert supervisor._record("worker").resource_token == token
    supervisor.pause_worker("worker")
    assert supervisor._record("worker").resource_token is None
    assert [value for event, value in reservations.events if event == "release"] == [token, token]


def test_batch_barrier_releases_all_old_generations_before_any_new_acquisition(make_supervisor):
    supervisor, reservations, children = make_supervisor([launch("one"), launch("two")], coordinated_launches=True)
    supervisor._started = True  # No monitor; starts/retries in this test are synchronous.
    supervisor.start_worker("one")
    supervisor.start_worker("two")
    original_tokens = set(reservations.held)
    original_release = supervisor._release_resources
    fail_token = supervisor._record("one").resource_token

    def release(token):
        if token == fail_token:
            raise OSError("private release failure")
        original_release(token)

    supervisor._release_resources = release
    replacements = [
        replace(launch(name), resource_claim=WorkerResourceClaim("changed", name, 12, 24)) for name in ("one", "two")
    ]
    with pytest.raises(RuntimeError, match="cleanup is incomplete"):
        supervisor.replace_launches(replacements, preserve_start_intent=True)
    assert len(children) == 2 and all(child.poll() is not None for child in children)
    assert set(reservations.held) == {fail_token}
    supervisor._release_resources = original_release

    def no_old_generation(_):
        assert not original_tokens.intersection(reservations.held)
        assert all(child.poll() is not None for child in children[:2])

    reservations.on_acquire = no_old_generation
    supervisor.replace_launches(replacements, preserve_start_intent=True)
    assert len(children) == 4
    assert supervisor.launch_transition_status["state"] == "idle"


def test_natural_exit_kills_descendants_before_releasing_and_restarting(make_supervisor):
    program = (
        "import subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "print(child.pid,flush=True); time.sleep(0.1)"
    )
    item = replace(launch(), command=(sys.executable, "-c", program, "communityai-resource-fixture", "worker"))
    supervisor, reservations, children = make_supervisor([item])
    supervisor.start_worker("worker")
    wait_for(lambda: bool(supervisor.snapshot("worker")["recent_logs"]))
    descendant_pid = int(supervisor.snapshot("worker")["recent_logs"][0])
    token = supervisor._record("worker").resource_token

    def descendants_absent(_):
        assert not psutil.pid_exists(descendant_pid) or psutil.Process(descendant_pid).status() == psutil.STATUS_ZOMBIE

    reservations.on_release = descendants_absent
    wait_for(lambda: (supervisor.snapshot("worker"), not reservations.held)[1])
    assert children[0].poll() is not None
    reservations.on_release = None
    assert supervisor.start_worker("worker")
    assert supervisor._record("worker").resource_token != token


def test_automatic_restart_gets_new_generation_after_verified_exit(make_supervisor):
    supervisor, reservations, children = make_supervisor([launch(auto_start=True)])
    original_popen = supervisor._popen

    def first_exits(command, **kwargs):
        if not children:
            command = (sys.executable, "-c", "import time; time.sleep(0.05)", "worker")
        return original_popen(command, **kwargs)

    supervisor._popen = first_exits
    supervisor.start_service()
    first_token = supervisor._record("worker").resource_token
    wait_for(lambda: len(children) == 2)
    assert first_token not in reservations.held
    assert supervisor._record("worker").resource_token != first_token
    assert reservations.sequence == 2
    assert children[0] in supervisor._verified_stops


def test_attach_cleanup_failure_retains_token_until_verified_retry(make_supervisor, monkeypatch):
    supervisor, reservations, children = make_supervisor()
    factory = edge_supervisor._new_containment
    original_stop = supervisor._terminate_launch_tree

    class FailingAttach:
        def __init__(self):
            self.inner = factory()

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def attach(self, process):
            self.inner.attach(process)
            raise RuntimeError("private attach failure")

    def fail_cleanup(_):
        raise RuntimeError("private cleanup failure")

    monkeypatch.setattr(edge_supervisor, "_new_containment", FailingAttach)
    supervisor._terminate_launch_tree = fail_cleanup
    assert not supervisor.start_worker("worker")
    assert len(reservations.held) == 1
    assert supervisor.snapshot("worker")["cleanup_pending"]
    assert not any(event[0] == "release" for event in reservations.events)
    supervisor._terminate_launch_tree = original_stop
    supervisor.pause_worker("worker")
    assert children[0] in supervisor._verified_stops
    assert not reservations.held


def test_release_failure_before_popen_retains_and_blocks_until_retry(make_supervisor):
    allowed = [True]
    supervisor, reservations, children = make_supervisor([launch(placement_available=lambda: allowed[0])])
    reservations.on_acquire = lambda item: allowed.__setitem__(0, False)
    reservations.fail_release = True
    assert not supervisor.start_worker("worker")
    assert len(reservations.held) == 1 and not children
    allowed[0] = True
    assert not supervisor.start_worker("worker")
    assert reservations.sequence == 1
    reservations.fail_release = False
    supervisor.pause_worker("worker")
    assert not reservations.held


def test_reentrant_start_cannot_reacquire_inside_release(make_supervisor):
    supervisor, reservations, children = make_supervisor()
    supervisor.start_worker("worker")

    def release_guard(_):
        with pytest.raises(WorkerReconfigurationBusyError, match="resource operation"):
            supervisor.start_worker("worker")

    reservations.on_release = release_guard
    supervisor.pause_worker("worker")
    assert not reservations.held and len(children) == reservations.sequence == 1
