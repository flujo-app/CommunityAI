"""Opt-in native clone3 tests in an explicitly delegated private test cgroup.

Runs with stdlib unittest as well as pytest. No delegation is created here: the
runner must supply COMMUNITYAI_TEST_CGROUP_ROOT. An optional exact extension
path permits the standalone compiler image to run without application imports.
"""

import importlib.util
import json
import os
import platform
import signal
import socket
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4


def _standalone_imports():
    extension = os.environ.get("COMMUNITYAI_NATIVE_TEST_EXTENSION")
    if not extension:
        return
    source = Path(__file__).resolve().parents[1] / "src" / "drift"
    for name, directory in (("drift", source), ("drift.node", source / "node")):
        package = types.ModuleType(name)
        package.__path__ = [str(directory)]
        sys.modules[name] = package
    spec = importlib.util.spec_from_file_location("drift.node._linux_cgroup_spawn", extension)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules[spec.name] = module


if __name__ == "__main__":
    _standalone_imports()


def _wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("native cgroup fixture did not reach the required state")


def _populated(directory):
    values = dict(line.split() for line in (directory / "cgroup.events").read_text().splitlines())
    return values["populated"] == "1"


def _owner_helper(leaf, evidence, resumed, private_input=False):
    import fcntl

    from drift.node.linux_cgroup_process import spawn

    evidence = Path(evidence)
    lease = os.open(evidence / "owner.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lease, fcntl.LOCK_EX)
    group = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY)
    child_code = (
        "import os,subprocess,sys,time;from pathlib import Path;"
        "Path(sys.argv[1]).write_text('executed');"
        "p=subprocess.Popen([sys.executable,'-c',"
        "'import os,time;from pathlib import Path;Path(os.environ[\"GRANDCHILD\"]).write_text(str(os.getpid()));time.sleep(60)'],"
        "start_new_session=True);time.sleep(60)"
    )
    read_fd = None
    options = {}
    if private_input:
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        os.close(write_fd)
        options = dict(input_fd=read_fd, text=False, stderr=subprocess.DEVNULL)
    try:
        process = spawn(
            group, [sys.executable, "-c", child_code, str(evidence / "executed")], env=os.environ.copy(), **options
        )
    finally:
        if read_fd is not None:
            os.close(read_fd)
    if resumed:
        process.resume()
    (evidence / "owner-ready.json").write_text(json.dumps({"pid": process.pid, "identity": process.cgroup_identity}))
    while True:
        time.sleep(1)


def _denied_probe_helper(name):
    """Install one irreversible filter only in this isolated fixture process."""
    import ctypes
    import errno

    from drift.node.linux_cgroup_process import LinuxCgroupProcessError, validate_capability

    # The native acceptance image is x86-64. Other ABIs need their own syscall
    # table qualification; never apply an assumed syscall number to an ABI.
    if platform.machine() != "x86_64":
        raise AssertionError("seccomp fixture requires its qualified x86-64 ABI")
    numbers = {"close_range": 436, "pidfd_send_signal": 424, "waitid": 247, "clone3": 435}

    class Filter(ctypes.Structure):
        _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte), ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint32)]

    class Program(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ushort), ("filters", ctypes.POINTER(Filter))]

    rules = (Filter * 7)(
        Filter(0x20, 0, 0, 4),  # seccomp_data.arch
        Filter(0x15, 1, 0, 0xC000003E),  # AUDIT_ARCH_X86_64
        Filter(0x06, 0, 0, 0x80000000),  # Unexpected ABI: kill only this fixture.
        Filter(0x20, 0, 0, 0),  # seccomp_data.nr
        Filter(0x15, 0, 1, numbers[name]),
        Filter(0x06, 0, 0, 0x00050000 | errno.EPERM),
        Filter(0x06, 0, 0, 0x7FFF0000),
    )
    program = Program(len(rules), rules)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0 or libc.prctl(22, 2, ctypes.byref(program), 0, 0) != 0:
        raise AssertionError("fixture could not install its bounded seccomp filter")
    try:
        validate_capability()
    except LinuxCgroupProcessError as error:
        if str(error) != "Linux cgroup process creation is unavailable":
            raise AssertionError("native denial exposed an unfixed public error")
    else:
        raise AssertionError("denied native operation passed explicit-profile preflight")


def _closed_stdio_helper(leaf):
    """Exercise collisions without changing the test runner's own standard FDs."""
    import fcntl

    from drift.node.linux_cgroup_process import spawn

    group = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY)
    read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
    safe_group = fcntl.fcntl(group, fcntl.F_DUPFD_CLOEXEC, 10)
    safe_read = fcntl.fcntl(read_fd, fcntl.F_DUPFD_CLOEXEC, 10)
    os.write(write_fd, b"collision fixture")
    for descriptor in (group, read_fd, write_fd, 0, 1, 2):
        os.close(descriptor)
    process = spawn(
        safe_group,
        [sys.executable, "-c", "import os;os.write(1,os.read(0,100));os.write(2,b'discarded')"],
        env={},
        input_fd=safe_read,
        stderr=subprocess.DEVNULL,
        text=False,
    )
    os.fstat(safe_group)
    os.fstat(safe_read)
    process.resume()
    if process.wait(timeout=5) != 0 or process.stdout.read() != b"collision fixture":
        raise AssertionError("private pipe failed with closed standard descriptors")


class LinuxCgroupProcessContractTests(unittest.TestCase):
    def test_unsupported_contract_is_rejected_before_backend_access(self):
        from drift.node import linux_cgroup_process as backend

        cases = (
            {"cgroup_fd": False},
            {"command": "not-a-vector"},
            {"command": ["relative"]},
            {"stdin": None},
            {"stdout": None},
            {"stderr": None},
            {"text": False},
            {"creationflags": True},
            {"start_new_session": 1},
            {"env": {"KEY": "private\0value"}},
            {"env": {"BAD=KEY": "value"}},
            {"env": {"KEY": False}},
            {"cwd": "relative"},
            {"input_fd": False, "text": False, "stderr": subprocess.DEVNULL},
            {"input_fd": -1, "text": False, "stderr": subprocess.DEVNULL},
            {"input_fd": 2, "text": False, "stderr": subprocess.DEVNULL},
            {"input_fd": 3},
            {"input_fd": 3, "text": False},
            {"input_fd": 3, "stderr": subprocess.DEVNULL},
            {"stderr": subprocess.DEVNULL},
        )
        for change in cases:
            arguments = {"cgroup_fd": 1, "command": [sys.executable], "env": {}}
            arguments.update(change)
            with self.subTest(change=change), patch.object(backend, "_backend") as native:
                with self.assertRaisesRegex(
                    backend.LinuxCgroupProcessError, "^Linux cgroup process creation is unavailable$"
                ):
                    backend.spawn(**arguments)
                native.assert_not_called()

    def test_older_extension_rejects_private_transport_without_fallback(self):
        from drift.node import linux_cgroup_process as backend

        with patch.object(backend, "_backend") as native:
            native.return_value.spawn.side_effect = TypeError("old five-argument ABI")
            with self.assertRaisesRegex(
                backend.LinuxCgroupProcessError, "^Linux cgroup process creation is unavailable$"
            ):
                backend.spawn(10, [sys.executable], env={}, input_fd=11, stderr=subprocess.DEVNULL, text=False)
            native.return_value.spawn.assert_called_once_with(10, (sys.executable,), (), None, True, 11)

    def test_unavailable_capability_has_fixed_public_error(self):
        from drift.node import linux_cgroup_process as backend

        with patch.object(backend, "_backend", side_effect=RuntimeError("private path and native error")):
            with self.assertRaises(backend.LinuxCgroupProcessError) as error:
                backend.validate_capability()
            self.assertEqual(str(error.exception), "Linux cgroup process creation is unavailable")

        for code in (1, 9, 22, 38):  # EPERM, EBADF, EINVAL, ENOSYS from the backend.
            with self.subTest(code=code), patch.object(backend, "_backend") as native:
                native.return_value.validate.side_effect = OSError(code, "private native details")
                with self.assertRaises(backend.LinuxCgroupProcessError) as error:
                    backend.validate_capability()
                self.assertEqual(str(error.exception), "Linux cgroup process creation is unavailable")
                native.return_value.spawn.assert_not_called()


@unittest.skipUnless(
    sys.platform.startswith("linux") and os.environ.get("COMMUNITYAI_TEST_CGROUP_ROOT"),
    "requires opt-in isolated Linux cgroup delegation and compiled native backend",
)
class LinuxCgroupProcessNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from drift.node import linux_cgroup_process

        cls.backend = linux_cgroup_process
        cls.root = Path(os.environ["COMMUNITYAI_TEST_CGROUP_ROOT"]).resolve(strict=True)
        if not cls.root.is_absolute() or not (cls.root / "cgroup.kill").is_file():
            raise AssertionError("the test runner must supply an explicit cgroup-v2 root")
        cls.backend.validate_capability()

    def setUp(self):
        self.leaf = self.root / ("native-" + uuid4().hex)
        self.leaf.mkdir(mode=0o700)
        self.group_fd = os.open(self.leaf, os.O_RDONLY | os.O_DIRECTORY)
        self.temporary = tempfile.TemporaryDirectory(prefix="communityai-native-")
        self.evidence = Path(self.temporary.name)
        self.children = []
        self.owners = []

    def tearDown(self):
        for owner in self.owners:
            if owner.poll() is None:
                owner.kill()
            owner.wait(timeout=5)
            if owner.stdout is not None:
                owner.stdout.close()
        # Direct-child exit is deliberately not used as whole-tree proof.
        (self.leaf / "cgroup.kill").write_text("1")
        _wait_for(lambda: not _populated(self.leaf))
        for process in self.children:
            process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
        os.close(self.group_fd)
        self.leaf.rmdir()
        self.temporary.cleanup()

    def spawn(self, code, *arguments, **kwargs):
        kwargs.setdefault("env", os.environ.copy())
        process = self.backend.spawn(self.group_fd, [sys.executable, "-c", code, *arguments], **kwargs)
        self.children.append(process)
        return process

    def start_owner(self, resumed, private_input=False):
        environment = os.environ.copy()
        environment["GRANDCHILD"] = str(self.evidence / "grandchild")
        owner = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--native-owner",
                str(self.leaf),
                str(self.evidence),
                str(int(resumed)),
                str(int(private_input)),
            ],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.owners.append(owner)

        def ready():
            if owner.poll() is not None:
                raise AssertionError("native owner failed: " + owner.stdout.read())
            return (self.evidence / "owner-ready.json").exists()

        _wait_for(ready)
        return owner

    def test_atomic_birth_precedes_execution_and_identity_is_borrowed(self):
        marker = self.evidence / "executed"
        process = self.spawn("from pathlib import Path;import sys;Path(sys.argv[1]).write_text('ran')", str(marker))
        identity = os.fstat(self.group_fd)
        self.assertEqual(process.cgroup_identity, (identity.st_dev, identity.st_ino))
        self.assertIn(str(process.pid), (self.leaf / "cgroup.procs").read_text().split())
        self.assertIsNone(process.poll())
        self.assertTrue(_populated(self.leaf))
        with self.assertRaises(AttributeError):
            process.cgroup_identity = (0, 0)
        with self.assertRaises(subprocess.TimeoutExpired):
            process.wait(timeout=0.03)
        self.assertFalse(marker.exists())
        process.resume()
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertEqual(marker.read_text(), "ran")
        self.assertEqual(os.fstat(self.group_fd).st_ino, identity.st_ino)
        with self.assertRaises(self.backend.LinuxCgroupProcessError):
            process.resume()

    def test_private_pipe_delivers_exact_binary_eof_without_command_or_environment_copy(self):
        payload = b"private-transport-fixture\0\xff\xfe" + bytes(range(256)) * 4
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        os.write(write_fd, payload)
        os.close(write_fd)
        identity = os.fstat(read_fd)
        try:
            process = self.spawn(
                "import os,sys;data=sys.stdin.buffer.read();os.write(1,data)",
                env={},
                input_fd=read_fd,
                stderr=subprocess.DEVNULL,
                text=False,
            )
            self.assertEqual(os.fstat(read_fd), identity)
            self.assertIsNone(process.poll())
            self.assertIn(str(process.pid), (self.leaf / "cgroup.procs").read_text().split())
            self.assertNotIn(b"private-transport-fixture", Path(f"/proc/{process.pid}/cmdline").read_bytes())
            self.assertNotIn(b"private-transport-fixture", Path(f"/proc/{process.pid}/environ").read_bytes())
            self.assertEqual(os.readlink(f"/proc/{process.pid}/fd/0"), os.readlink(f"/proc/self/fd/{read_fd}"))
            self.assertEqual(os.readlink(f"/proc/{process.pid}/fd/2"), "/dev/null")
        finally:
            # Closing the caller's borrowed end must not close the child's copy.
            os.close(read_fd)
        process.resume()
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertEqual(process.stdout.read(), payload)
        self.assertEqual(process.stdout.read(), b"")

    def test_private_stderr_flood_is_discarded_and_post_exec_authority_fds_are_closed(self):
        import fcntl

        lease = os.open(self.evidence / "private-lease", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(lease, fcntl.LOCK_EX)
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        os.close(write_fd)
        code = (
            "import os,json;fds={};"
            'exec(\'for name in os.listdir("/proc/self/fd"):\\n try: fds[name]=os.readlink("/proc/self/fd/"+name)'
            "\\n except FileNotFoundError: pass');"
            "os.write(2,b'fixture-private-backend-error'*65536);"
            "os.write(1,json.dumps(fds).encode())"
        )
        try:
            process = self.spawn(code, input_fd=read_fd, stderr=subprocess.DEVNULL, text=False)
        finally:
            os.close(read_fd)
            # No LOCK_UN: the gated child must already have closed its copy.
            os.close(lease)
        probe = os.open(self.evidence / "private-lease", os.O_RDWR)
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)
        process.resume()
        self.assertEqual(process.wait(timeout=5), 0)
        descriptors = json.loads(process.stdout.read())
        self.assertEqual(set(descriptors), {"0", "1", "2"})
        self.assertTrue(descriptors["0"].startswith("pipe:["))
        self.assertTrue(descriptors["1"].startswith("pipe:["))
        self.assertEqual(descriptors["2"], "/dev/null")

    def test_private_input_rejections_are_before_birth_and_do_not_leak_descriptors(self):
        from drift.node import _linux_cgroup_spawn

        regular = os.open(self.evidence / "input", os.O_CREAT | os.O_RDONLY, 0o600)
        directory = os.open(self.evidence, os.O_RDONLY | os.O_DIRECTORY)
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        nonblock, nonblock_write = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
        rdwr = os.open(f"/proc/self/fd/{read_fd}", os.O_RDWR | os.O_CLOEXEC)
        path_only = os.open(f"/proc/self/fd/{read_fd}", os.O_PATH | os.O_CLOEXEC)
        left, right = socket.socketpair()
        closed = os.dup(regular)
        os.close(closed)
        cases = (
            None,
            True,
            False,
            -1,
            0,
            1,
            2,
            1 << 100,
            "3",
            regular,
            directory,
            write_fd,
            nonblock,
            rdwr,
            path_only,
            left.fileno(),
            closed,
        )
        baseline = set(os.listdir("/proc/self/fd"))
        try:
            for descriptor in cases:
                with self.subTest(descriptor=descriptor):
                    for _ in range(3):
                        with self.assertRaisesRegex(RuntimeError, "^Linux cgroup process creation is unavailable$"):
                            _linux_cgroup_spawn.spawn(
                                self.group_fd, (sys.executable, "-c", "pass"), (), None, True, descriptor
                            )
                        self.assertFalse(_populated(self.leaf))
                        self.assertEqual(set(os.listdir("/proc/self/fd")), baseline)
            # A valid borrowed pipe is retained even when another preflight fails.
            with self.assertRaises(RuntimeError):
                _linux_cgroup_spawn.spawn(regular, (sys.executable,), (), None, True, read_fd)
            os.fstat(read_fd)
            self.assertEqual(set(os.listdir("/proc/self/fd")), baseline)
        finally:
            for descriptor in (regular, directory, read_fd, write_fd, nonblock, nonblock_write, rdwr, path_only):
                os.close(descriptor)
            left.close()
            right.close()

    def test_private_input_handles_closed_stdio_descriptor_collisions(self):
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--native-closed-stdio", str(self.leaf)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        _wait_for(lambda: not _populated(self.leaf))

    def test_original_five_argument_native_transport_keeps_null_input_and_merged_stderr(self):
        process = self.spawn("import os;assert os.read(0,1)==b'';os.write(1,b'output');os.write(2,b'error')")
        process.resume()
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertEqual(process.stdout.read(), "outputerror")

    def test_private_transport_failed_exec_retains_caller_pipe_and_reaps(self):
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        os.close(write_fd)
        try:
            process = self.backend.spawn(
                self.group_fd,
                [str(self.evidence / "absent-executable")],
                env={},
                input_fd=read_fd,
                stderr=subprocess.DEVNULL,
                text=False,
            )
            self.children.append(process)
            with self.assertRaises(self.backend.LinuxCgroupProcessError):
                process.resume()
            self.assertIsNotNone(process.wait(timeout=5))
            self.assertEqual(process.stdout.read(), b"")
            os.fstat(read_fd)
            _wait_for(lambda: not _populated(self.leaf))
        finally:
            os.close(read_fd)

    def test_private_transport_owner_death_before_release_never_executes(self):
        owner = self.start_owner(False, private_input=True)
        owner.kill()
        owner.wait(timeout=5)
        _wait_for(lambda: not _populated(self.leaf))
        self.assertFalse((self.evidence / "executed").exists())

    def test_private_transport_owner_death_after_release_retains_complete_tree(self):
        owner = self.start_owner(True, private_input=True)
        _wait_for((self.evidence / "grandchild").exists)
        owner.kill()
        owner.wait(timeout=5)
        self.assertGreaterEqual(len((self.leaf / "cgroup.procs").read_text().split()), 2)
        (self.leaf / "cgroup.kill").write_text("1")
        _wait_for(lambda: not _populated(self.leaf))

    def test_synchronous_gate_write_failure_reaps_without_executing_body(self):
        marker = self.evidence / "executed"
        process = self.spawn("from pathlib import Path;import sys;Path(sys.argv[1]).touch()", str(marker))
        gate = process._native.descriptors()[1]
        original = os.write

        def failed_write(descriptor, data):
            if descriptor == gate:
                raise BrokenPipeError("controlled gate failure")
            return original(descriptor, data)

        with patch.object(self.backend.os, "write", side_effect=failed_write):
            with self.assertRaises(self.backend.LinuxCgroupProcessError):
                process.resume()
        self.assertIsNotNone(process.poll())
        self.assertFalse(marker.exists())
        _wait_for(lambda: not _populated(self.leaf))

    def test_duplicate_resume_does_not_kill_an_already_running_child(self):
        process = self.spawn("import time;time.sleep(60)")
        process.resume()
        with self.assertRaises(self.backend.LinuxCgroupProcessError):
            process.resume()
        self.assertIsNone(process.poll())
        self.assertTrue(_populated(self.leaf))

    def test_ready_closes_inherited_owner_lease_before_execution(self):
        import fcntl

        filename = self.evidence / "lease"
        descriptor = os.open(filename, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            process = self.spawn("pass")
        finally:
            # Close only: LOCK_UN would also unlock the child's inherited OFD.
            os.close(descriptor)
        probe = os.open(filename, os.O_RDWR)
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIsNone(process.poll())
        finally:
            os.close(probe)
        process.resume()
        self.assertEqual(process.wait(timeout=5), 0)

    def test_environment_arguments_cwd_and_utf8_stdout(self):
        environment = os.environ.copy()
        environment["NATIVE_VALUE"] = "value with spaces = é"
        process = self.spawn(
            "import json,os,sys;print(json.dumps([os.getcwd(),os.environ['NATIVE_VALUE'],sys.argv[1]],ensure_ascii=False))",
            "quoted ' argument \\",
            env=environment,
            cwd=str(self.evidence),
        )
        process.resume()
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertEqual(
            json.loads(process.stdout.read()), [str(self.evidence), environment["NATIVE_VALUE"], "quoted ' argument \\"]
        )

    def test_pidfd_termination_reaps_current_direct_child(self):
        process = self.spawn("import time;time.sleep(60)")
        process.resume()
        process.terminate()
        self.assertEqual(process.wait(timeout=5), -signal.SIGTERM)
        self.assertEqual(process.poll(), -signal.SIGTERM)
        process.kill()  # Already-reaped identity is never replaced by a PID lookup.

    def test_checked_shutdown_observes_the_complete_native_subtree(self):
        from drift.node import linux_cgroup_recovery
        from drift.node.resource_recovery import RecoverableStateError

        self.assertEqual(linux_cgroup_recovery.verify_cgroup_tree_empty(str(self.leaf)).root, str(self.leaf))
        process = self.spawn("import time;time.sleep(60)")
        process.resume()
        with self.assertRaises(RecoverableStateError) as error:
            linux_cgroup_recovery.verify_cgroup_tree_empty(str(self.leaf))
        self.assertEqual(error.exception.reason, "cleanup_pending")
        process.terminate()
        self.assertEqual(process.wait(timeout=5), -signal.SIGTERM)
        _wait_for(lambda: not _populated(self.leaf))
        self.assertEqual(linux_cgroup_recovery.verify_cgroup_tree_empty(str(self.leaf)).root, str(self.leaf))

    def test_failed_exec_is_observable_and_contained(self):
        process = self.backend.spawn(self.group_fd, [str(self.evidence / "absent-executable")], env=os.environ.copy())
        self.children.append(process)
        with self.assertRaises(self.backend.LinuxCgroupProcessError):
            process.resume()
        self.assertIsNotNone(process.wait(timeout=5))
        _wait_for(lambda: not _populated(self.leaf))

    def test_invalid_working_directory_fails_ready_without_execution(self):
        marker = self.evidence / "executed"
        with self.assertRaises(self.backend.LinuxCgroupProcessError):
            self.spawn(
                "from pathlib import Path;import sys;Path(sys.argv[1]).write_text('ran')",
                str(marker),
                cwd=str(self.evidence / "absent-directory"),
            )
        _wait_for(lambda: not _populated(self.leaf))
        self.assertFalse(marker.exists())

    def test_stopped_gated_child_times_out_and_is_reaped_without_execution(self):
        marker = self.evidence / "executed"
        process = self.spawn("from pathlib import Path;import sys;Path(sys.argv[1]).write_text('ran')", str(marker))
        # Only the fixture sends STOP by its known live child PID. Production
        # termination and the timeout cleanup use the immutable pidfd instead.
        os.kill(process.pid, signal.SIGSTOP)
        _wait_for(lambda: ") T " in Path(f"/proc/{process.pid}/stat").read_text())
        started = time.monotonic()
        with self.assertRaises(self.backend.LinuxCgroupProcessError):
            process.resume()
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(process.wait(timeout=5), -signal.SIGKILL)
        _wait_for(lambda: not _populated(self.leaf))
        self.assertFalse(marker.exists())

    def test_invalid_contract_creates_no_child(self):
        changes = (
            {"stdin": None},
            {"stdout": None},
            {"stderr": None},
            {"text": False},
            {"creationflags": 1},
            {"cwd": "relative"},
            {"env": {"KEY": "bad\0value"}},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(self.backend.LinuxCgroupProcessError):
                self.spawn("raise SystemExit(99)", **change)
            self.assertFalse(_populated(self.leaf))
        descriptor = os.open(self.evidence, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with self.assertRaises(self.backend.LinuxCgroupProcessError):
                self.backend.spawn(descriptor, [sys.executable, "-c", "pass"], env=os.environ.copy())
        finally:
            os.close(descriptor)
        self.assertFalse(_populated(self.leaf))

    @unittest.skipUnless(platform.machine() == "x86_64", "seccomp fixture syscall table is qualified for x86-64")
    def test_preflight_rejects_each_required_native_syscall_denial(self):
        for name in ("clone3", "close_range", "pidfd_send_signal", "waitid"):
            with self.subTest(operation=name):
                result = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), "--native-denied", name],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env=os.environ.copy(),
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertFalse(_populated(self.leaf))

    def test_direct_exit_does_not_hide_detached_grandchild(self):
        marker = self.evidence / "grandchild"
        grandchild = (
            "import time;from pathlib import Path;Path(" + repr(str(marker)) + ").write_text('ready');time.sleep(60)"
        )
        process = self.spawn(
            "import subprocess,sys;subprocess.Popen([sys.executable,'-c',sys.argv[1]],start_new_session=True)",
            grandchild,
        )
        process.resume()
        self.assertEqual(process.wait(timeout=5), 0)
        _wait_for(marker.exists)
        self.assertTrue(_populated(self.leaf))
        (self.leaf / "cgroup.kill").write_text("1")
        _wait_for(lambda: not _populated(self.leaf))

    def test_owner_crash_before_resume_cannot_execute_later(self):
        import fcntl

        owner = self.start_owner(False)
        self.assertTrue(_populated(self.leaf))
        owner.kill()
        owner.wait(timeout=5)
        descriptor = os.open(self.evidence / "owner.lock", os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _wait_for(lambda: not _populated(self.leaf))
            self.assertFalse((self.evidence / "executed").exists())
        finally:
            os.close(descriptor)

    def test_owner_crash_after_resume_preserves_whole_tree_for_recovery(self):
        owner = self.start_owner(True)
        _wait_for((self.evidence / "grandchild").exists)
        owner.kill()
        owner.wait(timeout=5)
        self.assertTrue(_populated(self.leaf))
        self.assertGreaterEqual(len((self.leaf / "cgroup.procs").read_text().split()), 2)
        (self.leaf / "cgroup.kill").write_text("1")
        _wait_for(lambda: not _populated(self.leaf))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--native-owner":
        _owner_helper(
            sys.argv[2], sys.argv[3], bool(int(sys.argv[4])), bool(int(sys.argv[5])) if len(sys.argv) > 5 else False
        )
    elif len(sys.argv) > 1 and sys.argv[1] == "--native-denied":
        _denied_probe_helper(sys.argv[2])
    elif len(sys.argv) > 1 and sys.argv[1] == "--native-closed-stdio":
        _closed_stdio_helper(sys.argv[2])
    else:
        unittest.main()
