"""Restricted Linux worker process created atomically in an already-bound cgroup.

The optional C extension is the only birth implementation. It never enters
Python after clone. Cgroup ownership, delegation and complete-tree death proof
belong to the caller; this module only owns the direct child's native handles.
"""

from __future__ import annotations

import io
import math
import os
import select
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence

_HANDSHAKE_TIMEOUT = 2.0
_ERROR = "Linux cgroup process creation is unavailable"


class LinuxCgroupProcessError(RuntimeError):
    def __init__(self):
        super().__init__(_ERROR)


def _backend():
    try:
        if not sys.platform.startswith("linux"):
            raise LinuxCgroupProcessError()
        from drift.node import _linux_cgroup_spawn

        return _linux_cgroup_spawn
    except Exception:
        raise LinuxCgroupProcessError() from None


def validate_cgroup_backend() -> None:
    """Probe birth, FD closure and pidfd control without creating a process.

    Invalid-FD clone3, pidfd signal/wait and an empty close_range probe detect
    kernel/seccomp rejection. They neither touch real descriptors nor prove that
    a delegated leaf is writable. Actual spawn remains authoritative for that.
    Any rejection denies an explicitly configured profile, with no fallback.
    """
    try:
        _backend().validate()
    except Exception:
        raise LinuxCgroupProcessError() from None


validate_capability = validate_cgroup_backend


def _read_handshake(descriptor: int, expected: bytes, *, cancel=None) -> None:
    poller = select.poll()
    poller.register(descriptor, select.POLLIN | select.POLLHUP | select.POLLERR)
    deadline = time.monotonic() + _HANDSHAKE_TIMEOUT
    while True:
        if cancel is not None and cancel.is_set():
            raise LinuxCgroupProcessError()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LinuxCgroupProcessError()
        interval = remaining if cancel is None else min(remaining, 0.05)
        if not poller.poll(max(1, math.ceil(interval * 1000))):
            continue
        try:
            value = os.read(descriptor, 1)
        except InterruptedError:
            continue
        if value != expected:
            raise LinuxCgroupProcessError()
        return


class LinuxCgroupProcess:
    """Popen-like direct child identity; full-tree termination stays with cgroup."""

    def __init__(self, native, command):
        self._native = native
        self.args = list(command)
        self.pid = native.pid
        self._cgroup_identity = native.cgroup_identity
        self.returncode = None
        self.stdin = self.stderr = None
        self.stdout = None
        self._poll_lock = threading.Lock()
        self._resume_lock = threading.Lock()
        self._resumed = False

    @property
    def cgroup_identity(self):
        return self._cgroup_identity

    def poll(self):
        with self._poll_lock:
            if self.returncode is None:
                try:
                    self.returncode = self._native.poll()
                except Exception:
                    raise LinuxCgroupProcessError() from None
            return self.returncode

    def wait(self, timeout=None):
        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, (float, int))
            or not math.isfinite(timeout)
            or timeout < 0
        ):
            raise ValueError("process timeout must be finite and nonnegative")
        deadline = None if timeout is None else time.monotonic() + timeout
        poller = select.poll()
        poller.register(self._native.descriptors()[0], select.POLLIN | select.POLLHUP | select.POLLERR)
        while True:
            result = self.poll()
            if result is not None:
                return result
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise subprocess.TimeoutExpired(self.args, timeout)
            poller.poll(100 if remaining is None else min(100, max(1, math.ceil(remaining * 1000))))

    def _signal(self, number):
        if self.poll() is None:
            try:
                self._native.send_signal(number)
            except Exception:
                raise LinuxCgroupProcessError() from None

    def terminate(self):
        self._signal(signal.SIGTERM)

    def kill(self):
        self._signal(signal.SIGKILL)

    def resume(self):
        with self._resume_lock:
            if self._resumed:
                raise LinuxCgroupProcessError()
            try:
                self._release_gate_locked()
            except Exception:
                self._abort_unaccepted()
                raise LinuxCgroupProcessError() from None
        self.await_exec()

    def release_gate(self):
        """Commit execution with one pipe write, without waiting for exec."""
        with self._resume_lock:
            self._release_gate_locked()

    def _release_gate_locked(self):
        if self._resumed:
            raise LinuxCgroupProcessError()
        self._resumed = True
        _, gate, _ = self._native.descriptors()
        try:
            if os.write(gate, b"G") != 1:
                raise LinuxCgroupProcessError()
        except Exception:
            # The caller owns cleanup; never wait under its intent lock.
            self._native.close_control()
            raise LinuxCgroupProcessError() from None

    def await_exec(self, *, cancel=None):
        """Observe exec outside the supervisor lock; cancellation retains proof."""
        with self._resume_lock:
            if not self._resumed:
                raise LinuxCgroupProcessError()
            try:
                # Only successful exec closes this writer via CLOEXEC.
                _read_handshake(self._native.descriptors()[2], b"", cancel=cancel)
            except Exception:
                self._abort_unaccepted()
                raise LinuxCgroupProcessError() from None
            finally:
                self._native.close_control()

    def _abort_unaccepted(self):
        """Best-effort direct reap; caller retains cgroup proof on any failure."""
        self._native.close_control()
        try:
            self.kill()
            self.wait(timeout=_HANDSHAKE_TIMEOUT)
        except Exception:
            pass


def spawn(
    cgroup_fd,
    command,
    *,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    encoding="utf-8",
    errors="replace",
    bufsize=1,
    env,
    cwd=None,
    creationflags=0,
    start_new_session=True,
    input_fd=None,
):
    """Borrow the cgroup FD and return a tracked-ready, still unexecuted child.

    An explicit input_fd borrows a blocking read-only pipe, with binary stdout
    and discarded stderr. Callers must supply text=False and stderr=DEVNULL.
    This is transport only: framing, deadlines, cancellation and whole-tree
    containment remain the owning controller's responsibility.
    """
    if (
        type(cgroup_fd) is not int
        or cgroup_fd < 0
        or not isinstance(command, Sequence)
        or isinstance(command, (str, bytes))
        or not command
        or any(not isinstance(value, str) or "\0" in value for value in command)
        or not os.path.isabs(command[0])
        or stdin != subprocess.DEVNULL
        or stdout != subprocess.PIPE
        or (input_fd is None and (stderr != subprocess.STDOUT or text is not True))
        or (input_fd is not None and (stderr != subprocess.DEVNULL or text is not False))
        or encoding != "utf-8"
        or errors != "replace"
        or bufsize != 1
        or type(creationflags) is not int
        or creationflags != 0
        or type(start_new_session) is not bool
        or (input_fd is not None and (type(input_fd) is not int or input_fd < 3))
        or not isinstance(env, Mapping)
        or (cwd is not None and (not isinstance(cwd, str) or not os.path.isabs(cwd) or "\0" in cwd))
    ):
        raise LinuxCgroupProcessError()
    for key, value in env.items():
        if (
            not isinstance(key, str)
            or not key
            or "=" in key
            or "\0" in key
            or not isinstance(value, str)
            or "\0" in value
        ):
            raise LinuxCgroupProcessError()
    process = None
    try:
        arguments = (
            cgroup_fd,
            tuple(command),
            tuple(f"{key}={value}" for key, value in env.items()),
            cwd,
            start_new_session,
        )
        # Preserve the original native ABI for ordinary workers. Credential
        # helpers require the new explicit read-pipe capability, no fallback.
        native = _backend().spawn(*arguments, *((input_fd,) if input_fd is not None else ()))
        process = LinuxCgroupProcess(native, command)
        descriptor = native.take_stdout()
        try:
            if input_fd is None:
                process.stdout = io.open(descriptor, "r", buffering=1, encoding=encoding, errors=errors)
            else:
                process.stdout = io.open(descriptor, "rb", buffering=0)
        except BaseException:
            os.close(descriptor)
            raise
        _read_handshake(native.descriptors()[2], b"R")
        return process
    except Exception:
        if process is not None:
            process._abort_unaccepted()
            if process.stdout is not None:
                process.stdout.close()
        raise LinuxCgroupProcessError() from None
