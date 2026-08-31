"""Fresh-process supervision for schema-v3 edge resource envelopes."""

from __future__ import annotations

import copy
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

EDGE_BENCHMARK_SUPERVISED_SCHEMA_VERSION = 3
DEFAULT_SAMPLE_INTERVAL_SECONDS = 0.02
DEFAULT_EXIT_GRACE_SECONDS = 5.0


def _sample_tree(psutil_module, pid: int) -> Tuple[int, int]:
    """Collect resource evidence only; OS containment is the cleanup authority."""
    try:
        root = psutil_module.Process(pid)
        processes = [root, *root.children(recursive=True)]
    except psutil_module.Error:
        return 0, 0
    rss = 0
    observed = 0
    for process in processes:
        try:
            rss += process.memory_info().rss
            observed += 1
        except psutil_module.Error:
            continue
    return rss, observed


class _PosixProcessGroup:
    kind = "posix_process_group"

    def __init__(self) -> None:
        self._pgid: Optional[int] = None

    def popen_kwargs(self) -> Dict[str, Any]:
        return {"start_new_session": True}

    def attach(self, process: subprocess.Popen) -> None:
        self._pgid = process.pid

    def resume(self, process: subprocess.Popen) -> None:
        del process

    def has_members(self) -> bool:
        if self._pgid is None:
            return False
        try:
            os.killpg(self._pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def terminate(self) -> None:
        if not self.has_members():
            return
        try:
            os.killpg(self._pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def kill(self) -> None:
        if not self.has_members():
            return
        try:
            os.killpg(self._pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def close(self) -> None:
        pass


class _WindowsJobObject:
    kind = "windows_job_object"

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        job_object_limit_kill_on_close = 0x2000
        job_object_extended_limit_information = 9

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

        class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_int64),
                ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64),
                ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        class THREADENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        kernel32.QueryInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPVOID,
        )
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        kernel32.Thread32First.restype = wintypes.BOOL
        kernel32.Thread32First.argtypes = (wintypes.HANDLE, ctypes.POINTER(THREADENTRY32))
        kernel32.Thread32Next.restype = wintypes.BOOL
        kernel32.Thread32Next.argtypes = (wintypes.HANDLE, ctypes.POINTER(THREADENTRY32))
        kernel32.OpenThread.restype = wintypes.HANDLE
        kernel32.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.ResumeThread.restype = wintypes.DWORD
        kernel32.ResumeThread.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise OSError(ctypes.get_last_error(), "could not create benchmark job object")
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = job_object_limit_kill_on_close
        if not kernel32.SetInformationJobObject(
            job,
            job_object_extended_limit_information,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(job)
            raise OSError(error, "could not configure benchmark job object")

        self._ctypes = ctypes
        self._kernel32 = kernel32
        self._job = job
        self._accounting_type = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION
        self._thread_entry_type = THREADENTRY32

    def popen_kwargs(self) -> Dict[str, Any]:
        create_suspended = 0x00000004
        return {"creationflags": create_suspended}

    def attach(self, process: subprocess.Popen) -> None:
        process_set_quota_and_terminate = 0x0101
        handle = self._kernel32.OpenProcess(process_set_quota_and_terminate, False, process.pid)
        if not handle:
            raise OSError(self._ctypes.get_last_error(), "could not open suspended benchmark child")
        try:
            if not self._kernel32.AssignProcessToJobObject(self._job, handle):
                raise OSError(self._ctypes.get_last_error(), "could not contain benchmark child")
        finally:
            self._kernel32.CloseHandle(handle)

    def resume(self, process: subprocess.Popen) -> None:
        th32cs_snapthread = 0x00000004
        thread_suspend_resume = 0x0002
        invalid_handle_value = self._ctypes.c_void_p(-1).value
        snapshot = self._kernel32.CreateToolhelp32Snapshot(th32cs_snapthread, 0)
        if snapshot == invalid_handle_value:
            raise OSError(self._ctypes.get_last_error(), "could not enumerate suspended benchmark child")
        try:
            entry = self._thread_entry_type()
            entry.dwSize = self._ctypes.sizeof(entry)
            more = self._kernel32.Thread32First(snapshot, self._ctypes.byref(entry))
            while more:
                if entry.th32OwnerProcessID == process.pid:
                    thread = self._kernel32.OpenThread(thread_suspend_resume, False, entry.th32ThreadID)
                    if not thread:
                        raise OSError(self._ctypes.get_last_error(), "could not open suspended benchmark thread")
                    try:
                        if self._kernel32.ResumeThread(thread) == 0xFFFFFFFF:
                            raise OSError(self._ctypes.get_last_error(), "could not resume benchmark child")
                    finally:
                        self._kernel32.CloseHandle(thread)
                    return
                more = self._kernel32.Thread32Next(snapshot, self._ctypes.byref(entry))
        finally:
            self._kernel32.CloseHandle(snapshot)
        raise OSError("could not locate suspended benchmark thread")

    def has_members(self) -> bool:
        job_object_basic_accounting_information = 1
        info = self._accounting_type()
        if not self._kernel32.QueryInformationJobObject(
            self._job,
            job_object_basic_accounting_information,
            self._ctypes.byref(info),
            self._ctypes.sizeof(info),
            None,
        ):
            raise OSError(self._ctypes.get_last_error(), "could not query benchmark job object")
        return info.ActiveProcesses != 0

    def terminate(self) -> None:
        if self.has_members() and not self._kernel32.TerminateJobObject(self._job, 1):
            raise OSError(self._ctypes.get_last_error(), "could not terminate benchmark job object")

    def kill(self) -> None:
        self.terminate()

    def close(self) -> None:
        if self._job is not None:
            self._kernel32.CloseHandle(self._job)
            self._job = None


def _new_containment():
    if sys.platform == "win32":
        return _WindowsJobObject()
    return _PosixProcessGroup()


def _wait_for_containment_exit(containment, timeout: float, sample_interval: float) -> bool:
    deadline = time.monotonic() + timeout
    while containment.has_members():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(sample_interval, remaining))
    return True


def _force_containment_exit(containment, process: subprocess.Popen, sample_interval: float) -> bool:
    containment.terminate()
    absent = _wait_for_containment_exit(containment, 1.0, sample_interval)
    if not absent:
        containment.kill()
        absent = _wait_for_containment_exit(containment, 2.0, sample_interval)
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        return False
    return absent and not containment.has_members()


def _schema_v3(child_result: Dict[str, Any], supervisor: Dict[str, Any]) -> Dict[str, Any]:
    if child_result.get("schema_version") != 2:
        raise RuntimeError("benchmark child returned an unsupported schema")
    result = copy.deepcopy(child_result)
    result["schema_version"] = EDGE_BENCHMARK_SUPERVISED_SCHEMA_VERSION

    workload = result.get("workload")
    if not isinstance(workload, dict):
        raise RuntimeError("benchmark child omitted workload evidence")
    workload.pop("prompt", None)
    workload.pop("output_ids", None)
    workload.pop("decoded", None)
    workload["prompt_retained"] = False
    workload["output_retained"] = False

    memory = result.get("memory")
    cleanup = result.get("cleanup")
    if not isinstance(memory, dict) or not isinstance(cleanup, dict):
        raise RuntimeError("benchmark child omitted resource or cleanup evidence")
    child_peak = memory.get("process_tree_rss_peak_bytes")
    memory["child_reported_process_tree_rss_peak_bytes"] = child_peak
    memory["process_tree_rss_peak_bytes"] = supervisor["process_tree_rss_peak_bytes"]
    baseline = memory.get("process_tree_rss_baseline_bytes")
    memory["process_tree_rss_peak_delta_bytes"] = (
        max(0, supervisor["process_tree_rss_peak_bytes"] - baseline) if isinstance(baseline, int) else None
    )

    for field in ("runtime_close", "route_manager", "process_tree", "memory", "accelerators"):
        if not isinstance(cleanup.get(field), dict):
            raise RuntimeError(f"benchmark child omitted {field} cleanup evidence")
    cleanup["memory"]["diagnostic_only"] = True
    child_boundary_clean = all(
        cleanup[field].get("clean") is True
        for field in ("runtime_close", "route_manager", "process_tree", "accelerators")
    )
    cleanup["supervisor"] = supervisor
    cleanup["passed"] = child_boundary_clean and supervisor.get("clean") is True
    result["privacy"] = {
        "prompt_retained": False,
        "output_retained": False,
        "credentials_retained": False,
        "local_paths_retained": False,
    }
    return result


def supervise_edge_benchmark(
    child_spec: Dict[str, Any],
    *,
    timeout_seconds: float = 3600.0,
    sample_interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_SECONDS,
    exit_grace_seconds: float = DEFAULT_EXIT_GRACE_SECONDS,
    _child_command: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Run one fixed child command, sample its process tree, and require complete exit."""
    for name, value in (
        ("timeout_seconds", timeout_seconds),
        ("sample_interval_seconds", sample_interval_seconds),
        ("exit_grace_seconds", exit_grace_seconds),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"{name} must be a positive number")

    try:
        import psutil
    except ImportError as exc:
        raise RuntimeError("edge benchmark supervision requires psutil; install drift[benchmark]") from exc

    try:
        containment = _new_containment()
    except OSError as exc:
        raise RuntimeError("benchmark supervisor could not establish process containment") from exc

    try:
        with tempfile.TemporaryDirectory(prefix="drift-edge-supervisor-") as temporary:
            result_path = Path(temporary) / "child-result.json"
            private_spec = dict(child_spec)
            private_spec["result_path"] = str(result_path)
            command = (
                list(_child_command)
                if _child_command is not None
                else [sys.executable, "-m", "drift.cli.run_edge_benchmark", "--supervised-child"]
            )
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **containment.popen_kwargs(),
            )
            try:
                containment.attach(process)
                containment.resume(process)
            except OSError as exc:
                try:
                    process.kill()
                except OSError:
                    pass
                process.wait(timeout=5)
                raise RuntimeError("benchmark supervisor could not contain the child before execution") from exc

            if process.stdin is None:
                _force_containment_exit(containment, process, sample_interval_seconds)
                raise RuntimeError("benchmark supervisor could not open child input")
            try:
                process.stdin.write(json.dumps(private_spec, ensure_ascii=False, allow_nan=False).encode("utf-8"))
                process.stdin.close()
            except (BrokenPipeError, OSError):
                _force_containment_exit(containment, process, sample_interval_seconds)
                raise RuntimeError("benchmark child rejected its private input")

            peak_rss = 0
            peak_process_count = 0
            samples = 0
            timed_out = False
            deadline = time.monotonic() + timeout_seconds
            while process.poll() is None:
                rss, process_count = _sample_tree(psutil, process.pid)
                peak_rss = max(peak_rss, rss)
                peak_process_count = max(peak_process_count, process_count)
                samples += 1
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                time.sleep(sample_interval_seconds)
            rss, process_count = _sample_tree(psutil, process.pid)
            peak_rss = max(peak_rss, rss)
            peak_process_count = max(peak_process_count, process_count)
            samples += 1

            if timed_out:
                cleanup_succeeded = _force_containment_exit(containment, process, sample_interval_seconds)
                suffix = "" if cleanup_succeeded else " and process containment cleanup was not proved"
                raise RuntimeError(f"benchmark child exceeded its supervised timeout{suffix}")

            exit_code = process.wait(timeout=5)
            descendants_exited = _wait_for_containment_exit(containment, exit_grace_seconds, sample_interval_seconds)
            forced_cleanup_required = not descendants_exited
            forced_cleanup_succeeded = (
                _force_containment_exit(containment, process, sample_interval_seconds)
                if forced_cleanup_required
                else True
            )
            all_contained_processes_absent = forced_cleanup_succeeded and not containment.has_members()

            if not result_path.is_file():
                raise RuntimeError("benchmark child exited without a result")
            try:
                child_result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"benchmark child result was unreadable: {type(exc).__name__}") from exc
            if exit_code != 0:
                error_type = child_result.get("error_type") if isinstance(child_result, dict) else None
                suffix = f" ({error_type})" if isinstance(error_type, str) else ""
                raise RuntimeError(f"benchmark child failed{suffix}")

            supervisor = {
                "containment": containment.kind,
                "sample_interval_seconds": sample_interval_seconds,
                "samples": samples,
                "peak_process_count": peak_process_count,
                "process_tree_rss_peak_bytes": peak_rss,
                "child_exit_code": exit_code,
                "child_exited": True,
                "descendants_exited": descendants_exited,
                "forced_cleanup_required": forced_cleanup_required,
                "forced_cleanup_succeeded": forced_cleanup_succeeded,
                "all_contained_processes_absent": all_contained_processes_absent,
                "all_tracked_processes_absent": all_contained_processes_absent,
                "clean": not forced_cleanup_required and all_contained_processes_absent,
            }
            return _schema_v3(child_result, supervisor)
    finally:
        containment.close()
