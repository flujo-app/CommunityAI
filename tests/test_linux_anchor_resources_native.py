"""Native storage bootstrap faults; fixture service, no installed qualification."""

import os
import sys

import pytest
from test_linux_anchor_native import running  # noqa: F401
from test_linux_anchor_state_native import journal  # noqa: F401

from drift.node import linux_anchor_resources as resources
from drift.node import linux_anchor_state as state
from drift.node.resource_recovery import RecoverableStateError
from drift.node.resource_reservations import ResourceReservationManager

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or not os.environ.get("COMMUNITYAI_TEST_CGROUP_ROOT"),
    reason="requires private native cgroup fixture",
)


@pytest.fixture
def bound(journal):
    lease = state.node_lease(journal.profile, create=True)
    resources.create_directories(journal.profile, journal.owner.binding)
    manager = ResourceReservationManager(
        journal.profile / "node" / "resource-reservations",
        loading_protocol=True,
        recovery_protocol=True,
        worker_cgroup_root=journal.layout.profiles[3].root,
    )
    assert manager.recover()
    with manager.drain_guard():
        journal.resources = resources.create_resources(journal.profile, journal.owner.binding)
    yield journal
    manager.close()
    lease.close()


@pytest.mark.parametrize(
    "target", ["node-lifetime.lock", "node", "node/resource-reservations", "node/resource-reservations/admission.lock"]
)
@pytest.mark.parametrize("replace", [False, True])
def test_lifecycle_binding_refuses_missing_and_replaced_storage(bound, target, replace):
    path = bound.profile / target
    is_directory = path.is_dir()
    path.rename(path.with_name(path.name + ".retained"))
    if replace:
        if is_directory:
            path.mkdir(mode=0o700)
        else:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(fd)
    with pytest.raises((RecoverableStateError, OSError)):
        resources.read_resources(bound.profile, bound.owner.binding)
    if target == "node-lifetime.lock" and not replace:
        with pytest.raises(RecoverableStateError):
            state.node_lease(bound.profile, create=False)
        assert not path.exists()


@pytest.mark.parametrize("failure", ["profile", "node"])
def test_directory_fsync_failure_retains_partial_bootstrap_without_binding(journal, monkeypatch, failure):
    sync = resources._sync_directory
    target = journal.profile if failure == "profile" else journal.profile / "node"

    def interrupted(path, identity):
        sync(path, identity)
        if path == target:
            raise OSError("fixture missing parent durability acknowledgement")

    monkeypatch.setattr(resources, "_sync_directory", interrupted)
    with pytest.raises(OSError):
        resources.create_directories(journal.profile, journal.owner.binding)
    assert (journal.profile / "node").is_dir()
    assert not (journal.profile / "anchor" / "resources.json").exists()
    with pytest.raises(RecoverableStateError):
        state.node_lease(journal.profile, create=False)
