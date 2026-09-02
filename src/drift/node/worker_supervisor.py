"""Isolated contribution-worker process supervision for the local node."""

from __future__ import annotations

import collections
import logging
import math
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


class WorkerReconfigurationBusyError(RuntimeError):
    """Live policy cannot change while a worker still has running intent."""


class SystemBandwidthMonitor:
    """Estimate aggregate host network traffic without inspecting request contents."""

    def __init__(
        self,
        *,
        counters: Optional[Callable[[], Any]] = None,
        clock: Callable[[], float] = time.monotonic,
        min_interval: float = 0.05,
    ) -> None:
        if min_interval <= 0:
            raise ValueError("bandwidth sampling interval must be positive")
        self._counters = counters
        self._clock = clock
        self._min_interval = min_interval
        self._last_total: Optional[int] = None
        self._last_time: Optional[float] = None
        self._last_rate = 0.0
        self._lock = threading.Lock()
        self()

    def __call__(self) -> Optional[float]:
        counters = self._counters
        if counters is None:
            try:
                import psutil
            except ImportError:
                return None
            counters = psutil.net_io_counters
        try:
            sample = counters()
            total = int(sample.bytes_sent) + int(sample.bytes_recv)
            now = float(self._clock())
        except Exception:
            return None
        if total < 0 or not math.isfinite(now):
            return None
        with self._lock:
            if self._last_total is None or total < self._last_total:
                self._last_total = total
                self._last_time = now
                self._last_rate = 0.0
                return self._last_rate
            elapsed = now - self._last_time
            if elapsed <= 0 or elapsed < self._min_interval:
                return self._last_rate
            self._last_rate = (total - self._last_total) * 8 / elapsed / 1_000_000
            self._last_total = total
            self._last_time = now
            return self._last_rate


class NvidiaPowerMonitor:
    """Read aggregate NVIDIA device power; unsupported hardware stays unavailable."""

    def __init__(self, device_indices: Sequence[int]) -> None:
        self._device_indices = tuple(sorted(set(device_indices)))
        self._pynvml = None
        self._initialized = False
        self._lock = threading.Lock()

    def __call__(self) -> Optional[float]:
        if not self._device_indices:
            return None
        with self._lock:
            if self._pynvml is None:
                try:
                    import pynvml
                except ImportError:
                    return None
                self._pynvml = pynvml
            try:
                if not self._initialized:
                    self._pynvml.nvmlInit()
                    self._initialized = True
                return (
                    sum(
                        self._pynvml.nvmlDeviceGetPowerUsage(self._pynvml.nvmlDeviceGetHandleByIndex(index))
                        for index in self._device_indices
                    )
                    / 1000
                )
            except Exception:
                return None


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
    automatic: bool = False
    block_indices: Optional[str] = None
    placement_reason: Optional[str] = None
    intent_published: bool = False
    remote_acknowledged: bool = False
    max_disk_bytes: Optional[int] = None
    max_vram_bytes: Optional[int] = None
    vram_device: Optional[str] = None
    vram_pool_bytes: Optional[int] = None
    max_bandwidth_mbps: Optional[float] = None
    max_power_watts: Optional[float] = None
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
        if self.automatic and (self.block_indices is None or self.placement_reason is None):
            raise ValueError("automatic workers require a block range and placement reason")
        if not self.automatic and (self.block_indices is not None or self.placement_reason is not None):
            raise ValueError("manual workers must not carry automatic placement metadata")
        if type(self.intent_published) is not bool or type(self.remote_acknowledged) is not bool:
            raise ValueError("placement intent publication fields must be booleans")
        if self.intent_published != self.remote_acknowledged:
            raise ValueError("placement intent publication requires a remote acknowledgement")
        if not self.automatic and self.intent_published:
            raise ValueError("manual workers must not carry an acknowledged automatic intent")
        if self.automatic and self.policy_admitted and not self.remote_acknowledged:
            raise ValueError("admitted automatic workers require a remotely acknowledged intent")
        if self.max_disk_bytes is not None and (
            isinstance(self.max_disk_bytes, bool) or not isinstance(self.max_disk_bytes, int) or self.max_disk_bytes < 1
        ):
            raise ValueError("worker max_disk_bytes must be a positive integer")
        if self.max_vram_bytes is not None and (
            isinstance(self.max_vram_bytes, bool) or not isinstance(self.max_vram_bytes, int) or self.max_vram_bytes < 1
        ):
            raise ValueError("worker max_vram_bytes must be a positive integer")
        vram_fields = (self.max_vram_bytes, self.vram_device, self.vram_pool_bytes)
        if any(value is not None for value in vram_fields) and not all(value is not None for value in vram_fields):
            raise ValueError("worker VRAM reservation fields must be configured together")
        if self.vram_pool_bytes is not None and (
            isinstance(self.vram_pool_bytes, bool)
            or not isinstance(self.vram_pool_bytes, int)
            or self.vram_pool_bytes < self.max_vram_bytes
        ):
            raise ValueError("worker vram_pool_bytes must cover its reservation")
        for name, value in (
            ("max_bandwidth_mbps", self.max_bandwidth_mbps),
            ("max_power_watts", self.max_power_watts),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0
            ):
                raise ValueError(f"worker {name} must be a finite positive number")
        if any(not name or not isinstance(name, str) or not isinstance(value, str) for name, value in self.environment):
            raise ValueError("worker environment names and values must be strings")


@dataclass(frozen=True)
class WorkerSupervisorSettings:
    """A completely validated, atomically swappable worker-policy configuration."""

    launches: Tuple[WorkerLaunch, ...]
    stop_timeout: float
    schedule_allowed: Optional[Callable[[], bool]] = None
    bandwidth_mbps: Optional[Callable[[], Optional[float]]] = None
    power_watts: Optional[Callable[[str], Optional[float]]] = None

    def __post_init__(self) -> None:
        if not isinstance(self.launches, tuple):
            raise ValueError("worker launches must be a tuple")
        normalized_ids = [launch.worker_id.casefold() for launch in self.launches]
        if len(set(normalized_ids)) != len(normalized_ids):
            raise ValueError("worker ids must be unique case-insensitively")
        if self.stop_timeout <= 0:
            raise ValueError("worker stop timeout must be positive")


@dataclass
class _WorkerRecord:
    launch: WorkerLaunch
    state: WorkerState = WorkerState.PAUSED
    desired_running: bool = False
    operator_paused: bool = False
    process: Optional[subprocess.Popen] = None
    last_exit_code: Optional[int] = None
    last_error: Optional[str] = None
    started_at: Optional[float] = None
    restart_count: int = 0
    next_restart_at: float = 0.0
    schedule_suspended: bool = False
    resource_suspended: bool = False
    last_power_watts: Optional[float] = None
    suspension_stop_thread: Optional[threading.Thread] = field(default=None, repr=False)
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
        bandwidth_mbps: Optional[Callable[[], Optional[float]]] = None,
        power_watts: Optional[Callable[[str], Optional[float]]] = None,
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
        self._bandwidth_mbps = bandwidth_mbps
        self._power_watts = power_watts
        self._last_bandwidth_mbps: Optional[float] = None
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

    def _measured_budget_status_locked(
        self,
        *,
        label: str,
        unit: str,
        limit: Optional[float],
        provider: Optional[Callable[[], Optional[float]]],
        last_value_owner: Any,
        last_value_attribute: str,
    ) -> Tuple[bool, Optional[str]]:
        if limit is None:
            return True, None
        try:
            value = None if provider is None else provider()
        except Exception:
            logger.exception("Failed to measure contribution %s usage", label)
            value = None
        if (
            value is None
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            setattr(last_value_owner, last_value_attribute, None)
            return False, f"{label} telemetry is unavailable for the configured contribution budget"
        value = float(value)
        setattr(last_value_owner, last_value_attribute, value)
        if value > limit:
            return False, f"{label} usage {value:.2f} {unit} exceeds the {limit:.2f} {unit} contribution budget"
        return True, None

    def _resource_status_locked(self, record: _WorkerRecord) -> Tuple[bool, Optional[str]]:
        launch = record.launch
        if launch.max_vram_bytes is not None:
            reserved = sum(
                other.launch.max_vram_bytes
                for other in self._records.values()
                if other is not record
                and other.launch.vram_device == launch.vram_device
                and other.process is not None
                and other.process.poll() is None
            )
            if reserved + launch.max_vram_bytes > launch.vram_pool_bytes:
                return False, f"VRAM budget is already reserved on {launch.vram_device}"
        bandwidth_admitted, bandwidth_reason = self._measured_budget_status_locked(
            label="bandwidth",
            unit="Mbps",
            limit=launch.max_bandwidth_mbps,
            provider=self._bandwidth_mbps,
            last_value_owner=self,
            last_value_attribute="_last_bandwidth_mbps",
        )
        if not bandwidth_admitted:
            return False, bandwidth_reason
        return self._measured_budget_status_locked(
            label="power",
            unit="W",
            limit=launch.max_power_watts,
            provider=(None if self._power_watts is None else lambda: self._power_watts(record.launch.worker_id)),
            last_value_owner=record,
            last_value_attribute="last_power_watts",
        )

    def _spawn_locked(
        self,
        record: _WorkerRecord,
        *,
        defer_outside_schedule: bool = False,
        defer_unavailable_resources: bool = False,
    ) -> bool:
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
        resource_admitted, resource_reason = self._resource_status_locked(record)
        if not resource_admitted:
            record.state = WorkerState.PAUSED
            record.resource_suspended = defer_unavailable_resources and record.desired_running
            if defer_unavailable_resources:
                return False
            record.desired_running = False
            raise WorkerPolicyError(resource_reason)
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
        record.resource_suspended = False
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

    def _finish_suspension(self, record: _WorkerRecord, process: subprocess.Popen) -> None:
        error: Optional[Exception] = None
        exit_code: Optional[int] = None
        try:
            exit_code = self._terminate(process)
        except Exception as exc:
            error = exc

        with self._lock:
            if error is not None:
                record.schedule_suspended = False
                record.resource_suspended = False
                record.state = WorkerState.CRASHED
                record.last_error = f"{type(error).__name__}: {error}"
            else:
                if record.process is process:
                    record.process = None
                record.last_exit_code = exit_code
                record.last_error = None
                record.state = WorkerState.PAUSED
            if record.suspension_stop_thread is threading.current_thread():
                record.suspension_stop_thread = None

        if error is not None:
            logger.error(
                "Failed to suspend worker %r for its contribution policy",
                record.launch.worker_id,
                exc_info=(type(error), error, error.__traceback__),
            )

    def _suspend_locked(self, record: _WorkerRecord, *, schedule: bool = False, resource: bool = False) -> None:
        if schedule:
            record.schedule_suspended = True
        if resource:
            record.resource_suspended = True
        if record.suspension_stop_thread is not None:
            return
        process = record.process
        if process is None:
            record.state = WorkerState.PAUSED
            return
        record.state = WorkerState.STOPPING
        thread = threading.Thread(
            target=self._finish_suspension,
            args=(record, process),
            name=f"drift-worker-policy-stop-{record.launch.worker_id}",
            daemon=True,
        )
        record.suspension_stop_thread = thread
        thread.start()

    def _monitor_loop(self) -> None:
        while not self._stop.wait(self._poll_period):
            schedule_admitted, _ = self._schedule_status()
            with self._lock:
                for record in self._records.values():
                    self._refresh_locked(record)
                    if not schedule_admitted:
                        if record.desired_running and (
                            record.process is not None or record.state is WorkerState.PAUSED
                        ):
                            self._suspend_locked(record, schedule=True)
                        continue
                    if record.schedule_suspended:
                        if record.suspension_stop_thread is not None or record.process is not None:
                            continue
                        record.schedule_suspended = False
                        if record.desired_running:
                            self._spawn_locked(record, defer_unavailable_resources=True)
                        continue

                    resource_admitted, _ = self._resource_status_locked(record)
                    if (
                        not resource_admitted
                        and record.desired_running
                        and record.process is not None
                        and record.process.poll() is None
                    ):
                        self._suspend_locked(record, resource=True)
                        continue
                    if record.resource_suspended:
                        if record.suspension_stop_thread is not None or record.process is not None:
                            continue
                        if not resource_admitted:
                            continue
                        record.resource_suspended = False
                        if record.desired_running:
                            self._spawn_locked(record, defer_unavailable_resources=True)
                        continue
                    if (
                        record.desired_running
                        and record.process is None
                        and record.launch.auto_restart
                        and time.monotonic() >= record.next_restart_at
                    ):
                        self._spawn_locked(record, defer_unavailable_resources=True)

    def start_service(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("worker supervisor is closed")
            if self._started:
                return
            self._started = True
            for record in self._records.values():
                if record.desired_running:
                    self._spawn_locked(
                        record,
                        defer_outside_schedule=True,
                        defer_unavailable_resources=True,
                    )
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
            record.operator_paused = False
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
        """Pause a worker and persist the operator's explicit stopped intent."""

        return self._pause_worker(worker_id, operator_action=True)

    def pause_worker_for_reconfiguration(self, worker_id: str) -> bool:
        """Quiesce a worker without manufacturing an operator pause."""

        return self._pause_worker(worker_id, operator_action=False)

    def _pause_worker(self, worker_id: str, *, operator_action: bool) -> bool:
        record = self._record(worker_id)
        with self._lock:
            if operator_action:
                record.operator_paused = True
            record.desired_running = False
            record.schedule_suspended = False
            record.resource_suspended = False
            suspension_stop_thread = record.suspension_stop_thread
            process = None if suspension_stop_thread is not None else record.process
            if process is None and suspension_stop_thread is None:
                record.state = WorkerState.PAUSED
                return False
            if process is not None:
                record.state = WorkerState.STOPPING
        if suspension_stop_thread is not None:
            suspension_stop_thread.join(timeout=(self._stop_timeout * 2) + self._poll_period)
            if suspension_stop_thread.is_alive():
                raise RuntimeError(f"failed to pause worker {record.launch.worker_id!r} within the stop timeout")
            with self._lock:
                if record.process is not None:
                    raise RuntimeError(
                        f"failed to pause worker {record.launch.worker_id!r}: "
                        f"{record.last_error or 'policy suspension failed'}"
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
                resource_admitted, resource_reason = self._resource_status_locked(record)
                result.append(
                    {
                        "id": record.launch.worker_id,
                        "model": record.launch.model_id,
                        "state": record.state.value,
                        "desired_running": record.desired_running,
                        "operator_paused": record.operator_paused,
                        "auto_restart": record.launch.auto_restart,
                        "policy_admitted": record.launch.policy_admitted,
                        "policy_reason": record.launch.policy_reason,
                        "schedule_admitted": schedule_admitted,
                        "schedule_reason": schedule_reason,
                        "schedule_suspended": record.schedule_suspended,
                        "resource_admitted": resource_admitted,
                        "resource_reason": resource_reason,
                        "resource_suspended": record.resource_suspended,
                        "preferred": record.launch.preferred,
                        "automatic": record.launch.automatic,
                        "block_indices": record.launch.block_indices,
                        "placement_reason": record.launch.placement_reason,
                        "intent_published": record.launch.intent_published,
                        "remote_acknowledged": record.launch.remote_acknowledged,
                        "max_disk_bytes": record.launch.max_disk_bytes,
                        "max_vram_bytes": record.launch.max_vram_bytes,
                        "vram_pool_bytes": record.launch.vram_pool_bytes,
                        "max_bandwidth_mbps": record.launch.max_bandwidth_mbps,
                        "current_bandwidth_mbps": self._last_bandwidth_mbps,
                        "max_power_watts": record.launch.max_power_watts,
                        "current_power_watts": record.last_power_watts,
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

    def replace_launch(self, launch: WorkerLaunch, *, start: Optional[bool] = None) -> bool:
        """Replace one paused worker assignment without overriding an explicit pause."""
        record = self._record(launch.worker_id)
        with self._lock:
            if self._closed:
                raise RuntimeError("worker supervisor is closed")
            self._refresh_locked(record)
            if (
                record.desired_running
                or record.process is not None
                or record.suspension_stop_thread is not None
                or record.state in (WorkerState.STARTING, WorkerState.STOPPING)
            ):
                raise WorkerReconfigurationBusyError(
                    f"pause contribution worker {record.launch.worker_id!r} before replacing its placement"
                )
            changed = record.launch != launch
            record.launch = launch
            record.schedule_suspended = False
            record.resource_suspended = False
            record.last_power_watts = None
            requested_start = launch.auto_start if start is None else start
            # This decision is made while holding the same lock as pause_worker().
            # An operator pause that lands after a reconciler snapshot therefore
            # remains authoritative over stale automatic-start intent.
            should_start = requested_start and not record.operator_paused
            record.desired_running = should_start and launch.policy_admitted
            if self._started and record.desired_running:
                self._spawn_locked(
                    record,
                    defer_outside_schedule=True,
                    defer_unavailable_resources=True,
                )
            return changed

    def reconfigure(self, settings: WorkerSupervisorSettings, *, persist: Callable[[], None]) -> None:
        """Persist and apply a prevalidated policy while every worker is paused.

        The persistence callback runs while worker actions hold the same lock. If it
        fails, no in-memory field changes. Once it succeeds, the remaining assignments
        cannot perform I/O or spawn a process, so disk and active policy advance as one
        bounded transaction.
        """
        launches = {launch.worker_id.casefold(): launch for launch in settings.launches}
        with self._lock:
            if self._closed:
                raise RuntimeError("worker supervisor is closed")
            if set(launches) != set(self._records):
                raise ValueError("a policy update must preserve the configured worker set")
            for record in self._records.values():
                self._refresh_locked(record)
                if (
                    record.desired_running
                    or record.process is not None
                    or record.suspension_stop_thread is not None
                    or record.state in (WorkerState.STARTING, WorkerState.STOPPING)
                ):
                    raise WorkerReconfigurationBusyError(
                        "pause all contribution workers before changing the contribution policy"
                    )
            persist()
            for normalized, record in self._records.items():
                record.launch = launches[normalized]
                record.schedule_suspended = False
                record.resource_suspended = False
                record.last_power_watts = None
            self._stop_timeout = settings.stop_timeout
            self._schedule_allowed = settings.schedule_allowed
            self._bandwidth_mbps = settings.bandwidth_mbps
            self._power_watts = settings.power_watts
            self._last_bandwidth_mbps = None

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
                record.resource_suspended = False
            suspension_stop_threads = {
                id(record): record.suspension_stop_thread
                for record in records
                if record.suspension_stop_thread is not None
            }
        for thread in suspension_stop_threads.values():
            thread.join(timeout=(self._stop_timeout * 2) + self._poll_period)
        for record in records:
            suspension_stop_thread = suspension_stop_threads.get(id(record))
            if suspension_stop_thread is not None and suspension_stop_thread.is_alive():
                logger.error(
                    "Policy-stop thread for worker %r exceeded the shutdown timeout",
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
