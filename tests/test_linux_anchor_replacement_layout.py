"""Offline contract tests for the replacement layout and deferred channel."""

import stat
import threading
from types import SimpleNamespace

import pytest

from communityai_anchor import linux_anchor as anchor, linux_anchor_replacement_layout as replacement
from communityai_anchor.resource_recovery import RecoverableStateError


def _profile(path, inode):
    return anchor.cg.LinuxCgroupProfile(
        path,
        (42, inode),
        7,
        "/",
        "/sys/fs/cgroup",
        ((10, 11), (12, 13), (14, 15)),
        1000,
    )


def test_subgroup_scans_use_independent_directory_descriptions_under_concurrency(
    monkeypatch,
):
    held_descriptor = 40
    expected = set(anchor._CHILDREN)
    overlap = threading.Barrier(2)
    mutex = threading.Lock()
    next_descriptor = iter((101, 102))
    opened = []
    listed = []
    closed = []

    monkeypatch.setattr(anchor.os, "O_DIRECTORY", 0x10000, raising=False)
    monkeypatch.setattr(anchor.os, "O_CLOEXEC", 0x20000, raising=False)
    monkeypatch.setattr(anchor.os, "O_NOFOLLOW", 0x40000, raising=False)
    monkeypatch.setattr(anchor.cg, "_identity", lambda descriptor: (7, 11))

    def open_scan(path, flags, *, dir_fd=None):
        assert path == "."
        assert dir_fd == held_descriptor
        assert flags & anchor.os.O_DIRECTORY
        assert flags & anchor.os.O_CLOEXEC
        assert flags & anchor.os.O_NOFOLLOW
        with mutex:
            descriptor = next(next_descriptor)
            opened.append(descriptor)
        return descriptor

    def list_directory(descriptor):
        # Listing the long-held descriptor in two threads would share its open
        # file description and directory position. Each scan must instead use
        # one of the independently opened descriptors above.
        assert descriptor != held_descriptor
        with mutex:
            listed.append(descriptor)
        overlap.wait(timeout=2)
        return ["cgroup.procs", *anchor._CHILDREN]

    def observe_entry(name, *, dir_fd=None, follow_symlinks=True):
        assert dir_fd in opened
        assert follow_symlinks is False
        mode = stat.S_IFDIR | 0o700 if name in expected else stat.S_IFREG | 0o600
        return SimpleNamespace(st_mode=mode)

    def close_scan(descriptor):
        with mutex:
            closed.append(descriptor)

    monkeypatch.setattr(anchor.os, "open", open_scan)
    monkeypatch.setattr(anchor.os, "listdir", list_directory)
    monkeypatch.setattr(anchor.os, "stat", observe_entry)
    monkeypatch.setattr(anchor.os, "close", close_scan)

    results = []
    errors = []

    def scan():
        try:
            results.append(anchor._subgroups(held_descriptor))
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=scan) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert results == [expected, expected]
    assert len(set(opened)) == 2
    assert set(listed) == set(opened)
    assert set(closed) == set(opened)


class FakeLayoutKernel:
    def __init__(self, monkeypatch, *, retained):
        self.root = "/sys/fs/cgroup/user.slice/communityai.service"
        self.service = anchor.ServiceIdentity(100, 1000, 555, "a" * 32, "/user.slice/communityai.service")
        self.profiles = {"root": _profile(self.root, 1000)}
        self.procs = {"root": ["100"]}
        self.fd_names = {}
        self.controls = {}
        self.next_fd = 100
        self.mkdir_calls = []
        self.writes = []
        if retained:
            for name in anchor._CHILDREN:
                self.add_child(name)

        monkeypatch.setattr(anchor, "inspect_service", self.inspect_service)
        monkeypatch.setattr(anchor, "_delegated_path", lambda service: self.root)
        monkeypatch.setattr(anchor, "_observe_layout", self.observe_layout)
        monkeypatch.setattr(anchor.cg, "validate_cgroup_profile", self.validate_profile)
        monkeypatch.setattr(anchor.cg, "_open_root", self.open_root)
        monkeypatch.setattr(anchor.cg, "_open_directory", self.open_directory)
        monkeypatch.setattr(anchor.cg, "_observe_root", self.observe_root)
        monkeypatch.setattr(anchor.cg, "_read_control", self.read_control)
        monkeypatch.setattr(anchor.cg, "_require_unfrozen", lambda descriptor: None)
        monkeypatch.setattr(anchor.cg, "_control", self.control)
        monkeypatch.setattr(anchor.cg, "_close_descriptors", lambda *descriptors: None)
        monkeypatch.setattr(anchor, "_subgroups", self.subgroups)
        monkeypatch.setattr(replacement.os, "getpid", lambda: 100)
        monkeypatch.setattr(replacement.os, "mkdir", self.mkdir)
        monkeypatch.setattr(replacement.os, "write", self.write)
        monkeypatch.setattr(replacement.os, "close", lambda descriptor: None)

    def add_child(self, name):
        self.profiles[name] = _profile(self.root + "/" + name, 1001 + anchor._CHILDREN.index(name))
        self.procs[name] = []

    def _fd(self, name):
        self.next_fd += 1
        self.fd_names[self.next_fd] = name
        return self.next_fd

    def inspect_service(self, *, starting=False):
        if starting:
            anchor._require(self.procs["root"] == ["100"] and self.procs.get("anchor-control", []) == [])
        else:
            anchor._require(self.procs["root"] == [] and self.procs.get("anchor-control") == ["100"])
        return self.service

    def validate_profile(self, path, **kwargs):
        for name, profile in self.profiles.items():
            if profile.root == path:
                return profile
        raise RecoverableStateError()

    def open_root(self, path):
        profile = self.validate_profile(path)
        name = next(name for name, item in self.profiles.items() if item == profile)
        return self._fd(name)

    def open_directory(self, name, *, parent=None):
        anchor._require(self.fd_names[parent] == "root" and name in self.profiles)
        return self._fd(name)

    def observe_root(self, path, descriptor):
        name = self.fd_names[descriptor]
        profile = self.profiles[name]
        anchor._require(profile.root == path)
        return profile

    def read_control(self, descriptor, name):
        group = self.fd_names[descriptor]
        if name == "cgroup.procs":
            return "".join(pid + "\n" for pid in self.procs[group])
        if name == "cgroup.events":
            return "populated 0\nfrozen 0\n"
        if name == "cgroup.freeze":
            return "0\n"
        if name == "cgroup.type":
            return "domain\n"
        raise AssertionError(name)

    def subgroups(self, descriptor):
        return set(self.profiles) - {"root"} if self.fd_names[descriptor] == "root" else set()

    def control(self, descriptor, name, *, write=False):
        anchor._require(self.fd_names[descriptor] == "anchor-control" and name == "cgroup.procs" and write)
        result = self._fd("control-file")
        self.controls[result] = "anchor-control"
        return result

    def write(self, descriptor, payload):
        anchor._require(self.controls[descriptor] == "anchor-control" and payload == b"100")
        self.writes.append(payload)
        self.procs["root"] = []
        self.procs["anchor-control"] = ["100"]
        return len(payload)

    def mkdir(self, name, *, mode, dir_fd):
        anchor._require(mode == 0o700 and self.fd_names[dir_fd] == "root" and name not in self.profiles)
        self.mkdir_calls.append(name)
        self.add_child(name)

    def observe_layout(self, service):
        anchor._require(service == self.service)
        return (self.profiles["root"], *(self.profiles[name] for name in anchor._CHILDREN))

    def expected(self):
        return (self.profiles["root"], *(self.profiles[name] for name in anchor._CHILDREN))


def test_retained_layout_is_read_only_until_callback_gated_self_migration(monkeypatch):
    kernel = FakeLayoutKernel(monkeypatch, retained=True)
    events = []

    layout = replacement.prepare_retained_layout(kernel.expected())
    assert layout.root_profile == kernel.profiles["root"]
    assert kernel.mkdir_calls == [] and kernel.writes == []
    layout.migrate_self(
        begin_migration=lambda service, profiles: events.append(("begin", service, profiles)),
        record_migration=lambda service, profiles: events.append(("record", service, profiles)),
    )

    assert [item[0] for item in events] == ["begin", "record"]
    assert kernel.writes == [b"100"]
    assert kernel.procs["root"] == [] and kernel.procs["anchor-control"] == ["100"]
    layout.validate()
    layout.close()


def test_self_migration_never_runs_after_begin_failure_or_cancellation(monkeypatch):
    kernel = FakeLayoutKernel(monkeypatch, retained=True)
    layout = replacement.prepare_retained_layout(kernel.expected())

    with pytest.raises(RecoverableStateError):
        layout.migrate_self(
            begin_migration=lambda *args: (_ for _ in ()).throw(RuntimeError("journal unavailable")),
            record_migration=lambda *args: None,
        )
    assert kernel.writes == []

    stopping = False

    def begin(*args):
        nonlocal stopping
        stopping = True

    with pytest.raises(RecoverableStateError):
        layout.migrate_self(
            begin_migration=begin,
            record_migration=lambda *args: None,
            cancelled=lambda: stopping,
        )
    assert kernel.writes == []
    layout.close()


def test_lost_migration_record_reply_retries_record_without_second_write(monkeypatch):
    kernel = FakeLayoutKernel(monkeypatch, retained=True)
    layout = replacement.prepare_retained_layout(kernel.expected())
    records = []

    def record(*args):
        records.append("record")
        if len(records) == 1:
            raise RuntimeError("lost acknowledgement")

    with pytest.raises(RecoverableStateError):
        layout.migrate_self(begin_migration=lambda *args: None, record_migration=record)
    assert kernel.writes == [b"100"] and records == ["record"]

    layout.migrate_self(
        begin_migration=lambda *args: (_ for _ in ()).throw(AssertionError("begin repeated")),
        record_migration=record,
    )
    assert kernel.writes == [b"100"] and records == ["record", "record"]
    layout.close()


@pytest.mark.parametrize("fault", ["root-sibling", "child-identity", "control-member"])
def test_retained_layout_refuses_unknown_or_changed_topology(monkeypatch, fault):
    kernel = FakeLayoutKernel(monkeypatch, retained=True)
    if fault == "root-sibling":
        kernel.profiles["unknown"] = _profile(kernel.root + "/unknown", 2000)
        kernel.procs["unknown"] = []
    elif fault == "child-identity":
        expected = kernel.expected()
        kernel.profiles["workers"] = _profile(kernel.root + "/workers", 2000)
    else:
        kernel.procs["anchor-control"] = ["99"]
    if fault != "child-identity":
        expected = kernel.expected()

    with pytest.raises(RecoverableStateError):
        replacement.prepare_retained_layout(expected)


def test_manager_pruned_layout_journals_each_name_before_mkdir_and_records_identity(monkeypatch):
    kernel = FakeLayoutKernel(monkeypatch, retained=False)
    events = []

    def begin(name, root_profile, recorded):
        assert root_profile == kernel.profiles["root"]
        assert recorded == tuple(kernel.profiles[key] for key in anchor._CHILDREN[: len(recorded)])
        assert name not in kernel.profiles
        events.append(("begin", name))

    def record(name, profile):
        assert kernel.profiles[name] == profile
        events.append(("record", name))

    layout = replacement.prepare_manager_pruned_layout(
        recorded_children=(), pending_name=None, begin_child=begin, record_child=record
    )

    assert events == [item for name in anchor._CHILDREN for item in (("begin", name), ("record", name))]
    assert kernel.mkdir_calls == list(anchor._CHILDREN)
    assert layout.profiles == kernel.expected()
    layout.close()


def test_manager_pruned_layout_adopts_only_the_explicit_next_pending_name(monkeypatch):
    kernel = FakeLayoutKernel(monkeypatch, retained=False)
    kernel.add_child("anchor-control")
    events = []

    layout = replacement.prepare_manager_pruned_layout(
        recorded_children=(),
        pending_name="anchor-control",
        begin_child=lambda name, root, recorded: events.append(("begin", name)),
        record_child=lambda name, profile: events.append(("record", name)),
    )

    assert events[:2] == [("begin", "anchor-control"), ("record", "anchor-control")]
    assert kernel.mkdir_calls == ["nodes", "workers"]
    layout.close()


def test_manager_pruned_layout_refuses_unrecorded_existing_name(monkeypatch):
    kernel = FakeLayoutKernel(monkeypatch, retained=False)
    kernel.add_child("anchor-control")
    calls = []

    with pytest.raises(RecoverableStateError):
        replacement.prepare_manager_pruned_layout(
            recorded_children=(),
            pending_name=None,
            begin_child=lambda *args: calls.append("begin"),
            record_child=lambda *args: calls.append("record"),
        )
    assert calls == [] and kernel.mkdir_calls == []


def test_begin_failure_or_cancellation_never_reaches_mkdir(monkeypatch):
    kernel = FakeLayoutKernel(monkeypatch, retained=False)

    def fail(*args):
        raise RuntimeError("private journal error")

    with pytest.raises(RecoverableStateError):
        replacement.prepare_manager_pruned_layout(
            recorded_children=(), pending_name=None, begin_child=fail, record_child=lambda *args: None
        )
    assert kernel.mkdir_calls == []

    stopping = False

    def begin(*args):
        nonlocal stopping
        stopping = True

    with pytest.raises(RecoverableStateError):
        replacement.prepare_manager_pruned_layout(
            recorded_children=(),
            pending_name=None,
            begin_child=begin,
            record_child=lambda *args: None,
            cancelled=lambda: stopping,
        )
    assert kernel.mkdir_calls == []


def test_record_failure_retains_pending_directory_for_explicit_resume(monkeypatch):
    kernel = FakeLayoutKernel(monkeypatch, retained=False)

    with pytest.raises(RecoverableStateError):
        replacement.prepare_manager_pruned_layout(
            recorded_children=(),
            pending_name=None,
            begin_child=lambda *args: None,
            record_child=lambda *args: (_ for _ in ()).throw(RuntimeError("lost acknowledgement")),
        )
    assert set(kernel.profiles) == {"root", "anchor-control"}

    layout = replacement.prepare_manager_pruned_layout(
        recorded_children=(),
        pending_name="anchor-control",
        begin_child=lambda *args: None,
        record_child=lambda *args: None,
    )
    assert kernel.mkdir_calls == list(anchor._CHILDREN)
    layout.close()


def test_callback_path_replacement_is_detected_before_mkdir(monkeypatch):
    kernel = FakeLayoutKernel(monkeypatch, retained=False)

    def replace_root(*args):
        kernel.profiles["root"] = _profile(kernel.root, 9999)

    with pytest.raises(RecoverableStateError):
        replacement.prepare_manager_pruned_layout(
            recorded_children=(), pending_name=None, begin_child=replace_root, record_child=lambda *args: None
        )
    assert kernel.mkdir_calls == []


class FakeSocket:
    def __init__(self):
        self.bound = None
        self.listened = []
        self.timeout = None
        self.closed = False

    def bind(self, path):
        self.bound = path

    def listen(self, count):
        self.listened.append(count)

    def settimeout(self, timeout):
        self.timeout = timeout

    def close(self):
        self.closed = True


def test_bind_only_channel_exposes_identity_then_listens_exactly_once(monkeypatch, tmp_path):
    listener = FakeSocket()
    validations = []
    service = anchor.ServiceIdentity(100, 1000, 555, "a" * 32, "/unit")
    layout = SimpleNamespace(
        service=service,
        profiles=(_profile("/sys/fs/cgroup/unit", 1),),
        validate=lambda: validations.append("layout"),
    )

    lease = object.__new__(anchor.AnchorChannelLease)
    lease.directory = tmp_path
    lease._directory_identity = (20, 21)
    lease._lock_identity = (20, 22)
    monkeypatch.setattr(anchor.AnchorChannelLease, "validate", lambda self: validations.append("lease"))
    monkeypatch.setattr(anchor.socket, "socket", lambda *args: listener)
    monkeypatch.setattr(anchor.socket, "AF_UNIX", 1, raising=False)
    monkeypatch.setattr(anchor.socket, "SOCK_STREAM", 1, raising=False)
    monkeypatch.setattr(anchor, "_private_directory", lambda path: (20, 21))
    monkeypatch.setattr(anchor, "_socket_identity", lambda path: (20, 23))

    channel = anchor.AnchorChannel.bind_only(layout, lease=lease)
    assert listener.bound == str(tmp_path / "control.sock") and listener.listened == []
    assert channel.bound_identity()["socket_identity"] == [20, 23]
    with pytest.raises(RecoverableStateError):
        channel.serve_once()

    channel.begin_serving()
    assert listener.listened == [4] and listener.timeout == 1.0
    with pytest.raises(RecoverableStateError):
        channel.begin_serving()
    channel.close()


def test_abandon_closes_borrowed_listener_without_unlinking_endpoint(monkeypatch, tmp_path):
    listener = FakeSocket()
    service = anchor.ServiceIdentity(100, 1000, 555, "a" * 32, "/unit")
    layout = SimpleNamespace(
        service=service,
        profiles=(_profile("/sys/fs/cgroup/unit", 1),),
        validate=lambda: None,
    )
    lease = object.__new__(anchor.AnchorChannelLease)
    lease.directory = tmp_path
    lease._directory_identity = (20, 21)
    lease._lock_identity = (20, 22)
    monkeypatch.setattr(anchor.AnchorChannelLease, "validate", lambda self: None)
    monkeypatch.setattr(anchor.socket, "socket", lambda *args: listener)
    monkeypatch.setattr(anchor.socket, "AF_UNIX", 1, raising=False)
    monkeypatch.setattr(anchor.socket, "SOCK_STREAM", 1, raising=False)
    monkeypatch.setattr(anchor, "_private_directory", lambda path: (20, 21))
    monkeypatch.setattr(anchor, "_socket_identity", lambda path: (20, 23))

    channel = anchor.AnchorChannel.bind_only(layout, lease=lease)
    channel.begin_serving()
    channel.abandon()
    assert listener.closed and channel._identity == (20, 23)
    assert channel.path == tmp_path / "control.sock"

    # Generic finally blocks may still call close; abandonment remains
    # idempotent and must not invoke the unlinking close path.
    channel.abandon()
    channel.close()
    assert channel._identity == (20, 23)


def test_default_channel_still_binds_and_listens(monkeypatch, tmp_path):
    listener = FakeSocket()
    service = anchor.ServiceIdentity(100, 1000, 555, "a" * 32, "/unit")
    layout = SimpleNamespace(
        service=service,
        profiles=(_profile("/sys/fs/cgroup/unit", 1),),
        validate=lambda: None,
    )
    lease = SimpleNamespace(
        directory=tmp_path,
        _directory_identity=(20, 21),
        _lock_identity=(20, 22),
        validate=lambda: None,
    )
    monkeypatch.setattr(anchor.socket, "socket", lambda *args: listener)
    monkeypatch.setattr(anchor.socket, "AF_UNIX", 1, raising=False)
    monkeypatch.setattr(anchor.socket, "SOCK_STREAM", 1, raising=False)
    monkeypatch.setattr(anchor, "_private_directory", lambda path: (20, 21))
    monkeypatch.setattr(anchor, "_socket_identity", lambda path: (20, 23))

    channel = anchor.AnchorChannel(layout, lease=lease)
    assert listener.listened == [4] and listener.timeout == 1.0
    channel.close()
