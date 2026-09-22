"""Private owner exclusion and exact, durable reservation recovery authority.

No PID, loading acknowledgement or elapsed time authorizes resource release.
The caller keeps an owner lease for its entire operational lifetime and keeps
the recovery guard through containment proof, loading cleanup and journal commit.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from drift.node import worker_loading as _private

_MAX_OWNERS = 4096
_AUTHORITY = object()
_REASONS = {
    "active_owner": "previous resource owner is still active",
    "cleanup_pending": "previous worker cleanup is incomplete",
    "legacy_state": "legacy resource state requires verified cleanup",
    "unverifiable_state": "resource recovery state cannot be verified",
    "unsupported_platform": "resource recovery is unavailable for this platform state",
}


class RecoverableStateError(RuntimeError):
    def __init__(self, reason="unverifiable_state"):
        self.reason = reason if isinstance(reason, str) and reason in _REASONS else "unverifiable_state"
        super().__init__(_REASONS[self.reason])


def _require(condition, reason="unverifiable_state"):
    if not condition:
        raise RecoverableStateError(reason)


def _hex(value, size):
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{%d}" % size, value) is not None


def _digest(value):
    return isinstance(value, str) and value.startswith("sha256:") and _hex(value[7:], 64)


def _native_identity(value):
    return (
        type(value) is tuple
        and len(value) == 2
        and all(type(item) is int and 0 <= item < 2**128 for item in value)
        and value[1] != 0
    )


def _mapping(value, keys):
    _require(type(value) is dict and set(value) == set(keys))


def _version(value):
    _require(type(value) is int and value == 1)


def _cancel(cancelled):
    if cancelled is not None:
        _require(callable(cancelled) and not cancelled(), "cleanup_pending")


@dataclass(frozen=True)
class RecoveryIdentity:
    platform: str
    host_id: str = field(repr=False)
    boot_id: str | None = field(repr=False)

    def __post_init__(self):
        _require(self.platform in ("windows", "linux"), "unsupported_platform")
        _require(_digest(self.host_id))
        if self.platform == "windows":
            _require(self.boot_id is None)
        else:
            _require(isinstance(self.boot_id, str))
            try:
                _require(str(UUID(self.boot_id)) == self.boot_id)
            except (TypeError, ValueError, AttributeError):
                raise RecoverableStateError() from None

    def to_json(self):
        self.__post_init__()
        return {"platform": self.platform, "host_id": self.host_id, "boot_id": self.boot_id}

    @classmethod
    def from_json(cls, value):
        _mapping(value, ("platform", "host_id", "boot_id"))
        return cls(**value)


def current_recovery_identity():
    """Read native host identity, and the exact Linux kernel boot UUID.

    Host identifiers are hashed before persistence. Windows does not use a
    wall-clock estimate of reboot: its named job supplies recovery evidence.
    """
    try:
        if sys.platform == "win32":
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
                access=winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            ) as key:
                machine, kind = winreg.QueryValueEx(key, "MachineGuid")
            _require(kind == winreg.REG_SZ and isinstance(machine, str) and len(machine) <= 64)
            machine = str(UUID(machine))
            platform, boot_id = "windows", None
        elif sys.platform.startswith("linux"):
            with open("/etc/machine-id", "rb") as stream:
                machine = stream.read(129).decode("ascii").strip()
            _require(_hex(machine, 32))
            with open("/proc/sys/kernel/random/boot_id", "rb") as stream:
                boot_id = stream.read(129).decode("ascii").strip()
            platform = "linux"
        else:
            raise RecoverableStateError("unsupported_platform")
        host_id = "sha256:" + hashlib.sha256((platform + ":" + machine).encode("ascii")).hexdigest()
        return RecoveryIdentity(platform, host_id, boot_id)
    except RecoverableStateError:
        raise
    except Exception:
        raise RecoverableStateError() from None


@dataclass(frozen=True)
class OwnerBinding:
    owner_id: str = field(repr=False)
    identity: RecoveryIdentity = field(repr=False)
    lease_identity: tuple[int, int] = field(repr=False)
    directory_identity: tuple[int, int] = field(repr=False)

    def __post_init__(self):
        _require(_hex(self.owner_id, 32) and type(self.identity) is RecoveryIdentity)
        self.identity.__post_init__()
        _require(_native_identity(self.lease_identity) and _native_identity(self.directory_identity))

    @property
    def lease_name(self):
        return self.owner_id + ".lease"

    @property
    def descriptor_name(self):
        return self.owner_id + ".owner.json"

    def to_json(self):
        self.__post_init__()
        return dict(
            schema_version=1,
            owner_id=self.owner_id,
            identity=self.identity.to_json(),
            lease_identity=list(self.lease_identity),
            directory_identity=list(self.directory_identity),
        )

    @classmethod
    def from_json(cls, value):
        _mapping(value, ("schema_version", "owner_id", "identity", "lease_identity", "directory_identity"))
        _version(value["schema_version"])
        _require(type(value["lease_identity"]) is list and type(value["directory_identity"]) is list)
        return cls(
            value["owner_id"],
            RecoveryIdentity.from_json(value["identity"]),
            tuple(value["lease_identity"]),
            tuple(value["directory_identity"]),
        )


@dataclass(frozen=True)
class GenerationRecoveryBinding:
    owner: OwnerBinding = field(repr=False)
    reservation_id: str = field(repr=False)
    kind: str
    contract: str
    containment_name: str | None = field(repr=False)
    claim_digest: str = field(repr=False)

    def __post_init__(self):
        _require(type(self.owner) is OwnerBinding and _hex(self.reservation_id, 32) and _digest(self.claim_digest))
        self.owner.__post_init__()
        _require(self.kind in ("worker", "metadata"))
        contract, name = _contract(self.owner, self.reservation_id, self.kind)
        _require(self.contract == contract and self.containment_name == name)

    def to_json(self):
        self.__post_init__()
        return dict(
            schema_version=1,
            owner=self.owner.to_json(),
            reservation_id=self.reservation_id,
            kind=self.kind,
            contract=self.contract,
            containment_name=self.containment_name,
            claim_digest=self.claim_digest,
        )

    @classmethod
    def from_json(cls, value):
        if value is None:
            raise RecoverableStateError("legacy_state")
        _mapping(
            value, ("schema_version", "owner", "reservation_id", "kind", "contract", "containment_name", "claim_digest")
        )
        _version(value["schema_version"])
        return cls(
            OwnerBinding.from_json(value["owner"]),
            value["reservation_id"],
            value["kind"],
            value["contract"],
            value["containment_name"],
            value["claim_digest"],
        )


def _contract(owner, reservation_id, kind):
    if kind == "metadata":
        return "synchronous_metadata_v1", None
    if owner.identity.platform == "windows":
        return "windows_job_atomic_v1", "Global\\CommunityAI-" + owner.owner_id + "-" + reservation_id
    return "linux_boot_v1", None


def make_generation_binding(owner_binding, reservation_id, *, kind, claim_digest):
    _require(type(owner_binding) is OwnerBinding and kind in ("worker", "metadata") and _hex(reservation_id, 32))
    owner_binding.__post_init__()
    contract, name = _contract(owner_binding, reservation_id, kind)
    return GenerationRecoveryBinding(owner_binding, reservation_id, kind, contract, name, claim_digest)


def _lock(descriptor):
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            raise RecoverableStateError("active_owner") from None
        raise RecoverableStateError() from None


def _close(descriptor):
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _verify_owner(root, owner, descriptor):
    _require(_private._identity(_private._stat(_private._directory(root), directory=True)) == owner.directory_identity)
    before = _private._stat(root / owner.lease_name)
    opened = os.fstat(descriptor)
    _require(_private._identity(before) == owner.lease_identity and before.st_size == 1)
    _require(_private._same_opened(before, opened))
    observed = OwnerBinding.from_json(_private._read(root / owner.descriptor_name))
    _require(observed == owner)
    _require(_private._fingerprint(_private._stat(root / owner.lease_name)) == _private._fingerprint(before))


class OwnerLease:
    def __init__(self, root, owner, descriptor, key):
        _require(key is _AUTHORITY)
        self._root, self._owner, self._descriptor = root, owner, descriptor
        self._pid = os.getpid()

    @property
    def owner_binding(self):
        return self._owner

    def require_live(self):
        try:
            _require(self._descriptor is not None and self._pid == os.getpid())
            _verify_owner(self._root, self._owner, self._descriptor)
            _require(current_recovery_identity() == self._owner.identity)
        except RecoverableStateError:
            raise
        except Exception:
            raise RecoverableStateError() from None

    def close(self):
        _require(self._pid == os.getpid())
        if self._descriptor is not None:
            descriptor, self._descriptor = self._descriptor, None
            try:
                _close(descriptor)
            except Exception:
                raise RecoverableStateError() from None

    def __enter__(self):
        self.require_live()
        return self

    def __exit__(self, *args):
        self.close()


def open_owner_lease(directory, owner_id):
    """Create a never-reused owner and retain its noninheritable native lock.

    The trusted parent directory must exist. This function never replaces an
    old lease or owner descriptor. Partial creation remains unavailable.
    """
    descriptor = None
    locked = False
    try:
        _require(_hex(owner_id, 32))
        root = Path(directory)
        _private._directory(root.parent, private=False)
        try:
            root.mkdir(mode=0o700)
        except FileExistsError:
            pass
        root = _private._directory(root)
        for count, _ in enumerate(root.iterdir(), 1):
            _require(count <= 2 * _MAX_OWNERS - 2)
        identity = current_recovery_identity()
        descriptor = os.open(
            root / (owner_id + ".lease"), os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        os.set_inheritable(descriptor, False)
        _require(os.write(descriptor, b"\0") == 1)
        os.fsync(descriptor)
        _lock(descriptor)
        locked = True
        owner = OwnerBinding(
            owner_id,
            identity,
            _private._identity(os.fstat(descriptor)),
            _private._identity(_private._stat(root, directory=True)),
        )
        _private._exclusive(root / owner.descriptor_name, owner.to_json())
        if os.name != "nt":
            parent_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        lease = OwnerLease(root, owner, descriptor, _AUTHORITY)
        lease.require_live()
        descriptor = None
        return lease
    except RecoverableStateError:
        raise
    except Exception:
        raise RecoverableStateError() from None
    finally:
        if descriptor is not None:
            try:
                if locked:
                    _close(descriptor)
                else:
                    os.close(descriptor)
            except Exception:
                raise RecoverableStateError() from None


@dataclass(frozen=True, init=False)
class RecoveryProof:
    _guard: object = field(repr=False)
    reason: str

    def __init__(self, guard, reason, key):
        _require(key is _AUTHORITY)
        object.__setattr__(self, "_guard", guard)
        object.__setattr__(self, "reason", reason)


class OwnerRecoveryGuard:
    def __init__(self, root, binding, expected_claim_digest, descriptor, identity, cancelled, key):
        _require(key is _AUTHORITY)
        self._root, self._binding, self._expected = root, binding, expected_claim_digest
        self._descriptor, self._identity, self._cancelled = descriptor, identity, cancelled
        self._pid = os.getpid()

    @property
    def binding(self):
        return self._binding

    def require_binding(self, binding):
        try:
            _cancel(self._cancelled)
            _require(self._descriptor is not None and self._pid == os.getpid())
            _require(type(binding) is GenerationRecoveryBinding and binding == self.binding)
            binding.__post_init__()
            _require(_digest(self._expected) and binding.claim_digest == self._expected)
            current = current_recovery_identity()
            _require(current == self._identity)
            _require(
                current.platform == binding.owner.identity.platform
                and current.host_id == binding.owner.identity.host_id
            )
            _verify_owner(self._root, binding.owner, self._descriptor)
            _cancel(self._cancelled)
        except RecoverableStateError:
            raise
        except Exception:
            raise RecoverableStateError() from None

    def prove_empty(self, *, windows_probe=None):
        try:
            self.require_binding(self.binding)
            owner_identity = self.binding.owner.identity
            if self.binding.kind == "metadata":
                reason = "metadata_owner_excluded"
            elif owner_identity.platform == "linux":
                _require(self._identity.boot_id != owner_identity.boot_id, "unsupported_platform")
                reason = "linux_previous_boot"
            else:
                _require(callable(windows_probe), "unsupported_platform")
                _require(windows_probe(self.binding, self) is True, "cleanup_pending")
                reason = "windows_job_empty"
            self.require_binding(self.binding)
            return RecoveryProof(self, reason, _AUTHORITY)
        except RecoverableStateError:
            raise
        except Exception:
            raise RecoverableStateError() from None

    def require_proof(self, proof):
        _require(type(proof) is RecoveryProof and proof._guard is self)
        self.require_binding(self.binding)


@contextmanager
def acquire_recovery_guard(directory, binding, *, expected_claim_digest, cancelled=None):
    """One nonblocking attempt; caller retains the guard until journal commit."""
    descriptor = None
    guard = None
    locked = False
    try:
        _cancel(cancelled)
        if binding is None:
            raise RecoverableStateError("legacy_state")
        _require(type(binding) is GenerationRecoveryBinding and _digest(expected_claim_digest))
        binding.__post_init__()
        _require(binding.claim_digest == expected_claim_digest)
        root = _private._directory(directory)
        _private._stat(root / binding.owner.lease_name)
        descriptor = os.open(root / binding.owner.lease_name, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        os.set_inheritable(descriptor, False)
        _verify_owner(root, binding.owner, descriptor)
        _lock(descriptor)
        locked = True
        guard = OwnerRecoveryGuard(
            root, binding, expected_claim_digest, descriptor, current_recovery_identity(), cancelled, _AUTHORITY
        )
        guard.require_binding(binding)
        yield guard
    except RecoverableStateError:
        raise
    except Exception:
        raise RecoverableStateError() from None
    finally:
        if guard is not None:
            guard._descriptor = None
        if descriptor is not None:
            try:
                if locked:
                    _close(descriptor)
                else:
                    os.close(descriptor)
            except Exception:
                raise RecoverableStateError() from None
