"""Supervisor integration with native recovery jobs and bounded resource drain."""

import os
import sys
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_worker_resource_lifecycle import launch, wait_for

from drift.node import edge_supervisor, worker_loading
from drift.node.resource_recovery import (
    RecoverableStateError,
    acquire_recovery_guard,
    make_generation_binding,
    open_owner_lease,
)
from drift.node.worker_recovery_containment import create_recovery_containment, recover_windows_containment
from drift.node.worker_supervisor import WorkerSupervisor

DIGEST = "sha256:" + "d" * 64


@pytest.fixture
def harness(tmp_path):
    fixtures = []

    def make(*, asynchronous=False, item=None, loading=False):
        root = tmp_path / str(len(fixtures))
        root.mkdir()
        state = SimpleNamespace(
            jobs={},
            bindings={},
            loading={},
            held=set(),
            events=[],
            acquire_entered=threading.Event(),
            acquire_gate=threading.Event(),
            release_entered=threading.Event(),
            release_gate=threading.Event(),
            fail_release=False,
            lookup_missing=False,
            root=root,
        )
        state.acquire_gate.set()
        state.release_gate.set()
        state.lease = open_owner_lease(root / "owners", uuid4().hex)

        def acquire(item, cancel=None):
            state.acquire_entered.set()
            assert state.acquire_gate.wait(8)
            token = uuid4().hex
            binding = make_generation_binding(state.lease.owner_binding, token, kind="worker", claim_digest=DIGEST)
            state.bindings[token] = binding
            state.jobs[token] = create_recovery_containment(binding)
            if loading:
                state.loading[token] = worker_loading.create_loading_binding(root / "loading", token, DIGEST)
            state.held.add(token)
            state.events.append(("admitted", token))
            return token

        def lookup(token):
            assert token in state.held
            state.events.append(("lookup", token))
            return None if state.lookup_missing else state.jobs[token]

        def release(token):
            state.release_entered.set()
            assert state.release_gate.wait(8)
            if state.fail_release:
                raise OSError("private reservation release failure")
            job = state.jobs[token]
            if sys.platform == "win32" and job._job is not None:
                assert not job.has_members(), "reservation cannot be freed before complete job emptiness"
            job.close()
            state.held.remove(token)
            state.events.append(("released", token))

        def forbidden_popen(*args, **kwargs):
            raise AssertionError("atomic recovery job must own Windows process creation")

        options = {"acquire_resources_cancellable" if asynchronous else "acquire_resources": acquire}
        if loading:
            options["loading_binding_for_token"] = state.loading.__getitem__
        if sys.platform == "win32":
            options["popen"] = forbidden_popen
        state.supervisor = WorkerSupervisor(
            [item or launch(auto_restart=False)],
            release_resources=release,
            recovery_containment_for_token=lookup,
            stop_timeout=0.3,
            poll_period=0.01,
            **options,
        )
        fixtures.append(state)
        return state

    yield make
    for state in fixtures:
        state.acquire_gate.set()
        state.release_gate.set()
        state.fail_release = False
        state.supervisor.shutdown()
        state.supervisor.drain_resource_operations(timeout=5)
        state.supervisor.shutdown()
        assert state.supervisor.drain_resource_operations(timeout=5)
        state.lease.close()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_generation_lookup_uses_admitted_atomic_job_and_never_legacy_factory(harness, monkeypatch, asynchronous):
    state = harness(asynchronous=asynchronous)
    monkeypatch.setattr(
        edge_supervisor, "_new_containment", lambda: pytest.fail("created a throwaway legacy containment")
    )
    state.supervisor.start_worker("worker")
    wait_for(lambda: state.supervisor.snapshot("worker")["pid"] is not None)
    record = state.supervisor._record("worker")
    token, process = record.resource_token, record.process
    assert state.events[:2] == [("admitted", token), ("lookup", token)]
    assert state.jobs[token].has_members()
    state.supervisor.pause_worker("worker")
    wait_for(lambda: not state.held)
    assert process.poll() is not None
    assert process in state.supervisor._verified_stops
    state.supervisor.start_worker("worker")
    wait_for(lambda: state.supervisor.snapshot("worker")["pid"] is not None)
    assert record.resource_token != token


def test_missing_recovery_lookup_never_falls_back_and_releases_unspawned_claim(harness):
    state = harness()
    state.lookup_missing = True
    assert not state.supervisor.start_worker("worker")
    assert state.supervisor.snapshot("worker")["pid"] is None
    assert not state.held
    assert not state.supervisor._record("worker").resource_spawn_uncertain


def test_shutdown_drain_bounds_blocked_admission_and_late_token_release(harness):
    state = harness(asynchronous=True)
    state.acquire_gate.clear()
    state.release_gate.clear()
    assert not state.supervisor.drain_resource_operations()
    state.supervisor.start_worker("worker")
    assert state.acquire_entered.wait(3)
    state.supervisor.shutdown()
    started = time.monotonic()
    assert not state.supervisor.drain_resource_operations(timeout=0.05)
    assert time.monotonic() - started < 0.75
    state.acquire_gate.set()
    assert state.release_entered.wait(3)
    assert not state.supervisor.drain_resource_operations(timeout=0.05)
    assert not any(event[0] == "lookup" for event in state.events)
    binding = next(iter(state.bindings.values()))
    with pytest.raises(RecoverableStateError, match="still active"):
        with acquire_recovery_guard(state.root / "owners", binding, expected_claim_digest=DIGEST):
            pytest.fail("pending cleanup must keep owner authority")
    state.release_gate.set()
    assert state.supervisor.drain_resource_operations(timeout=5)
    assert not state.held and state.supervisor.snapshot("worker")["pid"] is None
    state.lease.close()
    with acquire_recovery_guard(state.root / "owners", binding, expected_claim_digest=DIGEST) as guard:
        if sys.platform == "win32":
            guard.prove_empty(windows_probe=recover_windows_containment)


def test_failed_release_never_reports_safe_drain_and_shutdown_retry_retains_token(harness):
    state = harness(asynchronous=True)
    state.supervisor.start_worker("worker")
    wait_for(lambda: state.supervisor.snapshot("worker")["pid"] is not None)
    state.fail_release = True
    state.supervisor.shutdown()
    wait_for(lambda: state.supervisor._record("worker").resource_thread is None)
    assert not state.supervisor.drain_resource_operations(timeout=0.03)
    assert state.held
    state.fail_release = False
    state.supervisor.shutdown()
    assert state.supervisor.drain_resource_operations(timeout=5)
    assert not state.held


def test_loading_identity_and_ready_acknowledgement_work_with_native_process_wrapper(harness):
    source = (
        "import os,time;from drift.node.worker_loading import child_loading_session_from_environment;"
        f"session=child_loading_session_from_environment(os.environ,expected_binding_digest={DIGEST!r});"
        "session.__enter__();session.ready();time.sleep(60)"
    )
    item = replace(launch(auto_restart=False), command=(sys.executable, "-c", source))
    state = harness(asynchronous=True, item=item, loading=True)
    state.supervisor.start_worker("worker")
    wait_for(lambda: state.supervisor.snapshot("worker")["model_ready"], timeout=15)
    assert state.held
    state.supervisor.pause_worker("worker")
    wait_for(lambda: not state.held)
    assert not state.supervisor.snapshot("worker")["model_ready"]


@pytest.mark.parametrize("timeout", [-1, float("nan"), float("inf"), True, "1"])
def test_drain_rejects_unbounded_or_invalid_timeout(timeout):
    supervisor = WorkerSupervisor([])
    with pytest.raises(ValueError):
        supervisor.drain_resource_operations(timeout)
    supervisor.shutdown()
    assert supervisor.drain_resource_operations()
