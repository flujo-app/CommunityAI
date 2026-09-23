"""Bounded ownership-composition tests; no replacement transaction is implied."""

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from drift.node import linux_anchor_node as node
from drift.node.linux_anchor_state import AnchorState, PrivateLease
from drift.node.resource_recovery import RecoverableStateError
from drift.node.resource_reservations import ResourceReservationError, ResourceReservationManager


def test_recovery_guard_uses_one_lock_rejects_nesting_and_expires(tmp_path, monkeypatch):
    manager = ResourceReservationManager(tmp_path / "reservations", loading_protocol=True, recovery_protocol=True)
    other = ResourceReservationManager(tmp_path / "other-reservations", loading_protocol=True, recovery_protocol=True)
    recovered = []

    def recover_locked(entries, cancelled):
        recovered.append((entries, cancelled))
        return entries

    monkeypatch.setattr(manager, "_recover_locked", recover_locked)
    with manager.recovery_guard() as guard:
        assert guard.recover() is True
        guard.require_empty_journal()
        guard.require_empty()
        guard.validate(manager)
        with pytest.raises(ResourceReservationError):
            guard.validate(other)
        with pytest.raises(ResourceReservationError):
            with manager.drain_guard():
                pytest.fail("nested admission lock was accepted")
        with pytest.raises(ResourceReservationError):
            with manager.recovery_guard():
                pytest.fail("nested recovery guard was accepted")
    assert len(recovered) == 1
    with pytest.raises(ResourceReservationError):
        guard.require_empty()
    assert manager.close()
    assert other.close()


def test_recovery_guard_journal_only_does_not_claim_containment_and_recover_failure_propagates(tmp_path, monkeypatch):
    manager = ResourceReservationManager(tmp_path / "reservations", loading_protocol=True, recovery_protocol=True)
    manager._worker_cgroup_root = "/fixture/old-worker-root-is-unavailable"

    def failed_recovery(entries, cancelled):
        raise ResourceReservationError("fixture recovery failed")

    monkeypatch.setattr(manager, "_recover_locked", failed_recovery)
    with manager.recovery_guard() as guard:
        guard.require_empty_journal()  # Deliberately does not inspect the missing old cgroup path.
        with pytest.raises(ResourceReservationError, match="fixture recovery failed"):
            guard.recover()
    manager._worker_cgroup_root = None
    assert manager.close()


class _FakeState:
    def __init__(self):
        self.binding = {"fixture": "binding"}
        self.poisoned = False
        self.closed = False
        self._value = dict(
            schema_version=1,
            revision=0,
            binding=self.binding,
            phase="checking",
            generation=None,
            request_id=None,
            operation=None,
        )

    @property
    def value(self):
        return dict(self._value)

    def validate(self):
        if self.closed:
            raise RecoverableStateError()

    def close(self):
        self.closed = True


class _FakeLease:
    def __init__(self):
        self.closed = False

    def validate(self):
        if self.closed:
            raise RecoverableStateError()

    def close(self):
        self.closed = True


class _FakeManager:
    def __init__(self, events=None):
        self.events = [] if events is None else events
        self.closed = False

    def recover(self, **_kwargs):
        return True

    @contextmanager
    def drain_guard(self, **_kwargs):
        self.events.append("guard-enter")
        try:
            yield
        finally:
            self.events.append("guard-exit")

    def close(self, _timeout=2.0):
        self.events.append("manager-close")
        self.closed = True
        return True


def _layout():
    return SimpleNamespace(
        service=SimpleNamespace(pid=os.getpid()),
        profiles=[SimpleNamespace(root=None) for _ in range(4)],
        validate=lambda: None,
    )


def _inactive_default(monkeypatch, tmp_path, *, retirement_hook=None, activate=False):
    layout = _layout()
    state, lease, manager = _FakeState(), _FakeLease(), _FakeManager()
    record = ({"identities": {"journal": [1, 2], "admission": [3, 4]}}, (5, 6))
    monkeypatch.setattr(node, "AnchorState", lambda *_args, **_kwargs: state)
    monkeypatch.setattr(node, "node_lease", lambda *_args, **_kwargs: lease)
    monkeypatch.setattr(node, "ResourceReservationManager", lambda *_args, **_kwargs: manager)
    monkeypatch.setattr(node.resources, "create_directories", lambda *_args: None)
    monkeypatch.setattr(node.resources, "create_resources", lambda *_args: record)
    monkeypatch.setattr(node.resources, "read_resources", lambda *_args: record)
    monkeypatch.setattr(node.anchor.cg, "verify_cgroup_tree_empty", lambda *_args: None)
    options = {} if activate is None else {"activate": activate}
    return (
        node.AnchorNode(
            layout,
            tmp_path,
            lambda _root: None,
            initialize=True,
            retirement_hook=retirement_hook,
            **options,
        ),
        state,
        lease,
        manager,
    )


def test_normal_constructor_still_activates_by_default(tmp_path, monkeypatch):
    activated = []
    monkeypatch.setattr(node.AnchorNode, "activate_owner", lambda owner: activated.append(owner))
    owner, _state, _lease, _manager = _inactive_default(monkeypatch, tmp_path, activate=None)
    assert activated == [owner]
    assert owner.abort_inactive()


def test_normal_constructor_can_stay_inactive_then_activate_exactly_once(tmp_path, monkeypatch):
    monkeypatch.setattr(node.threading, "Thread", lambda **_kwargs: pytest.fail("owner thread started early"))
    owner, _state, _lease, _manager = _inactive_default(monkeypatch, tmp_path)
    assert owner._runner is None and owner._activation_attempted is False

    class ImmediateThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

        def join(self, _timeout=None):
            pass

        def is_alive(self):
            return False

    owner._run = owner.finished.set
    monkeypatch.setattr(node.threading, "Thread", ImmediateThread)
    owner.activate_owner()
    assert owner._activation_attempted and owner.finished.is_set()
    with pytest.raises(RecoverableStateError):
        owner.activate_owner()


def test_quiescent_idle_refresh_is_one_guarded_hook_write_without_generic_transition():
    events = []
    owner = object.__new__(node.AnchorNode)
    owner._quiescence_hook = None
    owner._state = _FakeState()
    owner._state._value.update(
        schema_version=2,
        phase="idle",
        quiescence={"fixture": "sealed"},
    )
    owner._process = owner._reader = owner._credential_process = None
    owner._manager = _FakeManager(events)
    owner._clean_proof = lambda: events.append("clean")
    owner._publish = lambda: events.append("publish")
    owner._write = lambda **_changes: pytest.fail("generic state write cleared the idle seal")

    def hook(controller, *, validate_only=False, request_id=None):
        events.append(("hook", validate_only, request_id))
        assert controller is owner
        if not validate_only:
            controller._state._value.update(
                revision=controller._state._value["revision"] + 1,
                request_id=request_id,
                operation=None if request_id is None else "drain",
            )
        return controller._state.value

    owner._quiescence_hook = hook
    owner._quiescent_idle(request_id="a" * 32)
    assert events == [
        "guard-enter",
        "clean",
        ("hook", False, "a" * 32),
        "clean",
        "publish",
        "guard-exit",
    ]

    events.clear()
    before = owner._state.value
    owner._quiescent_idle(validate_only=True)
    assert owner._state.value == before
    assert events == [
        "guard-enter",
        "clean",
        ("hook", True, None),
        "clean",
        "publish",
        "guard-exit",
    ]


def _exact_lease(root, name):
    lease = object.__new__(PrivateLease)
    lease.root = root
    lease.path = root / name
    lease.created = False
    lease.fd = 123
    lease.closed = False
    return lease


def test_existing_authorities_adopt_without_reopen_manager_or_thread_and_abort_safely(tmp_path, monkeypatch):
    root = tmp_path / "profile"
    manager = ResourceReservationManager(
        root / "node" / "resource-reservations", loading_protocol=True, recovery_protocol=True
    )
    layout = _layout()
    state_lease = _exact_lease(root, "anchor-state.lock")
    lifetime = _exact_lease(root, "node-lifetime.lock")
    state = object.__new__(AnchorState)
    state._mutex = threading.RLock()
    state.lease = state_lease
    state.poisoned = False
    state.layout = layout
    state.binding = {"fixture": "binding"}
    state._value = dict(
        schema_version=1,
        revision=7,
        binding=state.binding,
        phase="idle",
        generation=None,
        request_id=None,
        operation=None,
    )
    state.closed = False

    def validate_lease(self):
        if self.closed:
            raise RecoverableStateError()

    def close_lease(self):
        self.closed = True
        self.fd = None

    def validate_state(self):
        if self.closed:
            raise RecoverableStateError()

    def close_state(self):
        self.closed = True
        self.lease.close()
        self.lease = None

    monkeypatch.setattr(PrivateLease, "validate", validate_lease)
    monkeypatch.setattr(PrivateLease, "close", close_lease)
    monkeypatch.setattr(AnchorState, "validate", validate_state)
    monkeypatch.setattr(AnchorState, "close", close_state)
    monkeypatch.setattr(node.threading, "Thread", lambda **_kwargs: pytest.fail("owner thread started early"))

    record = None

    def read_resources(_root, _binding):
        return record

    monkeypatch.setattr(node.resources, "read_resources", read_resources)
    with manager.recovery_guard() as guard:
        identities = manager._storage_binding
        record = (
            {
                "identities": {
                    "journal": list(identities["directory"]),
                    "admission": list(identities["lease"]),
                }
            },
            ("fixture-fingerprint",),
        )
        owner = node.AnchorNode.adopt_inactive(
            layout,
            root,
            lambda _root: None,
            state=state,
            node_lifetime_lease=lifetime,
            reservation_manager=manager,
            reservation_guard=guard,
            resources_record=record,
        )
        assert owner._state is state and owner._lease is lifetime and owner._manager is manager
        assert owner._runner is None and not owner._activation_attempted
    assert owner.abort_inactive()
    assert state.closed and state_lease.closed and lifetime.closed and manager._closed
    assert owner.finished.is_set()


def test_retirement_hook_runs_inside_final_guard_before_authority_release(tmp_path, monkeypatch):
    events = []

    def hook(controller):
        events.append("hook")
        assert not controller._state.closed and not controller._lease.closed

    owner, state, lease, manager = _inactive_default(monkeypatch, tmp_path, retirement_hook=hook)
    manager.events = events
    owner._cached["drain_complete"] = True
    owner._active = owner._pending = None
    owner._activation_attempted = True
    owner._clean_proof = lambda: events.append("clean")
    owner._release_leaf = lambda: events.append("leaf-release")

    class FinishedThread:
        def join(self, _timeout=None):
            events.append("join")

        def is_alive(self):
            return False

    owner._runner = FinishedThread()
    original_state_close, original_lease_close = state.close, lease.close
    state.close = lambda: (events.append("state-close"), original_state_close())[-1]
    lease.close = lambda: (events.append("lease-close"), original_lease_close())[-1]
    assert owner.close()
    assert events.index("guard-enter") < events.index("hook") < events.index("state-close")
    assert events.index("hook") < events.index("lease-close") < events.index("guard-exit")
    assert events.count("clean") == 3
    guarded = events[events.index("guard-enter") : events.index("guard-exit")]
    assert guarded.count("clean") == 2


def test_retirement_hook_failure_retains_state_and_lifetime_authority(tmp_path, monkeypatch):
    events = []

    def hook(_controller):
        events.append("hook")
        raise RecoverableStateError()

    owner, state, lease, manager = _inactive_default(monkeypatch, tmp_path, retirement_hook=hook)
    manager.events = events
    owner._cached["drain_complete"] = True
    owner._active = owner._pending = None
    owner._activation_attempted = True
    owner._clean_proof = lambda: events.append("clean")
    owner._runner = SimpleNamespace(join=lambda _timeout=None: None, is_alive=lambda: False)

    assert owner.close() is False
    assert events.index("guard-enter") < events.index("hook") < events.index("guard-exit")
    assert manager.closed and not state.closed and not lease.closed and not owner._closed


def test_start_refuses_but_never_deletes_retirement_receipt(tmp_path, monkeypatch):
    receipt = tmp_path / "anchor" / "retirement.json"
    receipt.parent.mkdir()
    receipt.write_text("retained authority", encoding="utf-8")
    owner = object.__new__(node.AnchorNode)
    owner.layout = _layout()
    owner.root = tmp_path
    owner._lock = threading.Lock()
    owner._stop = False
    owner._pending = None
    owner._cancel = threading.Event()
    owner._manager = _FakeManager()
    owner._state = SimpleNamespace(value={"phase": "idle"})
    owner._integrity = lambda: None
    owner._release_leaf = lambda: pytest.fail("Start continued after retirement receipt")
    monkeypatch.setattr(node.anchor.cg, "verify_cgroup_tree_empty", lambda *_args: None)
    with pytest.raises(RecoverableStateError):
        owner._start("a" * 32)
    assert receipt.read_text(encoding="utf-8") == "retained authority"
