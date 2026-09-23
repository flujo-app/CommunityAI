"""Read-only authority tests for replacement failure containment."""

import copy
import os
from types import SimpleNamespace

import pytest

from communityai_anchor import linux_anchor as anchor, linux_anchor_state as state, worker_loading as private
from communityai_anchor.resource_recovery import RecoverableStateError
from drift.node import linux_anchor_containment as containment


def _profile(path, inode, uid):
    return anchor.cg.LinuxCgroupProfile(
        path,
        (51, inode),
        17,
        "/",
        "/sys/fs/cgroup",
        ((1, 2), (3, 4), (5, 6)),
        uid,
    )


def _lease(kind, root, name, identity):
    owner = object.__new__(kind)
    owner.root = root
    owner.path = root / name
    owner.root_identity = private._identity(private._stat(root, directory=True))
    owner.identity = identity
    owner.fd = 100
    owner.created = False
    owner.valid = True
    return owner


def _channel_lease():
    lease = object.__new__(anchor.AnchorChannelLease)
    lease.valid = True
    return lease


@pytest.fixture
def authority(tmp_path, monkeypatch):
    root = tmp_path / "profile"
    directory = root / "anchor"
    root.mkdir(mode=0o700)
    directory.mkdir(mode=0o700)
    uid = os.geteuid() if hasattr(os, "geteuid") else 1000
    current = anchor.ServiceIdentity(os.getpid(), uid, 900, "b" * 32, "/user.slice/communityai.service")
    previous = dict(
        pid=max(2, os.getpid() + 1000),
        uid=uid,
        start_ticks=700,
        invocation="a" * 32,
        control_group=current.control_group,
    )
    root_group = "/sys/fs/cgroup/user.slice/communityai.service"
    profiles = tuple(
        _profile(root_group + ("" if index == 0 else "/" + name), 300 + index, uid)
        for index, name in enumerate(("", *anchor._CHILDREN))
    )
    state_identity = (81, 82)
    machine = {
        "platform": "linux",
        "host_id": "sha256:" + "c" * 64,
        "boot_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
    }
    value = dict(
        schema_version=1,
        profile=anchor.PROFILE,
        binding=dict(
            service=previous,
            machine=machine,
            layout=[profile.to_json() for profile in profiles],
            storage=dict(
                profile=list(private._identity(private._stat(root, directory=True))),
                directory=list(private._identity(private._stat(directory, directory=True))),
                lease=list(state_identity),
            ),
        ),
        revision=4,
        phase="idle",
        generation=None,
        request_id=None,
        operation=None,
    )
    path = directory / "state.json"
    path.write_bytes(containment._canonical(value))
    path.chmod(0o600)
    info = private._stat(path)
    evidence = dict(
        fingerprint=list(private._fingerprint(info)),
        digest=containment.hashlib.sha256(containment._canonical(value)).hexdigest(),
    )
    channel = _channel_lease()
    state_lease = _lease(state.PrivateLease, root, "anchor-state.lock", state_identity)
    lifetime = _lease(state.PrivateLease, root, "node-lifetime.lock", (83, 84))

    monkeypatch.setattr(anchor.AnchorChannelLease, "validate", lambda self: anchor._require(self.valid))
    monkeypatch.setattr(state.PrivateLease, "validate", lambda self: anchor._require(self.valid))
    monkeypatch.setattr(containment, "current_recovery_identity", lambda: SimpleNamespace(to_json=lambda: machine))
    monkeypatch.setattr(anchor, "inspect_service", lambda **_kwargs: current)
    monkeypatch.setattr(anchor, "_delegated_path", lambda _service: root_group)
    monkeypatch.setattr(anchor, "_process", lambda _pid: (previous["start_ticks"] + 1, previous["control_group"]))
    monkeypatch.setattr(
        anchor.cg,
        "observe_cgroup_profile",
        lambda path, **_kwargs: profiles[0] if path == root_group else pytest.fail("unexpected cgroup path"),
    )
    owner = containment.StateContainment(
        root,
        current,
        channel,
        state_lease,
        lifetime,
        value,
        evidence,
    )
    return SimpleNamespace(
        owner=owner,
        root=root,
        current=current,
        previous=previous,
        profiles=profiles,
        value=value,
        evidence=evidence,
        channel=channel,
        state_lease=state_lease,
        lifetime=lifetime,
        path=path,
        machine=machine,
    )


def test_authority_deep_copies_exact_state_and_has_no_effect_api(authority):
    authority.value["revision"] += 1
    authority.evidence["digest"] = "f" * 64

    assert authority.owner.validate() is True
    assert authority.owner.profiles == authority.profiles
    assert not any(hasattr(authority.owner, name) for name in ("kill", "clean", "remove", "adopt", "acknowledge"))


def test_validation_never_requests_a_cgroup_or_filesystem_effect(authority, monkeypatch):
    def effect(*_args, **_kwargs):
        pytest.fail("read-only containment authority attempted an effect")

    monkeypatch.setattr(anchor.cg, "_control", effect)
    monkeypatch.setattr(containment.os, "write", effect)
    monkeypatch.setattr(containment.os, "unlink", effect)
    monkeypatch.setattr(containment.os, "mkdir", effect)

    assert authority.owner.validate() is True


@pytest.mark.parametrize("lease_name", ["channel", "state_lease", "lifetime"])
def test_each_borrowed_lease_is_revalidated(authority, lease_name):
    getattr(authority, lease_name).valid = False

    with pytest.raises(RecoverableStateError):
        authority.owner.validate()


def test_live_prior_service_refuses_authority(authority, monkeypatch):
    monkeypatch.setattr(
        anchor,
        "_process",
        lambda _pid: (authority.previous["start_ticks"], authority.previous["control_group"]),
    )

    with pytest.raises(RecoverableStateError):
        authority.owner.validate()


@pytest.mark.parametrize("change", ["boot", "unit", "uid", "root"])
def test_identity_changes_refuse_authority(authority, monkeypatch, change):
    if change == "boot":
        different = dict(authority.machine, boot_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
        monkeypatch.setattr(
            containment,
            "current_recovery_identity",
            lambda: SimpleNamespace(to_json=lambda: different),
        )
    elif change == "unit":
        current = anchor.ServiceIdentity(
            authority.current.pid,
            authority.current.uid,
            authority.current.start_ticks,
            authority.current.invocation,
            "/user.slice/other.service",
        )
        monkeypatch.setattr(anchor, "inspect_service", lambda **_kwargs: current)
    elif change == "uid":
        current = anchor.ServiceIdentity(
            authority.current.pid,
            authority.current.uid + 1,
            authority.current.start_ticks,
            authority.current.invocation,
            authority.current.control_group,
        )
        monkeypatch.setattr(anchor, "inspect_service", lambda **_kwargs: current)
    else:
        replaced = _profile(authority.profiles[0].root, 999, authority.current.uid)
        monkeypatch.setattr(anchor.cg, "observe_cgroup_profile", lambda *_args, **_kwargs: replaced)

    with pytest.raises(RecoverableStateError):
        authority.owner.validate()


def test_state_replacement_or_mutation_refuses_authority(authority):
    changed = copy.deepcopy(authority.owner.value)
    changed["revision"] += 1
    authority.path.write_bytes(containment._canonical(changed))

    with pytest.raises(RecoverableStateError):
        authority.owner.validate()


@pytest.mark.parametrize("lease_name", ["state_lease", "lifetime"])
@pytest.mark.parametrize("bad", ["created", "root", "name"])
def test_constructor_requires_exact_existing_borrowed_leases(authority, lease_name, bad):
    state_lease, lifetime = authority.state_lease, authority.lifetime
    lease = getattr(authority, lease_name)
    if bad == "created":
        lease.created = True
    elif bad == "root":
        lease.root = authority.root.parent
    else:
        wrong = "node-lifetime.lock" if lease_name == "state_lease" else "anchor-state.lock"
        lease.path = authority.root / wrong

    with pytest.raises(RecoverableStateError):
        containment.StateContainment(
            authority.root,
            authority.current,
            authority.channel,
            state_lease,
            authority.lifetime,
            authority.owner.value,
            authority.owner.evidence,
        )


@pytest.mark.parametrize("lease_name", ["channel", "state_lease", "lifetime"])
def test_constructor_rejects_nonlease_objects(authority, lease_name):
    leases = dict(
        channel=authority.channel,
        state_lease=authority.state_lease,
        lifetime=authority.lifetime,
    )
    leases[lease_name] = object()

    with pytest.raises(RecoverableStateError):
        containment.StateContainment(
            authority.root,
            authority.current,
            leases["channel"],
            leases["state_lease"],
            leases["lifetime"],
            authority.owner.value,
            authority.owner.evidence,
        )


def test_generic_observation_errors_are_sanitized(authority, monkeypatch):
    monkeypatch.setattr(
        anchor.cg, "observe_cgroup_profile", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("private path"))
    )

    with pytest.raises(RecoverableStateError) as caught:
        authority.owner.validate()
    assert "private path" not in str(caught.value)
