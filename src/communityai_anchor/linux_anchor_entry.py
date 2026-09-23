"""Exact durable child admission for the fixed Linux volunteer launcher.

The anchor retains the profile lease. Children receive no general launch grant:
a token only matches one persisted PID/start identity in one native node leaf.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

from communityai_anchor import linux_anchor as anchor, linux_anchor_state as state, worker_loading as private
from communityai_anchor.linux_anchor_resources import read_resources
from communityai_anchor.resource_recovery import RecoverableStateError, current_recovery_identity

NODE_TOKEN_ENV = "COMMUNITYAI_ANCHOR_NODE_TOKEN"
_ADMITTED = None
_ADMITTED_CATALOG = None

_BOOTSTRAP_V1_FIELDS = frozenset(
    {
        "schema_version",
        "binding",
        "transaction",
        "bundle",
        "plan",
        "directories",
        "locks",
        "service",
        "account",
        "attempt",
        "admitted_at_ms",
        "progress",
        "pending",
        "credential",
        "credential_digest",
        "ready",
    }
)
_BOOTSTRAP_V2_FIELDS = _BOOTSTRAP_V1_FIELDS | {"catalog_binding"}


def catalog_discriminator(root, binding_digest):
    return dict(purpose="communityai-anchor-catalog", profile=str(root), binding=binding_digest)


def bootstrap_catalog_binding(value):
    """Return the immutable catalog discriminator from a strict record shape."""
    anchor._require(type(value) is dict and type(value.get("schema_version")) is int)
    version = value["schema_version"]
    anchor._require(
        (version == 1 and set(value) == _BOOTSTRAP_V1_FIELDS) or (version == 2 and set(value) == _BOOTSTRAP_V2_FIELDS)
    )
    binding = value["binding"]
    anchor._require(type(binding) is str and anchor.re.fullmatch("[0-9a-f]{64}", binding) is not None)
    if version == 1:
        return binding
    catalog_binding = value["catalog_binding"]
    anchor._require(type(catalog_binding) is str and anchor.re.fullmatch("[0-9a-f]{64}", catalog_binding) is not None)
    return catalog_binding


def anchored_catalog_paths(data_dir, config_path):
    """Read-only ownership detection shared by every catalog/config writer."""
    if not sys.platform.startswith("linux"):
        return False
    data_dir, config_path = Path(data_dir).absolute(), Path(config_path).absolute()
    fixed = Path.home() / ".communityai" / "multigpu-volunteer"
    parents = set((*data_dir.parents, *config_path.parents))
    root = data_dir.parent
    anchored = (
        _ADMITTED is not None
        or fixed in parents
        or (
            data_dir.name == "node"
            and all(os.path.lexists(root / name) for name in ("anchor/bootstrap.json", "anchor-state.lock"))
        )
    )
    # An in-node discriminator survives a bind mount that hides root markers.
    # Scan before mkdir, including nested linked spellings. Ordinary empty
    # sidecars and unrelated root-level lock filenames confer no authority.
    for parent in {data_dir, *parents}:
        marker = parent / ".catalog-bootstrap.lock"
        if os.path.lexists(marker) and marker.lstat().st_size:
            value = private._read(marker)
            anchored = anchored or value.get("purpose") == "communityai-anchor-catalog"
    return anchored


def _catalog_entry(root, binding, generation):
    """Pin bootstrap evidence at entry, never adopt it on the first refresh."""
    path = root / "anchor" / "bootstrap.json"
    # Internal controllers without first-use preparation cannot write catalogs.
    if not os.path.lexists(path):
        return None
    fingerprint = private._fingerprint(private._stat(path))
    value = private._read(path)
    anchor._require(private._fingerprint(private._stat(path)) == fingerprint)
    bootstrap_catalog_binding(value)
    anchor._require(value["ready"] is True)
    anchor._require(value["binding"] == hashlib.sha256(private._encode(binding) + b"\n").hexdigest())
    anchor._require(value["attempt"]["generation"] == generation["id"])
    anchor._require(value["credential"] == "ready" and value["pending"] is False)
    anchor._require(set(value["locks"]) == {"node/.catalog-bootstrap.lock", "node/.node-config.json.write.lock"})
    proof = (root, fingerprint, value)
    _validate_catalog_entry(proof)
    return proof


def _validate_catalog_entry(proof):
    root, fingerprint, value = proof
    path = root / "anchor" / "bootstrap.json"
    anchor._require(private._fingerprint(private._stat(path)) == fingerprint)
    anchor._require(private._read(path) == value)
    for name, identity in value["directories"].items():
        anchor._require(
            name in {"anchor", "node", "node/catalogs", "node/manifests"}
            or (name.startswith("node/catalogs/") and len(Path(name).parts) == 3 and ".." not in Path(name).parts)
        )
        private._directory(root / name)
        anchor._require(list(private._identity(private._stat(root / name, directory=True))) == identity)
    anchor._require({"anchor", "node", "node/catalogs", "node/manifests"} <= set(value["directories"]))
    for name, identity in value["locks"].items():
        anchor._require(list(anchor._lock_identity((root / name).lstat())) == identity)
    anchor._require(
        private._read(root / "node" / ".catalog-bootstrap.lock")
        == catalog_discriminator(root, bootstrap_catalog_binding(value))
    )


def _read(path):
    for attempt in range(4):
        try:
            return state.validate_state(private._read(path))
        except private._FileReplaced:
            if attempt == 3:
                raise RecoverableStateError() from None


def validate_node_entry(profile_root, token, worker_root):
    """Before profile mutation/keyring/runtime; no standalone Linux fallback."""
    global _ADMITTED, _ADMITTED_CATALOG
    _ADMITTED = None
    _ADMITTED_CATALOG = None
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
        catalog = _catalog_entry(root, before["binding"], generation)
        _ADMITTED = (os.getpid(), root, worker_root, before["binding"], resources, generation)
        _ADMITTED_CATALOG = catalog
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


def require_admitted_catalog_writer(data_dir, config_path):
    """A generic standalone catalog CLI cannot borrow an anchor's file paths."""
    anchor._require(_ADMITTED is not None)
    root = _ADMITTED[1]
    anchor._require(Path(data_dir).absolute() == root / "node")
    anchor._require(Path(config_path).absolute() == root / "node" / "node-config.json")
    anchor._require(admitted_control_identity() is not None)
    anchor._require(_ADMITTED_CATALOG is not None and _ADMITTED_CATALOG[0] == root)
    _validate_catalog_entry(_ADMITTED_CATALOG)
    return {name: tuple(identity) for name, identity in _ADMITTED_CATALOG[2]["locks"].items()}


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
