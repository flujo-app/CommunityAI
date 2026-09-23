"""Exact durable child admission for the fixed Linux volunteer launcher.

The anchor retains the profile lease. Children receive no general launch grant:
a token only matches one persisted PID/start identity in one native node leaf.
"""

from __future__ import annotations

import os
from pathlib import Path

from communityai_anchor import linux_anchor as anchor, linux_anchor_state as state, worker_loading as private
from communityai_anchor.linux_anchor_resources import read_resources
from communityai_anchor.resource_recovery import RecoverableStateError, current_recovery_identity

NODE_TOKEN_ENV = "COMMUNITYAI_ANCHOR_NODE_TOKEN"
_ADMITTED = None


def _read(path):
    for attempt in range(4):
        try:
            return state.validate_state(private._read(path))
        except private._FileReplaced:
            if attempt == 3:
                raise RecoverableStateError() from None


def validate_node_entry(profile_root, token, worker_root):
    """Before profile mutation/keyring/runtime; no standalone Linux fallback."""
    global _ADMITTED
    _ADMITTED = None
    try:
        import fcntl

        anchor.cg._platform()
        anchor._require(state._hex(token))
        root = private._directory(profile_root)
        directory = private._directory(root / "anchor")
        before = _read(directory / "state.json")
        anchor._require(before["phase"] in {"starting", "running"})
        generation = before["generation"]
        anchor._require(generation is not None and generation["token"] == token and generation["pid"] == os.getpid())
        service = anchor.inspect_service()
        anchor._require(service.pid == os.getppid() and before["binding"]["service"] == service.to_json())
        anchor._require(before["binding"]["machine"] == current_recovery_identity().to_json())
        profiles = anchor._observe_layout(service)
        anchor._require([p.to_json() for p in profiles] == before["binding"]["layout"])
        anchor._require(worker_root == profiles[3].root)
        storage = before["binding"]["storage"]
        anchor._require(list(private._identity(private._stat(root, directory=True))) == storage["profile"])
        anchor._require(list(private._identity(private._stat(directory, directory=True))) == storage["directory"])
        anchor._require(list(anchor._lock_identity((root / "anchor-state.lock").lstat())) == storage["lease"])
        resources = read_resources(root, before["binding"])
        # The ordinary launcher must not become an owner if the anchor's
        # lifetime exclusion has disappeared. No lease handoff/unlocked gap.
        descriptor = os.open(root / "node-lifetime.lock", os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            identity = anchor._lock_identity(os.fstat(descriptor))
            anchor._require(identity == anchor._lock_identity((root / "node-lifetime.lock").lstat()))
            anchor._require(list(identity) == resources[0]["identities"]["lifetime"])
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise RecoverableStateError()
        finally:
            os.close(descriptor)
        profile = anchor.cg.LinuxCgroupProfile.from_json(generation["cgroup"])
        anchor._require(anchor.cg.validate_cgroup_profile(profile.root) == profile)
        ticks, group = anchor._process(os.getpid())
        anchor._require(ticks == generation["start_ticks"])
        anchor._require(group == service.control_group + "/nodes/node-" + generation["id"])
        after = _read(directory / "state.json")
        anchor._require(after["binding"] == before["binding"] and after["generation"] == generation)
        anchor._require(after["phase"] in {"starting", "running"})
        anchor._require(read_resources(root, before["binding"]) == resources)
        anchor._require(anchor.inspect_service() == service and anchor._observe_layout(service) == profiles)
        _ADMITTED = (os.getpid(), root, worker_root, before["binding"], resources, generation)
        return generation["id"]
    except Exception:
        raise RecoverableStateError() from None


def reservation_storage_binding(directory, worker_root):
    """Carry exact entry evidence into every node-manager instance/reload.

    Ordinary non-volunteer CLI has no admitted entry. An admitted child cannot fall
    back to first-use initialization, even if storage vanishes before first lock.
    The returned IDs are pinned again by the manager at every lock acquisition."""
    if _ADMITTED is None:
        return None
    try:
        pid, root, workers, binding, observed, _generation = _ADMITTED
        anchor._require(os.getpid() == pid and worker_root == workers)
        anchor._require(Path(directory).absolute() == root / "node" / "resource-reservations")
        anchor._require(read_resources(root, binding) == observed)
        identities = observed[0]["identities"]
        return dict(directory=tuple(identities["journal"]), lease=tuple(identities["admission"]))
    except Exception:
        raise RecoverableStateError() from None


def admitted_control_identity():
    """Private same-process launch evidence, never a CLI/env/config assertion."""
    if _ADMITTED is None:
        return None
    from communityai_anchor.linux_node_identity import make_identity

    pid, root, workers, binding, observed, generation = _ADMITTED
    anchor._require(os.getpid() == pid)
    reservation_storage_binding(root / "node" / "resource-reservations", workers)
    current = _read(root / "anchor" / "state.json")
    anchor._require(current["binding"] == binding and current["generation"] == generation)
    anchor._require(current["phase"] in {"starting", "running"})
    service = anchor.inspect_service()
    anchor._require(service.to_json() == binding["service"])
    anchor._require([p.to_json() for p in anchor._observe_layout(service)] == binding["layout"])
    ticks, group = anchor._process(pid)
    anchor._require(ticks == generation["start_ticks"])
    anchor._require(group == binding["service"]["control_group"] + "/nodes/node-" + generation["id"])
    profile = anchor.cg.LinuxCgroupProfile.from_json(generation["cgroup"])
    anchor._require(anchor.cg.validate_cgroup_profile(profile.root) == profile)
    return make_identity(binding, generation)


def inspect_profile_entry(profile_root, receipt):
    """Read-only desktop bootstrap boundary; missing evidence never creates it."""
    try:
        return _inspect_profile_entry(profile_root, receipt)
    except Exception:
        raise RecoverableStateError() from None


def _inspect_profile_entry(profile_root, receipt):
    import fcntl

    from communityai_anchor.linux_node_identity import digest, make_identity

    root = private._directory(profile_root)
    directory = private._directory(root / "anchor")
    before = _read(directory / "state.json")
    binding = before["binding"]
    anchor._require(binding["service"] == receipt["service"])
    anchor._require(digest(binding["layout"]) == receipt["layout_digest"])
    anchor._require(before["revision"] == receipt["node"]["revision"])
    anchor._require(before["phase"] == receipt["node"]["phase"])
    anchor._require(make_identity(binding, before["generation"]) == receipt["node"]["api_identity"])
    storage = binding["storage"]
    anchor._require(list(private._identity(private._stat(root, directory=True))) == storage["profile"])
    anchor._require(list(private._identity(private._stat(directory, directory=True))) == storage["directory"])
    anchor._require(list(anchor._lock_identity((root / "anchor-state.lock").lstat())) == storage["lease"])
    resources = read_resources(root, binding)
    descriptor = os.open(root / "node-lifetime.lock", os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        identity = anchor._lock_identity(os.fstat(descriptor))
        anchor._require(list(identity) == resources[0]["identities"]["lifetime"])
        anchor._require(identity == anchor._lock_identity((root / "node-lifetime.lock").lstat()))
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise RecoverableStateError()
    finally:
        os.close(descriptor)
    anchor._require(_read(directory / "state.json") == before)
    anchor._require(read_resources(root, binding) == resources)
    return binding, resources
