"""
Tie the lifetime of child processes -- most importantly the go-libp2p daemon, p2pd -- to this process.

hivemind spawns p2pd as an ordinary child process and only terminates it during graceful shutdown.
A hard kill of the server (``Stop-Process`` on Windows, SIGKILL anywhere) therefore orphans the
daemon, which keeps holding ports and log files and can poison subsequent runs: a fresh server may
even attach to a stale daemon over the TCP control channel used on win32.

``tie_child_processes_to_this_process()`` arms a per-platform guard against that:

- On Windows, the current process is placed into a job object with
  JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE. Children (and their children) join the job automatically, and
  the OS terminates every remaining member when the last handle to the job disappears -- which
  happens exactly when this process exits, no matter how it exits.
- On Linux, hivemind's p2pd spawn is wrapped so that the child calls prctl(PR_SET_PDEATHSIG, SIGKILL)
  between fork and exec: the kernel then kills p2pd as soon as the process that spawned it dies.

Both are best-effort: if the guard cannot be armed, a warning is logged and startup continues.
The drift CLI entrypoints (``drift server``, ``drift up``, ``drift dht``) arm the guard themselves;
library users who embed drift in a longer-lived process can opt in by calling it explicitly.
"""

import ctypes
import os
import signal
import sys

from hivemind.utils.logging import get_logger

logger = get_logger(__name__)

# Held for the life of the process: closing the last handle to the job is what kills its members.
_windows_job_handle = None

# libc handle and spawning PID resolved before any fork so that the preexec hook stays minimal.
_libc = None
_linux_parent_pid = None


def tie_child_processes_to_this_process() -> bool:
    """Arm a platform-specific guard that kills our child processes when this process dies.

    Returns True if the guard is armed (idempotent), False if the platform has no mechanism
    or arming failed; failures are logged and never raise.
    """
    if sys.platform == "win32":
        return _arm_windows_kill_on_close_job()
    if sys.platform.startswith("linux"):
        return _arm_linux_pdeathsig()
    logger.debug(f"No parent-death guard available on {sys.platform}; a hard-killed server may orphan p2pd")
    return False


def _arm_windows_kill_on_close_job() -> bool:
    global _windows_job_handle
    if _windows_job_handle is not None:
        return True

    from ctypes import wintypes

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JobObjectExtendedLimitInformation = 9

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = ()
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        logger.warning(f"Could not create a job object (error {ctypes.get_last_error()}); p2pd may outlive a kill")
        return False

    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
    ):
        logger.warning(f"Could not configure the job object (error {ctypes.get_last_error()}); p2pd may outlive a kill")
        kernel32.CloseHandle(job)
        return False

    if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        # Most likely we already sit in a job that forbids nesting (only possible on pre-Windows 8 kernels
        # or in deliberately restricted jobs); nothing more we can do.
        logger.warning(f"Could not join the job object (error {ctypes.get_last_error()}); p2pd may outlive a kill")
        kernel32.CloseHandle(job)
        return False

    _windows_job_handle = job
    logger.info("Child processes (including p2pd) are now tied to this process via a kill-on-close job object")
    return True


def _ensure_libc():
    global _libc, _linux_parent_pid
    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
    if _linux_parent_pid is None:
        _linux_parent_pid = os.getpid()
    return _libc


def _set_pdeathsig():
    """Runs in the child between fork and exec (subprocess preexec_fn): die when the parent dies."""
    PR_SET_PDEATHSIG = 1
    expected_parent_pid = _linux_parent_pid if _linux_parent_pid is not None else os.getppid()
    _ensure_libc().prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
    # If the spawning process already died before prctl took effect, the signal never fires. Compare
    # against the captured spawning PID instead of assuming PID 1 means orphaned: a healthy container
    # entrypoint is PID 1, so its p2pd children legitimately report getppid() == 1.
    if os.getppid() != expected_parent_pid:
        os._exit(1)


class _SubprocessWithPdeathsig:
    """Mirrors asyncio.subprocess but makes spawned children die with the process that spawned them."""

    def __init__(self, real_subprocess):
        self._real = real_subprocess

    def __getattr__(self, name):
        return getattr(self._real, name)

    def create_subprocess_exec(self, *args, **kwargs):
        kwargs.setdefault("preexec_fn", _set_pdeathsig)
        return self._real.create_subprocess_exec(*args, **kwargs)


class _AsyncioWithPdeathsig:
    """Mirrors the asyncio module with the subprocess submodule swapped for _SubprocessWithPdeathsig.

    Installed as the module-global ``asyncio`` of hivemind.p2p.p2p_daemon, so it only affects
    hivemind's p2pd spawn and nothing else in the process.
    """

    def __init__(self, real_asyncio):
        self._real = real_asyncio
        self.subprocess = _SubprocessWithPdeathsig(real_asyncio.subprocess)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _arm_linux_pdeathsig() -> bool:
    from hivemind.p2p import p2p_daemon

    if isinstance(p2p_daemon.asyncio, _AsyncioWithPdeathsig):
        return True

    _ensure_libc()  # resolve before any fork so the preexec hook never has to dlopen after fork
    p2p_daemon.asyncio = _AsyncioWithPdeathsig(p2p_daemon.asyncio)
    logger.info("p2pd daemons will now receive SIGKILL when the process that spawned them dies (PR_SET_PDEATHSIG)")
    return True
