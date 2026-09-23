"""Immutable lifecycle storage binding, established only by explicit bootstrap.

This retains loss evidence for the node lease and journal directory chain. It
does not authorize migration, deletion, replacement-anchor or reboot recovery.
"""

from drift.node import linux_anchor as anchor
from drift.node import worker_loading as private
from drift.node.linux_anchor_state import _sync_directory


def identities(root):
    result = {}
    for key, path in (("node", root / "node"), ("journal", root / "node" / "resource-reservations")):
        private._directory(path)
        result[key] = list(private._identity(private._stat(path, directory=True)))
    for key, path in (
        ("lifetime", root / "node-lifetime.lock"),
        ("admission", root / "node" / "resource-reservations" / "admission.lock"),
    ):
        result[key] = list(anchor._lock_identity(path.lstat()))
    return result


def read_resources(root, binding):
    path = root / "anchor" / "resources.json"
    before = private._fingerprint(private._stat(path))
    value = private._read(path)
    anchor._require(type(value) is dict and set(value) == {"version", "binding", "identities"})
    anchor._require(type(value["version"]) is int and value["version"] == 1)
    anchor._require(value["binding"] == binding and value["identities"] == identities(root))
    anchor._require(private._fingerprint(private._stat(path)) == before)
    return value, before


def create_resources(root, binding):
    value = dict(version=1, binding=binding, identities=identities(root))
    private._exclusive(root / "anchor" / "resources.json", value)
    _sync_directory(root / "anchor", tuple(binding["storage"]["directory"]))
    return read_resources(root, binding)


def create_directories(root, binding):
    (root / "node").mkdir(mode=0o700)
    _sync_directory(root, tuple(binding["storage"]["profile"]))
    (root / "node" / "resource-reservations").mkdir(mode=0o700)
    _sync_directory(root / "node", private._identity(private._stat(root / "node", directory=True)))
