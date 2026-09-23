"""Strict delegated cgroup-v2 identity and whole-generation recovery.

The configured anchor must survive owner restarts. Same-UID workers and other
local writers are cooperative: no migration out of a generation, namespace
changes, renames or delegated-root replacement are permitted by this contract.
No process-group, PID-list or missing-path inference authorizes release.
"""

from __future__ import annotations

import ctypes
import math
import os
import re
import stat
import sys
import time
from dataclasses import dataclass, field

from drift.node.resource_recovery import (
    GenerationRecoveryBinding,
    OwnerBinding,
    OwnerRecoveryGuard,
    RecoverableStateError,
    current_recovery_identity,
)
from drift.node.resource_recovery_config import normalize_worker_cgroup_root

_MAX_TEXT = 4096
_MAX_MOUNTINFO = 1024 * 1024
_MAX_GENERATIONS = 4096
_CGROUP2_MAGIC = 0x63677270
_KEY = object()


def _require(value, reason="unverifiable_state"):
    if not value:
        raise RecoverableStateError(reason)


def _integer(value, *, positive=False):
    return type(value) is int and int(positive) <= value < 2**64


def _native_id(value):
    return type(value) is tuple and len(value) == 2 and _integer(value[0]) and _integer(value[1], positive=True)


def _path(value):
    try:
        return isinstance(value, str) and normalize_worker_cgroup_root(value) == value
    except ValueError:
        return False


def generation_name(owner_id, reservation_id):
    _require(all(isinstance(v, str) and re.fullmatch("[0-9a-f]{32}", v) for v in (owner_id, reservation_id)))
    return "communityai-" + owner_id + "-" + reservation_id


@dataclass(frozen=True)
class LinuxCgroupProfile:
    root: str = field(repr=False)
    root_identity: tuple[int, int] = field(repr=False)
    mount_id: int = field(repr=False)
    mount_root: str = field(repr=False)
    mount_point: str = field(repr=False)
    namespaces: tuple[tuple[int, int], ...] = field(repr=False)
    uid: int = field(repr=False)

    def __post_init__(self):
        _require(_path(self.root) and _native_id(self.root_identity) and _integer(self.mount_id, positive=True))
        _require(
            (self.mount_root == "/" or _path(self.mount_root)) and (self.mount_point == "/" or _path(self.mount_point))
        )
        _require(self.root != self.mount_point and self.root.startswith(self.mount_point.rstrip("/") + "/"))
        _require(
            type(self.namespaces) is tuple and len(self.namespaces) == 3 and all(_native_id(n) for n in self.namespaces)
        )
        _require(_integer(self.uid))

    def to_json(self):
        self.__post_init__()
        return dict(
            root=self.root,
            root_identity=list(self.root_identity),
            mount_id=self.mount_id,
            mount_root=self.mount_root,
            mount_point=self.mount_point,
            namespaces=[list(n) for n in self.namespaces],
            uid=self.uid,
        )

    @classmethod
    def from_json(cls, value):
        _require(
            type(value) is dict
            and set(value) == {"root", "root_identity", "mount_id", "mount_root", "mount_point", "namespaces", "uid"}
        )
        _require(
            type(value["root_identity"]) is list
            and type(value["namespaces"]) is list
            and all(type(n) is list for n in value["namespaces"])
        )
        return cls(
            **{
                **value,
                "root_identity": tuple(value["root_identity"]),
                "namespaces": tuple(tuple(n) for n in value["namespaces"]),
            }
        )


@dataclass(frozen=True)
class LinuxCgroupIdentity:
    profile: LinuxCgroupProfile = field(repr=False)
    name: str = field(repr=False)
    directory_identity: tuple[int, int] = field(repr=False)

    def __post_init__(self):
        _require(type(self.profile) is LinuxCgroupProfile)
        self.profile.__post_init__()
        _require(isinstance(self.name, str) and re.fullmatch("communityai-[0-9a-f]{32}-[0-9a-f]{32}", self.name))
        _require(_native_id(self.directory_identity) and self.directory_identity[0] == self.profile.root_identity[0])

    def to_json(self):
        self.__post_init__()
        return dict(
            schema_version=1,
            profile=self.profile.to_json(),
            name=self.name,
            directory_identity=list(self.directory_identity),
        )

    @classmethod
    def from_json(cls, value):
        _require(type(value) is dict and set(value) == {"schema_version", "profile", "name", "directory_identity"})
        _require(
            type(value["schema_version"]) is int
            and value["schema_version"] == 1
            and type(value["directory_identity"]) is list
        )
        return cls(LinuxCgroupProfile.from_json(value["profile"]), value["name"], tuple(value["directory_identity"]))


def _platform():
    # Kernfs exposes its complete generation-bearing ID as ino on 64-bit Linux.
    _require(
        sys.platform.startswith("linux") and ctypes.sizeof(ctypes.c_void_p) == 8 and ctypes.sizeof(ctypes.c_long) == 8,
        "unsupported_platform",
    )


def _identity(descriptor):
    observed = os.fstat(descriptor)
    _require(
        stat.S_ISDIR(observed.st_mode)
        and observed.st_uid == os.geteuid()
        and observed.st_mode & 0o700 == 0o700
        and not observed.st_mode & 0o022
    )
    value = (observed.st_dev, observed.st_ino)
    _require(_native_id(value))
    return value


def _open_directory(path, *, parent=None):
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=parent)


def _close_descriptors(*descriptors):
    failed = False
    for descriptor in descriptors:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                failed = True
    if failed:
        raise RecoverableStateError() from None


def _open_root(path):
    _require(_path(path))
    descriptor = _open_directory("/")
    try:
        for part in path[1:].split("/"):
            child = _open_directory(part, parent=descriptor)
            os.close(descriptor)
            descriptor = child
        result, descriptor = descriptor, None
        return result
    finally:
        _close_descriptors(descriptor)


def _read_fd(descriptor, maximum):
    data = os.read(descriptor, maximum + 1)
    _require(len(data) <= maximum)
    return data.decode("ascii")


def _read_path(path, maximum, *, filesystem_text=False):
    with open(path, "rb") as stream:
        data = stream.read(maximum + 1)
    _require(len(data) <= maximum)
    return os.fsdecode(data) if filesystem_text else data.decode("ascii")


def _control(descriptor, name, *, write=False):
    result = os.open(name, (os.O_WRONLY if write else os.O_RDONLY) | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=descriptor)
    try:
        observed = os.fstat(result)
        _require(stat.S_ISREG(observed.st_mode) and observed.st_dev == os.fstat(descriptor).st_dev)
        return result
    except BaseException:
        os.close(result)
        raise


def _read_control(descriptor, name):
    fd = _control(descriptor, name)
    try:
        return _read_fd(fd, _MAX_TEXT)
    finally:
        os.close(fd)


def _events(text, *, require_unfrozen=False):
    _require(isinstance(text, str) and len(text) <= _MAX_TEXT)
    parsed = {}
    for line in text.splitlines():
        parts = line.split()
        _require(
            len(parts) == 2
            and parts[0] in {"populated", "frozen"}
            and parts[0] not in parsed
            and parts[1] in {"0", "1"}
        )
        parsed[parts[0]] = parts[1]
    _require("populated" in parsed)
    if require_unfrozen:
        _require(parsed.get("frozen") == "0", "unsupported_platform")
    return parsed["populated"] == "1"


def _require_unfrozen(descriptor):
    requested = _read_control(descriptor, "cgroup.freeze")
    _require(requested in {"0\n", "1\n"})
    _require(requested == "0\n", "unsupported_platform")
    _events(_read_control(descriptor, "cgroup.events"), require_unfrozen=True)


def _mount_path(value):
    _require(isinstance(value, str) and len(value) <= _MAX_TEXT)
    # mountinfo's four reserved characters have exact octal encodings.
    escaped = {"040": " ", "011": "\t", "012": "\n", "134": "\\"}
    _require(re.search(r"\\(?!040|011|012|134)", value) is None)
    return re.sub(r"\\(040|011|012|134)", lambda m: escaped[m.group(1)], value)


def _mount_record(text, mount_id, device):
    _require(isinstance(text, str) and len(text) <= _MAX_MOUNTINFO)
    matches = []
    # Kernel mountinfo escapes only space, tab, newline and backslash. Other
    # filesystem bytes, including Unicode whitespace, are part of the name.
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    for line in lines:
        parts = line.split(" ")
        _require(len(parts) >= 10 and "-" in parts)
        if parts[0] != str(mount_id):
            continue
        split = parts.index("-")
        _require(split >= 6 and len(parts) == split + 4 and parts[split + 1] == "cgroup2")
        _require(parts[2] == f"{os.major(device)}:{os.minor(device)}")
        matches.append((_mount_path(parts[3]), _mount_path(parts[4])))
    _require(len(matches) == 1)
    return matches[0]


def _filesystem(descriptor):
    libc = ctypes.CDLL(None, use_errno=True)
    function = libc.fstatfs
    function.argtypes = (ctypes.c_int, ctypes.c_void_p)
    function.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(256)
    _require(function(descriptor, buffer) == 0)
    _require(ctypes.c_long.from_buffer(buffer).value == _CGROUP2_MAGIC, "unsupported_platform")


def _observe_root(root, descriptor):
    _filesystem(descriptor)
    native = _identity(descriptor)
    info = _read_path(f"/proc/self/fdinfo/{descriptor}", _MAX_TEXT)
    mounts = [line.split()[1] for line in info.splitlines() if line.startswith("mnt_id:") and len(line.split()) == 2]
    _require(len(mounts) == 1 and re.fullmatch("[1-9][0-9]{0,19}", mounts[0]))
    mount_id = int(mounts[0])
    mount_root, mount_point = _mount_record(
        _read_path("/proc/self/mountinfo", _MAX_MOUNTINFO, filesystem_text=True), mount_id, native[0]
    )
    namespaces = tuple(
        (item.st_dev, item.st_ino) for item in (os.stat("/proc/self/ns/" + name) for name in ("mnt", "cgroup", "user"))
    )
    result = LinuxCgroupProfile(root, native, mount_id, mount_root, mount_point, namespaces, os.geteuid())
    _require(_read_control(descriptor, "cgroup.type") == "domain\n", "unsupported_platform")
    _events(_read_control(descriptor, "cgroup.events"))
    kill_fd = _control(descriptor, "cgroup.kill", write=True)
    os.close(kill_fd)  # Capability/open check only: no kill and no child.
    return result


def _validate_backend():
    try:
        from drift.node.linux_cgroup_process import validate_cgroup_backend

        validate_cgroup_backend()
    except Exception:
        raise RecoverableStateError("unsupported_platform") from None


def validate_cgroup_profile(rootpath, *, require_unfrozen=True):
    descriptor = None
    try:
        _platform()
        root = os.fspath(rootpath)
        descriptor = _open_root(root)
        profile = _observe_root(root, descriptor)
        if require_unfrozen:
            _require_unfrozen(descriptor)
        _validate_backend()
        _require(profile == _observe_root(root, descriptor))
        if require_unfrozen:
            _require_unfrozen(descriptor)
        return profile
    except RecoverableStateError:
        raise
    except Exception:
        raise RecoverableStateError() from None
    finally:
        _close_descriptors(descriptor)


class LinuxCgroupContainment:
    def __init__(self, identity, owner, reservation_id, root_fd, directory_fd, key):
        _require(key is _KEY)
        self.identity, self._owner, self._reservation_id = identity, owner, reservation_id
        self._root_fd, self._directory_fd = root_fd, directory_fd
        self._binding, self._process, self._spawned = None, None, False

    def _validate(self):
        _platform()
        _require(self._root_fd is not None and self._directory_fd is not None)
        _require(current_recovery_identity() == self._owner.identity)
        _require(_observe_root(self.identity.profile.root, self._root_fd) == self.identity.profile)
        reopened = _open_root(self.identity.profile.root)
        try:
            _require(_observe_root(self.identity.profile.root, reopened) == self.identity.profile)
            leaf = _open_directory(self.identity.name, parent=reopened)
            try:
                _require(_identity(leaf) == self.identity.directory_identity == _identity(self._directory_fd))
            finally:
                os.close(leaf)
        finally:
            os.close(reopened)
        _require(_read_control(self._directory_fd, "cgroup.type") == "domain\n")

    def bind(self, binding):
        try:
            _require(
                type(binding) is GenerationRecoveryBinding
                and binding.owner == self._owner
                and binding.reservation_id == self._reservation_id
                and binding.contract == "linux_cgroup_v1"
                and binding.linux_cgroup == self.identity
                and self._binding in (None, binding)
            )
            binding.__post_init__()
            self._validate()
            self._binding = binding
        except RecoverableStateError:
            raise
        except Exception:
            raise RecoverableStateError() from None

    @property
    def cgroup_fd(self):
        try:
            _require(self._binding is not None)
            self._validate()
            _require_unfrozen(self._root_fd)
            _require_unfrozen(self._directory_fd)
            return self._directory_fd
        except RecoverableStateError:
            raise
        except Exception:
            raise RecoverableStateError() from None

    def popen_kwargs(self):
        return {}

    def spawn(self, command, **kwargs):
        try:
            from drift.node.linux_cgroup_process import spawn

            descriptor = self.cgroup_fd
            _require(not self._spawned)
            self._spawned = True
            self._process = spawn(descriptor, command, **kwargs)
            self.attach(self._process)
            return self._process
        except RecoverableStateError:
            raise
        except Exception:
            raise RecoverableStateError() from None

    def attach(self, process):
        try:
            self._validate()
            _require(
                process is self._process
                and process is not None
                and process.cgroup_identity == self.identity.directory_identity
            )
        except RecoverableStateError:
            raise
        except Exception:
            raise RecoverableStateError() from None

    def resume(self, process):
        self.attach(process)
        try:
            process.resume()
        except Exception:
            raise RecoverableStateError() from None

    def has_members(self):
        try:
            self._validate()
            return _events(_read_control(self._directory_fd, "cgroup.events"))
        except RecoverableStateError:
            raise
        except Exception:
            raise RecoverableStateError() from None

    def terminate(self):
        descriptor = None
        try:
            self._validate()
            descriptor = _control(self._directory_fd, "cgroup.kill", write=True)
            _require(os.write(descriptor, b"1\n") == 2)
            self._validate()
        except RecoverableStateError:
            raise
        except Exception:
            raise RecoverableStateError() from None
        finally:
            _close_descriptors(descriptor)

    kill = terminate

    def close(self):
        # Never remove the generation, including on uncertain journal commit.
        # Kernel lifetime and directory cleanup are distinct from FD ownership.
        descriptors = (self._directory_fd, self._root_fd)
        self._directory_fd = self._root_fd = None
        _close_descriptors(*descriptors)


def prepare_generation(rootpath, owner_binding, reservation_id):
    root_fd = directory_fd = None
    try:
        _platform()
        _require(type(owner_binding) is OwnerBinding and owner_binding.identity.platform == "linux")
        owner_binding.__post_init__()
        _require(current_recovery_identity() == owner_binding.identity)
        profile = validate_cgroup_profile(rootpath)
        name = generation_name(owner_binding.owner_id, reservation_id)
        root_fd = _open_root(profile.root)
        _require(_observe_root(profile.root, root_fd) == profile)
        _require_unfrozen(root_fd)
        with os.scandir(root_fd) as entries:
            for count, _ in enumerate(entries, 1):
                _require(count < _MAX_GENERATIONS * 2)
        os.mkdir(name, mode=0o700, dir_fd=root_fd)  # Existing names are never adopted.
        directory_fd = _open_directory(name, parent=root_fd)
        identity = LinuxCgroupIdentity(profile, name, _identity(directory_fd))
        result = LinuxCgroupContainment(identity, owner_binding, reservation_id, root_fd, directory_fd, _KEY)
        _require_unfrozen(root_fd)
        _require_unfrozen(directory_fd)
        _require(not result.has_members())
        root_fd = directory_fd = None
        return result
    except RecoverableStateError:
        raise
    except Exception:
        raise RecoverableStateError() from None
    finally:
        _close_descriptors(directory_fd, root_fd)


def _reopen_generation(binding):
    root_fd = directory_fd = None
    try:
        _platform()
        _require(type(binding) is GenerationRecoveryBinding and binding.contract == "linux_cgroup_v1")
        binding.__post_init__()
        identity = binding.linux_cgroup
        root_fd = _open_root(identity.profile.root)
        directory_fd = _open_directory(identity.name, parent=root_fd)
        result = LinuxCgroupContainment(identity, binding.owner, binding.reservation_id, root_fd, directory_fd, _KEY)
        result.bind(binding)
        root_fd = directory_fd = None
        return result
    finally:
        _close_descriptors(directory_fd, root_fd)


def recover_linux_cgroup(binding, guard, *, timeout=5.0):
    containment = None
    try:
        _require(
            type(guard) is OwnerRecoveryGuard
            and type(timeout) in (int, float)
            and math.isfinite(timeout)
            and 0 <= timeout <= 60
        )
        guard.require_binding(binding)
        containment = _reopen_generation(binding)
        guard.require_binding(binding)
        if containment.has_members():
            guard.require_binding(binding)
            containment.terminate()
        deadline = time.monotonic() + timeout
        while containment.has_members():
            guard.require_binding(binding)
            remaining = deadline - time.monotonic()
            _require(remaining > 0, "cleanup_pending")
            time.sleep(min(0.02, remaining))
        guard.require_binding(binding)
        _require(not containment.has_members(), "cleanup_pending")
        guard.require_binding(binding)
        return True
    except RecoverableStateError:
        raise
    except Exception:
        raise RecoverableStateError() from None
    finally:
        if containment is not None:
            containment.close()
