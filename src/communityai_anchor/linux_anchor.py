"""Fixed volunteer service identity and peer-bound local anchor channels.

Version 1 inspection never grants node or maintenance authority. Version 2
optionally controls the fixed node owner; it never grants installer authority. Same-UID code is
cooperative, as in linux_cgroup_recovery; this is not a same-user sandbox.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import socket
import stat
import struct
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from communityai_anchor import linux_cgroup_recovery as cg
from communityai_anchor.resource_recovery import RecoverableStateError

UNIT = "communityai-multigpu-anchor.service"
PROFILE = "multigpu-volunteer"
_PROPERTIES = (
    "Id",
    "LoadState",
    "ActiveState",
    "SubState",
    "MainPID",
    "ControlGroup",
    "InvocationID",
    "Delegate",
    "Type",
    "KillMode",
    "Restart",
    "Transient",
)
_LIMIT = 16384
_IO_TIMEOUT = 2.0
_CHILDREN = ("anchor-control", "nodes", "workers")


def _require(condition):
    if not condition:
        raise RecoverableStateError()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _properties(text, keys):
    _require(isinstance(text, str) and len(text) <= _LIMIT)
    pairs = []
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        _require(separator and key in keys)
        pairs.append((key, value))
    result = _unique(pairs)
    _require(set(result) == set(keys))
    return result


def _private_directory(path):
    descriptor = cg._open_root(str(path))
    try:
        identity = cg._identity(descriptor)
        _require(not os.fstat(descriptor).st_mode & 0o077)
        return identity
    finally:
        os.close(descriptor)


def _runtime_directory():
    cg._platform()
    _require(os.getuid() == os.geteuid() and os.getgid() == os.getegid() and os.geteuid() != 0)
    directory = Path("/run/user") / str(os.geteuid())
    _private_directory(directory)
    return directory


def _query_properties():
    directory = _runtime_directory()
    # Do not inherit remote-manager, pager, bus, locale, or executable overrides.
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "XDG_RUNTIME_DIR": str(directory),
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + str(directory / "bus"),
    }

    def query(command):
        result = subprocess.run(
            command,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=True,
        )
        _require(len(result.stdout) <= _LIMIT)
        return result.stdout.decode("ascii")

    # Observe existing persistent-manager policy; never enable lingering.
    linger = _properties(query(["/usr/bin/loginctl", "show-user", str(os.geteuid()), "--property=Linger"]), ("Linger",))
    _require(linger["Linger"] == "yes")
    return _properties(
        query(
            [
                "/usr/bin/systemctl",
                "--user",
                "show",
                UNIT,
                "--property=" + ",".join(_PROPERTIES),
            ]
        ),
        _PROPERTIES,
    )


def _process_uid(status):
    # Proc directory ownership changes to root for a nondumpable process.
    entries = [line for line in status.splitlines() if line.startswith("Uid:")]
    _require(len(entries) == 1)
    values = entries[0][4:].split()
    _require(len(values) == 4 and all(re.fullmatch(r"0|[1-9][0-9]{0,9}", n) for n in values))
    _require(all(int(n) == os.geteuid() for n in values))
    return int(values[0])


def _process_ticks(value, pid):
    # comm may contain spaces and closing parentheses. Fields after its final
    # ')' start at field 3; starttime is field 22.
    head, separator, tail = value.rpartition(") ")
    _require(separator and head.startswith(str(pid) + " (") and len(tail.split()) >= 20)
    ticks = tail.split()[19]
    _require(re.fullmatch("[1-9][0-9]{0,19}", ticks) is not None)
    return int(ticks)


def _process(pid):
    _require(type(pid) is int and 1 < pid < 2**31)
    directory = f"/proc/{pid}"
    uid = _process_uid(cg._read_path(directory + "/status", _LIMIT))
    ticks = _process_ticks(cg._read_path(directory + "/stat", _LIMIT), pid)
    group = cg._read_path(directory + "/cgroup", _LIMIT)
    _require(group.startswith("0::/") and group.count("\n") == 1 and group.endswith("\n"))
    path = group[3:-1]
    _require(cg._path(path))
    _require(_process_uid(cg._read_path(directory + "/status", _LIMIT)) == uid)
    _require(_process_ticks(cg._read_path(directory + "/stat", _LIMIT), pid) == ticks)
    return ticks, path


@dataclass(frozen=True)
class ServiceIdentity:
    pid: int
    uid: int
    start_ticks: int = field(repr=False)
    invocation: str = field(repr=False)
    control_group: str = field(repr=False)

    def to_json(self):
        return dict(
            pid=self.pid,
            uid=self.uid,
            start_ticks=self.start_ticks,
            invocation=self.invocation,
            control_group=self.control_group,
        )


def inspect_service(*, starting=False):
    """Observe the fixed ordinary-user service, not an environment assertion."""
    try:
        cg._platform()
        properties = _query_properties()
        expected = {
            "Id": UNIT,
            "LoadState": "loaded",
            "Delegate": "yes",
            "Type": "exec",
            "KillMode": "control-group",
            "Restart": "no",
            "Transient": "no",
        }
        _require(all(properties.get(key) == value for key, value in expected.items()))
        allowed = {("active", "running")}
        if starting:
            allowed.add(("activating", "start"))
        _require((properties["ActiveState"], properties["SubState"]) in allowed)
        _require(re.fullmatch("[1-9][0-9]{0,9}", properties["MainPID"]) is not None)
        _require(re.fullmatch("[0-9a-f]{32}", properties["InvocationID"]) is not None)
        _require(properties["InvocationID"] != "0" * 32 and cg._path(properties["ControlGroup"]))
        pid = int(properties["MainPID"])
        ticks, group = _process(pid)
        root = properties["ControlGroup"]
        _require(group == (root if starting else root + "/anchor-control"))
        if starting:
            _require(pid == os.getpid())
        return ServiceIdentity(pid, os.geteuid(), ticks, properties["InvocationID"], root)
    except RecoverableStateError:
        raise
    except Exception:
        raise RecoverableStateError() from None


def _delegated_path(service):
    # First version deliberately rejects sub-root/ambiguous mounts. Comparing
    # namespace-relative paths with a host manager's paths must not be guessed.
    text = cg._read_path("/proc/self/mountinfo", cg._MAX_MOUNTINFO, filesystem_text=True)
    candidates = []
    for line in text.splitlines():
        parts = line.split(" ")
        _require(len(parts) >= 10 and "-" in parts)
        split = parts.index("-")
        _require(split >= 6 and len(parts) == split + 4)
        _require(parts[split + 1] != "cgroup")
        if parts[split + 1] != "cgroup2" or "rw" not in parts[5].split(","):
            continue
        _require(cg._mount_path(parts[3]) == "/")
        mount = cg._mount_path(parts[4])
        _require(cg._path(mount))
        candidates.append(mount + service.control_group)
    _require(len(candidates) == 1)
    return candidates[0]


def _layout_profiles(root):
    # Identity/topology observations must not let a freezer veto Stop. Start
    # and successful drain separately require unfrozen empty-tree proofs.
    return tuple(
        cg.observe_cgroup_profile(path, require_unfrozen=False) for path in (root, *(root + "/" + n for n in _CHILDREN))
    )


def _layout_digest(profiles):
    return "sha256:" + hashlib.sha256(_encode([p.to_json() for p in profiles])).hexdigest()


def _subgroups(descriptor):
    names = os.listdir(descriptor)
    _require(len(names) <= 1024)
    return {name for name in names if stat.S_ISDIR(os.stat(name, dir_fd=descriptor, follow_symlinks=False).st_mode)}


def _observe_layout(service):
    root = _delegated_path(service)
    profiles = _layout_profiles(root)
    descriptor = cg._open_root(root)
    try:
        _require(cg._observe_root(root, descriptor) == profiles[0])
        _require(cg._read_control(descriptor, "cgroup.procs") == "")
        _require(_subgroups(descriptor) == set(_CHILDREN))
        control = cg._open_directory("anchor-control", parent=descriptor)
        try:
            _require(cg._observe_root(root + "/anchor-control", control) == profiles[1])
            _require(not _subgroups(control))
            _require(cg._read_control(control, "cgroup.procs").split() == [str(service.pid)])
        finally:
            os.close(control)
    finally:
        os.close(descriptor)
    return profiles


class AnchorLayout:
    """Own fixed subgroups for this invocation; retain them even after close.

    Existing names are never adopted or deleted. The only migration is the
    anchor itself, before work exists. Future nodes require atomic native birth.
    """

    def __init__(self):
        self._descriptor = None
        self.service = inspect_service(starting=True)
        self.root = _delegated_path(self.service)
        try:
            root_profile = cg.validate_cgroup_profile(self.root)
            self._descriptor = cg._open_root(self.root)
            _require(cg._observe_root(self.root, self._descriptor) == root_profile)
            _require(cg._read_control(self._descriptor, "cgroup.procs").split() == [str(self.service.pid)])
            # Refuse all pre-existing subgroups, including unknown retained work.
            _require(not _subgroups(self._descriptor))
            for name in _CHILDREN:
                os.mkdir(name, mode=0o700, dir_fd=self._descriptor)
            control = cg._open_directory("anchor-control", parent=self._descriptor)
            try:
                fd = cg._control(control, "cgroup.procs", write=True)
                try:
                    payload = str(self.service.pid).encode("ascii")
                    _require(os.write(fd, payload) == len(payload))
                finally:
                    os.close(fd)
            finally:
                os.close(control)
            self.profiles = _layout_profiles(self.root)
            _require(self.profiles[0] == root_profile)
            self.validate()
        except BaseException:
            self.close()
            raise

    def validate(self):
        _require(self._descriptor is not None and inspect_service() == self.service)
        _require(_delegated_path(self.service) == self.root)
        _require(cg._observe_root(self.root, self._descriptor) == self.profiles[0])
        _require(_observe_layout(self.service) == self.profiles)

    def receipt(self, nonce):
        self.validate()
        return _receipt(self.service, self.profiles, nonce)

    def close(self):
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None


def _receipt(service, profiles, nonce):
    return dict(
        version=1,
        profile=PROFILE,
        operation="inspect",
        nonce=nonce,
        service=service.to_json(),
        layout_digest=_layout_digest(profiles),
        node_generation=None,
        admission=False,
        maintenance=False,
    )


def _request(nonce):
    _require(isinstance(nonce, str) and re.fullmatch("[0-9a-f]{64}", nonce) is not None)
    return dict(version=1, profile=PROFILE, operation="inspect", nonce=nonce)


def _receive(connection):
    deadline = time.monotonic() + _IO_TIMEOUT
    payload = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        _require(remaining > 0)
        connection.settimeout(remaining)
        chunk = connection.recv(min(4096, _LIMIT + 1 - len(payload)))
        if not chunk:
            break
        payload.extend(chunk)
        _require(len(payload) <= _LIMIT)
    try:
        return json.loads(payload.decode("ascii"), object_pairs_hook=_unique)
    except (ValueError, RecursionError):
        raise RecoverableStateError() from None


def _peer(connection):
    pid, uid, _gid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    _require(uid == os.geteuid() and pid > 1)
    return pid


def _socket_identity(path):
    info = path.lstat()
    _require(stat.S_ISSOCK(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077)
    return info.st_dev, info.st_ino


def _lock_identity(info):
    _require(
        stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077 and info.st_nlink == 1
    )
    return info.st_dev, info.st_ino


def _channel_directory(*, create=False):
    directory = _runtime_directory() / "communityai-multigpu-anchor"
    if create:
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
    _private_directory(directory)
    return directory


class AnchorChannel:
    """One fixed private Unix socket. Never removes a stale socket."""

    def __init__(self, layout, controller=None):
        import fcntl

        self.layout = layout
        self.controller = controller
        self._listener = None
        self._lock = None
        self._lock_identity = None
        self._identity = None
        self.directory = _channel_directory(create=True)
        self._directory_identity = _private_directory(self.directory)
        self.path = self.directory / "control.sock"
        try:
            self._lock = os.open(
                self.directory / "anchor.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
            )
            self._lock_identity = _lock_identity(os.fstat(self._lock))
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._validate_lock()
            layout.validate()
            self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            # bind() refuses any previous name. No stale-name recovery is implied.
            previous = os.umask(0o177)
            try:
                self._listener.bind(str(self.path))
            finally:
                os.umask(previous)
            self._identity = _socket_identity(self.path)
            self._listener.listen(4)
            self._listener.settimeout(1.0)
        except BaseException:
            self.close()
            raise

    def _validate_lock(self):
        _require(self._lock is not None)
        _require(_private_directory(self.directory) == self._directory_identity)
        _require(_lock_identity(os.fstat(self._lock)) == self._lock_identity)
        _require(_lock_identity((self.directory / "anchor.lock").lstat()) == self._lock_identity)

    def serve_once(self):
        _require(self._listener is not None)
        self._validate_lock()
        _require(_socket_identity(self.path) == self._identity)
        try:
            connection, _ = self._listener.accept()
        except socket.timeout:
            return
        with connection:
            try:
                _peer(connection)
                request = _receive(connection)
                _require(type(request) is dict and type(request.get("version")) is int)
                if request["version"] == 2:
                    from communityai_anchor.linux_anchor_control import respond

                    response = respond(self.layout, self.controller, request)
                else:
                    _require(request == _request(request.get("nonce")))
                    response = self.layout.receipt(request["nonce"])
                self._validate_lock()
                _require(_socket_identity(self.path) == self._identity)
                connection.settimeout(_IO_TIMEOUT)
                connection.sendall(_encode(response))
            except (OSError, ValueError, RecoverableStateError):
                # No reflection of paths, credentials, or raw exceptions.
                return

    def close(self):
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        try:
            if (
                self._identity is not None
                and _private_directory(self.directory) == self._directory_identity
                and _socket_identity(self.path) == self._identity
            ):
                self.path.unlink()
        except (OSError, RecoverableStateError):
            pass
        self._identity = None
        if self._lock is not None:
            os.close(self._lock)
            self._lock = None


def inspect_anchor():
    """Reconnect observation, bound to kernel peer and fresh service/layout facts.

    The result deliberately has no node generation or maintenance permission.
    It must not be used as a drain acknowledgement or an external-node grant.
    """
    try:
        service = inspect_service()
        root = _delegated_path(service)
        profiles = _observe_layout(service)
        directory = _channel_directory()
        directory_identity = _private_directory(directory)
        path = directory / "control.sock"
        identity = _socket_identity(path)
        nonce = os.urandom(32).hex()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(_IO_TIMEOUT)
            connection.connect(str(path))
            _require(_peer(connection) == service.pid)
            connection.sendall(_encode(_request(nonce)))
            connection.shutdown(socket.SHUT_WR)
            response = _receive(connection)
        expected = _receipt(service, profiles, nonce)
        # Compare canonical bytes as well as values to reject bool/int aliases.
        _require(_encode(response) == _encode(expected))
        _require(inspect_service() == service and _observe_layout(service) == profiles)
        _require(_delegated_path(service) == root)
        _require(_private_directory(directory) == directory_identity and _socket_identity(path) == identity)
        return response
    except RecoverableStateError:
        raise
    except Exception:
        raise RecoverableStateError() from None


def serve_anchor(*, controller_factory=None):
    """Service loop; optional controller factory is trusted internal wiring only."""
    layout = channel = controller = None
    stopping = False
    handlers = {}

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    try:
        # Abrupt death retains durable evidence; no restarted invocation may
        # infer recovery permission from service/process or socket absence.
        for signum in (signal.SIGINT, signal.SIGTERM):
            handlers[signum] = signal.signal(signum, stop)
        layout = AnchorLayout()
        if controller_factory is not None:
            controller = controller_factory(layout)
        channel = AnchorChannel(layout) if controller is None else AnchorChannel(layout, controller)
        while True:
            if stopping:
                if controller is None:
                    return 0
                controller.request_shutdown()
                if controller.finished.is_set():
                    return 0 if controller.close() else 75
            channel.serve_once()
    except Exception:
        raise RecoverableStateError() from None
    finally:
        if controller is not None:
            controller.close()
        if channel is not None:
            channel.close()
        if layout is not None:
            layout.close()
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
