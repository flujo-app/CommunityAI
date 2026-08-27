"""
Tests for drift.utils.process_lifetime (issue #5): child processes -- above all p2pd -- must not
outlive a hard-killed server. The integration tests spawn a disposable "server" process that arms
the guard and starts a sleeper child, hard-kill the server, and assert the child dies with it.
"""

import subprocess
import sys
import time

import psutil
import pytest

from drift.utils.process_lifetime import _set_pdeathsig, _SubprocessWithPdeathsig

CHILD_EXIT_DEADLINE = 15.0

WINDOWS_PARENT_SCRIPT = """
import subprocess, sys, time
from drift.utils.process_lifetime import tie_child_processes_to_this_process

assert tie_child_processes_to_this_process(), "failed to arm the kill-on-close job object"
assert tie_child_processes_to_this_process(), "arming must be idempotent"

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
print(child.pid, flush=True)
time.sleep(300)
"""

LINUX_PARENT_SCRIPT = """
import subprocess, sys, time
from drift.utils.process_lifetime import _ensure_libc, _set_pdeathsig

_ensure_libc()
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], preexec_fn=_set_pdeathsig)
print(child.pid, flush=True)
time.sleep(300)
"""


def _spawn_parent_and_read_child_pid(script: str) -> "tuple[subprocess.Popen, int]":
    parent = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    line = parent.stdout.readline().strip()
    assert line, f"parent died before reporting its child pid (exit code {parent.poll()})"
    return parent, int(line)


def _assert_pid_exits(pid: int) -> None:
    deadline = time.monotonic() + CHILD_EXIT_DEADLINE
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            return
        time.sleep(0.2)
    psutil.Process(pid).kill()  # do not leak the sleeper on failure
    pytest.fail(f"child {pid} was still alive {CHILD_EXIT_DEADLINE} seconds after its parent was hard-killed")


@pytest.mark.skipif(sys.platform != "win32", reason="Job objects are the Windows mechanism")
def test_windows_job_object_kills_children_on_hard_kill():
    parent, child_pid = _spawn_parent_and_read_child_pid(WINDOWS_PARENT_SCRIPT)
    assert psutil.pid_exists(child_pid)
    parent.kill()  # TerminateProcess: the moral equivalent of Stop-Process
    parent.wait(timeout=CHILD_EXIT_DEADLINE)
    _assert_pid_exits(child_pid)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG is Linux-only")
def test_linux_pdeathsig_kills_child_when_parent_dies():
    import os
    import signal

    parent, child_pid = _spawn_parent_and_read_child_pid(LINUX_PARENT_SCRIPT)
    assert psutil.pid_exists(child_pid)
    os.kill(parent.pid, signal.SIGKILL)
    parent.wait(timeout=CHILD_EXIT_DEADLINE)
    _assert_pid_exits(child_pid)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG is Linux-only")
def test_linux_pdeathsig_accepts_container_parent_pid_one(monkeypatch):
    import drift.utils.process_lifetime as process_lifetime

    prctl_calls = []

    class FakeLibc:
        @staticmethod
        def prctl(*args):
            prctl_calls.append(args)
            return 0

    monkeypatch.setattr(process_lifetime, "_linux_parent_pid", 1)
    monkeypatch.setattr(process_lifetime, "_ensure_libc", lambda: FakeLibc())
    monkeypatch.setattr(process_lifetime.os, "getppid", lambda: 1)
    monkeypatch.setattr(
        process_lifetime.os,
        "_exit",
        lambda code: pytest.fail(f"expected container parent PID 1 must not exit with {code}"),
    )

    process_lifetime._set_pdeathsig()

    assert prctl_calls == [(1, process_lifetime.signal.SIGKILL, 0, 0, 0)]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="The hivemind spawn wrapper is armed on Linux only")
def test_linux_arming_patches_hivemind_p2pd_spawn():
    from hivemind.p2p import p2p_daemon

    from drift.utils.process_lifetime import _AsyncioWithPdeathsig, tie_child_processes_to_this_process

    assert tie_child_processes_to_this_process()
    assert tie_child_processes_to_this_process(), "arming must be idempotent"
    assert isinstance(p2p_daemon.asyncio, _AsyncioWithPdeathsig)
    # Everything hivemind reaches through its module-global asyncio must still resolve
    import asyncio

    assert p2p_daemon.asyncio.subprocess.PIPE == asyncio.subprocess.PIPE
    assert p2p_daemon.asyncio.wait_for is asyncio.wait_for


def test_subprocess_wrapper_injects_preexec_fn():
    captured = {}

    class FakeSubprocess:
        PIPE = "fake-pipe"

        @staticmethod
        def create_subprocess_exec(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return "spawned"

    wrapper = _SubprocessWithPdeathsig(FakeSubprocess())
    assert wrapper.PIPE == "fake-pipe"
    assert wrapper.create_subprocess_exec("p2pd", "-arg", stdout="out") == "spawned"
    assert captured["args"] == ("p2pd", "-arg")
    assert captured["kwargs"]["stdout"] == "out"
    assert captured["kwargs"]["preexec_fn"] is _set_pdeathsig

    # An explicitly provided preexec_fn must win over the injected one
    sentinel = object()
    wrapper.create_subprocess_exec("p2pd", preexec_fn=sentinel)
    assert captured["kwargs"]["preexec_fn"] is sentinel
