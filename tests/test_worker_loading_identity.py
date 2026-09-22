"""Real interpreter ancestry and controlled invalid-identity regressions."""

import json
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from drift.node import worker_loading_identity as identity


def test_real_child_identity_resolves_current_interpreter_and_remains_bound():
    command = [sys.executable, "-c", "import os,time; print(os.getpid(), flush=True); time.sleep(1)"]
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as process:
        actual_pid = int(process.stdout.readline())
        resolved = identity.resolve_loading_worker_pid(process, command)
        assert resolved.pid == actual_pid
        assert resolved.root_pid == process.pid
        assert identity.resolve_loading_worker_pid(process, command, expected_identity=resolved) == resolved
        with pytest.raises(identity.LoadingIdentityError):
            identity.resolve_loading_worker_pid(
                process, command, expected_identity=replace(resolved, creation_time=resolved.creation_time + 1)
            )
        process.wait(timeout=5)
    with pytest.raises(identity.LoadingIdentityError):
        identity.resolve_loading_worker_pid(process, command, expected_identity=resolved)


@pytest.fixture
def fake_runtime(monkeypatch):
    command = [sys.executable, "-m", "drift.cli", "server"]
    base = getattr(sys, "_base_executable", sys.executable)
    child = SimpleNamespace(
        pid=102,
        create_time=lambda: 101.0,
        ppid=lambda: 101,
        exe=lambda: base,
        cmdline=lambda: [base, *command[1:]],
    )
    children = [child]
    root = SimpleNamespace(
        pid=101,
        create_time=lambda: 100.0,
        exe=lambda: sys.executable,
        cmdline=lambda: list(command),
        children=lambda recursive: list(children),
    )
    processes = {101: root, 102: child}
    monkeypatch.setattr(identity.psutil, "Process", lambda pid: processes[pid])
    process = SimpleNamespace(pid=101, poll=lambda: None)
    return SimpleNamespace(
        root=root, child=child, process=process, command=command, children=children, processes=processes
    )


def test_root_generation_change_is_rejected(fake_runtime):
    f = fake_runtime
    original = identity.resolve_loading_worker_pid(f.process, f.command)
    f.root.create_time = lambda: 100.5
    with pytest.raises(identity.LoadingIdentityError):
        identity.resolve_loading_worker_pid(f.process, f.command, expected_identity=original)


@pytest.mark.skipif(
    sys.platform != "win32" or sys.executable == getattr(sys, "_base_executable", sys.executable),
    reason="Windows venv launcher validation",
)
@pytest.mark.parametrize("fault", ["parent", "arguments", "executable", "created", "multiple", "replaced"])
def test_venv_identity_rejects_unproven_descendants(fake_runtime, fault):
    f = fake_runtime
    original = identity.resolve_loading_worker_pid(f.process, f.command)
    if fault == "parent":
        f.child.ppid = lambda: 999
    elif fault == "arguments":
        f.child.cmdline = lambda: [sys.executable, "-c", "private invalid command"]
    elif fault == "executable":
        f.child.exe = lambda: "private-unrelated-executable"
    elif fault == "created":
        f.child.create_time = lambda: 99.0
    elif fault == "multiple":
        f.children.append(f.child)
    else:
        f.child.create_time = lambda: 102.0
    with pytest.raises(identity.LoadingIdentityError) as error:
        identity.resolve_loading_worker_pid(f.process, f.command, expected_identity=original)
    assert "private" not in str(error.value)


@pytest.mark.skipif(
    sys.platform != "win32" or sys.executable == getattr(sys, "_base_executable", sys.executable),
    reason="Windows venv launcher validation",
)
def test_venv_boot_waits_only_before_an_identity_is_observed(fake_runtime):
    f = fake_runtime
    original = identity.resolve_loading_worker_pid(f.process, f.command)
    f.children.clear()
    assert identity.resolve_loading_worker_pid(f.process, f.command) is None
    with pytest.raises(identity.LoadingIdentityError):
        identity.resolve_loading_worker_pid(f.process, f.command, expected_identity=original)


def test_identity_lookup_failure_has_fixed_error(fake_runtime, monkeypatch):
    monkeypatch.setattr(identity.psutil, "Process", lambda pid: json.loads("private path and credentials"))
    with pytest.raises(identity.LoadingIdentityError) as error:
        identity.resolve_loading_worker_pid(fake_runtime.process, fake_runtime.command)
    assert str(error.value) == "managed worker loading identity is unavailable"


@pytest.mark.parametrize("fault", ["arguments", "executable"])
def test_native_root_also_requires_exact_command_identity(fake_runtime, monkeypatch, fault):
    f = fake_runtime
    monkeypatch.setattr(identity, "sys", SimpleNamespace(platform="linux", executable=sys.executable))
    original = identity.resolve_loading_worker_pid(f.process, f.command)
    assert original.pid == f.root.pid
    if fault == "arguments":
        f.root.cmdline = lambda: [sys.executable, "-c", "other work"]
    else:
        f.root.exe = lambda: "another-program"
    with pytest.raises(identity.LoadingIdentityError):
        identity.resolve_loading_worker_pid(f.process, f.command, expected_identity=original)


@pytest.mark.skipif(
    sys.platform != "win32" or sys.executable == getattr(sys, "_base_executable", sys.executable),
    reason="Windows venv console-host validation",
)
def test_windows_console_host_does_not_impersonate_or_hide_interpreter(fake_runtime):
    f = fake_runtime
    host = SimpleNamespace(
        pid=103,
        exe=identity._console_host_path,
        ppid=lambda: 101,
        create_time=lambda: 100.5,
    )
    f.children[:] = [host]
    assert identity.resolve_loading_worker_pid(f.process, f.command) is None
    f.children.append(f.child)
    original = identity.resolve_loading_worker_pid(f.process, f.command)
    assert original.pid == f.child.pid
    assert identity.resolve_loading_worker_pid(f.process, f.command, expected_identity=original) == original
    host.exe = lambda: "C:/untrusted/conhost.exe"
    with pytest.raises(identity.LoadingIdentityError):
        identity.resolve_loading_worker_pid(f.process, f.command, expected_identity=original)


def test_initially_unavailable_command_is_pending_but_cannot_replace_known_identity(fake_runtime):
    f = fake_runtime
    original = identity.resolve_loading_worker_pid(f.process, f.command)
    f.root.cmdline = lambda: []
    assert identity.resolve_loading_worker_pid(f.process, f.command) is None
    with pytest.raises(identity.LoadingIdentityError):
        identity.resolve_loading_worker_pid(f.process, f.command, expected_identity=original)
