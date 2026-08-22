"""Isolated contribution-worker process supervision for the local node."""

from __future__ import annotations

import collections
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Deque, Dict, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


class WorkerState(str, Enum):
    PAUSED = "paused"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    CRASHED = "crashed"


class WorkerNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class WorkerLaunch:
    worker_id: str
    model_id: str
    command: Tuple[str, ...]
    auto_start: bool = False
    auto_restart: bool = True
    restart_backoff: float = 5.0
    environment: Tuple[Tuple[str, str], ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if not self.worker_id or not self.command:
            raise ValueError("worker id and command must not be empty")
        if self.restart_backoff <= 0:
            raise ValueError("worker restart_backoff must be positive")
        if any(not name or not isinstance(name, str) or not isinstance(value, str) for name, value in self.environment):
            raise ValueError("worker environment names and values must be strings")


@dataclass
class _WorkerRecord:
    launch: WorkerLaunch
    state: WorkerState = WorkerState.PAUSED
    desired_running: bool = False
    process: Optional[subprocess.Popen] = None
    last_exit_code: Optional[int] = None
    last_error: Optional[str] = None
    started_at: Optional[float] = None
    restart_count: int = 0
    next_restart_at: float = 0.0
    recent_logs: Deque[str] = field(default_factory=lambda: collections.deque(maxlen=50))


class WorkerSupervisor:
    """Own worker subprocesses while keeping failures outside the API process."""

    def __init__(
        self,
        launches: Sequence[WorkerLaunch],
        *,
        stop_timeout: float = 10.0,
        poll_period: float = 0.25,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    ) -> None:
        if stop_timeout <= 0 or poll_period <= 0:
            raise ValueError("worker supervisor timeouts must be positive")
        self._records: Dict[str, _WorkerRecord] = {}
        for launch in launches:
            normalized = launch.worker_id.casefold()
            if normalized in self._records:
                raise ValueError(f"duplicate worker id {launch.worker_id!r}")
            self._records[normalized] = _WorkerRecord(launch=launch, desired_running=launch.auto_start)
        self._stop_timeout = stop_timeout
        self._poll_period = poll_period
        self._popen = popen
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._monitor: Optional[threading.Thread] = None
        self._started = False
        self._closed = False

    def _record(self, worker_id: str) -> _WorkerRecord:
        with self._lock:
            record = self._records.get(worker_id.casefold())
            if record is None:
                raise WorkerNotFoundError(f"unknown worker {worker_id!r}")
            return record

    @staticmethod
    def _creation_flags() -> int:
        return getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    def _spawn_locked(self, record: _WorkerRecord) -> bool:
        if self._closed:
            raise RuntimeError("worker supervisor is closed")
        if record.process is not None and record.process.poll() is None:
            return False
        record.state = WorkerState.STARTING
        environment = os.environ.copy()
        environment.update(record.launch.environment)
        environment["PYTHONUNBUFFERED"] = "1"
        try:
            process = self._popen(
                list(record.launch.command),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=environment,
                creationflags=self._creation_flags(),
            )
        except Exception as exc:
            record.process = None
            record.state = WorkerState.CRASHED
            record.last_error = f"{type(exc).__name__}: {exc}"
            record.next_restart_at = time.monotonic() + record.launch.restart_backoff
            return False

        if record.started_at is not None:
            record.restart_count += 1
        record.process = process
        record.state = WorkerState.RUNNING
        record.last_error = None
        record.last_exit_code = None
        record.started_at = time.time()
        thread = threading.Thread(
            target=self._drain_output,
            args=(record, process),
            name=f"drift-worker-log-{record.launch.worker_id}",
            daemon=True,
        )
        thread.start()
        return True

    def _drain_output(self, record: _WorkerRecord, process: subprocess.Popen) -> None:
        stream = process.stdout
        if stream is None:
            return
        try:
            for line in stream:
                message = line.rstrip("\r\n")
                with self._lock:
                    record.recent_logs.append(message)
                logger.info("worker[%s] %s", record.launch.worker_id, message)
        except Exception:
            logger.exception("Failed to read logs for worker %r", record.launch.worker_id)
        finally:
            stream.close()

    def _refresh_locked(self, record: _WorkerRecord) -> None:
        process = record.process
        if process is None or record.state is WorkerState.STOPPING:
            return
        exit_code = process.poll()
        if exit_code is None:
            return
        record.process = None
        record.last_exit_code = exit_code
        if record.desired_running:
            record.state = WorkerState.CRASHED
            record.last_error = f"worker exited with code {exit_code}"
            record.next_restart_at = time.monotonic() + record.launch.restart_backoff
        else:
            record.state = WorkerState.PAUSED

    def _monitor_loop(self) -> None:
        while not self._stop.wait(self._poll_period):
            with self._lock:
                for record in self._records.values():
                    self._refresh_locked(record)
                    if (
                        record.desired_running
                        and record.process is None
                        and record.launch.auto_restart
                        and time.monotonic() >= record.next_restart_at
                    ):
                        self._spawn_locked(record)

    def start_service(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("worker supervisor is closed")
            if self._started:
                return
            self._started = True
            for record in self._records.values():
                if record.desired_running:
                    self._spawn_locked(record)
            self._monitor = threading.Thread(
                target=self._monitor_loop,
                name="drift-worker-supervisor",
                daemon=True,
            )
            self._monitor.start()

    def start_worker(self, worker_id: str) -> bool:
        record = self._record(worker_id)
        with self._lock:
            record.desired_running = True
            return self._spawn_locked(record)

    def _terminate(self, process: subprocess.Popen) -> int:
        if process.poll() is None:
            process.terminate()
            try:
                return process.wait(timeout=self._stop_timeout)
            except subprocess.TimeoutExpired:
                process.kill()
        return process.wait(timeout=self._stop_timeout)

    def pause_worker(self, worker_id: str) -> bool:
        record = self._record(worker_id)
        with self._lock:
            record.desired_running = False
            process = record.process
            if process is None:
                record.state = WorkerState.PAUSED
                return False
            record.state = WorkerState.STOPPING
        try:
            exit_code = self._terminate(process)
        except Exception as exc:
            with self._lock:
                record.state = WorkerState.CRASHED
                record.last_error = f"{type(exc).__name__}: {exc}"
            raise RuntimeError(f"failed to pause worker {record.launch.worker_id!r}: {exc}") from exc
        with self._lock:
            if record.process is process:
                record.process = None
            record.last_exit_code = exit_code
            record.state = WorkerState.PAUSED
        return True

    def restart_worker(self, worker_id: str) -> bool:
        self.pause_worker(worker_id)
        return self.start_worker(worker_id)

    def snapshots(self) -> Tuple[Dict[str, Any], ...]:
        with self._lock:
            result = []
            for record in sorted(self._records.values(), key=lambda item: item.launch.worker_id.casefold()):
                self._refresh_locked(record)
                result.append(
                    {
                        "id": record.launch.worker_id,
                        "model": record.launch.model_id,
                        "state": record.state.value,
                        "desired_running": record.desired_running,
                        "auto_restart": record.launch.auto_restart,
                        "pid": record.process.pid if record.process is not None else None,
                        "started_at": record.started_at,
                        "restart_count": record.restart_count,
                        "last_exit_code": record.last_exit_code,
                        "last_error": record.last_error,
                        "recent_logs": list(record.recent_logs),
                    }
                )
            return tuple(result)

    def snapshot(self, worker_id: str) -> Dict[str, Any]:
        record = self._record(worker_id)
        with self._lock:
            self._refresh_locked(record)
            return next(
                snapshot
                for snapshot in self.snapshots()
                if snapshot["id"].casefold() == record.launch.worker_id.casefold()
            )

    @property
    def launches(self) -> Tuple[WorkerLaunch, ...]:
        with self._lock:
            return tuple(record.launch for record in self._records.values())

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            records = tuple(self._records.values())
            monitor = self._monitor
            for record in records:
                record.desired_running = False
        for record in records:
            process = record.process
            if process is not None:
                with self._lock:
                    record.state = WorkerState.STOPPING
                try:
                    exit_code = self._terminate(process)
                except Exception as exc:
                    with self._lock:
                        record.last_error = f"{type(exc).__name__}: {exc}"
                    logger.exception("Failed to stop worker %r", record.launch.worker_id)
                else:
                    with self._lock:
                        record.last_exit_code = exit_code
                        record.process = None
                        record.state = WorkerState.PAUSED
        if monitor is not None:
            monitor.join(timeout=5)
