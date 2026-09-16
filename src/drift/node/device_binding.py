"""Private, persistent physical-device pins for node-managed contribution workers.

Enrollment reads metadata only. A worker ID is never silently rebound; choosing a
different device requires a new worker ID. POSIX files are owner-only. Windows
uses the enclosing local profile's ACL; these records are not a public API.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import UUID

from drift.node.hardware_status import MAX_VISIBLE_ACCELERATORS

_MAX_RECORD_BYTES = 1024
_WORKER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_CUDA_UUID = re.compile(r"(?:GPU-)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_INVALID_STORE = "Private device selection record is unavailable or invalid; restore it before starting sharing"
_CHANGED_SELECTION = "Selected device changed; keep this worker stopped and explicitly select a new worker ID"
_UNAVAILABLE = "Selected CUDA device is unavailable or its physical identity changed"


class DeviceBindingError(ValueError):
    """A fixed, identifier-free device-selection failure suitable for local UI."""


def normalize_cuda_uuid(value: Any) -> Optional[str]:
    """Accept full Torch/NVML physical GPU UUIDs, never MIG or partial IDs."""
    if not isinstance(value, str) or len(value) > 40 or _CUDA_UUID.fullmatch(value) is None:
        return None
    raw = value[4:] if value[:4].upper() == "GPU-" else value
    parsed = UUID(raw)
    return None if parsed.int == 0 else "GPU-" + str(parsed)


def _cuda_identity(device: str) -> Optional[str]:
    try:
        import torch

        index = int(device.split(":", 1)[1])
        if not torch.cuda.is_available() or index >= torch.cuda.device_count():
            return None
        value = getattr(torch.cuda.get_device_properties(index), "uuid", None)
        # Torch 2.6 returns a _CUuuid object whose string is a bare full UUID.
        return normalize_cuda_uuid(None if value is None else str(value))
    except Exception:
        return None


class _NvidiaIdentityProbe:
    """Query the selected UUID afresh; Torch properties alone can be cached."""

    def __init__(self) -> None:
        self._nvml = None
        self._lock = threading.Lock()

    def __call__(self, identity: str) -> bool:
        with self._lock:
            try:
                if self._nvml is None:
                    import pynvml

                    pynvml.nvmlInit()
                    self._nvml = pynvml
                handle = self._nvml.nvmlDeviceGetHandleByUUID(identity)
                observed = self._nvml.nvmlDeviceGetUUID(handle)
                if isinstance(observed, bytes):
                    observed = observed.decode("ascii")
                if normalize_cuda_uuid(observed) != identity:
                    return False
                # A fresh operational query also detects a lost GPU when its
                # handle/name/UUID may still be cached by the driver.
                total = self._nvml.nvmlDeviceGetMemoryInfo(handle).total
                return isinstance(total, int) and not isinstance(total, bool) and total > 0
            except Exception:
                return False


# Reconciliation rebuilds stores repeatedly. One process-level session avoids
# incrementing NVML's reference count on every rebuild while each probe still
# obtains fresh UUID-addressed handles and liveness metadata under its lock.
_DEFAULT_LIVENESS_PROBE = _NvidiaIdentityProbe()


def _is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _private_regular_file(info: os.stat_result) -> bool:
    return stat.S_ISREG(info.st_mode) and not _is_reparse(info) and (os.name == "nt" or info.st_mode & 0o077 == 0)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate record field")
        result[key] = value
    return result


class DeviceBinding:
    """Opaque local pin; expose only the sanitized guard result to status APIs."""

    def __init__(self, store: "DeviceBindingStore", path: Path, record: dict) -> None:
        self._store = store
        self._path = path
        self._record = record

    def __repr__(self) -> str:
        return f"DeviceBinding(device={self._record['device']!r})"

    @property
    def cuda_visible_devices(self) -> str:
        """Private child-only mask; its CUDA command must use device cuda:0."""
        return self._record["identity"]

    def check(self) -> Optional[str]:
        """Fail closed on record deletion/tampering, renumbering, or GPU loss."""
        try:
            if self._store._read(self._path) != self._record:
                return _INVALID_STORE
        except Exception:
            return _INVALID_STORE
        if not self._store._matches_hardware(self._record):
            return _UNAVAILABLE
        return None


class DeviceBindingStore:
    """Atomic first enrollment, immutable per-worker device pins across boots.

    A missing file on first enrollment is a migration/first-use condition. After
    enrollment the guard treats deletion as failure. An administrator deleting
    a pin between node runs is an explicit local reset, not detectable history.
    """

    def __init__(
        self,
        directory: Path | str,
        *,
        identity_provider: Optional[Callable[[str], Optional[str]]] = None,
        liveness_provider: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self._directory = Path(os.path.abspath(directory))
        self._identity_provider = _cuda_identity if identity_provider is None else identity_provider
        self._liveness_provider = _DEFAULT_LIVENESS_PROBE if liveness_provider is None else liveness_provider

    def _ensure_directory(self) -> None:
        try:
            self._check_ancestors()
            self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._check_ancestors()
            info = self._directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or _is_reparse(info) or (os.name != "nt" and info.st_mode & 0o077):
                raise ValueError("unsafe binding directory")
        except Exception:
            raise DeviceBindingError(_INVALID_STORE) from None

    def _check_ancestors(self) -> None:
        for path in (*reversed(self._directory.parents), self._directory):
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(info.st_mode) or _is_reparse(info):
                raise ValueError("linked or invalid binding ancestor")

    def _read(self, path: Path) -> Optional[dict]:
        self._ensure_directory()
        try:
            try:
                before = path.lstat()
            except FileNotFoundError:
                return None
            if not _private_regular_file(before) or before.st_size > _MAX_RECORD_BYTES:
                raise ValueError("unsafe record")
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
            with os.fdopen(descriptor, "rb") as stream:
                current = os.fstat(stream.fileno())
                if not _private_regular_file(current) or (before.st_dev, before.st_ino) != (
                    current.st_dev,
                    current.st_ino,
                ):
                    raise ValueError("record changed during read")
                raw = stream.read(_MAX_RECORD_BYTES + 1)
            if len(raw) > _MAX_RECORD_BYTES:
                raise ValueError("oversized record")
            result = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
            if not isinstance(result, dict) or set(result) != {"schema_version", "worker_id", "device", "identity"}:
                raise ValueError("invalid record fields")
            if type(result["schema_version"]) is not int or result["schema_version"] != 1:
                raise ValueError("unsupported schema")
            worker_id = result["worker_id"]
            if (
                not isinstance(worker_id, str)
                or _WORKER_ID.fullmatch(worker_id) is None
                or worker_id != worker_id.casefold()
            ):
                raise ValueError("invalid worker")
            device = self._validate_device(result["device"])
            if device == "cpu":
                if result["identity"] is not None:
                    raise ValueError("invalid CPU identity")
            elif (
                normalize_cuda_uuid(result["identity"]) is None
                or normalize_cuda_uuid(result["identity"]) != result["identity"]
            ):
                raise ValueError("invalid GPU identity")
            return result
        except Exception:
            raise DeviceBindingError(_INVALID_STORE) from None

    @staticmethod
    def _validate_device(device: Any) -> str:
        if device == "cpu":
            return device
        if not isinstance(device, str) or re.fullmatch(r"cuda:(0|[1-9][0-9]?)", device) is None:
            raise DeviceBindingError("Persistent physical selection currently supports CPU and CUDA only")
        if int(device.split(":", 1)[1]) >= MAX_VISIBLE_ACCELERATORS:
            raise DeviceBindingError("Selected device is outside the supported visible-device range")
        return device

    def _matches_hardware(self, record: dict) -> bool:
        try:
            identity = normalize_cuda_uuid(self._identity_provider(record["device"]))
            return identity == record["identity"] and identity is not None and self._liveness_provider(identity) is True
        except Exception:
            return False

    def _create(self, path: Path, record: dict) -> None:
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=self._directory)
            with os.fdopen(descriptor, "wb") as stream:
                os.chmod(temporary, 0o600)
                stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # Publish a complete record atomically without replacing a pin
                # enrolled by a concurrent node. A crash can leave only a private
                # .pending file or the complete pin, never a partial public pin.
                os.link(temporary, path)
            except FileExistsError:
                pass
            if os.name != "nt":
                directory_fd = os.open(self._directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except Exception:
            raise DeviceBindingError(_INVALID_STORE) from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def bind(self, worker_id: str, device: str) -> Optional[DeviceBinding]:
        """Persist the first selection, or validate it without ever rebinding.

        CPU enrollment returns None after verifying any persisted selection so
        automatic detection cannot silently move a CUDA worker onto CPU or back.
        """
        if not isinstance(worker_id, str) or _WORKER_ID.fullmatch(worker_id) is None:
            raise DeviceBindingError("Worker ID is invalid for private device selection")
        device = self._validate_device(device)
        worker_id = worker_id.casefold()
        path = self._directory / (hashlib.sha256(worker_id.encode("ascii")).hexdigest() + ".json")
        record = self._read(path)
        if record is not None:
            if record["worker_id"] != worker_id or record["device"] != device:
                raise DeviceBindingError(_CHANGED_SELECTION)
        else:
            identity = None
            if device != "cpu":
                try:
                    identity = normalize_cuda_uuid(self._identity_provider(device))
                except Exception:
                    pass
                if identity is None:
                    raise DeviceBindingError(_UNAVAILABLE)
            record = {"schema_version": 1, "worker_id": worker_id, "device": device, "identity": identity}
            if device != "cpu" and not self._matches_hardware(record):
                raise DeviceBindingError(_UNAVAILABLE)
            self._create(path, record)
            if self._read(path) != record:
                raise DeviceBindingError(_CHANGED_SELECTION)
        if device == "cpu":
            return None
        binding = DeviceBinding(self, path, record)
        failure = binding.check()
        if failure is not None:
            raise DeviceBindingError(failure)
        return binding
