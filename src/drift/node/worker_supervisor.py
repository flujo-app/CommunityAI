"""Isolated contribution-worker process supervision for the local node."""

from __future__ import annotations

import collections
import json
import logging
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Optional, Sequence, Tuple
from uuid import UUID

from drift.node.contribution_planner import MAX_AUTOMATIC_PLACEMENT_BLOCKS
from drift.node.hardware_status import MAX_VISIBLE_ACCELERATORS
from drift.node.placement_resources import WorkerResourceClaim
from drift.node.worker_loading import (
    LOADING_ENV_PREFIX,
    WORKER_LOADING_FAILED_EXIT_CODE,
    LoadingBinding,
    read_loading_status,
)
from drift.node.worker_loading_identity import resolve_loading_worker_pid
from drift.utils.resource_limits import DEVICE_MEMORY_BUDGET_EXIT_CODE

logger = logging.getLogger(__name__)

_RESOURCE_WAIT = "worker is waiting for an aggregate resource reservation"
_RESOURCE_CAPACITY = "shared host memory or cache storage is unavailable for this worker"
_RESOURCE_RELEASE_PENDING = "worker resource release is incomplete; retry cleanup"
_RESOURCE_SPAWN_UNCERTAIN = "worker process creation is uncertain; resource reservation remains held"
_LOADING_FAILED = "worker loading acknowledgement failed; choose Start to retry after cleanup"


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
    """Read NVIDIA power from explicit NVML indices or a private CUDA UUID binding."""

    def __init__(self, device_indices: Sequence[int]) -> None:
        self._device_indices = tuple(sorted(set(device_indices)))
        self._pynvml = None
        self._initialized = False
        self._lock = threading.Lock()
        self._cuda_uuid: Optional[str] = None
        self._cuda_identity: Optional[Callable[[], Optional[str]]] = None

    @classmethod
    def from_cuda_device(cls, visible_index: int) -> "NvidiaPowerMonitor":
        """Bind a visible CUDA ordinal to NVML identity, never to an NVML ordinal.

        UUIDs remain private to this monitor. Unknown or changed bindings and
        unsupported MIG power telemetry fail closed under an optional power cap.
        """
        monitor = cls(())
        if isinstance(visible_index, bool) or not isinstance(visible_index, int) or visible_index < 0:
            return monitor

        def identity() -> Optional[str]:
            try:
                import torch

                value = getattr(torch.cuda.get_device_properties(visible_index), "uuid", None)
                value = None if value is None else str(value)
                # Torch 2.6 exposes _CUuuid as a bare UUID; NVML expects GPU-.
                # MIG-prefixed or malformed identifiers are not physical GPUs.
                if value and not value.startswith("GPU-"):
                    value = "GPU-" + str(UUID(value))
                return value if value and value.startswith("GPU-") and len(value) <= 80 else None
            except Exception:
                return None

        monitor._cuda_identity = identity
        monitor._cuda_uuid = identity()
        return monitor

    def __call__(self) -> Optional[float]:
        if not self._device_indices and self._cuda_uuid is None:
            return None
        with self._lock:
            if self._cuda_identity is not None and self._cuda_identity() != self._cuda_uuid:
                return None
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
                if self._cuda_uuid is not None:
                    handles = (self._pynvml.nvmlDeviceGetHandleByUUID(self._cuda_uuid),)
                else:
                    handles = tuple(self._pynvml.nvmlDeviceGetHandleByIndex(index) for index in self._device_indices)
                return sum(self._pynvml.nvmlDeviceGetPowerUsage(handle) for handle in handles) / 1000
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
    # Coverage/demand explanations change without changing the worker assignment.
    # They must not make the placement reconciler stop a healthy worker.
    placement_reason: Optional[str] = field(default=None, compare=False)
    intent_published: bool = False
    remote_acknowledged: bool = False
    placement_manifest_digest: Optional[str] = None
    placement_artifact_bytes: Optional[int] = None
    placement_artifact_set_digest: Optional[str] = None
    placement_cache_root: Optional[str] = None
    max_disk_bytes: Optional[int] = None
    max_host_memory_bytes: Optional[int] = None
    resource_claim: Optional[WorkerResourceClaim] = field(default=None, repr=False)
    max_vram_bytes: Optional[int] = None
    vram_device: Optional[str] = None
    vram_pool_bytes: Optional[int] = None
    max_bandwidth_mbps: Optional[float] = None
    max_power_watts: Optional[float] = None
    environment: Tuple[Tuple[str, str], ...] = field(default=(), repr=False)
    device: Optional[str] = None
    # Keep the guard with the exact child mask and resource reservation when an
    # automatic placement is replaced. Fresh equivalent probes do not reassign it.
    device_available: Optional[Callable[[], bool]] = field(default=None, compare=False, repr=False)
    placement_available: Optional[Callable[[], bool]] = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not self.worker_id or not self.command:
            raise ValueError("worker id and command must not be empty")
        if self.max_host_memory_bytes is not None and (
            type(self.max_host_memory_bytes) is not int or not 0 < self.max_host_memory_bytes <= 2**63 - 1
        ):
            raise ValueError("worker max_host_memory_bytes must be a positive bounded integer")
        if self.resource_claim is not None and (
            not isinstance(self.resource_claim, WorkerResourceClaim)
            or self.resource_claim.worker_id.casefold() != self.worker_id.casefold()
            or self.max_host_memory_bytes is None
        ):
            raise ValueError("worker resource claim requires a matching worker and finite host memory ceiling")
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
        placement_claims = (
            self.placement_manifest_digest,
            self.placement_artifact_bytes,
            self.placement_artifact_set_digest,
            self.placement_cache_root,
        )
        if any(value is not None for value in placement_claims) and not all(
            value is not None for value in placement_claims
        ):
            raise ValueError("automatic placement artifact claims must be configured together")
        if not self.automatic and any(value is not None for value in placement_claims):
            raise ValueError("manual workers must not carry automatic placement artifact claims")
        if self.automatic and self.policy_admitted and not all(value is not None for value in placement_claims):
            raise ValueError("admitted automatic workers require an exact placement artifact binding")
        if self.placement_manifest_digest is not None and (
            not isinstance(self.placement_manifest_digest, str)
            or len(self.placement_manifest_digest) != 71
            or not self.placement_manifest_digest.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in self.placement_manifest_digest[7:])
        ):
            raise ValueError("placement manifest digest must be canonical sha256")
        if self.placement_artifact_bytes is not None and (
            isinstance(self.placement_artifact_bytes, bool)
            or not isinstance(self.placement_artifact_bytes, int)
            or self.placement_artifact_bytes < 0
        ):
            raise ValueError("placement artifact bytes must be a non-negative integer")
        if self.placement_artifact_set_digest is not None and (
            not isinstance(self.placement_artifact_set_digest, str)
            or len(self.placement_artifact_set_digest) != 64
            or any(character not in "0123456789abcdef" for character in self.placement_artifact_set_digest)
        ):
            raise ValueError("placement artifact-set digest must be lowercase SHA-256")
        if self.placement_cache_root is not None:
            if not isinstance(self.placement_cache_root, str) or not self.placement_cache_root:
                raise ValueError("placement cache root must be a canonical absolute path")
            canonical_cache_root = os.path.realpath(os.path.abspath(os.path.expanduser(self.placement_cache_root)))
            if self.placement_cache_root != canonical_cache_root:
                raise ValueError("placement cache root must be a canonical absolute path")

            command = self.command
            if any(not isinstance(value, str) or not value for value in command):
                raise ValueError("placement-bound worker command arguments must be non-empty strings")
            if command[0] != sys.executable or os.path.realpath(command[0]) != os.path.realpath(sys.executable):
                raise ValueError("placement-bound worker command must use the current node executable")
            forbidden_options = (
                "-c",
                "--config",
                "--custom_module_path",
                "--allow_training_rpcs",
                "--token",
                "--use_auth_token",
            )
            if any(
                value == option or value.startswith(f"{option}=") or (option == "-c" and value.startswith("-c"))
                for value in command
                for option in forbidden_options
            ):
                raise ValueError("placement-bound worker command contains a forbidden server option")
            module_entrypoint = len(command) >= 4 and command[1:4] == ("-m", "drift.cli", "server")
            frozen_entrypoint = len(command) >= 2 and command[1] == "server"
            if not module_entrypoint and not frozen_entrypoint:
                raise ValueError("placement-bound worker command must invoke the drift server entrypoint")
            if any(value == "--num_blocks" or value.startswith("--num_blocks=") for value in command):
                raise ValueError("placement-bound worker command must not use --num_blocks")

            def bound_option(option: str) -> str:
                positions = [
                    index for index, value in enumerate(command) if value == option or value.startswith(f"{option}=")
                ]
                if len(positions) != 1 or command[positions[0]] != option or positions[0] + 1 >= len(command):
                    raise ValueError(f"placement-bound worker command requires exactly one {option}")
                return command[positions[0] + 1]

            for option, expected in (
                ("--block_indices", self.block_indices),
                ("--expected_block_indices", self.block_indices),
                ("--expected_manifest_digest", self.placement_manifest_digest),
                ("--expected_artifact_bytes", str(self.placement_artifact_bytes)),
                ("--expected_artifact_set_digest", self.placement_artifact_set_digest),
                ("--cache_dir", self.placement_cache_root),
                ("--expected_cache_root", self.placement_cache_root),
            ):
                if bound_option(option) != expected:
                    raise ValueError(f"placement-bound worker command has a mismatched {option}")
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
        if self.vram_device is not None and not _canonical_device(self.vram_device, accelerator_only=True):
            raise ValueError("worker VRAM reservation requires a canonical accelerator device")
        if self.device is not None and not _canonical_device(self.device):
            raise ValueError("worker device must be canonical")
        if self.device is not None and self.vram_device is not None and self.device != self.vram_device:
            raise ValueError("worker device must match its VRAM reservation device")
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


def _canonical_device(value: Any, *, accelerator_only: bool = False) -> bool:
    return isinstance(value, str) and (
        value == "mps"
        or (value == "cpu" and not accelerator_only)
        or (
            re.fullmatch(r"(?:cuda|xpu):(?:0|[1-9][0-9]{0,5})", value) is not None
            and int(value.partition(":")[2]) < MAX_VISIBLE_ACCELERATORS
        )
    )


def _validate_vram_pools(launches: Sequence[WorkerLaunch]) -> None:
    pools: Dict[str, int] = {}
    for launch in launches:
        if launch.vram_device is not None:
            prior = pools.setdefault(launch.vram_device, launch.vram_pool_bytes)
            if prior != launch.vram_pool_bytes:
                raise ValueError("workers on the same device must agree on its VRAM pool")


@dataclass(frozen=True)
class WorkerSupervisorSettings:
    """A completely validated, atomically swappable worker-policy configuration."""

    launches: Tuple[WorkerLaunch, ...]
    stop_timeout: float
    schedule_allowed: Optional[Callable[[], bool]] = None
    bandwidth_mbps: Optional[Callable[[], Optional[float]]] = None
    power_watts: Optional[Callable[[str], Optional[float]]] = None
    device_available: Optional[Callable[[str], bool]] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.launches, tuple):
            raise ValueError("worker launches must be a tuple")
        normalized_ids = [launch.worker_id.casefold() for launch in self.launches]
        if len(set(normalized_ids)) != len(normalized_ids):
            raise ValueError("worker ids must be unique case-insensitively")
        _validate_vram_pools(self.launches)
        if self.stop_timeout <= 0:
            raise ValueError("worker stop timeout must be positive")


@dataclass(frozen=True)
class _PreparedSpawn:
    launch: WorkerLaunch
    token: str
    environment: dict
    options: dict
    containment: Any
    spawn: Callable
    loading_binding: Optional[LoadingBinding]
    loading_values: tuple


@dataclass
class _WorkerRecord:
    progress_directory: Any = field(default=None, init=False, repr=False)
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
    memory_rejected_command: Optional[Tuple[str, ...]] = None
    last_power_watts: Optional[float] = None
    suspension_stop_thread: Optional[threading.Thread] = field(default=None, repr=False)
    cleanup_pending: bool = False
    natural_exit_code: Optional[int] = None
    start_after_cleanup: bool = False
    start_after_admission: bool = False
    resource_token: Optional[str] = field(default=None, repr=False)
    resource_release_pending: bool = False
    resource_spawn_uncertain: bool = False
    resource_acquire_failed: bool = False
    resource_acquire_reason: Optional[str] = None
    resource_operation_active: bool = False
    resource_operation: Optional[tuple] = field(default=None, repr=False)
    resource_thread: Optional[threading.Thread] = field(default=None, repr=False)
    loading_ticket: Optional[tuple] = field(default=None, repr=False)
    loading_thread: Optional[threading.Thread] = field(default=None, repr=False)
    load_state: Optional[str] = None
    loading_failed: bool = False
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
        device_available: Optional[Callable[[str], bool]] = None,
        coordinated_launches: bool = False,
        acquire_resources: Optional[Callable[[WorkerLaunch], str]] = None,
        acquire_resources_cancellable: Optional[Callable[[WorkerLaunch, threading.Event], str]] = None,
        release_resources: Optional[Callable[[str], None]] = None,
        loading_binding_for_token: Optional[Callable[[str], LoadingBinding]] = None,
        recovery_containment_for_token: Optional[Callable[[str], Any]] = None,
    ) -> None:
        if stop_timeout <= 0 or poll_period <= 0:
            raise ValueError("worker supervisor timeouts must be positive")
        if type(coordinated_launches) is not bool:
            raise ValueError("coordinated_launches must be a boolean")
        if any(
            hook is not None and not callable(hook)
            for hook in (
                acquire_resources,
                acquire_resources_cancellable,
                release_resources,
                loading_binding_for_token,
                recovery_containment_for_token,
            )
        ):
            raise ValueError("worker resource hooks must be callable or None")
        launches = tuple(launches)
        if coordinated_launches:
            self._validate_joint_launches(launches)
        else:
            _validate_vram_pools(launches)
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
        self._device_available = device_available
        self._acquire_resources = acquire_resources
        self._acquire_resources_cancellable = acquire_resources_cancellable
        self._release_resources = release_resources
        self._loading_binding_for_token = loading_binding_for_token
        self._recovery_containment_for_token = recovery_containment_for_token
        self._last_bandwidth_mbps: Optional[float] = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._monitor: Optional[threading.Thread] = None
        self._started = False
        self._closed = False
        self._configuration_restart_pending = False
        self._sharing_disabled = False
        self._coordinated_launches = coordinated_launches
        self._process_containments: Dict[int, Any] = {}
        self._process_cleanup_lock = threading.Lock()
        self._process_cleanup_locks = weakref.WeakKeyDictionary()
        self._verified_stops = weakref.WeakSet()
        self._launch_transition_ids: frozenset[str] = frozenset()
        self._launch_transition_start_requests: Dict[str, bool] = {}
        self._launch_transition_preserve_start_intent = False
        self._launch_transition_active = False
        self._launch_transition_state = "idle"
        self._launch_transition_done = threading.Event()
        self._launch_transition_done.set()

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

    def _resource_status_locked(
        self, record: _WorkerRecord, *, ignore_operation: bool = False
    ) -> Tuple[bool, Optional[str]]:
        if record.resource_spawn_uncertain:
            return False, _RESOURCE_SPAWN_UNCERTAIN
        if record.resource_release_pending:
            return False, _RESOURCE_RELEASE_PENDING
        if record.cleanup_pending:
            return False, "worker process cleanup is incomplete"
        if record.loading_failed:
            return False, _LOADING_FAILED
        launch = record.launch
        if launch.resource_claim is not None:
            if (
                self._acquire_resources is None and self._acquire_resources_cancellable is None
            ) or self._release_resources is None:
                return False, _RESOURCE_WAIT
            if record.resource_operation is not None and not ignore_operation:
                return False, _RESOURCE_WAIT
            if record.resource_acquire_failed:
                return False, record.resource_acquire_reason or _RESOURCE_WAIT
        device_check = launch.device_available
        if device_check is None and self._device_available is not None:
            device_check = lambda: self._device_available(launch.worker_id)
        if device_check is not None:
            try:
                device_available = device_check()
            except Exception:
                # Device probes may include private hardware identifiers in errors.
                device_available = False
            if device_available is not True:
                return False, "selected device is unavailable or has changed; reselect it before sharing"
        if launch.placement_available is not None:
            try:
                placement_available = launch.placement_available()
            except Exception:
                # Signed-placement checks can contain private keys or paths in
                # failures. A fixed operational reason is sufficient to retry.
                placement_available = False
            if placement_available is not True:
                return False, "automatic placement is waiting for a live signed intent"
        if record.memory_rejected_command == launch.command:
            return False, "selected blocks exceed the VRAM budget; increase VRAM or contribute fewer blocks"
        if launch.max_vram_bytes is not None:
            reserved = sum(
                other.launch.max_vram_bytes
                for other in self._records.values()
                if other is not record
                and other.launch.vram_device == launch.vram_device
                and (
                    other.cleanup_pending
                    or other.suspension_stop_thread is not None
                    or (other.process is not None and (self._coordinated_launches or other.process.poll() is None))
                )
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

    def _contained_launch(self, record: _WorkerRecord) -> bool:
        return (
            self._coordinated_launches or record.launch.resource_claim is not None or record.resource_token is not None
        )

    def _invalidate_loading_locked(self, record: _WorkerRecord) -> None:
        if record.loading_ticket is not None:
            record.loading_ticket[4].set()
            record.loading_ticket = None
        record.load_state = "failed" if record.loading_failed else None

    def _loading_ticket_current_locked(self, record: _WorkerRecord, ticket: tuple) -> bool:
        launch, process, token, binding, cancel = ticket
        return (
            self._records.get(launch.worker_id.casefold()) is record
            and record.loading_ticket is ticket
            and record.launch is launch
            and record.process is process
            and record.resource_token == token
            and not cancel.is_set()
            and record.desired_running
            and not record.operator_paused
            and not self._closed
            and not self._sharing_disabled
            and not record.loading_failed
            and record.state is WorkerState.RUNNING
            and process.poll() is None
        )

    def _fail_loading_locked(self, record: _WorkerRecord) -> None:
        record.loading_failed = True
        self._invalidate_loading_locked(record)
        record.desired_running = False
        record.start_after_cleanup = False
        record.start_after_admission = False
        record.last_error = _LOADING_FAILED
        if record.process is not None:
            self._suspend_locked(record)
        else:
            record.state = WorkerState.CRASHED

    def _start_loading_observer_locked(self, record: _WorkerRecord, binding: LoadingBinding) -> None:
        self._invalidate_loading_locked(record)
        record.load_state = "waiting"
        record.loading_ticket = (record.launch, record.process, record.resource_token, binding, threading.Event())
        if record.loading_thread is None:
            try:
                record.loading_thread = threading.Thread(
                    target=self._observe_loading,
                    args=(record,),
                    name=f"drift-worker-loading-{record.launch.worker_id}",
                    daemon=True,
                )
                record.loading_thread.start()
            except Exception:
                record.loading_thread = None
                self._fail_loading_locked(record)

    def _observe_loading(self, record: _WorkerRecord) -> None:
        # One runner follows the latest ticket. A blocked stale read can delay
        # fresh observation, but never create an accumulating thread/queue.
        observed_ticket = identity = None
        while True:
            with self._lock:
                ticket = record.loading_ticket
                if ticket is None:
                    record.loading_thread = None
                    return
                if ticket is not observed_ticket:
                    observed_ticket, identity = ticket, None
                if not self._loading_ticket_current_locked(record, ticket):
                    self._invalidate_loading_locked(record)
                    continue
            launch, process, token, binding, cancel = ticket
            status = None
            failed = False
            try:
                identity = resolve_loading_worker_pid(process, launch.command, expected_identity=identity)
                if identity is not None:
                    status = read_loading_status(binding, expected_pid=identity.pid)
                    confirmed = resolve_loading_worker_pid(process, launch.command, expected_identity=identity)
                    if confirmed is None or confirmed != identity:
                        raise ValueError("loading process identity changed")
                if status not in (None, "waiting", "loading", "ready", "failed", "memory_rejected"):
                    raise ValueError("invalid loading status")
            except Exception:
                failed = True
            with self._lock:
                if self._loading_ticket_current_locked(record, ticket):
                    if status == "memory_rejected" and not failed:
                        record.memory_rejected_command = record.launch.command
                        record.last_error = self._resource_status_locked(record)[1]
                        self._suspend_locked(record, resource=True)
                    elif failed or status == "failed":
                        self._fail_loading_locked(record)
                    elif status is not None:
                        rank = {"waiting": 0, "loading": 1, "ready": 2}
                        if rank[status] < rank.get(record.load_state, 0):
                            self._fail_loading_locked(record)
                        else:
                            record.load_state = status
                    elif record.load_state == "ready":
                        # Missing current evidence cannot remain ready.
                        self._fail_loading_locked(record)
            cancel.wait(self._poll_period)

    def _release_resources_locked(self, record: _WorkerRecord) -> bool:
        """Release only after the owner established that no child remains.

        A Popen call which did not return a handle is not no-child evidence.
        Release callbacks must be idempotent: an uncertain journal write can
        cause the same private generation token to be retried.
        """
        if record.process is not None or record.resource_spawn_uncertain:
            return False
        if record.resource_token is None:
            return True
        if record.resource_operation_active:
            return False
        if self._acquire_resources_cancellable is not None:
            record.resource_release_pending = True
            record.cleanup_pending = True
            record.last_error = _RESOURCE_RELEASE_PENDING
            self._queue_resource_operation_locked(record, ("release", record.resource_token, None))
            return False
        record.resource_operation_active = True
        try:
            self._release_resources(record.resource_token)
        except Exception:
            record.resource_release_pending = True
            record.cleanup_pending = True
            record.last_error = _RESOURCE_RELEASE_PENDING
            record.state = WorkerState.CRASHED
            return False
        finally:
            record.resource_operation_active = False
        record.resource_token = None
        record.resource_release_pending = False
        record.cleanup_pending = False
        return True

    @staticmethod
    def _acquire_error_reason(error: Exception) -> str:
        from drift.node.resource_reservations import ResourceReservationError

        return (
            _RESOURCE_CAPACITY
            if isinstance(error, ResourceReservationError) and error.category == "capacity"
            else _RESOURCE_WAIT
        )

    def _queue_resource_operation_locked(self, record: _WorkerRecord, operation: tuple) -> None:
        # One callback runner per record. A completion can enqueue its release
        # or a newer Start onto this same runner, never an unbounded queue.
        if record.resource_operation is not None:
            raise WorkerReconfigurationBusyError("worker resource operation is in progress")
        record.resource_operation = operation
        record.resource_operation_active = True
        if record.resource_thread is None:
            record.resource_thread = threading.Thread(
                target=self._run_resource_operations,
                args=(record,),
                name=f"drift-worker-resource-{record.launch.worker_id}",
                daemon=True,
            )
            try:
                record.resource_thread.start()
            except Exception:
                # Thread.start failed before the callback ran. Keep any token
                # awaiting release, but do not leave a nonexistent runner busy.
                record.resource_thread = None
                record.resource_operation = None
                record.resource_operation_active = False
                record.state = WorkerState.CRASHED
                if operation[0] == "acquire":
                    record.resource_acquire_failed = True
                    record.resource_acquire_reason = _RESOURCE_WAIT
                    record.last_error = _RESOURCE_WAIT
                else:
                    record.resource_release_pending = record.cleanup_pending = True
                    record.last_error = _RESOURCE_RELEASE_PENDING
                record.next_restart_at = time.monotonic() + record.launch.restart_backoff

    def _cancel_resource_acquisition_locked(self, record: _WorkerRecord) -> bool:
        operation = record.resource_operation
        if operation is not None and operation[0] in ("acquire", "spawn"):
            operation[2].set()
            record.cleanup_pending = True
            record.state = WorkerState.STOPPING
            return True
        return False

    def _run_resource_operations(self, record: _WorkerRecord) -> None:
        while True:
            with self._lock:
                operation = record.resource_operation
                if operation is None:
                    record.resource_thread = None
                    return
            kind, value, cancel = operation
            if kind == "spawn":
                self._run_spawn_operation(record, operation)
                continue
            token = error = None
            try:
                if kind == "acquire":
                    token = self._acquire_resources_cancellable(value, cancel)
                else:
                    self._release_resources(value)
            except Exception as exc:
                error = exc
            with self._lock:
                # The exact ticket, not launch equality, owns this completion.
                if record.resource_operation is not operation:
                    raise RuntimeError("worker resource operation ownership changed")
                record.resource_operation = None
                record.resource_operation_active = False
                if kind == "release":
                    if error is not None:
                        record.resource_release_pending = True
                        record.cleanup_pending = True
                        record.state = WorkerState.CRASHED
                        record.last_error = _RESOURCE_RELEASE_PENDING
                        record.next_restart_at = time.monotonic() + record.launch.restart_backoff
                        continue
                    record.resource_token = None
                    record.resource_release_pending = False
                    record.cleanup_pending = False
                    record.state = WorkerState.PAUSED
                    record.last_error = None
                    if record.natural_exit_code is not None:
                        record.schedule_suspended = record.resource_suspended = False
                        if record.natural_exit_code == DEVICE_MEMORY_BUDGET_EXIT_CODE:
                            record.resource_suspended = record.desired_running
                            record.last_error = self._resource_status_locked(record)[1]
                        elif record.desired_running:
                            record.state = WorkerState.CRASHED
                            record.last_error = f"worker exited with code {record.natural_exit_code}"
                            record.next_restart_at = time.monotonic() + record.launch.restart_backoff
                        record.natural_exit_code = None
                    self._resume_after_resource_cleanup_locked(record)
                    continue
                record.cleanup_pending = False
                if error is not None:
                    record.state = WorkerState.PAUSED if cancel.is_set() else WorkerState.CRASHED
                    record.resource_acquire_failed = not cancel.is_set()
                    record.resource_acquire_reason = None if cancel.is_set() else self._acquire_error_reason(error)
                    record.last_error = record.resource_acquire_reason
                    record.next_restart_at = time.monotonic() + record.launch.restart_backoff
                    if cancel.is_set():
                        self._resume_after_resource_cleanup_locked(record)
                    continue
                if not isinstance(token, str) or not 0 < len(token) <= 128:
                    record.resource_spawn_uncertain = record.cleanup_pending = True
                    record.state = WorkerState.CRASHED
                    record.last_error = _RESOURCE_SPAWN_UNCERTAIN
                    continue
                record.resource_token = token
                record.state = WorkerState.PAUSED
                current = self._records.get(value.worker_id.casefold())
                try:
                    if (
                        current is record
                        and record.launch is value
                        and not cancel.is_set()
                        and record.desired_running
                        and not record.operator_paused
                        and not self._closed
                        and not self._configuration_restart_pending
                        and not self._launch_transition_ids
                    ):
                        self._spawn_locked(
                            record,
                            defer_outside_schedule=True,
                            defer_unavailable_resources=True,
                            resources_acquired=True,
                        )
                        if record.process is not None:
                            record.start_after_cleanup = False
                except Exception:
                    record.state = WorkerState.CRASHED
                    record.last_error = _RESOURCE_WAIT
                finally:
                    if record.process is None and not record.resource_spawn_uncertain:
                        self._release_resources_locked(record)

    def _resume_after_resource_cleanup_locked(self, record: _WorkerRecord) -> None:
        if (
            record.start_after_cleanup
            and record.desired_running
            and not record.operator_paused
            and not self._closed
            and not self._sharing_disabled
            and not self._launch_transition_ids
            and not self._configuration_restart_pending
        ):
            record.start_after_cleanup = False
            self._spawn_locked(record, defer_outside_schedule=True, defer_unavailable_resources=True)

    def _spawn_ticket_current_locked(self, record: _WorkerRecord, operation: tuple) -> bool:
        _, prepared, cancel = operation
        if not (
            record.resource_operation is operation
            and self._records.get(prepared.launch.worker_id.casefold()) is record
            and record.launch is prepared.launch
            and record.resource_token == prepared.token
            and not cancel.is_set()
            and record.desired_running
            and not record.operator_paused
            and not self._closed
            and not self._sharing_disabled
            and not self._configuration_restart_pending
            and not self._launch_transition_ids
        ):
            return False
        schedule, _ = self._schedule_status()
        resources, _ = self._resource_status_locked(record, ignore_operation=True)
        record.schedule_suspended = not schedule
        record.resource_suspended = not resources
        return schedule and resources

    def _run_spawn_operation(self, record: _WorkerRecord, operation: tuple) -> None:
        """The reservation runner owns birth, handshakes and uncertain cleanup.

        The process remains private to this operation until exec is acknowledged.
        Pause can invalidate the ticket without contending on cgroup or pipe I/O.
        Only the final gate write is serialized with Pause; Linux exec observation
        and identity validation run outside the control lock.
        """
        _, prepared, cancel = operation
        process = None
        attempted = False
        error = None
        cleaned = True
        try:
            with self._lock:
                current = self._spawn_ticket_current_locked(record, operation)
            if current:
                attempted = True
                process = prepared.spawn(
                    list(prepared.launch.command),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=prepared.environment,
                    **prepared.options,
                )
                if prepared.containment is not None:
                    with self._lock:
                        self._process_containments[id(process)] = prepared.containment
                    prepared.containment.attach(process)
                    split_resume = callable(getattr(prepared.containment, "release_gate", None))
                    with self._lock:
                        current = self._spawn_ticket_current_locked(record, operation)
                        if current:
                            if split_resume:
                                prepared.containment.release_gate(process)
                            else:
                                prepared.containment.resume(process)
                    if current and split_resume:
                        prepared.containment.await_exec(process, cancel=cancel)
                with self._lock:
                    if current and self._spawn_ticket_current_locked(record, operation):
                        record.resource_operation = None
                        record.resource_operation_active = False
                        record.cleanup_pending = False
                        record.start_after_cleanup = False
                        try:
                            self._publish_spawn_locked(
                                record, process, prepared.environment, prepared.loading_binding, prepared.loading_values
                            )
                        except Exception:
                            # Publication (for example log-thread startup) may
                            # fail after storing the process. Restore this sole
                            # owner before exposing state to Pause or shutdown.
                            record.resource_operation = operation
                            record.resource_operation_active = True
                            record.process = None
                            record.cleanup_pending = True
                            raise
                        return
        except Exception as exc:
            error = exc
        # A cancelled or failed generation cannot publish readiness. Keep its
        # exact containment and token until this same runner proves its death.
        if process is not None:
            try:
                process.kill()
                self._terminate(process)
                if process.stdout is not None:
                    process.stdout.close()
            except Exception:
                cleaned = False
        elif prepared.containment is not None:
            try:
                prepared.containment.close()
            except Exception:
                pass
        with self._lock:
            if record.resource_operation is not operation:
                raise RuntimeError("worker spawn operation ownership changed")
            record.resource_operation = None
            record.resource_operation_active = False
            record.process = None if cleaned else process
            record.resource_spawn_uncertain = attempted and process is None
            record.cleanup_pending = not cleaned or record.resource_spawn_uncertain
            record.state = WorkerState.PAUSED if cancel.is_set() else WorkerState.CRASHED
            record.last_error = (
                _RESOURCE_SPAWN_UNCERTAIN
                if record.resource_spawn_uncertain
                else "worker process cleanup is incomplete"
                if not cleaned
                else "worker process containment could not be established"
                if error is not None
                else None
            )
            record.next_restart_at = time.monotonic() + record.launch.restart_backoff
            if record.process is None and not record.resource_spawn_uncertain:
                self._release_resources_locked(record)

    def _spawn_locked(
        self,
        record: _WorkerRecord,
        *,
        defer_outside_schedule: bool = False,
        defer_unavailable_resources: bool = False,
        resources_acquired: bool = False,
    ) -> bool:
        # The monitor must remain alive while a batch is quiescing or waiting for
        # a cleanup retry. Public Start checks this latch before changing intent.
        if self._launch_transition_ids:
            record.resource_suspended = record.desired_running
            return False
        if record.resource_operation_active or (record.state is WorkerState.STARTING and record.process is None):
            return False
        if self._contained_launch(record):
            self._refresh_locked(record)
        if record.resource_release_pending and record.process is None and record.suspension_stop_thread is None:
            self._release_resources_locked(record)
        if record.cleanup_pending:
            return False
        if self._configuration_restart_pending:
            raise WorkerReconfigurationBusyError("node configuration restart is pending")
        if self._sharing_disabled:
            raise WorkerPolicyError("sharing is disabled by contribution policy")
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
        record.resource_acquire_failed = False
        record.resource_acquire_reason = None
        resource_admitted, resource_reason = self._resource_status_locked(record)
        if not resource_admitted:
            if record.process is not None and record.process.poll() is None:
                # Start may be requested again after a live device disappeared.
                # Keep the process tracked until the ordinary stop path finishes.
                self._suspend_locked(record, resource=True)
            elif record.suspension_stop_thread is None:
                record.state = WorkerState.PAUSED
            record.resource_suspended = defer_unavailable_resources and record.desired_running
            if defer_unavailable_resources:
                return False
            record.desired_running = False
            raise WorkerPolicyError(resource_reason)
        if self._closed:
            raise RuntimeError("worker supervisor is closed")
        if record.suspension_stop_thread is not None:
            # A new Start intent must wait for the previous process cleanup, even
            # when its device becomes available before the stop thread completes.
            record.resource_suspended = record.desired_running
            return False
        if record.process is not None and record.process.poll() is None:
            return False
        if (
            record.launch.resource_claim is not None
            and self._acquire_resources_cancellable is not None
            and not resources_acquired
        ):
            record.state = WorkerState.STARTING
            record.start_after_cleanup = False
            self._queue_resource_operation_locked(record, ("acquire", record.launch, threading.Event()))
            return False
        record.state = WorkerState.STARTING
        self._invalidate_loading_locked(record)
        environment = os.environ.copy()
        environment.update(record.launch.environment)
        for key in tuple(environment):
            if key.upper().startswith(LOADING_ENV_PREFIX):
                del environment[key]
        environment["PYTHONUNBUFFERED"] = "1"
        environment.pop("DRIFT_DOWNLOAD_PROGRESS", None)
        try:
            if record.progress_directory is not None:
                record.progress_directory.cleanup()
            record.progress_directory = tempfile.TemporaryDirectory(prefix="communityai-download-")
            environment["DRIFT_DOWNLOAD_PROGRESS"] = str(Path(record.progress_directory.name) / "progress.json")
        except OSError:
            record.progress_directory = None
            logger.warning("Local download progress is unavailable for worker %s", record.launch.worker_id)
        containment = None
        process = None
        spawn_attempted = False
        loading_binding = None
        loading_values = ()
        recovery_mode = self._recovery_containment_for_token is not None and record.launch.resource_claim is not None
        try:
            spawn_options = {"creationflags": self._creation_flags()}
            if self._contained_launch(record) and not recovery_mode:
                from drift.node.edge_supervisor import _new_containment

                containment = _new_containment()
                options = containment.popen_kwargs()
                spawn_options.update(options)
                spawn_options["creationflags"] |= self._creation_flags()
            elif sys.platform.startswith("linux"):
                spawn_options["start_new_session"] = True
            if record.launch.resource_claim is not None and not resources_acquired:
                record.resource_operation_active = True
                try:
                    token = self._acquire_resources(record.launch)
                except Exception as exc:
                    # The manager owns atomic journal failure/quarantine. A
                    # missing token never authorizes rollback of an unknown
                    # durable reservation.
                    from drift.node.resource_reservations import ResourceReservationError

                    reason = (
                        _RESOURCE_CAPACITY
                        if isinstance(exc, ResourceReservationError) and getattr(exc, "category", None) == "capacity"
                        else _RESOURCE_WAIT
                    )
                    record.resource_acquire_failed = True
                    record.resource_acquire_reason = reason
                    record.resource_suspended = False
                    record.state = WorkerState.CRASHED
                    record.last_error = reason
                    record.next_restart_at = time.monotonic() + record.launch.restart_backoff
                    if containment is not None:
                        containment.close()
                    return False
                finally:
                    record.resource_operation_active = False
                if not isinstance(token, str) or not 0 < len(token) <= 128:
                    record.resource_spawn_uncertain = True
                    record.cleanup_pending = True
                    record.state = WorkerState.CRASHED
                    record.last_error = _RESOURCE_SPAWN_UNCERTAIN
                    if containment is not None:
                        containment.close()
                    return False
                record.resource_token = token
                # Acquiring a durable reservation can take time. Recheck live
                # gates and any reentrant Pause/close before invoking Popen.
                schedule_admitted, schedule_reason = self._schedule_status()
                resource_admitted, resource_reason = self._resource_status_locked(record)
                if (
                    self._closed
                    or self._configuration_restart_pending
                    or self._launch_transition_ids
                    or record.operator_paused
                    or not record.desired_running
                    or not schedule_admitted
                    or not resource_admitted
                ):
                    if containment is not None:
                        containment.close()
                    if self._release_resources_locked(record):
                        record.state = WorkerState.PAUSED
                        record.last_error = resource_reason or schedule_reason
                        record.schedule_suspended = not schedule_admitted and record.desired_running
                        record.resource_suspended = not resource_admitted and record.desired_running
                    return False
            if resources_acquired:
                schedule_admitted, schedule_reason = self._schedule_status()
                resource_admitted, resource_reason = self._resource_status_locked(record)
                if (
                    not schedule_admitted
                    or not resource_admitted
                    or self._closed
                    or self._sharing_disabled
                    or record.operator_paused
                    or not record.desired_running
                ):
                    if containment is not None:
                        containment.close()
                    self._release_resources_locked(record)
                    record.schedule_suspended = not schedule_admitted and record.desired_running
                    record.resource_suspended = not resource_admitted and record.desired_running
                    return False
            if recovery_mode:
                # The manager created this generation's containment before its
                # durable admission. This lookup is in-memory only. Never make
                # an unnamed substitute or fall back after a missing binding.
                containment = self._recovery_containment_for_token(record.resource_token)
                from drift.node.worker_recovery_containment import WindowsRecoveryContainment

                if containment is None or (
                    sys.platform == "win32" and not isinstance(containment, WindowsRecoveryContainment)
                ):
                    raise ValueError("worker recovery containment is unavailable")
                options = containment.popen_kwargs()
                spawn_options.update(options)
                spawn_options["creationflags"] |= self._creation_flags()
            if self._loading_binding_for_token is not None and record.resource_token is not None:
                try:
                    loading_binding = self._loading_binding_for_token(record.resource_token)
                    if not isinstance(loading_binding, LoadingBinding):
                        raise ValueError("missing loading binding")
                    loading_environment = loading_binding.environment()
                    environment.update(loading_environment)
                    loading_values = tuple(loading_environment.values())
                except Exception:
                    self._fail_loading_locked(record)
                    raise ValueError(_LOADING_FAILED) from None
            spawn = getattr(containment, "spawn", self._popen) if recovery_mode else self._popen
            if resources_acquired and self._acquire_resources_cancellable is not None:
                self._queue_resource_operation_locked(
                    record,
                    (
                        "spawn",
                        _PreparedSpawn(
                            record.launch,
                            record.resource_token,
                            environment,
                            spawn_options,
                            containment,
                            spawn,
                            loading_binding,
                            loading_values,
                        ),
                        threading.Event(),
                    ),
                )
                return False
            spawn_attempted = True
            process = spawn(
                list(record.launch.command),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=environment,
                **spawn_options,
            )
            if containment is not None:
                self._process_containments[id(process)] = containment
                record.process = process
                record.cleanup_pending = True
                containment.attach(process)
                containment.resume(process)
                record.cleanup_pending = False
        except Exception as exc:
            if process is not None:
                # A failed Windows attach leaves a suspended direct child. It
                # must be reaped as well as any successfully attached members.
                try:
                    process.kill()
                    self._terminate_launch_tree(process)
                except Exception:
                    record.process = process
                    record.cleanup_pending = True
                else:
                    record.process = None
                    record.cleanup_pending = False
            elif containment is not None:
                try:
                    containment.close()
                except Exception:
                    pass
            if process is None:
                record.process = None
                if spawn_attempted and record.resource_token is not None:
                    record.resource_spawn_uncertain = True
                    record.cleanup_pending = True
            if record.resource_token is not None and record.process is None and not record.resource_spawn_uncertain:
                self._release_resources_locked(record)
            record.state = WorkerState.CRASHED
            record.last_error = (
                _RESOURCE_SPAWN_UNCERTAIN
                if record.resource_spawn_uncertain
                else (
                    _RESOURCE_RELEASE_PENDING
                    if record.resource_release_pending
                    else (
                        _LOADING_FAILED
                        if record.loading_failed
                        else (
                            "worker process containment could not be established"
                            if self._contained_launch(record)
                            else f"{type(exc).__name__}: {exc}"
                        )
                    )
                )
            )
            record.next_restart_at = time.monotonic() + record.launch.restart_backoff
            return False

        return self._publish_spawn_locked(record, process, environment, loading_binding, loading_values)

    def _publish_spawn_locked(self, record, process, environment, loading_binding, loading_values) -> bool:
        if record.started_at is not None:
            record.restart_count += 1
        record.process = process
        record.natural_exit_code = None
        record.state = WorkerState.RUNNING
        record.schedule_suspended = False
        record.resource_suspended = False
        record.last_error = None
        record.last_exit_code = None
        record.started_at = time.time()
        private_device_ids = re.findall(
            r"(?:GPU-)?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            environment.get("CUDA_VISIBLE_DEVICES", ""),
            flags=re.IGNORECASE,
        )
        private_device_pattern = (
            re.compile(r"(?:GPU-)?(?:" + "|".join(map(re.escape, private_device_ids)) + r")", re.IGNORECASE)
            if private_device_ids
            else None
        )
        try:
            thread = threading.Thread(
                target=self._drain_output,
                args=(record, process, private_device_pattern, loading_values),
                name=f"drift-worker-log-{record.launch.worker_id}",
                daemon=True,
            )
            thread.start()
        except Exception:
            if loading_binding is None:
                raise
            self._fail_loading_locked(record)
            return False
        if loading_binding is not None:
            self._start_loading_observer_locked(record, loading_binding)
        return True

    def _drain_output(
        self,
        record: _WorkerRecord,
        process: subprocess.Popen,
        private_device_pattern: Optional[re.Pattern[str]] = None,
        private_loading_values: Tuple[str, ...] = (),
    ) -> None:
        stream = process.stdout
        if stream is None:
            return
        try:
            for line in stream:
                message = line.rstrip("\r\n")
                if private_device_pattern is not None:
                    # Capture this launch's binding rather than a future assignment.
                    message = private_device_pattern.sub("[private device]", message)
                for private in private_loading_values:
                    message = message.replace(private, "[private loading binding]")
                with self._lock:
                    record.recent_logs.append(message)
                logger.info("worker[%s] %s", record.launch.worker_id, message)
        except Exception:
            logger.exception("Failed to read logs for worker %r", record.launch.worker_id)
        finally:
            stream.close()

    def _refresh_locked(self, record: _WorkerRecord) -> None:
        process = record.process
        if process is None or record.state is WorkerState.STOPPING or record.cleanup_pending:
            return
        exit_code = process.poll()
        if exit_code is None:
            return
        if (
            exit_code == WORKER_LOADING_FAILED_EXIT_CODE
            and self._loading_binding_for_token is not None
            and record.resource_token is not None
        ):
            # The child may fail and exit before the observer reads its status.
            # Only an explicit Start can request another admitted generation.
            record.loading_failed = True
            record.desired_running = False
            record.start_after_cleanup = False
            record.start_after_admission = False
            record.last_error = _LOADING_FAILED
        self._invalidate_loading_locked(record)
        if self._contained_launch(record) and id(process) in self._process_containments:
            # Parent exit does not prove its job/group has released descendants.
            # Preserve the reservation until the asynchronous stop verifies it.
            record.cleanup_pending = True
            record.natural_exit_code = exit_code
            if exit_code == DEVICE_MEMORY_BUDGET_EXIT_CODE:
                record.memory_rejected_command = record.launch.command
            self._suspend_locked(record)
            return
        self._kill_linux_worker_group(process)
        record.process = None
        record.last_exit_code = exit_code
        if exit_code == DEVICE_MEMORY_BUDGET_EXIT_CODE:
            record.memory_rejected_command = record.launch.command
            record.state = WorkerState.PAUSED
            record.resource_suspended = record.desired_running
            record.last_error = self._resource_status_locked(record)[1]
        elif record.desired_running:
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
                record.last_error = (
                    "worker process cleanup is incomplete"
                    if self._contained_launch(record)
                    else f"{type(error).__name__}: {error}"
                )
            else:
                if record.process is process:
                    record.process = None
                record.last_exit_code = exit_code
                record.last_error = None
                record.state = WorkerState.PAUSED
                record.cleanup_pending = False
                if not self._release_resources_locked(record):
                    error = RuntimeError(_RESOURCE_RELEASE_PENDING)
            if error is None:
                explicit_start = record.start_after_cleanup and record.desired_running and not record.operator_paused
                record.start_after_cleanup = False
                if record.natural_exit_code is not None:
                    record.schedule_suspended = False
                    record.resource_suspended = False
                    if record.natural_exit_code == DEVICE_MEMORY_BUDGET_EXIT_CODE:
                        record.resource_suspended = record.desired_running
                        record.last_error = self._resource_status_locked(record)[1]
                    elif record.desired_running:
                        record.state = WorkerState.CRASHED
                        record.last_error = f"worker exited with code {record.natural_exit_code}"
                        record.next_restart_at = time.monotonic() + record.launch.restart_backoff
                    record.natural_exit_code = None
                if explicit_start and not self._closed:
                    if record.suspension_stop_thread is threading.current_thread():
                        record.suspension_stop_thread = None
                    self._spawn_locked(record, defer_outside_schedule=True, defer_unavailable_resources=True)
            if record.suspension_stop_thread is threading.current_thread():
                record.suspension_stop_thread = None

        if error is not None and record.resource_operation is None:
            if self._contained_launch(record):
                logger.error("Worker %r process cleanup is incomplete", record.launch.worker_id)
            else:
                logger.error(
                    "Failed to suspend worker %r for its contribution policy",
                    record.launch.worker_id,
                    exc_info=(type(error), error, error.__traceback__),
                )

    def _suspend_locked(self, record: _WorkerRecord, *, schedule: bool = False, resource: bool = False) -> None:
        self._invalidate_loading_locked(record)
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
        if self._contained_launch(record):
            record.cleanup_pending = True
        thread = None
        try:
            thread = threading.Thread(
                target=self._finish_suspension,
                args=(record, process),
                name=f"drift-worker-policy-stop-{record.launch.worker_id}",
                daemon=True,
            )
            record.suspension_stop_thread = thread
            thread.start()
        except Exception:
            if record.suspension_stop_thread is thread and (
                thread is None or (thread.ident is None and not thread.is_alive())
            ):
                # Never leave a never-started runner for Pause/shutdown to join.
                # A runner which did start remains the sole cleanup owner.
                record.suspension_stop_thread = None
            record.cleanup_pending = True
            record.state = WorkerState.CRASHED
            record.last_error = "worker process cleanup is incomplete"

    def _monitor_loop(self) -> None:
        while not self._stop.wait(self._poll_period):
            schedule_admitted, _ = self._schedule_status()
            with self._lock:
                for record in self._records.values():
                    if record.launch.worker_id.casefold() in self._launch_transition_ids:
                        continue
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
            if self._configuration_restart_pending:
                raise WorkerReconfigurationBusyError("node configuration restart is pending")
            self._require_no_launch_transition_locked()
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
            if self._sharing_disabled:
                raise WorkerPolicyError("sharing is disabled by contribution policy")
            if record.resource_operation is not None:
                if self._closed or self._configuration_restart_pending or self._launch_transition_ids:
                    raise WorkerReconfigurationBusyError("worker configuration or launch transition is busy")
                record.loading_failed = False
                record.operator_paused = False
                record.desired_running = True
                if record.resource_operation[0] == "release" or record.resource_operation[2].is_set():
                    record.start_after_cleanup = True
                return False
            if self._acquire_resources_cancellable is None:
                self._require_no_launch_transition_locked()
            elif self._launch_transition_ids:
                raise WorkerReconfigurationBusyError("worker launch transition requires completed cleanup")
            if self._closed:
                raise RuntimeError("worker supervisor is closed")
            if self._configuration_restart_pending:
                raise WorkerReconfigurationBusyError("node configuration restart is pending")
            self._refresh_locked(record)
            record.loading_failed = False
            if not record.launch.policy_admitted:
                record.desired_running = False
                if record.launch.automatic:
                    # A user's Start clears an earlier Pause even while placement
                    # is pending. The reconciler may start it only after every
                    # policy and signed-placement check admits its next launch.
                    record.operator_paused = False
                    record.start_after_admission = True
                    return False
                raise WorkerPolicyError(record.launch.policy_reason)
            record.operator_paused = False
            record.start_after_admission = False
            record.desired_running = True
            if self._contained_launch(record):
                record.start_after_cleanup = True
            try:
                return self._spawn_locked(
                    record,
                    defer_outside_schedule=record.launch.automatic,
                    defer_unavailable_resources=record.launch.automatic,
                )
            finally:
                if not record.cleanup_pending and record.resource_operation is None:
                    record.start_after_cleanup = False

    @staticmethod
    def _kill_linux_worker_group(process: subprocess.Popen) -> None:
        if sys.platform.startswith("linux"):
            # Each worker owns a new session. Its multiprocessing DHT children
            # can survive the direct child's exit and otherwise retain p2pd's
            # identity/port, preventing the replacement worker from starting.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _terminate(self, process: subprocess.Popen) -> int:
        if (
            self._coordinated_launches
            or id(process) in self._process_containments
            or any(record.process is process and self._contained_launch(record) for record in self._records.values())
        ):
            return self._terminate_launch_tree(process)
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    return process.wait(timeout=self._stop_timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
            return process.wait(timeout=self._stop_timeout)
        finally:
            self._kill_linux_worker_group(process)

    def _terminate_launch_tree(self, process: subprocess.Popen) -> int:
        """Reap one owned process and verify its original containment is empty."""
        with self._process_cleanup_lock:
            cleanup_lock = self._process_cleanup_locks.setdefault(process, threading.Lock())
        with cleanup_lock:
            if process in self._verified_stops:
                return process.wait(timeout=self._stop_timeout)
            return self._terminate_launch_tree_locked(process)

    def _terminate_launch_tree_locked(self, process: subprocess.Popen) -> int:
        from drift.node.edge_supervisor import _wait_for_containment_exit

        containment = self._process_containments.get(id(process))
        if containment is None:
            raise RuntimeError("worker process containment is unavailable")
        containment.terminate()
        try:
            exit_code = process.wait(timeout=self._stop_timeout)
        except subprocess.TimeoutExpired:
            containment.kill()
            # A failed attach can leave a child outside the otherwise empty job.
            if process.poll() is None:
                process.kill()
            exit_code = process.wait(timeout=self._stop_timeout)
        interval = min(self._poll_period, 0.05)
        if not _wait_for_containment_exit(containment, self._stop_timeout, interval):
            containment.kill()
            if not _wait_for_containment_exit(containment, self._stop_timeout, interval):
                raise RuntimeError("worker process containment cleanup is incomplete")
        containment.close()
        del self._process_containments[id(process)]
        with self._process_cleanup_lock:
            self._verified_stops.add(process)
        return exit_code

    def pause_worker(self, worker_id: str) -> bool:
        """Pause a worker and persist the operator's explicit stopped intent."""

        return self._pause_worker(worker_id, operator_action=True)

    def pause_worker_for_reconfiguration(self, worker_id: str) -> bool:
        """Quiesce a worker without manufacturing an operator pause."""

        return self._pause_worker(worker_id, operator_action=False)

    def _pause_worker(self, worker_id: str, *, operator_action: bool) -> bool:
        record = self._record(worker_id)
        with self._lock:
            self._invalidate_loading_locked(record)
            if operator_action:
                record.operator_paused = True
                record.start_after_admission = False
            record.desired_running = False
            record.start_after_cleanup = False
            record.schedule_suspended = False
            record.resource_suspended = False
            if self._cancel_resource_acquisition_locked(record):
                return False
            if self._launch_transition_active and worker_id.casefold() in self._launch_transition_ids:
                # The batch owns cleanup. Pause records authoritative intent and
                # leaves the public state STOPPING until that owner finishes.
                return record.process is not None or record.suspension_stop_thread is not None
            suspension_stop_thread = record.suspension_stop_thread
            process = None if suspension_stop_thread is not None else record.process
            if self._contained_launch(record) and process is not None:
                # Register the sole cleanup owner before releasing the lock so
                # a concurrent batch can join it instead of closing the same job.
                self._suspend_locked(record)
                suspension_stop_thread = record.suspension_stop_thread
                process = None
            if process is None and suspension_stop_thread is None:
                if not self._release_resources_locked(record):
                    if record.resource_operation is not None:
                        return False
                    raise RuntimeError(record.last_error or _RESOURCE_SPAWN_UNCERTAIN)
                record.state = WorkerState.PAUSED
                return False
            if process is not None:
                record.state = WorkerState.STOPPING
                if self._coordinated_launches:
                    record.cleanup_pending = True
        if suspension_stop_thread is not None:
            multiplier = 4 if self._contained_launch(record) else 2
            suspension_stop_thread.join(timeout=(self._stop_timeout * multiplier) + self._poll_period)
            if suspension_stop_thread.is_alive():
                raise RuntimeError(f"failed to pause worker {record.launch.worker_id!r} within the stop timeout")
            with self._lock:
                if record.process is None and record.resource_operation is not None:
                    return True
                if record.process is not None or record.cleanup_pending or record.resource_token is not None:
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
            record.cleanup_pending = False
            if not self._release_resources_locked(record):
                raise RuntimeError(_RESOURCE_RELEASE_PENDING)
        return True

    def restart_worker(self, worker_id: str) -> bool:
        with self._lock:
            self._require_no_launch_transition_locked()
            if self._configuration_restart_pending:
                raise WorkerReconfigurationBusyError("node configuration restart is pending")
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
                        "load_state": record.load_state,
                        "model_ready": bool(
                            record.load_state == "ready"
                            and record.loading_ticket is not None
                            and self._loading_ticket_current_locked(record, record.loading_ticket)
                            and schedule_admitted
                            and resource_admitted
                            and record.launch.policy_admitted
                        ),
                        "download_progress": self._download_snapshot(record),
                        "desired_running": record.desired_running,
                        "operator_paused": record.operator_paused,
                        "start_after_admission": record.start_after_admission,
                        "cleanup_pending": record.cleanup_pending,
                        "resource_operation": None
                        if record.resource_operation is None
                        else record.resource_operation[0],
                        "resource_cancel_requested": bool(
                            record.resource_operation is not None
                            and record.resource_operation[0] in ("acquire", "spawn")
                            and record.resource_operation[2].is_set()
                        ),
                        "auto_restart": record.launch.auto_restart,
                        "policy_admitted": record.launch.policy_admitted and not self._sharing_disabled,
                        "policy_reason": "sharing is disabled by contribution policy"
                        if self._sharing_disabled
                        else record.launch.policy_reason,
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
                        "device": record.launch.device or record.launch.vram_device,
                        "vram_device": record.launch.vram_device,
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

    @staticmethod
    def _download_snapshot(record):
        from drift.utils.download_progress import public_progress

        if record.progress_directory is None:
            return None
        try:
            with (Path(record.progress_directory.name) / "progress.json").open("rb") as stream:
                payload = stream.read(16385)
            if len(payload) > 16384:
                return None
            result = json.loads(payload)
            if not isinstance(result, dict) or result.get("schema_version") != 1:
                return None
            # The fresh per-launch directory binds this report to the worker.
            # Windows venv launchers may write from a child PID, and frozen
            # workers may use their own PID; neither changes that ownership.
            if record.state in (WorkerState.PAUSED, WorkerState.CRASHED, WorkerState.STOPPING):
                result["state"] = "failed" if record.state is WorkerState.CRASHED else "paused"
                result["bytes_per_second"] = 0
            return public_progress(result)
        except (OSError, ValueError):
            return None

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

    @property
    def configuration_restart_pending(self) -> bool:
        """Whether a durable worker-configuration change is waiting for node restart."""

        with self._lock:
            return self._configuration_restart_pending

    def _require_no_launch_transition_locked(self) -> None:
        if any(record.resource_operation_active for record in self._records.values()):
            raise WorkerReconfigurationBusyError("worker resource operation is in progress")
        if self._launch_transition_ids:
            raise WorkerReconfigurationBusyError("worker launch transition requires completed cleanup")

    @property
    def launch_transition_status(self) -> Dict[str, Any]:
        """In-memory retry state; it is not a durable power-outage checkpoint."""
        with self._lock:
            return {
                "state": self._launch_transition_state,
                "worker_ids": sorted(self._launch_transition_ids),
                "closed": self._closed,
            }

    @staticmethod
    def _validate_joint_launches(launches: Sequence[WorkerLaunch]) -> None:
        _validate_vram_pools(launches)
        spans: Dict[str, list[tuple[int, int]]] = {}
        for launch in launches:
            if not launch.automatic or not launch.policy_admitted:
                continue
            # WorkerLaunch validates these exact artifact claims against the
            # command. Loose model labels or ordinal device names are not proof.
            if launch.placement_manifest_digest is None or not isinstance(launch.block_indices, str):
                raise ValueError("joint automatic placement requires exact manifest ranges")
            if re.fullmatch(r"(?:0|[1-9][0-9]{0,2}):[1-9][0-9]{0,2}", launch.block_indices) is None:
                raise ValueError("joint automatic placement requires a canonical bounded block range")
            start, end = map(int, launch.block_indices.split(":"))
            if not 0 <= start < end <= MAX_AUTOMATIC_PLACEMENT_BLOCKS:
                raise ValueError("joint automatic placement requires a canonical bounded block range")
            prior = spans.setdefault(launch.placement_manifest_digest, [])
            if any(start < other_end and other_start < end for other_start, other_end in prior):
                raise ValueError("joint automatic placements overlap within an exact manifest")
            prior.append((start, end))

    def replace_launches(
        self,
        launches: Sequence[WorkerLaunch],
        *,
        start: Optional[bool] = None,
        before_install: Optional[Callable[[], None]] = None,
        preserve_start_intent: bool = False,
    ) -> bool:
        """Quiesce an existing subset before atomically installing its launches.

        Coordinated containment must have been enabled at construction. Every
        supplied worker is stopped, even if its assignment compares equal. Start
        defaults to each launch's auto_start, always subject to operator Pause.
        A failed cleanup preserves old assignments and blocks all new starts;
        only a retry covering the pending subset can complete the transition.
        The caller still owns signed placement admission and aggregate budgets.
        An optional final admission callback runs under the supervisor lock after
        cleanup and before installation. It must not acquire higher-order locks.
        Preserving start intent captures each worker's current request under that
        same lock and keeps it across a failed batch until complete-set retry.
        """
        if not isinstance(launches, (tuple, list)) or not 1 <= len(launches) <= MAX_VISIBLE_ACCELERATORS:
            raise ValueError("joint launch transition requires a bounded nonempty worker list")
        if start is not None and type(start) is not bool:
            raise ValueError("joint launch start must be a boolean or None")
        if before_install is not None and not callable(before_install):
            raise ValueError("joint launch before_install must be callable or None")
        if type(preserve_start_intent) is not bool or (preserve_start_intent and start is not None):
            raise ValueError("preserve_start_intent must be a boolean and requires start=None when enabled")
        replacements = {}
        for launch in launches:
            if not isinstance(launch, WorkerLaunch) or not isinstance(launch.worker_id, str):
                raise ValueError("joint launch transition requires worker launches")
            key = launch.worker_id.casefold()
            if key in replacements:
                raise ValueError("joint launch worker IDs must be unique case-insensitively")
            replacements[key] = launch
        with self._lock:
            if not self._coordinated_launches:
                raise WorkerReconfigurationBusyError("joint launches require containment from supervisor construction")
            if self._closed:
                raise RuntimeError("worker supervisor is closed")
            if (
                self._configuration_restart_pending
                or self._launch_transition_active
                or any(record.resource_operation_active for record in self._records.values())
            ):
                raise WorkerReconfigurationBusyError("worker configuration or launch transition is busy")
            if not replacements.keys() <= self._records.keys():
                raise WorkerNotFoundError("joint launch transition names an unknown worker")
            if not self._launch_transition_ids <= replacements.keys():
                raise WorkerReconfigurationBusyError("retry must include every pending worker transition")
            if self._launch_transition_ids and preserve_start_intent != self._launch_transition_preserve_start_intent:
                raise WorkerReconfigurationBusyError("retry must preserve the launch transition start-intent mode")
            final_launches = tuple(replacements.get(key, record.launch) for key, record in self._records.items())
            self._validate_joint_launches(final_launches)
            records = tuple(self._records[key] for key in sorted(replacements))
            changed = any(record.launch != replacements[record.launch.worker_id.casefold()] for record in records)
            if preserve_start_intent:
                for record in records:
                    key = record.launch.worker_id.casefold()
                    self._launch_transition_start_requests.setdefault(
                        key,
                        record.desired_running
                        or record.start_after_admission
                        or (not record.launch.policy_admitted and replacements[key].auto_start),
                    )
            self._launch_transition_preserve_start_intent = preserve_start_intent
            self._launch_transition_ids = frozenset(replacements)
            self._launch_transition_active = True
            self._launch_transition_state = "stopping"
            self._launch_transition_done.clear()
            for record in records:
                record.desired_running = False
                self._invalidate_loading_locked(record)
                record.start_after_cleanup = False
                record.schedule_suspended = False
                record.resource_suspended = False
                if record.process is not None:
                    record.cleanup_pending = True
                    record.state = WorkerState.STOPPING
        try:
            failed = False
            for record in records:
                try:
                    with self._lock:
                        stop_thread = record.suspension_stop_thread
                    if stop_thread is not None:
                        stop_thread.join(timeout=(self._stop_timeout * 4) + self._poll_period)
                        if stop_thread.is_alive():
                            raise RuntimeError("worker policy cleanup is still running")
                    with self._lock:
                        process = record.process
                    if process is not None:
                        exit_code = self._terminate_launch_tree(process)
                        with self._lock:
                            if record.process is process:
                                record.process = None
                            record.last_exit_code = exit_code
                            record.cleanup_pending = False
                            record.natural_exit_code = None
                    with self._lock:
                        if record.process is None and not self._release_resources_locked(record):
                            raise RuntimeError(record.last_error or _RESOURCE_RELEASE_PENDING)
                        if record.cleanup_pending:
                            raise RuntimeError("worker cleanup has no completed evidence")
                        record.state = WorkerState.PAUSED
                        record.last_error = None
                except Exception:
                    # Keep stopping the rest. No old assignment is overwritten,
                    # and neither parent exit nor an error releases reservations.
                    failed = True
                    with self._lock:
                        # A timed-out stop-thread observation may already be
                        # stale: its verified cleanup can finish before this
                        # lock is acquired. Never recreate an orphaned cleanup
                        # latch after that owner has released the process.
                        if (
                            record.process is not None
                            or record.suspension_stop_thread is not None
                            or record.cleanup_pending
                            or record.resource_token is not None
                        ):
                            record.cleanup_pending = True
                            record.state = WorkerState.STOPPING
                            record.last_error = (
                                _RESOURCE_SPAWN_UNCERTAIN
                                if record.resource_spawn_uncertain
                                else (
                                    _RESOURCE_RELEASE_PENDING
                                    if record.resource_release_pending
                                    else "worker process cleanup is incomplete; retry the launch transition"
                                )
                            )
                        else:
                            record.state = WorkerState.PAUSED
                            record.last_error = None
            with self._lock:
                if failed or self._closed:
                    self._launch_transition_state = "cleanup_failed"
                    raise RuntimeError(
                        "worker supervisor closed during launch transition"
                        if self._closed
                        else "worker launch transition cleanup is incomplete"
                    )
                if before_install is not None:
                    before_install()
                # Install every new assignment before evaluating the first start.
                # Pause writes use this same lock, so stale start intent cannot
                # undo a Pause received while cleanup was in progress.
                for record in records:
                    key = record.launch.worker_id.casefold()
                    launch = replacements[key]
                    record.launch = launch
                    record.last_power_watts = None
                    requested_start = (
                        self._launch_transition_start_requests[key]
                        if preserve_start_intent
                        else (launch.auto_start if start is None else start) or record.start_after_admission
                    )
                    record.desired_running = requested_start and not record.operator_paused and launch.policy_admitted
                    if record.desired_running:
                        record.start_after_admission = False
                    elif preserve_start_intent and launch.automatic and not launch.policy_admitted:
                        record.start_after_admission = requested_start and not record.operator_paused
                self._launch_transition_ids = frozenset()
                self._launch_transition_start_requests = {}
                self._launch_transition_preserve_start_intent = False
                self._launch_transition_state = "idle"
                if self._started:
                    for record in records:
                        if record.desired_running:
                            self._spawn_locked(record, defer_outside_schedule=True, defer_unavailable_resources=True)
                return changed
        finally:
            with self._lock:
                self._launch_transition_active = False
                if self._launch_transition_ids:
                    self._launch_transition_state = "cleanup_failed"
                self._launch_transition_done.set()

    def replace_launch(self, launch: WorkerLaunch, *, start: Optional[bool] = None) -> bool:
        """Replace one paused worker assignment without overriding an explicit pause."""
        record = self._record(launch.worker_id)
        with self._lock:
            self._require_no_launch_transition_locked()
            if self._closed:
                raise RuntimeError("worker supervisor is closed")
            if self._configuration_restart_pending:
                raise WorkerReconfigurationBusyError("node configuration restart is pending")
            final_launches = tuple(launch if other is record else other.launch for other in self._records.values())
            if self._coordinated_launches:
                self._validate_joint_launches(final_launches)
            self._refresh_locked(record)
            if (
                record.desired_running
                or record.cleanup_pending
                or record.resource_token is not None
                or record.process is not None
                or record.suspension_stop_thread is not None
                or record.state in (WorkerState.STARTING, WorkerState.STOPPING)
            ):
                raise WorkerReconfigurationBusyError(
                    f"pause contribution worker {record.launch.worker_id!r} before replacing its placement"
                )
            changed = record.launch != launch
            if not self._coordinated_launches:
                _validate_vram_pools(final_launches)
            record.launch = launch
            record.schedule_suspended = False
            record.resource_suspended = False
            record.last_power_watts = None
            requested_start = (launch.auto_start if start is None else start) or record.start_after_admission
            # This decision is made while holding the same lock as pause_worker().
            # An operator pause that lands after a reconciler snapshot therefore
            # remains authoritative over stale automatic-start intent.
            should_start = requested_start and not record.operator_paused
            record.desired_running = should_start and launch.policy_admitted
            if record.desired_running:
                record.start_after_admission = False
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
            self._require_no_launch_transition_locked()
            if self._closed:
                raise RuntimeError("worker supervisor is closed")
            if self._configuration_restart_pending:
                raise WorkerReconfigurationBusyError("node configuration restart is pending")
            if set(launches) != set(self._records):
                raise ValueError("a policy update must preserve the configured worker set")
            if self._coordinated_launches:
                self._validate_joint_launches(tuple(launches.values()))
            for record in self._records.values():
                self._refresh_locked(record)
                if (
                    record.desired_running
                    or record.cleanup_pending
                    or record.resource_token is not None
                    or record.process is not None
                    or record.suspension_stop_thread is not None
                    or record.state in (WorkerState.STARTING, WorkerState.STOPPING)
                ):
                    raise WorkerReconfigurationBusyError(
                        "pause all contribution workers before changing the contribution policy"
                    )
            persist()
            self._sharing_disabled = False
            for normalized, record in self._records.items():
                record.launch = launches[normalized]
                record.schedule_suspended = False
                record.resource_suspended = False
                record.last_power_watts = None
            self._stop_timeout = settings.stop_timeout
            self._schedule_allowed = settings.schedule_allowed
            self._bandwidth_mbps = settings.bandwidth_mbps
            self._power_watts = settings.power_watts
            self._device_available = settings.device_available
            self._last_bandwidth_mbps = None

    def persist_sharing_disabled(self, persist: Callable[[], None]) -> None:
        """Persist only master Pause while cancelled resource work drains.

        The policy store must validate that the sole document change is disabling
        sharing. No launch/settings/operation is replaced through this escape.
        """
        if not callable(persist):
            raise ValueError("sharing persistence must be callable")
        with self._lock:
            if self._closed:
                raise RuntimeError("worker supervisor is closed")
            if self._configuration_restart_pending or self._launch_transition_active:
                raise WorkerReconfigurationBusyError("worker configuration or launch transition is busy")
            for record in self._records.values():
                operation = record.resource_operation
                if (
                    not record.operator_paused
                    or record.desired_running
                    or (operation is not None and operation[0] in ("acquire", "spawn") and not operation[2].is_set())
                ):
                    raise WorkerReconfigurationBusyError("pause all contribution workers before disabling sharing")
            persist()
            self._sharing_disabled = True
            self._launch_transition_start_requests = {key: False for key in self._launch_transition_start_requests}

    def commit_configuration_restart(self, persist: Callable[[], None]) -> None:
        """Durably commit a node configuration while contribution is explicitly quiescent.

        The callback runs under the same lock as every worker action. A successful
        callback permanently closes all launch/reconfiguration paths for this
        supervisor; the owning node must restart to construct the new worker set.
        A failed callback leaves the restart latch unchanged.
        """

        with self._lock:
            self._require_no_launch_transition_locked()
            if self._closed:
                raise RuntimeError("worker supervisor is closed")
            if self._configuration_restart_pending:
                raise WorkerReconfigurationBusyError("node configuration restart is pending")
            for record in self._records.values():
                self._refresh_locked(record)
                if (
                    not record.operator_paused
                    or record.desired_running
                    or record.cleanup_pending
                    or record.resource_token is not None
                    or record.process is not None
                    or record.suspension_stop_thread is not None
                    or record.schedule_suspended
                    or record.resource_suspended
                    or record.state is not WorkerState.PAUSED
                ):
                    raise WorkerReconfigurationBusyError(
                        "pause all contribution workers before changing the worker configuration"
                    )
            persist()
            self._configuration_restart_pending = True

    def drain_resource_operations(self, timeout: float = 0.0) -> bool:
        """After shutdown, bound the wait before the owner lease may be closed.

        False retains recovery authority: the caller must not close the resource
        manager or reload this node. No callback is joined under the supervisor
        lock, and one deadline covers every worker rather than each separately.
        This does not guess cleanup success or discard failed releases.
        """
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (float, int))
            or not math.isfinite(timeout)
            or timeout < 0
        ):
            raise ValueError("resource drain timeout must be finite and nonnegative")
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                if not self._closed:
                    return False
                records = tuple(self._records.values())
                if (
                    not self._launch_transition_active
                    and not self._process_containments
                    and all(
                        record.process is None
                        and record.suspension_stop_thread is None
                        and record.resource_thread is None
                        and record.resource_operation is None
                        and not record.resource_operation_active
                        and record.resource_token is None
                        and not record.resource_spawn_uncertain
                        and not record.cleanup_pending
                        for record in records
                    )
                ):
                    return True
                threads = tuple(
                    thread
                    for record in records
                    for thread in (record.resource_thread, record.suspension_stop_thread)
                    if thread is not None and thread is not threading.current_thread()
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if threads:
                for thread in threads:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    thread.join(timeout=min(0.02, remaining))
            else:
                time.sleep(min(0.02, remaining))

    def shutdown(self) -> None:
        with self._lock:
            if (
                self._closed
                and not self._launch_transition_active
                and all(
                    record.process is None
                    and record.suspension_stop_thread is None
                    and record.resource_token is None
                    and not record.resource_spawn_uncertain
                    and not record.resource_operation_active
                    for record in self._records.values()
                )
            ):
                return
            self._closed = True
            self._stop.set()
            records = tuple(self._records.values())
            monitor = self._monitor
            for record in records:
                record.operator_paused = True
                record.desired_running = False
                self._invalidate_loading_locked(record)
                record.start_after_cleanup = False
                record.start_after_admission = False
                record.schedule_suspended = False
                record.resource_suspended = False
                self._cancel_resource_acquisition_locked(record)
                if (
                    self._contained_launch(record)
                    and record.process is not None
                    and not (
                        self._launch_transition_active
                        and record.launch.worker_id.casefold() in self._launch_transition_ids
                    )
                ):
                    self._suspend_locked(record)
            suspension_stop_threads = {
                id(record): record.suspension_stop_thread
                for record in records
                if record.suspension_stop_thread is not None
            }
        # A running batch observes _closed and cannot install its new launches.
        # If it fails cleanup, shutdown can retry the retained processes. A timed
        # out cleanup remains tracked and a later shutdown call may retry it.
        self._launch_transition_done.wait(timeout=(self._stop_timeout * 4) + self._poll_period)
        for thread in suspension_stop_threads.values():
            multiplier = 4 if any(self._contained_launch(record) for record in records) else 2
            thread.join(timeout=(self._stop_timeout * multiplier) + self._poll_period)
        for record in records:
            with self._lock:
                if self._launch_transition_active and record.launch.worker_id.casefold() in self._launch_transition_ids:
                    # The batch is the sole cleanup owner and observes _closed
                    # before installing or starting replacements.
                    continue
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
                        record.last_error = (
                            "worker process cleanup is incomplete"
                            if self._contained_launch(record)
                            else f"{type(exc).__name__}: {exc}"
                        )
                    if self._contained_launch(record):
                        logger.error("Worker %r process cleanup is incomplete", record.launch.worker_id)
                    else:
                        logger.exception("Failed to stop worker %r", record.launch.worker_id)
                else:
                    with self._lock:
                        record.last_exit_code = exit_code
                        record.process = None
                        record.state = WorkerState.PAUSED
                        record.cleanup_pending = False
                        record.natural_exit_code = None
            with self._lock:
                if record.process is None:
                    self._release_resources_locked(record)
        if monitor is not None:
            monitor.join(timeout=5)
        for record in records:
            if record.process is None and record.resource_operation is None and record.progress_directory is not None:
                record.progress_directory.cleanup()
