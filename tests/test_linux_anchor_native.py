"""Real cgroup/socket operations with FIXTURE systemd properties, not installed qualification."""

import json
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

if __name__ == "__main__":
    import types

    for name, folder in (("drift", "drift"), ("drift.node", "drift/node")):
        module = types.ModuleType(name)
        module.__path__ = [str(Path(__file__).resolve().parents[1] / "src" / folder)]
        sys.modules[name] = module
else:
    import pytest

from drift.node import linux_anchor as anchor, linux_cgroup_process as native
from drift.node.resource_recovery import RecoverableStateError


def fixture_properties(pid, group):
    return dict(
        Id=anchor.UNIT,
        LoadState="loaded",
        ActiveState="active",
        SubState="running",
        MainPID=str(pid),
        ControlGroup=group,
        InvocationID="a" * 32,
        Delegate="yes",
        Type="exec",
        KillMode="control-group",
        Restart="no",
        Transient="no",
    )


def child(root, directory, mode):
    group = Path("/proc/self/cgroup").read_text()[3:].strip()
    anchor._query_properties = lambda: fixture_properties(os.getpid(), group)
    anchor._runtime_directory = lambda: directory
    if mode == "signals":
        channel_class = anchor.AnchorChannel

        def ready_channel(layout):
            channel = channel_class(layout)
            print(json.dumps(dict(pid=os.getpid(), group=group)), flush=True)
            return channel

        anchor.AnchorChannel = ready_channel
        assert anchor.serve_anchor() == 0
        return
    if mode == "existing":
        (root / "retained").mkdir()
        try:
            anchor.AnchorLayout()
        except RecoverableStateError:
            assert (root / "retained").exists() and not (root / "anchor-control").exists()
            return
        raise AssertionError("adopted existing cgroup")
    layout = anchor.AnchorLayout()
    original = layout.receipt

    def receipt(nonce):
        value = original(nonce)
        if mode == "nonce":
            value["nonce"] = "0" * 64
        elif mode == "boolean":
            value["version"] = True
        elif mode == "permission":
            value["maintenance"] = True
        elif mode == "stale":
            value["service"]["invocation"] = "b" * 32
        elif mode == "late-subgroup":
            (root / "late-subgroup").mkdir()
        return value

    layout.receipt = receipt
    channel = anchor.AnchorChannel(layout)
    print(json.dumps(dict(pid=os.getpid(), group=group, profiles=[p.to_json() for p in layout.profiles])), flush=True)
    try:
        while not (directory / "stop").exists():
            channel.serve_once()
    finally:
        channel.close()
        layout.close()


if __name__ != "__main__":
    pytestmark = pytest.mark.skipif(
        not sys.platform.startswith("linux") or not os.environ.get("COMMUNITYAI_TEST_CGROUP_ROOT"),
        reason="requires private native cgroup fixture",
    )

    @pytest.fixture
    def running(tmp_path, monkeypatch):
        root = Path(os.environ["COMMUNITYAI_TEST_CGROUP_ROOT"]) / ("anchor-" + uuid4().hex)
        root.mkdir(mode=0o700)
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        children = []

        def start(mode="normal"):
            process = native.spawn(
                fd,
                [sys.executable, str(Path(__file__).resolve()), str(root), str(tmp_path), mode],
                env=os.environ.copy(),
            )
            children.append(process)
            process.resume()
            if mode == "existing":
                assert process.wait(timeout=8) == 0, process.stdout.read()
                return process, None
            assert select.select([process.stdout], [], [], 10)[0], "anchor did not report ready"
            line = process.stdout.readline()
            assert line.startswith("{"), line + process.stdout.read()
            report = json.loads(line)
            monkeypatch.setattr(anchor, "_query_properties", lambda: fixture_properties(report["pid"], report["group"]))
            monkeypatch.setattr(anchor, "_runtime_directory", lambda: tmp_path)
            return process, report

        yield root, tmp_path, start
        (root / "cgroup.kill").write_text("1")
        for process in children:
            process.wait(timeout=8)
            process.stdout.close()
        deadline = time.monotonic() + 5
        while "populated 1" in (root / "cgroup.events").read_text():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        # Only the explicit test-owned subtree is removed after native emptiness.
        for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_dir():
                path.rmdir()
        os.close(fd)
        root.rmdir()

    def test_native_layout_and_fresh_reconnect_are_not_a_drain_grant(running):
        root, directory, start = running
        process, report = start()
        assert (root / "cgroup.procs").read_text() == ""
        assert (root / "anchor-control" / "cgroup.procs").read_text().split() == [str(process.pid)]
        first = anchor.inspect_anchor()
        second = anchor.inspect_anchor()
        assert first["nonce"] != second["nonce"] and first["layout_digest"] == second["layout_digest"]
        assert first["service"]["pid"] == process.pid and first["maintenance"] is False
        assert first["admission"] is False and first["node_generation"] is None
        (directory / "stop").touch()
        assert process.wait(timeout=8) == 0
        assert not (directory / "communityai-multigpu-anchor" / "control.sock").exists()
        assert all((root / name).exists() for name in anchor._CHILDREN)

    @pytest.mark.parametrize("mode", ["nonce", "boolean", "permission", "stale", "late-subgroup"])
    def test_native_client_rejects_changed_receipt(running, mode):
        _, _, start = running
        start(mode)
        with pytest.raises(RecoverableStateError):
            anchor.inspect_anchor()

    def test_native_existing_layout_is_retained_not_adopted(running):
        root, _, start = running
        start("existing")
        assert (root / "retained").exists() and not (root / "workers").exists()

    @pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
    def test_native_orderly_signal_closes_only_own_socket_not_cgroups(running, signum):
        root, directory, start = running
        process, _ = start("signals")
        assert anchor.inspect_anchor()["admission"] is False
        os.kill(process.pid, signum)
        assert process.wait(timeout=8) == 0
        assert not (directory / "communityai-multigpu-anchor" / "control.sock").exists()
        assert (directory / "communityai-multigpu-anchor" / "anchor.lock").exists()
        assert all((root / name).is_dir() for name in anchor._CHILDREN)

    def test_native_replaced_worker_root_refuses_attestation(running):
        root, _, start = running
        start()
        (root / "workers").rmdir()
        (root / "workers").mkdir()
        with pytest.raises(RecoverableStateError):
            anchor.inspect_anchor()

    @pytest.mark.parametrize("name", ["unexpected-sibling", "anchor-control/unexpected-child"])
    def test_native_new_subgroups_refuse_receipt_without_cleanup(running, name):
        root, _, start = running
        start()
        unexpected = root / name
        unexpected.mkdir()
        with pytest.raises(RecoverableStateError):
            anchor.inspect_anchor()
        assert unexpected.exists()

    def test_native_extra_control_process_refuses_receipt_without_killing_it(running):
        root, _, start = running
        start()
        fd = os.open(root / "anchor-control", os.O_RDONLY | os.O_DIRECTORY)
        try:
            extra = native.spawn(fd, [sys.executable, "-c", "import time;time.sleep(30)"], env=os.environ.copy())
            extra.resume()
        finally:
            os.close(fd)
        try:
            with pytest.raises(RecoverableStateError):
                anchor.inspect_anchor()
            assert extra.poll() is None
        finally:
            extra.kill()
            extra.wait(timeout=5)
            extra.stdout.close()

    def test_native_replaced_lock_refuses_receipt_and_retains_foreign_file(running):
        _, directory, start = running
        process, _ = start()
        lock = directory / "communityai-multigpu-anchor" / "anchor.lock"
        lock.rename(lock.with_name("old.lock"))
        lock.write_bytes(b"replacement")
        lock.chmod(0o600)
        with pytest.raises(RecoverableStateError):
            anchor.inspect_anchor()
        assert process.wait(timeout=5) != 0
        assert lock.read_bytes() == b"replacement"

    def test_native_forged_socket_peer_refused_before_sending_request(running, monkeypatch):
        _, directory, start = running
        process, _ = start()
        os.kill(process.pid, signal.SIGSTOP)
        path = directory / "communityai-multigpu-anchor" / "control.sock"
        path.rename(path.with_name("original.sock"))
        received = []
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as impostor:
            impostor.bind(str(path))
            path.chmod(0o600)
            impostor.listen(1)
            impostor.settimeout(5)

            def listen():
                connection, _ = impostor.accept()
                with connection:
                    connection.settimeout(5)
                    received.append(connection.recv(100))

            thread = threading.Thread(target=listen)
            thread.start()
            try:
                with pytest.raises(RecoverableStateError):
                    anchor.inspect_anchor()
            finally:
                thread.join(6)
            assert not thread.is_alive() and received == [b""]

    def test_native_duplicate_channel_cannot_unlink_live_socket(running):
        _, directory, start = running
        start()
        path = directory / "communityai-multigpu-anchor" / "control.sock"
        identity = path.stat().st_ino

        class ReadOnlyLayout:
            def validate(self):
                pytest.fail("second channel passed singleton lock")

        with pytest.raises(OSError):
            anchor.AnchorChannel(ReadOnlyLayout())
        assert path.stat().st_ino == identity
        assert anchor.inspect_anchor()["maintenance"] is False

    def test_native_stale_socket_is_never_removed(running):
        _, directory, start = running
        process, _ = start()
        path = directory / "communityai-multigpu-anchor" / "control.sock"
        identity = path.stat().st_ino
        process.kill()
        process.wait(timeout=5)

        class ReadOnlyLayout:
            def validate(self):
                pass

        with pytest.raises(OSError):
            anchor.AnchorChannel(ReadOnlyLayout())
        assert path.stat().st_ino == identity

    @pytest.mark.parametrize("kind", ["symlink", "hardlink", "public", "directory"])
    def test_native_unsafe_lock_file_is_never_accepted(running, kind):
        _, directory, start = running
        start()
        lock = directory / "communityai-multigpu-anchor" / "anchor.lock"
        original = lock.with_name("original.lock")
        lock.rename(original)
        if kind == "symlink":
            lock.symlink_to(original)
        elif kind == "hardlink":
            os.link(original, lock)
        elif kind == "directory":
            lock.mkdir()
        else:
            lock.touch(mode=0o644)

        class ReadOnlyLayout:
            def validate(self):
                pytest.fail("unsafe lock reached layout")

        with pytest.raises((OSError, RecoverableStateError)):
            anchor.AnchorChannel(ReadOnlyLayout())
        assert lock.exists()

    def test_native_dead_anchor_does_not_turn_stale_socket_into_authority(running):
        _, directory, start = running
        process, _ = start()
        process.kill()
        process.wait(timeout=8)
        assert (directory / "communityai-multigpu-anchor" / "control.sock").exists()
        with pytest.raises(RecoverableStateError):
            anchor.inspect_anchor()

    @pytest.mark.parametrize(
        "payload",
        [
            b'{"version":1,"version":1}',
            b'{"version":true}',
            b'{"version":1,"profile":"multigpu-volunteer","operation":"start","nonce":"' + b"a" * 64 + b'"}',
            b'{"version":1,"profile":"multigpu-volunteer","operation":"inspect","nonce":"'
            + b"a" * 64
            + b'","argv":["shell"]}',
            b"x" * (anchor._LIMIT + 1),
            b"[" * 2000 + b"0" + b"]" * 2000,
        ],
        ids=["duplicate", "boolean", "start", "argv", "oversized", "deep-json"],
    )
    def test_native_unknown_or_ambiguous_request_has_no_response_or_effect(running, payload):
        root, directory, start = running
        start()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(str(directory / "communityai-multigpu-anchor" / "control.sock"))
            connection.sendall(payload)
            connection.shutdown(socket.SHUT_WR)
            try:
                assert connection.recv(1000) == b""
            except ConnectionResetError:
                pass
        assert anchor.inspect_anchor()["admission"] is False
        assert (root / "nodes" / "cgroup.procs").read_text() == ""
        assert (root / "workers" / "cgroup.procs").read_text() == ""

    def test_native_slow_client_is_bounded_and_does_not_break_next_reconnect(running):
        _, directory, start = running
        start()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(str(directory / "communityai-multigpu-anchor" / "control.sock"))
            connection.sendall(b"{")
            try:
                assert connection.recv(100) == b""
            except ConnectionResetError:
                pass
        assert anchor.inspect_anchor()["operation"] == "inspect"

    def test_native_uid_mismatch_rejected(monkeypatch):
        class Peer:
            def getsockopt(self, *args):
                return anchor.struct.pack("3i", 123, os.geteuid() + 1, 0)

        with pytest.raises(RecoverableStateError):
            anchor._peer(Peer())


if __name__ == "__main__":
    child(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])
