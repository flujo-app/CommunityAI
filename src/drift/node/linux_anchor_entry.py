"""Exact durable child admission for the fixed Linux volunteer launcher.

The anchor retains the profile lease. Children receive no general launch grant:
a token only matches one persisted PID/start identity in one native node leaf.
"""

from __future__ import annotations

import os
from pathlib import Path

from drift.node import linux_anchor as anchor
from drift.node import linux_anchor_state as state
from drift.node import worker_loading as private
from drift.node.linux_anchor_resources import read_resources
from drift.node.resource_recovery import RecoverableStateError, current_recovery_identity

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
        _ADMITTED = (os.getpid(), root, worker_root, before["binding"], resources)
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
        pid, root, workers, binding, observed = _ADMITTED
        anchor._require(os.getpid() == pid and worker_root == workers)
        anchor._require(Path(directory).absolute() == root / "node" / "resource-reservations")
        anchor._require(read_resources(root, binding) == observed)
        identities = observed[0]["identities"]
        return dict(directory=tuple(identities["journal"]), lease=tuple(identities["admission"]))
    except Exception:
        raise RecoverableStateError() from None
