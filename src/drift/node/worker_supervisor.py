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


class WorkerPolicyError(PermissionError):
    """A contribution worker is blocked by the node's authoritative policy."""


@dataclass(frozen=True)
class WorkerLaunch:
    worker_id: str
    model_id: str
    command: Tuple[str, ...]
    auto_start: bool = False
    auto_restart: bool = True
    restart_backoff: float = 5.0
    policy_admitted: bool = True
    policy_reason: Optional[str] = None
    preferred: bool = False
    max_disk_bytes: Optional[int] = None
    environment: Tuple[Tuple[str, str], ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if not self.worker_id or not self.command:
            raise ValueError("worker id and command must not be empty")
        if self.restart_backoff <= 0:
            raise ValueError("worker restart_backoff must be positive")
        if self.policy_admitted and self.policy_reason is not None:
            raise ValueError("an admitted worker must not have a policy reason")
        if not self.policy_admitted and (not isinstance(self.policy_reason, str) or not self.policy_reason):
            raise ValueError("a policy-blocked worker must have a reason")
        if self.max_disk_bytes is not None and (
            isinstance(self.max_disk_bytes, bool) or not isinstance(self.max_disk_bytes, int) or self.max_disk_bytes < 1
        ):
            raise ValueError("worker max_disk_bytes must be a positive integer")
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
    schedule_suspended: bool = False
    schedule_stop_thread: Optional[threading.Thread] = field(default=None, repr=False)
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
        schedule_allowed: Optional[Callable[[], bool]] = None,
    ) -> None:
        if stop_timeout <= 0 or poll_period <= 0:
            raise ValueError("worker supervisor timeouts must be positive")
        self._records: Dict[str, _WorkerRecord] = {}
        for launch in launches:
            normalized = launch.worker_id.casefold()
            if normalized in self._records:
                raise ValueError(f"duplicate worker id {launch.worker_id!r}")
            self._records[normalized] = _WorkerRecord(
                launch=launch,
                desired_running=launch.auto_start and launch.policy_admitted,
            )
        self._stop_timeout = stop_timeout
        self._poll_period = poll_period
        self._popen = popen
        self._schedule_allowed = schedule_allowed
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

    def _schedule_status(self) -> Tuple[bool, Optional[str]]:
        if self._schedule_allowed is None:
            return True, None
        try:
            allowed = self._schedule_allowed()
        except Exception:
            logger.exception("Failed to evaluate the contribution schedule")
            return False, "the configured contribution schedule could not be evaluated"
        if not isinstance(allowed, bool):
            logger.error("Contribution schedule evaluator returned a non-boolean value")
            return False, "the configured contribution schedule could not be evaluated"
        if not allowed:
            return False, "outside the configured contribution schedule"
        return True, None

    def _spawn_locked(self, record: _WorkerRecord, *, defer_outside_schedule: bool = False) -> bool:
        if not record.launch.policy_admitted:
            record.desired_running = False
            raise WorkerPolicyError(record.launch.policy_reason)
        schedule_admitted, schedule_reason = self._schedule_status()
        if not schedule_admitted:
            record.state = WorkerState.PAUSED
            record.schedule_suspended = defer_outside_schedule and record.desired_running
            if defer_outside_schedule:
                return False
            record.desired_running = False
            raise WorkerPolicyError(schedule_reason)
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
        record.schedule_suspended = False
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

    def _finish_schedule_suspension(self, record: _WorkerRecord, process: subprocess.Popen) -> None:
        error: Optional[Exception] = None
        exit_code: Optional[int] = None
        try:
            exit_code = self._terminate(process)
        except Exception as exc:
            error = exc

        with self._lock:
            if error is not None:
                record.schedule_suspended = False
                record.state = WorkerState.CRASHED
                record.last_error = f"{type(error).__name__}: {error}"
            else:
                if record.process is process:
                    record.process = None
                record.last_exit_code = exit_code
                record.last_error = None
                record.state = WorkerState.PAUSED
            if record.schedule_stop_thread is threading.current_thread():
                record.schedule_stop_thread = None

        if error is not None:
            logger.error(
                "Failed to suspend worker %r for its contribution schedule",
                record.launch.worker_id,
                exc_info=(type(error), error, error.__traceback__),
            )

    def _suspend_for_schedule_locked(self, record: _WorkerRecord) -> None:
        if record.schedule_suspended or record.schedule_stop_thread is not None:
            return
        process = record.process
        if process is None:
            return
        record.schedule_suspended = True
        record.state = WorkerState.STOPPING
        thread = threading.Thread(
            target=self._finish_schedule_suspension,
            args=(record, process),
            name=f"drift-worker-schedule-stop-{record.launch.worker_id}",
            daemon=True,
        )
        record.schedule_stop_thread = thread
        thread.start()

    def _monitor_loop(self) -> None:
        while not self._stop.wait(self._poll_period):
            schedule_admitted, _ = self._schedule_status()
            with self._lock:
                for record in self._records.values():
                    self._refresh_locked(record)
                    if not schedule_admitted:
                        if record.desired_running:
                            self._suspend_for_schedule_locked(record)
                        continue
                    if record.schedule_suspended:
                        if record.schedule_stop_thread is not None or record.process is not None:
                            continue
                        record.schedule_suspended = False
                        if record.desired_running:
                            self._spawn_locked(record)
                        continue
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
                    self._spawn_locked(record, defer_outside_schedule=True)
            self._monitor = threading.Thread(
                target=self._monitor_loop,
                name="drift-worker-supervisor",
                daemon=True,
            )
            self._monitor.start()

    def start_worker(self, worker_id: str) -> bool:
        record = self._record(worker_id)
        with self._lock:
            if not record.launch.policy_admitted:
                record.desired_running = False
                raise WorkerPolicyError(record.launch.policy_reason)
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
            record.schedule_suspended = False
            schedule_stop_thread = record.schedule_stop_thread
            process = None if schedule_stop_thread is not None else record.process
            if process is None and schedule_stop_thread is None:
                record.state = WorkerState.PAUSED
                return False
            if process is not None:
                record.state = WorkerState.STOPPING
        if schedule_stop_thread is not None:
            schedule_stop_thread.join(timeout=(self._stop_timeout * 2) + self._poll_period)
            if schedule_stop_thread.is_alive():
                raise RuntimeError(f"failed to pause worker {record.launch.worker_id!r} within the stop timeout")
            with self._lock:
                if record.process is not None:
                    raise RuntimeError(
                        f"failed to pause worker {record.launch.worker_id!r}: "
                        f"{record.last_error or 'schedule suspension failed'}"
                    )
                record.state = WorkerState.PAUSED
            return True
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
        schedule_admitted, schedule_reason = self._schedule_status()
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
                        "policy_admitted": record.launch.policy_admitted,
                        "policy_reason": record.launch.policy_reason,
                        "schedule_admitted": schedule_admitted,
                        "schedule_reason": schedule_reason,
                        "schedule_suspended": record.schedule_suspended,
                        "preferred": record.launch.preferred,
                        "max_disk_bytes": record.launch.max_disk_bytes,
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
                record.schedule_suspended = False
            schedule_stop_threads = {
                id(record): record.schedule_stop_thread for record in records if record.schedule_stop_thread is not None
            }
        for thread in schedule_stop_threads.values():
            thread.join(timeout=(self._stop_timeout * 2) + self._poll_period)
        for record in records:
            schedule_stop_thread = schedule_stop_threads.get(id(record))
            if schedule_stop_thread is not None and schedule_stop_thread.is_alive():
                logger.error(
                    "Schedule-stop thread for worker %r exceeded the shutdown timeout",
                    record.launch.worker_id,
                )
                continue
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
