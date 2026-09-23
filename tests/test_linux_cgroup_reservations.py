"""Journal integration with a controlled Linux profile; native proof is separate."""

import json

import pytest
from test_loading_resource_reservations import loading_admission
from test_resource_reservations import admission, metadata_manifest, records

from drift.node import linux_cgroup_recovery as cgroups, resource_recovery as authority
from drift.node.resource_reservations import ResourceReservationError, ResourceReservationManager


@pytest.fixture
def linux_admission(loading_admission, monkeypatch):
    f = loading_admission
    profile = cgroups.LinuxCgroupProfile(
        "/delegated/communityai", (17, 10), 12, "/", "/delegated", ((1, 11), (1, 12), (1, 13)), 1000
    )
    identity = authority.RecoveryIdentity("linux", "sha256:" + "a" * 64, "12345678-1234-1234-1234-123456789abc")
    monkeypatch.setattr(authority, "current_recovery_identity", lambda: identity)
    f.profile_checks = []
    f.prepared = []

    def validate(root, *, require_unfrozen=True):
        assert root == profile.root
        f.profile_checks.append(root)
        return profile

    class Prepared:
        def __init__(self, owner, token):
            self.identity = cgroups.LinuxCgroupIdentity(
                profile, cgroups.generation_name(owner.owner_id, token), (17, 99)
            )
            self.binding = None
            self.closed = False

        def bind(self, binding):
            assert binding.linux_cgroup == self.identity
            self.binding = binding

        def close(self):
            self.closed = True

    def prepare(root, owner, token):
        assert root == profile.root
        result = Prepared(owner, token)
        f.prepared.append(result)
        return result

    monkeypatch.setattr(cgroups, "validate_cgroup_profile", validate)
    monkeypatch.setattr(cgroups, "prepare_generation", prepare)
    f.manager = ResourceReservationManager(
        f.directory,
        snapshot_provider=f.snapshot,
        clock=lambda: 100,
        loading_protocol=True,
        recovery_protocol=True,
        worker_cgroup_root=profile.root,
    )
    yield f
    f.manager.close()
    # Controlled fixtures never create a child or native cgroup.
    for prepared in f.prepared:
        prepared.close()
    if f.manager._owner_lease is not None:
        f.manager._owner_lease.close()


def test_native_identity_is_bound_before_journal_publication(linux_admission, monkeypatch):
    f = linux_admission
    original = f.manager._write
    published = []

    def write(entries):
        for entry in entries:
            if entry.get("recovery"):
                prepared = f.prepared[-1]
                assert prepared.binding is not None and not prepared.closed
                assert entry["recovery"] == prepared.binding.to_json()
                published.append(prepared.binding)
        return original(entries)

    monkeypatch.setattr(f.manager, "_write", write)
    token = f.manager.acquire(f.launch())
    assert f.profile_checks and len(published) == 1
    assert published[0].contract == "linux_cgroup_v1"
    assert published[0].reservation_id == token
    assert records(f)[0]["recovery"]["schema_version"] == 2
    assert f.manager.recovery_containment_for_token(token) is f.prepared[0]
    f.manager.release(token)
    assert f.prepared[0].closed and records(f) == []


@pytest.mark.parametrize("reason", ["unsupported_platform", "unverifiable_state"])
def test_explicit_unavailable_profile_blocks_empty_journal_and_metadata(linux_admission, monkeypatch, reason):
    f = linux_admission

    def unavailable(root, **kwargs):
        raise authority.RecoverableStateError(reason)

    monkeypatch.setattr(cgroups, "validate_cgroup_profile", unavailable)
    assert not f.manager.recover()
    assert f.manager.recovery_snapshot() == dict(state="blocked", reason=reason, retryable=False)
    with pytest.raises(ResourceReservationError):
        f.manager.acquire(f.launch())
    with pytest.raises(ResourceReservationError):
        with f.manager.metadata_admission(
            metadata_manifest(), cache_dir=f.root, host_limit_bytes=2 * 1024**3, disk_limit_bytes=1000
        ):
            pytest.fail("Unavailable explicit containment profile entered metadata loading")
    assert not f.prepared and f.manager._owner_lease is None
    assert records(f) == []


def test_profile_is_revalidated_after_ready_before_admission(linux_admission, monkeypatch):
    f = linux_admission
    assert f.manager.recover()

    def replaced(root, **kwargs):
        raise authority.RecoverableStateError()

    monkeypatch.setattr(cgroups, "validate_cgroup_profile", replaced)
    with pytest.raises(ResourceReservationError):
        f.manager.acquire(f.launch())
    assert f.manager.recovery_snapshot()["reason"] == "unverifiable_state"
    assert not f.prepared


def test_failed_identity_binding_does_not_publish_or_fall_back(linux_admission, monkeypatch):
    f = linux_admission

    def denied(*args, **kwargs):
        raise authority.RecoverableStateError()

    monkeypatch.setattr(authority, "make_generation_binding", denied)
    with pytest.raises(ResourceReservationError):
        f.manager.acquire(f.launch())
    assert f.prepared[0].closed
    assert not f.manager._recovery_containments and not f.manager._loading_bindings
    document = json.loads((f.directory / "generations.json").read_text(encoding="utf-8"))
    assert document["reservations"] == []
    assert f.manager.recovery_snapshot()["reason"] == "unverifiable_state"


def test_uncertain_journal_publication_retains_native_authority(linux_admission, monkeypatch):
    f = linux_admission
    original = f.manager._write

    def uncertain(entries):
        original(entries)
        if entries:
            raise OSError("controlled post-publication failure")

    monkeypatch.setattr(f.manager, "_write", uncertain)
    with pytest.raises(ResourceReservationError):
        f.manager.acquire(f.launch())
    assert not f.prepared[0].closed
    assert f.manager._recovery_containments and f.manager._loading_bindings
    assert len(records(f)) == 1 and f.manager._uncertain
    assert not f.manager.close()


def test_frozen_profile_does_not_prevent_orphan_cleanup_but_blocks_readmission(linux_admission, monkeypatch):
    f = linux_admission
    f.manager.acquire(f.launch())
    f.manager._owner_lease.close()
    frozen = True
    checks = []

    def validate(root, *, require_unfrozen=True):
        checks.append(require_unfrozen)
        if require_unfrozen and frozen:
            raise authority.RecoverableStateError()

    def prove(binding, guard):
        assert checks == [False]
        assert records(f), "journal release preceded native proof"
        return True

    monkeypatch.setattr(cgroups, "validate_cgroup_profile", validate)
    monkeypatch.setattr(cgroups, "recover_linux_cgroup", prove)
    restarted = ResourceReservationManager(
        f.directory,
        snapshot_provider=f.snapshot,
        clock=lambda: 100,
        loading_protocol=True,
        recovery_protocol=True,
        worker_cgroup_root="/delegated/communityai",
    )
    try:
        assert not restarted.recover()
        assert checks == [False, True]
        assert records(f) == []
        assert restarted.recovery_snapshot()["state"] == "blocked"
        with pytest.raises(ResourceReservationError):
            restarted.acquire(f.launch())
        frozen = False
        assert restarted.recover()
        assert restarted.recovery_snapshot()["state"] == "ready"
    finally:
        restarted.close()
