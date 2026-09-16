"""A small on-disk registry of DRIFT-LLM servers running on this machine.

``drift up`` / ``drift server`` write one JSON record per live server under ``$DRIFT_CACHE/run/``
(default ``~/.cache/drift/run/``)
so that ``drift down`` can find and stop them. Records are best-effort: a server that exits cleanly
removes its own record, while one that is hard-killed leaves a stale file behind -- ``drift down``
notices the pid is gone and cleans it up.

This module also holds the small cross-platform process helpers ``drift down`` needs (liveness,
terminate, and a best-effort command line lookup used to guard against pid reuse). None of this
needs psutil: POSIX uses ``os.kill`` and Windows uses a couple of ctypes calls.
"""

import json
import os
import signal
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

RUN_DIR = Path(os.environ.get("DRIFT_CACHE") or (Path.home() / ".cache" / "drift")).expanduser() / "run"


@dataclass
class ServerRecord:
    pid: int
    model: Optional[str]
    dht_prefix: Optional[str]
    maddrs: List[str] = field(default_factory=list)
    started_at: float = 0.0
    path: Optional[str] = None  # filled in when the record is read back from disk

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.started_at)


def _record_path(pid: int) -> Path:
    return RUN_DIR / f"server-{pid}.json"


def register_server(*, model: Optional[str], dht_prefix: Optional[str], maddrs) -> Path:
    """Record the current process as a running server. Overwrites any record for this pid."""
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    record = ServerRecord(
        pid=os.getpid(),
        model=model,
        dht_prefix=dht_prefix,
        maddrs=[str(m) for m in maddrs],
        started_at=time.time(),
    )
    payload = {k: v for k, v in asdict(record).items() if k != "path"}
    path = _record_path(record.pid)
    # Write-then-rename so a reader never sees a half-written file.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)
    return path


def unregister_server(pid: Optional[int] = None) -> None:
    """Remove a server's record (defaults to the current process). No-op if it is already gone."""
    try:
        _record_path(pid if pid is not None else os.getpid()).unlink()
    except FileNotFoundError:
        pass


def iter_records() -> List[ServerRecord]:
    """Return every server record currently on disk (unsorted liveness is the caller's problem)."""
    if not RUN_DIR.exists():
        return []
    records = []
    for path in sorted(RUN_DIR.glob("server-*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        data.pop("path", None)
        try:
            records.append(ServerRecord(path=str(path), **data))
        except TypeError:
            continue  # a record written by an incompatible future/older schema
    return records


def _win_process_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_ACCESS_DENIED = 5

    open_process = kernel32.OpenProcess
    open_process.restype = wintypes.HANDLE
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = open_process(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # No such pid -> ERROR_INVALID_PARAMETER (87). A live but unqueryable pid -> ACCESS_DENIED.
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.restype = wintypes.BOOL
        get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        code = wintypes.DWORD()
        if get_exit_code(handle, ctypes.byref(code)):
            return code.value == STILL_ACTIVE
        return True  # couldn't read the exit code, but the handle opened, so assume alive
    finally:
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle(handle)


def _pid_is_zombie(pid: int) -> bool:
    """True if pid is a defunct (already-exited, not-yet-reaped) process. POSIX only."""
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return False
        # Format: "<pid> (<comm>) <state> ..."; comm may itself contain spaces and parens, so split
        # after the last ") " to reach the state field.
        try:
            return stat.rsplit(") ", 1)[1].split(" ", 1)[0] == "Z"
        except IndexError:
            return False
    if sys.platform == "darwin":
        import subprocess

        try:
            out = subprocess.run(["ps", "-o", "state=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        except Exception:
            return False
        return out.stdout.strip().startswith("Z")  # e.g. "Z", "Z+"
    return False


def process_alive(pid: int) -> bool:
    """True if a process with this pid exists and is not a defunct (zombie) child."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _win_process_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists, we just may not signal it
    # os.kill(pid, 0) also succeeds for a zombie -- a child that has exited but not yet been reaped
    # by its parent. A zombie is not a running server, so treat it as gone. (Zombies only arise for
    # one's own children, which drift down never targets, so in practice this mainly keeps the test
    # suite honest, where a terminated-but-unreaped sleeper child would otherwise read as alive.)
    if _pid_is_zombie(pid):
        return False
    return True


def terminate_process(pid: int, *, force: bool = False) -> None:
    """Ask a process to stop. POSIX: SIGTERM (or SIGKILL if ``force``). Windows: TerminateProcess.

    On Windows there is no graceful signal for a non-console process, so ``force`` has no effect
    there -- the issue #5 job object ensures the server's p2pd child dies with it regardless.
    """
    sig = signal.SIGKILL if (force and hasattr(signal, "SIGKILL")) else signal.SIGTERM
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, OSError):
        pass


def process_command_line(pid: int) -> Optional[str]:
    """Best-effort command line (or image name) for a pid, or None if it cannot be determined.

    ``drift down`` uses this only as a guard against pid reuse, so an unknown answer (None) is fine
    -- the caller falls back to liveness alone.
    """
    try:
        if sys.platform.startswith("linux"):
            return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace").strip()
        if sys.platform == "darwin":
            import subprocess

            out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5)
            return out.stdout.strip() or None
        if sys.platform == "win32":
            import subprocess

            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True, text=True, timeout=10
            )
            line = out.stdout.strip()
            # "No tasks are running..." when the pid is gone; otherwise a CSV row starting with the image name.
            return line if line.startswith('"') else None
    except Exception:
        return None
    return None
