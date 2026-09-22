"""Bind a loading acknowledgement to the supervised interpreter, including venvs.

Windows' venv executable is a launcher with one immediate interpreter child.
Accept only the current runtime's known launcher/base pair and exact arguments;
never discover a worker by arbitrary descendant PID or by the status file alone.
All OS inspection runs on the supervisor's background observer.
"""

from __future__ import annotations

import ctypes
import math
import os
import sys
from dataclasses import dataclass

import psutil


class LoadingIdentityError(RuntimeError):
    def __init__(self):
        super().__init__("managed worker loading identity is unavailable")


@dataclass(frozen=True)
class LoadingProcessIdentity:
    pid: int
    creation_time: float
    root_pid: int
    root_creation_time: float


def _path(value):
    return os.path.normcase(os.path.realpath(os.path.abspath(value)))


def _created(process):
    value = process.create_time()
    if not isinstance(value, (float, int)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise LoadingIdentityError()
    return value


def _console_host_path():
    # A no-window console process may spawn conhost before its interpreter.
    # Resolve the OS directory through the API, not an inherited environment.
    buffer = ctypes.create_unicode_buffer(32768)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetSystemDirectoryW.argtypes = (ctypes.c_wchar_p, ctypes.c_uint)
    kernel32.GetSystemDirectoryW.restype = ctypes.c_uint
    length = kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise LoadingIdentityError()
    return _path(os.path.join(buffer.value, "conhost.exe"))


def resolve_loading_worker_pid(process, command, *, expected_identity=None):
    """Return the current, proven interpreter identity, or None during launcher boot.

    The live Popen handle establishes the root generation; stable creation times
    and a live immediate-parent relationship bind the one supported launcher hop.
    The caller must revalidate after status I/O and then check its supervisor
    generation ticket before accepting the acknowledgement.
    """
    try:
        if not command or process.poll() is not None:
            raise LoadingIdentityError()
        root = psutil.Process(process.pid)
        root_created = _created(root)
        if expected_identity is not None and (
            not isinstance(expected_identity, LoadingProcessIdentity)
            or expected_identity.root_pid != process.pid
            or expected_identity.root_creation_time != root_created
        ):
            raise LoadingIdentityError()
        base = getattr(sys, "_base_executable", sys.executable)
        launcher = (
            sys.platform == "win32"
            and not getattr(sys, "frozen", False)
            and _path(sys.executable) != _path(base)
            and _path(command[0]) == _path(sys.executable)
        )
        expected_root_exe = sys.executable if launcher else command[0]
        root_arguments = root.cmdline()
        if not root_arguments and expected_identity is None:
            return None
        if _path(root.exe()) != _path(expected_root_exe) or root_arguments[1:] != list(command[1:]):
            raise LoadingIdentityError()
        worker = root
        if launcher:
            children = root.children(recursive=False)
            interpreters = []
            for child in children:
                executable = _path(child.exe())
                if executable == _path(base):
                    interpreters.append(child)
                elif executable != _console_host_path() or child.ppid() != root.pid or _created(child) < root_created:
                    raise LoadingIdentityError()
            if not interpreters and expected_identity is None:
                if process.poll() is not None:
                    raise LoadingIdentityError()
                return None
            if len(interpreters) != 1:
                raise LoadingIdentityError()
            worker = interpreters[0]
            worker_arguments = worker.cmdline()
            if not worker_arguments and expected_identity is None:
                return None
            if (
                worker.ppid() != root.pid
                or _path(worker.exe()) != _path(base)
                or worker_arguments[1:] != list(command[1:])
                or _created(worker) < root_created
            ):
                raise LoadingIdentityError()
        identity = LoadingProcessIdentity(worker.pid, _created(worker), root.pid, root_created)
        if expected_identity is not None and identity != expected_identity:
            raise LoadingIdentityError()
        # Recheck both identities after the relationship/argument observations.
        if (
            process.poll() is not None
            or _created(psutil.Process(root.pid)) != root_created
            or _created(psutil.Process(worker.pid)) != identity.creation_time
            or (launcher and psutil.Process(worker.pid).ppid() != root.pid)
        ):
            raise LoadingIdentityError()
        return identity
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        if expected_identity is None and process.poll() is None:
            return None
        raise LoadingIdentityError() from None
    except Exception:
        raise LoadingIdentityError() from None
