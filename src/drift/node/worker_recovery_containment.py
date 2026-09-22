"""Atomic Windows job creation for durably reserved worker generations.

This intentionally implements only the supervisor's redirected, UTF-8 Popen
contract. A child is born suspended *in* its named job; there is no interval in
which a successful CreateProcess can leave an uncontained child. Linux keeps
its existing runtime process group and permits durable recovery only after a
verified change of boot (the separate recovery-authority module enforces it).
"""

from __future__ import annotations

import io
import math
import os
import subprocess
import sys
import threading
import time
from typing import Any, Mapping, Optional, Sequence


class RecoveryContainmentError(RuntimeError):
    """Native containment could not establish the required full-tree proof."""


def _failure() -> RecoveryContainmentError:
    return RecoveryContainmentError("worker recovery containment is unavailable")


def _windows_binding(binding: Any) -> None:
    from drift.node.resource_recovery import GenerationRecoveryBinding

    if (
        not isinstance(binding, GenerationRecoveryBinding)
        or binding.kind != "worker"
        or binding.contract != "windows_job_atomic_v1"
        or binding.owner.identity.platform != "windows"
        or binding.containment_name != f"Global\\CommunityAI-{binding.owner.owner_id}-{binding.reservation_id}"
    ):
        raise _failure()
    binding.__post_init__()


class _WindowsAPI:
    """Private, explicit ctypes signatures; importing this module is portable."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise _failure()
        import ctypes
        from ctypes import wintypes as w

        self.c = c = ctypes
        self.w = w

        class SECURITY_ATTRIBUTES(c.Structure):
            _fields_ = [("nLength", w.DWORD), ("lpSecurityDescriptor", w.LPVOID), ("bInheritHandle", w.BOOL)]

        class STARTUPINFOW(c.Structure):
            _fields_ = [
                ("cb", w.DWORD),
                ("lpReserved", w.LPWSTR),
                ("lpDesktop", w.LPWSTR),
                ("lpTitle", w.LPWSTR),
                ("dwX", w.DWORD),
                ("dwY", w.DWORD),
                ("dwXSize", w.DWORD),
                ("dwYSize", w.DWORD),
                ("dwXCountChars", w.DWORD),
                ("dwYCountChars", w.DWORD),
                ("dwFillAttribute", w.DWORD),
                ("dwFlags", w.DWORD),
                ("wShowWindow", w.WORD),
                ("cbReserved2", w.WORD),
                ("lpReserved2", c.POINTER(w.BYTE)),
                ("hStdInput", w.HANDLE),
                ("hStdOutput", w.HANDLE),
                ("hStdError", w.HANDLE),
            ]

        class STARTUPINFOEXW(c.Structure):
            _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", w.LPVOID)]

        class PROCESS_INFORMATION(c.Structure):
            _fields_ = [
                ("hProcess", w.HANDLE),
                ("hThread", w.HANDLE),
                ("dwProcessId", w.DWORD),
                ("dwThreadId", w.DWORD),
            ]

        class BASIC_LIMIT(c.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", c.c_int64),
                ("PerJobUserTimeLimit", c.c_int64),
                ("LimitFlags", w.DWORD),
                ("MinimumWorkingSetSize", c.c_size_t),
                ("MaximumWorkingSetSize", c.c_size_t),
                ("ActiveProcessLimit", w.DWORD),
                ("Affinity", c.c_size_t),
                ("PriorityClass", w.DWORD),
                ("SchedulingClass", w.DWORD),
            ]

        class IO_COUNTERS(c.Structure):
            _fields_ = [
                (name, c.c_uint64)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class EXTENDED_LIMIT(c.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC_LIMIT),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", c.c_size_t),
                ("JobMemoryLimit", c.c_size_t),
                ("PeakProcessMemoryUsed", c.c_size_t),
                ("PeakJobMemoryUsed", c.c_size_t),
            ]

        class ACCOUNTING(c.Structure):
            _fields_ = [
                ("TotalUserTime", c.c_int64),
                ("TotalKernelTime", c.c_int64),
                ("ThisPeriodTotalUserTime", c.c_int64),
                ("ThisPeriodTotalKernelTime", c.c_int64),
                ("TotalPageFaultCount", w.DWORD),
                ("TotalProcesses", w.DWORD),
                ("ActiveProcesses", w.DWORD),
                ("TotalTerminatedProcesses", w.DWORD),
            ]

        self.security = SECURITY_ATTRIBUTES
        self.startup = STARTUPINFOEXW
        self.process_info = PROCESS_INFORMATION
        self.limits = EXTENDED_LIMIT
        self.accounting = ACCOUNTING
        self.k = k = c.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": (w.HANDLE, (w.LPVOID, w.LPCWSTR)),
            "OpenJobObjectW": (w.HANDLE, (w.DWORD, w.BOOL, w.LPCWSTR)),
            "SetInformationJobObject": (w.BOOL, (w.HANDLE, c.c_int, w.LPVOID, w.DWORD)),
            "QueryInformationJobObject": (w.BOOL, (w.HANDLE, c.c_int, w.LPVOID, w.DWORD, w.LPVOID)),
            "TerminateJobObject": (w.BOOL, (w.HANDLE, w.UINT)),
            "IsProcessInJob": (w.BOOL, (w.HANDLE, w.HANDLE, c.POINTER(w.BOOL))),
            "CloseHandle": (w.BOOL, (w.HANDLE,)),
            "SetHandleInformation": (w.BOOL, (w.HANDLE, w.DWORD, w.DWORD)),
            "CreatePipe": (w.BOOL, (c.POINTER(w.HANDLE), c.POINTER(w.HANDLE), w.LPVOID, w.DWORD)),
            "CreateFileW": (w.HANDLE, (w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE)),
            "InitializeProcThreadAttributeList": (w.BOOL, (w.LPVOID, w.DWORD, w.DWORD, c.POINTER(c.c_size_t))),
            "UpdateProcThreadAttribute": (
                w.BOOL,
                (w.LPVOID, w.DWORD, c.c_size_t, w.LPVOID, c.c_size_t, w.LPVOID, w.LPVOID),
            ),
            "DeleteProcThreadAttributeList": (None, (w.LPVOID,)),
            "CreateProcessW": (
                w.BOOL,
                (w.LPCWSTR, w.LPWSTR, w.LPVOID, w.LPVOID, w.BOOL, w.DWORD, w.LPVOID, w.LPCWSTR, w.LPVOID, w.LPVOID),
            ),
            "ResumeThread": (w.DWORD, (w.HANDLE,)),
            "WaitForSingleObject": (w.DWORD, (w.HANDLE, w.DWORD)),
            "GetExitCodeProcess": (w.BOOL, (w.HANDLE, c.POINTER(w.DWORD))),
            "TerminateProcess": (w.BOOL, (w.HANDLE, w.UINT)),
        }
        for name, (result, arguments) in signatures.items():
            function = getattr(k, name)
            function.restype = result
            function.argtypes = arguments

    def active(self, job: Any) -> bool:
        info = self.accounting()
        if not self.k.QueryInformationJobObject(job, 1, self.c.byref(info), self.c.sizeof(info), None):
            raise _failure()
        return info.ActiveProcesses != 0

    def verify_limits(self, job: Any) -> None:
        info = self.limits()
        if not self.k.QueryInformationJobObject(job, 9, self.c.byref(info), self.c.sizeof(info), None):
            raise _failure()
        flags = info.BasicLimitInformation.LimitFlags
        if not flags & 0x2000 or flags & (0x800 | 0x1000):
            raise _failure()


class NativeWindowsProcess:
    """The small Popen surface consumed by worker supervision, bound to handles."""

    def __init__(self, owner: "WindowsRecoveryContainment", command: Sequence[str], stdout: Any) -> None:
        self.args = list(command)
        self.pid = 0
        self.stdout = stdout
        self.stdin = None
        self.stderr = None
        self.returncode: Optional[int] = None
        self._owner = owner
        self._api = owner._api
        self._handle = None
        self._thread_handle = None
        self._wait_lock = threading.Lock()

    def poll(self) -> Optional[int]:
        with self._wait_lock:
            if self.returncode is not None:
                return self.returncode
            if self._handle is None:
                raise _failure()
            result = self._api.k.WaitForSingleObject(self._handle, 0)
            if result == 0x102:
                return None
            if result != 0:
                raise _failure()
            code = self._api.w.DWORD()
            if not self._api.k.GetExitCodeProcess(self._handle, self._api.c.byref(code)):
                raise _failure()
            self.returncode = int(code.value)
            return self.returncode

    def wait(self, timeout: Optional[float] = None) -> int:
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("process timeout must be finite and nonnegative")
        # A wait never holds the poll lock: status and Pause remain responsive.
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            code = self.poll()
            if code is not None:
                return code
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if remaining == 0:
                raise subprocess.TimeoutExpired(self.args, timeout)
            milliseconds = 100 if remaining is None else min(100, max(1, math.ceil(remaining * 1000)))
            result = self._api.k.WaitForSingleObject(self._handle, milliseconds)
            if result not in (0, 0x102):
                raise _failure()

    def terminate(self) -> None:
        if self.poll() is None and not self._api.k.TerminateProcess(self._handle, 1):
            if self.poll() is None:
                raise _failure()

    kill = terminate

    def __del__(self) -> None:
        api = getattr(self, "_api", None)
        if api is not None:
            for name in ("_thread_handle", "_handle"):
                handle = getattr(self, name, None)
                if handle is not None:
                    api.k.CloseHandle(handle)
                    setattr(self, name, None)


class WindowsRecoveryContainment:
    kind = "windows_job_atomic_v1"

    def __init__(self, binding: Any) -> None:
        _windows_binding(binding)
        self._api = api = _WindowsAPI()
        self._job = None
        self._spawned = False
        self.binding = binding
        api.c.set_last_error(0)
        job = api.k.CreateJobObjectW(None, binding.containment_name)
        error = api.c.get_last_error()
        if not job:
            raise _failure()
        if error == 183:  # Never adopt or change a pre-existing named job.
            api.k.CloseHandle(job)
            raise _failure()
        try:
            info = api.limits()
            info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway.
            if not api.k.SetInformationJobObject(job, 9, api.c.byref(info), api.c.sizeof(info)):
                raise _failure()
            if not api.k.SetHandleInformation(job, 1, 0):
                raise _failure()
            api.verify_limits(job)
        except BaseException:
            api.k.CloseHandle(job)
            raise
        self._job = job

    def popen_kwargs(self) -> dict:
        return {"creationflags": 4}  # The primary thread is resumed only after tracking.

    def spawn(
        self,
        command: Sequence[str],
        *,
        stdin: Any,
        stdout: Any,
        stderr: Any,
        text: bool,
        encoding: str,
        errors: str,
        bufsize: int,
        env: Mapping[str, str],
        cwd: Optional[str] = None,
        creationflags: int = 0,
    ) -> NativeWindowsProcess:
        """Create a suspended member with only the two redirected handles inherited."""
        if (
            self._job is None
            or self._spawned
            or isinstance(command, (str, bytes))
            or not command
            or any(not isinstance(arg, str) or "\0" in arg for arg in command)
            or not os.path.isabs(command[0])
            or stdin != subprocess.DEVNULL
            or stdout != subprocess.PIPE
            or stderr != subprocess.STDOUT
            or text is not True
            or encoding != "utf-8"
            or errors != "replace"
            or bufsize != 1
            or type(creationflags) is not int
            or creationflags < 0
            or creationflags & ~(4 | 0x08000000 | 0x00000200)
            or (cwd is not None and (not isinstance(cwd, str) or not os.path.isabs(cwd) or "\0" in cwd))
            or not isinstance(env, Mapping)
        ):
            raise _failure()
        keys = set()
        for key, value in env.items():
            if (
                not isinstance(key, str)
                or not key
                or "=" in key
                or "\0" in key
                or not isinstance(value, str)
                or "\0" in value
                or key.casefold() in keys
            ):
                raise _failure()
            keys.add(key.casefold())
        api = self._api
        c, k, w = api.c, api.k, api.w
        command_line = c.create_unicode_buffer(subprocess.list2cmdline(command))
        environment = c.create_unicode_buffer(
            "\0".join(f"{key}={env[key]}" for key in sorted(env, key=str.casefold)) + "\0\0"
        )
        security = api.security(c.sizeof(api.security), None, True)
        read_pipe, write_pipe = w.HANDLE(), w.HANDLE()
        null = None
        output = None
        attributes = None
        attributes_initialized = False
        process = None
        try:
            if not k.CreatePipe(c.byref(read_pipe), c.byref(write_pipe), c.byref(security), 0):
                raise _failure()
            if not k.SetHandleInformation(read_pipe, 1, 0):
                raise _failure()
            null = k.CreateFileW("NUL", 0x80000000, 3, c.byref(security), 3, 0, None)
            if null in (None, c.c_void_p(-1).value):
                null = None
                raise _failure()
            import msvcrt

            descriptor = msvcrt.open_osfhandle(read_pipe.value, os.O_RDONLY | os.O_BINARY)
            read_pipe = w.HANDLE()  # descriptor owns it now.
            try:
                output = io.open(descriptor, "r", buffering=1, encoding=encoding, errors=errors)
            except BaseException:
                os.close(descriptor)
                raise
            process = NativeWindowsProcess(self, command, output)
            size = c.c_size_t()
            k.InitializeProcThreadAttributeList(None, 2, 0, c.byref(size))
            if not 0 < size.value < 1024 * 1024:
                raise _failure()
            attributes = c.create_string_buffer(size.value)
            if not k.InitializeProcThreadAttributeList(attributes, 2, 0, c.byref(size)):
                raise _failure()
            attributes_initialized = True
            handles = (w.HANDLE * 2)(null, write_pipe.value)
            jobs = (w.HANDLE * 1)(self._job)
            for attribute, value in ((0x00020002, handles), (0x0002000D, jobs)):
                if not k.UpdateProcThreadAttribute(attributes, 0, attribute, value, c.sizeof(value), None, None):
                    raise _failure()
            startup = api.startup()
            startup.StartupInfo.cb = c.sizeof(startup)
            startup.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput = null
            startup.StartupInfo.hStdOutput = write_pipe.value
            startup.StartupInfo.hStdError = write_pipe.value
            startup.lpAttributeList = c.cast(attributes, w.LPVOID)
            info = api.process_info()
            self._spawned = True
            if not k.CreateProcessW(
                command[0],
                command_line,
                None,
                None,
                True,
                creationflags | 4 | 0x80000 | 0x400,
                environment,
                cwd,
                c.byref(startup),
                c.byref(info),
            ):
                raise _failure()
            # Store the handles immediately. No PID reopening or thread search.
            process._handle = info.hProcess
            process._thread_handle = info.hThread
            process.pid = int(info.dwProcessId)
            return process
        except BaseException:
            if output is not None:
                output.close()
            raise
        finally:
            if attributes_initialized:
                k.DeleteProcThreadAttributeList(attributes)
            for handle in (read_pipe.value, write_pipe.value, null):
                if handle is not None:
                    k.CloseHandle(handle)

    def attach(self, process: Any) -> None:
        if not isinstance(process, NativeWindowsProcess) or process._owner is not self or self._job is None:
            raise _failure()
        member = self._api.w.BOOL()
        if not self._api.k.IsProcessInJob(process._handle, self._job, self._api.c.byref(member)) or not member.value:
            raise _failure()

    def resume(self, process: Any) -> None:
        self.attach(process)
        if process._thread_handle is None:
            raise _failure()
        if self._api.k.ResumeThread(process._thread_handle) != 1:
            raise _failure()
        self._api.k.CloseHandle(process._thread_handle)
        process._thread_handle = None

    def has_members(self) -> bool:
        if self._job is None:
            raise _failure()  # A closed handle cannot certify emptiness.
        return self._api.active(self._job)

    def terminate(self) -> None:
        if self.has_members() and not self._api.k.TerminateJobObject(self._job, 1):
            raise _failure()

    kill = terminate

    def close(self) -> None:
        if self._job is not None:
            if not self._api.k.CloseHandle(self._job):
                raise _failure()
            self._job = None


def create_recovery_containment(binding: Any) -> Any:
    from drift.node.resource_recovery import GenerationRecoveryBinding

    if not isinstance(binding, GenerationRecoveryBinding) or binding.kind != "worker":
        raise _failure()
    binding.__post_init__()
    if binding.owner.identity.platform == "windows":
        return WindowsRecoveryContainment(binding)
    if (
        sys.platform.startswith("linux")
        and binding.owner.identity.platform == "linux"
        and binding.contract == "linux_boot_v1"
    ):
        from drift.node.edge_supervisor import _PosixProcessGroup

        return _PosixProcessGroup()
    raise _failure()


def recover_windows_containment(binding: Any, guard: Any, *, timeout: float = 5.0) -> bool:
    """Prove complete job emptiness while the old owner's exclusive lease is held."""
    from drift.node.resource_recovery import OwnerRecoveryGuard, RecoverableStateError

    _windows_binding(binding)
    if (
        not isinstance(guard, OwnerRecoveryGuard)
        or isinstance(timeout, bool)
        or not math.isfinite(timeout)
        or not 0 <= timeout <= 60
    ):
        raise _failure()
    guard.require_binding(binding)
    api = _WindowsAPI()
    job = api.k.OpenJobObjectW(0x0004 | 0x0008, False, binding.containment_name)
    if not job:
        error = api.c.get_last_error()
        guard.require_binding(binding)
        if error == 2:  # Windows destroys the named job only after its last member exits.
            return True
        raise _failure()
    try:
        api.verify_limits(job)
        guard.require_binding(binding)
        if api.active(job) and not api.k.TerminateJobObject(job, 1):
            raise _failure()
        deadline = time.monotonic() + timeout
        while api.active(job):
            guard.require_binding(binding)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RecoverableStateError("cleanup_pending")
            time.sleep(min(0.02, remaining))
        guard.require_binding(binding)
        return True
    finally:
        api.k.CloseHandle(job)
