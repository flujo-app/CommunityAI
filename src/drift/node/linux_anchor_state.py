"""Private durable anchor intent; not node-launch or maintenance authority.

Single-owner, serialized API. Callers must prove lifecycle transitions themselves.
An uncertain write or changed identity permanently poisons this owner. Reopening
requires the same live service invocation and exact storage/cgroup identities;
replacement-anchor, reboot and lost-state recovery are separate transactions.
Same-UID code is cooperative, not sandboxed. Lifecycle composition is separate.
"""

from __future__ import annotations

import copy
import os
import re
import threading
from functools import wraps
from pathlib import Path

from drift.node import linux_anchor as anchor
from drift.node import worker_loading as private
from drift.node.resource_recovery import RecoverableStateError, RecoveryIdentity, current_recovery_identity

_PHASES = {"checking", "idle", "starting", "running", "draining", "blocked"}
_NAMES = {"anchor-state.lock", "node-lifetime.lock"}


def _hex(value):
    return type(value) is str and re.fullmatch("[0-9a-f]{32}", value) is not None


def _integer(value, minimum=0):
    return type(value) is int and minimum <= value < 2**63


def _identity(value):
    return (
        type(value) is list
        and len(value) == 2
        and all(type(n) is int and 0 <= n < 2**64 for n in value)
        and value[1] > 0
    )


def _sync_directory(path, identity):
    fd = anchor.cg._open_root(str(path))
    try:
        anchor._require(anchor.cg._identity(fd) == identity)
        os.fsync(fd)
    finally:
        os.close(fd)


class PrivateLease:
    """Nonblocking lifetime flock on a fixed private marker, never unlinked."""

    def __init__(self, root, name, *, create=True):
        self.fd = None
        self.created = False
        try:
            anchor.cg._platform()
            import fcntl

            anchor._require(type(name) is str and name in _NAMES)
            anchor._require(type(create) is bool)
            self.root = private._directory(root)
            self.path = self.root / name
            self.root_identity = private._identity(private._stat(self.root, directory=True))
            if create:
                try:
                    self.fd = os.open(
                        self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
                    )
                    self.created = True
                except FileExistsError:
                    self.fd = os.open(self.path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
            else:
                self.fd = os.open(self.path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
            self.identity = anchor._lock_identity(os.fstat(self.fd))
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.validate()
            if self.created:
                os.fsync(self.fd)
                _sync_directory(self.root, self.root_identity)
        except BaseException as error:
            self.close()
            if isinstance(error, Exception):
                raise RecoverableStateError(
                    "active_owner" if isinstance(error, BlockingIOError) else "unverifiable_state"
                ) from None
            raise

    def validate(self):
        try:
            anchor._require(self.fd is not None)
            anchor._require(private._identity(private._stat(self.root, directory=True)) == self.root_identity)
            private._directory(self.root)
            anchor._require(anchor._lock_identity(os.fstat(self.fd)) == self.identity)
            anchor._require(anchor._lock_identity(self.path.lstat()) == self.identity)
        except Exception:
            raise RecoverableStateError() from None

    def close(self):
        if self.fd is not None:
            descriptor, self.fd = self.fd, None
            os.close(descriptor)


def node_lease(profile_root, *, create=True):
    """Lifetime exclusion; an existing lifecycle must pass create=False."""
    return PrivateLease(profile_root, "node-lifetime.lock", create=create)


def validate_state(value):
    """Strict bounded codec. A valid intent is never evidence of cleanup."""
    try:
        anchor._require(
            type(value) is dict
            and set(value)
            == {
                "schema_version",
                "profile",
                "binding",
                "revision",
                "phase",
                "generation",
                "request_id",
                "operation",
            }
        )
        anchor._require(type(value["schema_version"]) is int and value["schema_version"] == 1)
        anchor._require(value["profile"] == anchor.PROFILE and _integer(value["revision"]))
        anchor._require(type(value["phase"]) is str and value["phase"] in _PHASES)
        anchor._require(
            value["operation"] is None or (type(value["operation"]) is str and value["operation"] in {"start", "drain"})
        )
        anchor._require(value["request_id"] is None or _hex(value["request_id"]))
        anchor._require((value["request_id"] is None) == (value["operation"] is None))
        binding = value["binding"]
        anchor._require(type(binding) is dict and set(binding) == {"service", "machine", "layout", "storage"})
        service = binding["service"]
        anchor._require(
            type(service) is dict and set(service) == {"pid", "uid", "start_ticks", "invocation", "control_group"}
        )
        anchor._require(_integer(service["pid"], 2) and service["pid"] < 2**31 and _integer(service["uid"]))
        anchor._require(
            _integer(service["start_ticks"], 1) and _hex(service["invocation"]) and service["invocation"] != "0" * 32
        )
        anchor._require(anchor.cg._path(service["control_group"]))
        anchor._require(RecoveryIdentity.from_json(binding["machine"]).platform == "linux")
        storage = binding["storage"]
        anchor._require(type(storage) is dict and set(storage) == {"profile", "directory", "lease"})
        anchor._require(all(_identity(identity) for identity in storage.values()))
        anchor._require(type(binding["layout"]) is list and len(binding["layout"]) == 4)
        profiles = [anchor.cg.LinuxCgroupProfile.from_json(item) for item in binding["layout"]]
        for index, profile in enumerate(profiles):
            anchor._require(profile.uid == service["uid"])
            anchor._require(profile.root_identity[0] == profiles[0].root_identity[0])
            if index:
                base = profiles[0].to_json()
                base.update(
                    root=profiles[0].root + "/" + anchor._CHILDREN[index - 1], root_identity=list(profile.root_identity)
                )
                anchor._require(profile.to_json() == base and profile.root_identity != profiles[0].root_identity)
        anchor._require(len({profile.root_identity for profile in profiles}) == 4)
        generation = value["generation"]
        if generation is not None:
            anchor._require(
                type(generation) is dict and set(generation) == {"id", "token", "cgroup", "pid", "start_ticks"}
            )
            anchor._require(_hex(generation["id"]) and _hex(generation["token"]))
            if generation["cgroup"] is not None:
                profile = anchor.cg.LinuxCgroupProfile.from_json(generation["cgroup"])
                expected = profiles[2].to_json()
                expected.update(
                    root=profiles[2].root + "/node-" + generation["id"], root_identity=list(profile.root_identity)
                )
                anchor._require(
                    profile.to_json() == expected and profile.root_identity not in {p.root_identity for p in profiles}
                )
                anchor._require(profile.root_identity[0] == profiles[2].root_identity[0])
            anchor._require((generation["pid"] is None) == (generation["start_ticks"] is None))
            if generation["pid"] is not None:
                anchor._require(_integer(generation["pid"], 2) and generation["pid"] < 2**31)
                anchor._require(_integer(generation["start_ticks"], 1) and generation["cgroup"] is not None)
        anchor._require(value["phase"] not in {"starting", "running"} or generation is not None)
        anchor._require(value["phase"] != "running" or generation["pid"] is not None)
        anchor._require(value["phase"] != "checking" or (generation is None and value["request_id"] is None))
        private._encode(value)  # Includes the same on-disk size and JSON bounds.
        return value
    except Exception:
        raise RecoverableStateError() from None


def _serialized(method):
    @wraps(method)
    def held(self, *args, **kwargs):
        with self._mutex:
            return method(self, *args, **kwargs)

    return held


class AnchorState:
    """Durable intent journal under a separate profile-level initialization marker."""

    def __init__(self, profile_root, layout, *, initialize=False):
        self._mutex = threading.RLock()
        self.lease = None
        self.poisoned = False
        self.layout = layout
        self.path = Path(profile_root) / "anchor" / "state.json"
        try:
            layout.validate()
            anchor._require(type(initialize) is bool)
            private._directory(profile_root)
            if initialize:
                # Explicit first-install transaction only, before any profile
                # configuration/work exists. Never infer permission from loss.
                anchor._require(not os.listdir(profile_root))
            self.lease = PrivateLease(profile_root, "anchor-state.lock", create=initialize)
            anchor._require(self.lease.created == initialize)
            if initialize:
                anchor._require(set(os.listdir(profile_root)) == {"anchor-state.lock"})
                self.path.parent.mkdir(mode=0o700)
                _sync_directory(self.lease.root, self.lease.root_identity)
            private._directory(self.path.parent)
            self.directory_identity = private._identity(private._stat(self.path.parent, directory=True))
            self.binding = dict(
                service=layout.service.to_json(),
                machine=current_recovery_identity().to_json(),
                layout=[profile.to_json() for profile in layout.profiles],
                storage=dict(
                    profile=list(self.lease.root_identity),
                    directory=list(self.directory_identity),
                    lease=list(self.lease.identity),
                ),
            )
            if self.lease.created:
                value = validate_state(
                    dict(
                        schema_version=1,
                        profile=anchor.PROFILE,
                        binding=self.binding,
                        revision=0,
                        phase="checking",
                        generation=None,
                        request_id=None,
                        operation=None,
                    )
                )
                private._exclusive(self.path, value)
                _sync_directory(self.path.parent, self.directory_identity)
            self._value, self._fingerprint = self._read()
            anchor._require(self._value["binding"] == self.binding)
            # Complete durability even when the previous owner exited after a
            # replace but before receiving its fsync acknowledgement. This is
            # still only intent, never an acknowledgement of actual cleanup.
            descriptor = os.open(self.path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            try:
                anchor._require(private._fingerprint(os.fstat(descriptor)) == self._fingerprint)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            _sync_directory(self.path.parent, self.directory_identity)
            _sync_directory(self.lease.root, self.lease.root_identity)
            self.validate()
        except BaseException as error:
            self.close()
            if isinstance(error, Exception):
                raise RecoverableStateError() from None
            raise

    @property
    @_serialized
    def value(self):
        return copy.deepcopy(self._value)

    def _read(self):
        before = private._fingerprint(private._stat(self.path))
        value = validate_state(private._read(self.path))
        anchor._require(private._fingerprint(private._stat(self.path)) == before)
        return value, before

    @_serialized
    def validate(self):
        try:
            anchor._require(not self.poisoned and self.lease is not None)
            self.lease.validate()
            private._directory(self.path.parent)
            anchor._require(
                private._identity(private._stat(self.path.parent, directory=True)) == self.directory_identity
            )
            self.layout.validate()
            anchor._require(self.layout.service.to_json() == self.binding["service"])
            anchor._require([p.to_json() for p in self.layout.profiles] == self.binding["layout"])
            anchor._require(current_recovery_identity().to_json() == self.binding["machine"])
            value, fingerprint = self._read()
            anchor._require(value == self._value and fingerprint == self._fingerprint)
        except BaseException as error:
            self.poisoned = True
            if isinstance(error, Exception):
                raise RecoverableStateError() from None
            raise

    @_serialized
    def write(self, expected_revision, **changes):
        self.validate()
        # Invalid/stale requests have no effects and do not poison the journal.
        anchor._require(_integer(expected_revision) and expected_revision == self._value["revision"])
        anchor._require(set(changes) <= {"phase", "generation", "request_id", "operation"})
        value = validate_state({**self._value, **copy.deepcopy(changes), "revision": expected_revision + 1})
        try:
            private._replace(self.path, value)
            _sync_directory(self.path.parent, self.directory_identity)
            observed, fingerprint = self._read()
            anchor._require(observed == value)
            self._value, self._fingerprint = observed, fingerprint
            self.validate()
        except BaseException as error:
            self.poisoned = True
            if isinstance(error, Exception):
                raise RecoverableStateError() from None
            raise
        return self.value

    @_serialized
    def close(self):
        if self.lease is not None:
            self.lease.close()
            self.lease = None
