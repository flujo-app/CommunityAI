"""Native Unix filesystem negative seams with explicit admitted-identity fixture."""

import os
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

from drift.node import linux_node_channel as channel
from drift.node.linux_node_identity import make_identity
from drift.node.resource_recovery import RecoverableStateError

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="native Unix filesystem/socket checks")


@pytest.fixture
def local_channel(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    identity = make_identity({}, dict(id="a" * 32, pid=os.getpid(), start_ticks=123, cgroup={}))
    monkeypatch.setattr(channel.anchor, "_channel_directory", lambda: tmp_path)
    monkeypatch.setattr(channel, "admitted_control_identity", lambda: identity)
    return tmp_path, identity


def test_stale_socket_collision_never_unlinks_existing_listener(local_channel):
    directory, identity = local_channel
    owner = channel.NodeControlSocket(identity)
    try:
        original = owner.path.stat()
        with pytest.raises(OSError):
            channel.NodeControlSocket(identity)
        assert owner.path.stat().st_ino == original.st_ino
        owner.validate()
    finally:
        owner.close()
    assert not owner.path.exists()


@pytest.mark.parametrize("replacement", ["socket", "directory"])
def test_socket_close_retains_replaced_evidence(local_channel, replacement):
    directory, identity = local_channel
    owner = channel.NodeControlSocket(identity)
    displaced = directory.with_name(directory.name + "-retained")
    if replacement == "directory":
        directory.rename(displaced)
        directory.mkdir(mode=0o700)
    else:
        owner.path.rename(owner.path.with_suffix(".retained"))
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as other:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            other.bind(f"/proc/self/fd/{descriptor}/{owner.path.name}")
            os.chmod(owner.path, 0o600)
            inode = owner.path.stat().st_ino
            with pytest.raises(RecoverableStateError):
                owner.close()
            assert owner.path.stat().st_ino == inode
            assert owner.descriptor is None
        finally:
            os.close(descriptor)


@pytest.mark.parametrize("fault", ["tcp", "start", "run", None])
def test_actual_listener_cleanup_and_noninheritance_on_all_serve_paths(local_channel, fault):
    directory, identity = local_channel
    listeners = []

    def bind():
        if fault == "tcp":
            raise SystemExit(1)
        value = socket.socket()
        value.bind(("127.0.0.1", 0))
        value.set_inheritable(True)  # Current Uvicorn's bind_socket behavior.
        listeners.append(value)
        return value

    def before():
        if fault == "start":
            raise RuntimeError("start fixture")

    def run(*, sockets):
        assert len(sockets) == 2 and all(not s.get_inheritable() for s in sockets)
        targets = {str(s.fileno()): os.readlink(f"/proc/self/fd/{s.fileno()}") for s in sockets}
        code = "import os,sys,json; expected=json.loads(sys.argv[1]); assert all(not os.path.exists('/proc/self/fd/'+fd) or os.readlink('/proc/self/fd/'+fd)!=value for fd,value in expected.items())"
        import json

        subprocess.run([sys.executable, "-c", code, json.dumps(targets)], close_fds=False, check=True, timeout=10)
        if fault == "run":
            raise RuntimeError("run fixture")
        for value in sockets:
            value.close()  # Actual Uvicorn also owns/closes supplied sockets.

    server = SimpleNamespace(
        config=SimpleNamespace(bind_socket=bind, app=SimpleNamespace(state=SimpleNamespace())), run=run
    )
    if fault is None:
        channel.run_node_server(server, identity, before_run=before)
    else:
        with pytest.raises((RuntimeError, SystemExit)):
            channel.run_node_server(server, identity, before_run=before)
    assert not (directory / ("api-" + identity["generation"] + ".sock")).exists()
    assert all(value.fileno() == -1 for value in listeners)


def test_socket_creation_failure_closes_held_directory(local_channel, monkeypatch):
    directory, identity = local_channel
    opened = []
    original = channel._directory

    def capture():
        result = original()
        opened.append(result[2])
        return result

    monkeypatch.setattr(channel, "_directory", capture)

    def fail(*args):
        raise OSError("fixture no descriptors")

    monkeypatch.setattr(channel.socket, "socket", fail)
    with pytest.raises(OSError):
        channel.NodeControlSocket(identity)
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_tcp_close_failure_still_removes_owned_unix_socket_and_closes_fd(local_channel, monkeypatch):
    directory, identity = local_channel
    channels = []
    original = channel.NodeControlSocket

    def capture(value):
        owner = original(value)
        channels.append(owner)
        return owner

    class FaultyTCP:
        def set_inheritable(self, value):
            assert value is False

        def get_inheritable(self):
            return False

        def close(self):
            raise OSError("fixture TCP close failed")

    monkeypatch.setattr(channel, "NodeControlSocket", capture)
    server = SimpleNamespace(
        config=SimpleNamespace(bind_socket=FaultyTCP, app=SimpleNamespace(state=SimpleNamespace())),
        run=lambda **kwargs: None,
    )
    with pytest.raises(OSError, match="TCP close failed"):
        channel.run_node_server(server, identity)
    assert len(channels) == 1
    assert channels[0].descriptor is None and channels[0].listener.fileno() == -1
    assert not (directory / ("api-" + identity["generation"] + ".sock")).exists()
