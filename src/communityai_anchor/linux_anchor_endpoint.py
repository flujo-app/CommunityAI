"""Durable fixed-socket provenance under the service's held channel lease.

This is cooperative same-UID exclusion, not a sandbox. The caller must prove
service death and hold profile ownership before clearing another invocation's
endpoint. A missing record is never synthesized for an existing profile.
"""

from __future__ import annotations

import copy
import os
from uuid import uuid4

from communityai_anchor import linux_anchor as anchor, worker_loading as private
from communityai_anchor.linux_anchor_state import _sync_directory


def _identity(value):
    return (
        type(value) is list
        and len(value) == 2
        and all(type(n) is int and 0 <= n < 2**64 for n in value)
        and value[1] > 0
    )


def validate_endpoint(value):
    anchor._require(
        type(value) is dict
        and set(value) == {"schema_version", "binding", "nonce", "directory", "lease", "phase", "socket"}
        and type(value["schema_version"]) is int
        and value["schema_version"] == 1
        and type(value["binding"]) is str
        and anchor.re.fullmatch("[0-9a-f]{64}", value["binding"]) is not None
        and type(value["nonce"]) is str
        and anchor.re.fullmatch("[0-9a-f]{32}", value["nonce"]) is not None
        and _identity(value["directory"])
        and _identity(value["lease"])
        and type(value["phase"]) is str
        and value["phase"] in {"pending", "bound", "clearing", "retired"}
    )
    anchor._require(value["socket"] is None or _identity(value["socket"]))
    anchor._require(value["phase"] != "bound" or value["socket"] is not None)
    anchor._require(value["phase"] not in {"pending", "retired"} or value["socket"] is None)
    private._encode(value)
    return value


class EndpointFence:
    """One existing fixed record, never a socket-path or reset capability."""

    def __init__(self, profile_root, lease, *, guard):
        anchor._require(type(lease) is anchor.AnchorChannelLease and callable(guard))
        self.root = private._directory(profile_root)
        self.directory = private._directory(self.root / "anchor")
        self.directory_identity = private._identity(private._stat(self.directory, directory=True))
        self.path = self.directory / "endpoint.json"
        self.lease, self.guard = lease, guard
        self.poisoned = False
        self.value = validate_endpoint(private._read(self.path))
        self.fingerprint = private._fingerprint(private._stat(self.path))
        self.validate()

    @classmethod
    def create(cls, profile_root, lease, binding, *, guard):
        guard()
        lease.validate()
        directory = private._directory(profile_root / "anchor")
        identity = private._identity(private._stat(directory, directory=True))
        anchor._require(not os.path.lexists(lease.directory / "control.sock"))
        value = validate_endpoint(
            dict(
                schema_version=1,
                binding=binding,
                nonce=uuid4().hex,
                directory=list(lease._directory_identity),
                lease=list(lease._lock_identity),
                phase="pending",
                socket=None,
            )
        )
        private._exclusive(directory / "endpoint.json", value)
        _sync_directory(directory, identity)
        guard()
        return cls(profile_root, lease, guard=guard)

    def validate(self):
        anchor._require(not self.poisoned)
        self.guard()
        self.lease.validate()
        anchor._require(private._directory(self.root / "anchor") == self.directory)
        anchor._require(private._identity(private._stat(self.directory, directory=True)) == self.directory_identity)
        anchor._require(private._fingerprint(private._stat(self.path)) == self.fingerprint)
        anchor._require(private._read(self.path) == self.value)
        validate_endpoint(self.value)
        anchor._require(self.value["directory"] == list(self.lease._directory_identity))
        anchor._require(self.value["lease"] == list(self.lease._lock_identity))

    def _write(self, value):
        self.validate()
        validate_endpoint(value)
        try:
            private._replace(self.path, value)
            _sync_directory(self.directory, self.directory_identity)
            anchor._require(private._read(self.path) == value)
            self.value = copy.deepcopy(value)
            self.fingerprint = private._fingerprint(private._stat(self.path))
            self.validate()
        except BaseException:
            self.poisoned = True
            raise

    def reserve(self, binding):
        self.validate()
        anchor._require(self.value["phase"] == "retired")
        anchor._require(not os.path.lexists(self.lease.directory / "control.sock"))
        self._write(dict(self.value, binding=binding, nonce=uuid4().hex, phase="pending", socket=None))

    def record_bound(self, channel):
        self.validate()
        anchor._require(self.value["phase"] == "pending" and channel.lease is self.lease)
        anchor._require(channel.path == self.lease.directory / "control.sock")
        identity = anchor._socket_identity(channel.path)
        anchor._require(channel._identity == identity and channel.path.lstat().st_nlink == 1)
        self._write(dict(self.value, phase="bound", socket=list(identity)))

    def require_bound(self, binding, channel):
        self.validate()
        anchor._require(self.value["phase"] == "bound" and self.value["binding"] == binding)
        anchor._require(channel.lease is self.lease and list(channel._identity) == self.value["socket"])
        anchor._require(list(anchor._socket_identity(channel.path)) == self.value["socket"])
        return True

    def clear(self, *, channel=None):
        """Caller proves old service death, or supplies its own live channel.

        The pending phase reserves this exact private fixed name before bind.
        Only that durable reservation can reconcile death before inode capture.
        A bound socket disappearing without clearing intent remains a failure.
        """
        self.validate()
        path = self.lease.directory / "control.sock"
        if channel is not None:
            anchor._require(channel.lease is self.lease and channel.path == path)
        exists = os.path.lexists(path)
        if self.value["phase"] == "retired":
            anchor._require(not exists)
            return
        observed = list(anchor._socket_identity(path)) if exists else None
        if exists:
            anchor._require(path.lstat().st_nlink == 1)
        if self.value["phase"] == "bound":
            anchor._require(observed == self.value["socket"])
        elif self.value["phase"] == "clearing":
            anchor._require(observed is None or observed == self.value["socket"])
        else:
            anchor._require(self.value["phase"] == "pending")
        if self.value["phase"] != "clearing":
            self._write(dict(self.value, phase="clearing", socket=observed))
        self.validate()
        if channel is not None:
            channel.close()
        if os.path.lexists(path):
            anchor._require(list(anchor._socket_identity(path)) == self.value["socket"])
            descriptor = anchor.cg._open_root(str(self.lease.directory))
            try:
                anchor._require(anchor.cg._identity(descriptor) == self.lease._directory_identity)
                anchor._require(list(anchor._socket_identity(path)) == self.value["socket"])
                os.unlink("control.sock", dir_fd=descriptor)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        _sync_directory(self.lease.directory, self.lease._directory_identity)
        self.validate()
        anchor._require(not os.path.lexists(path))
        self._write(dict(self.value, phase="retired", socket=None))
