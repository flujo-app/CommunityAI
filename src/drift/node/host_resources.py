"""Conservative CUDA host estimates and bounded, read-only local resource observations.

These are admission estimates, not memory caps or atomic filesystem reservations.
The caller must serialize admission/loading and retain uncertain generations.
"""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import stat
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import psutil
import torch

from drift.model_manifest import ManifestBlockArtifactPlan
from drift.node.placement_resources import (
    MAX_BYTES,
    MAX_FILES,
    MAX_WORKERS,
    ArtifactClaim,
    CacheSnapshot,
    ResourceSnapshot,
    VolumeSnapshot,
    WorkerResourceClaim,
)
from drift.server.memory_budget import ModelMemoryProfile
from drift.utils.convert_block import QuantType

GIB = 1024**3
WORKER_OVERHEAD_BYTES = GIB
LOAD_OVERHEAD_BYTES = GIB // 2
_CHUNK_BYTES = 4 * 1024**2
_CHECKPOINT_ROLES = {"weight", "converted_weight", "quantized_weight"}


class HostResourceError(RuntimeError):
    """A fixed, operator-safe reason for unavailable resource evidence."""


class ResourceScanPending(HostResourceError):
    """A cooperative budget expired; retry with the same verification cache."""


class ResourceScanCancelled(HostResourceError):
    """The caller cancelled or invalidated this scan; no snapshot is available."""


def _bytes(value, *, positive=False):
    if type(value) is not int or not int(positive) <= value <= MAX_BYTES:
        raise ValueError("resource bytes must be bounded non-negative integers")
    return value


@dataclass(frozen=True)
class HostMemoryEstimate:
    persistent_bytes: int
    staging_bytes: int
    dense_parameter_bytes: int
    checkpoint_bytes: int


def estimate_host_memory(profile, artifact_plan, *, device="cuda") -> HostMemoryEstimate:
    """Reserve dense CPU copies even for quantized, single-CUDA-device execution.

    Full declared payload is charged for BIN deserialization and, conservatively,
    for selected safetensor clones (config/index alone cannot prove tensor sizes).
    Six FP32-sized conversion copies cover source/target, tensor-parallel cloning,
    fused conversion and FP8's FP32 cast/product intermediates. Fixed Python,
    torch, allocator and metadata allowances are estimates, not measured bounds.
    """
    if not isinstance(profile, ModelMemoryProfile) or not isinstance(artifact_plan, ManifestBlockArtifactPlan):
        raise ValueError("host estimates require verified model and exact artifact geometry")
    if torch.device(device).type != "cuda" or profile.num_devices != 1 or profile.adapter_memory_per_block:
        raise ValueError("host estimates support single-device CUDA execution without adapters")
    start, end = artifact_plan.start_block, artifact_plan.end_block
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= profile.num_blocks <= 512:
        raise ValueError("host estimate span must match the verified model")
    if len(artifact_plan.artifacts) > MAX_FILES:
        raise ValueError("host estimate has too many artifacts")
    # get_block_size includes a 1% allowance and rounds to nearest byte. Adding
    # one byte before expansion covers that rounding. NF4 uses 0.53125 bytes per
    # parameter; 8x exceeds the corresponding FP32 expansion, without floats.
    if profile.quant_type == QuantType.NF4:
        multiplier = 8
    elif profile.quant_type == QuantType.INT8:
        multiplier = 4
    elif profile.quant_type in (QuantType.NONE, QuantType.FP8_DEQUANT):
        multiplier = 1 if profile.dtype == torch.float32 else 2
    else:
        raise ValueError("unsupported host estimate quantization")
    dense = sum((_bytes(value, positive=True) + 1) * multiplier for value in profile.block_weight_bytes[start:end])
    checkpoint = metadata = 0
    for artifact in artifact_plan.artifacts:
        size = _bytes(artifact.size)
        if artifact.role in _CHECKPOINT_ROLES:
            if not artifact.path.endswith((".bin", ".safetensors")):
                raise ValueError("host estimate checkpoint format is unsupported")
            checkpoint += size
        elif artifact.role in ("config", "weight_index"):
            metadata += size
        else:
            raise ValueError("host estimate contains an unsupported artifact role")
    if checkpoint < 1:
        raise ValueError("host estimate requires checkpoint payload")
    persistent = WORKER_OVERHEAD_BYTES + dense + 16 * metadata
    staging = LOAD_OVERHEAD_BYTES + 2 * checkpoint + 6 * dense
    return HostMemoryEstimate(_bytes(persistent), _bytes(staging), _bytes(dense), _bytes(checkpoint))


def _fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def _same_open_file(path_fingerprint, descriptor_fingerprint):
    # CPython on Windows can report creation time via lstat's deprecated ctime,
    # but change time via fstat. Compare ctime only within the same API; retain
    # it in both before/after checks and in the path verification-cache key.
    if os.name == "nt":
        return path_fingerprint[:5] + path_fingerprint[6:] == descriptor_fingerprint[:5] + descriptor_fingerprint[6:]
    return path_fingerprint == descriptor_fingerprint


def _checked_stat(path):
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise HostResourceError("resource measurement refuses links or reparse points")
    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)) or not info.st_ino:
        raise HostResourceError("resource measurement requires stable regular files and directories")
    return info


def canonical_cache_root(path) -> str:
    """Return an existing native physical root, refusing linked ancestors; never mkdir."""
    try:
        raw = os.fspath(path)
        if not isinstance(raw, str) or not os.path.isabs(raw) or "\0" in raw or len(raw) > 4096:
            raise ValueError("cache roots must be bounded absolute native paths")
        normalized = os.path.normcase(os.path.normpath(raw))
        root = Path(normalized)
        for component in (*reversed(root.parents), root):
            if not stat.S_ISDIR(_checked_stat(component).st_mode):
                raise HostResourceError("resource cache root is not an existing directory")
        if os.path.normcase(os.path.realpath(normalized)) != normalized:
            raise HostResourceError("resource cache root has an ambiguous physical path")
        return normalized
    except OSError:
        raise HostResourceError("resource cache root is unavailable or unreadable") from None


class VerificationCache:
    """Bounded SHA state without retained handles; stat keys are not writer locks."""

    def __init__(self, max_entries=MAX_FILES, *, max_partial_entries=16):
        if type(max_entries) is not int or not 1 <= max_entries <= MAX_FILES:
            raise ValueError("verification cache capacity must be between 1 and 4096")
        if type(max_partial_entries) is not int or not 1 <= max_partial_entries <= 2 * MAX_WORKERS:
            raise ValueError("partial verification capacity must be between 1 and 32")
        self._max_entries = max_entries
        self._max_partial_entries = max_partial_entries
        self._entries = OrderedDict()
        self._partials = OrderedDict()
        self._epoch = 0
        self._lock = threading.Lock()

    def discard_pending(self):
        """Cancel saved/in-flight partial work, including between retry attempts."""
        with self._lock:
            self._partials.clear()
            self._epoch += 1

    def _generation(self):
        with self._lock:
            return self._epoch

    def _check_generation(self, epoch):
        with self._lock:
            if epoch != self._epoch:
                raise ResourceScanCancelled("resource verification was cancelled; retry with a new request")

    def _resume(self, key, epoch):
        with self._lock:
            if epoch != self._epoch:
                raise ResourceScanCancelled("resource verification was cancelled; retry with a new request")
            for old in tuple(self._partials):
                if old[0] == key[0] and old != key:
                    del self._partials[old]
            state = self._partials.get(key)
            if state is None:
                return None
            self._partials.move_to_end(key)
            offset, digest, opened = state
            return offset, digest.copy(), opened

    def _save_partial(self, key, offset, digest, opened, epoch):
        with self._lock:
            if epoch != self._epoch:
                raise ResourceScanCancelled("resource verification was cancelled; retry with a new request")
            previous = self._partials.get(key)
            # Concurrent readers own independent digest copies; an older reader
            # must not roll back a farther verified prefix or a completed check.
            if key in self._entries or (previous is not None and previous[0] >= offset):
                return
            self._partials[key] = (offset, digest.copy(), opened)
            self._partials.move_to_end(key)
            while len(self._partials) > self._max_partial_entries:
                self._partials.popitem(last=False)

    def _prune_missing(self, records, roots):
        with self._lock:
            for key in tuple(self._partials):
                if any(_inside(key[0], root) for root in roots) and records.get(key[0]) != key[3]:
                    del self._partials[key]

    def _contains(self, key):
        with self._lock:
            if key not in self._entries:
                return False
            self._entries.move_to_end(key)
            return True

    def _add(self, key, epoch=None):
        with self._lock:
            if epoch is not None and epoch != self._epoch:
                raise ResourceScanCancelled("resource verification was cancelled; retry with a new request")
            self._partials.pop(key, None)
            self._entries[key] = True
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)


def _linux_mounts():
    if not sys.platform.startswith("linux"):
        return ()
    # ismount/st_dev alone cannot recognize same-filesystem bind mounts.
    with open("/proc/self/mountinfo", "r", encoding="utf-8") as stream:
        payload = stream.read(4 * 1024**2 + 1)
    if len(payload) > 4 * 1024**2:
        raise HostResourceError("resource mount topology exceeds measurement bounds")
    result = []
    for line in payload.splitlines():
        fields = line.split()
        if len(fields) < 7 or "-" not in fields:
            raise HostResourceError("resource mount topology is unavailable")
        decode = (
            lambda value: value.replace("\\040", " ")
            .replace("\\011", "\t")
            .replace("\\012", "\n")
            .replace("\\134", "\\")
        )
        separator = fields.index("-")
        if separator + 1 >= len(fields):
            raise HostResourceError("resource mount topology is unavailable")
        result.append((decode(fields[3]), os.path.normpath(decode(fields[4])), fields[separator + 1]))
    if not result:
        raise HostResourceError("resource mount topology is unavailable")
    return tuple(result)


def _inside(path, root):
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def _volume(path, info):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        mount = ctypes.create_unicode_buffer(32768)
        name = ctypes.create_unicode_buffer(32768)
        get_mount = kernel.GetVolumePathNameW
        get_mount.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
        get_mount.restype = wintypes.BOOL
        get_name = kernel.GetVolumeNameForVolumeMountPointW
        get_name.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
        get_name.restype = wintypes.BOOL
        drive_type = kernel.GetDriveTypeW
        drive_type.argtypes = (wintypes.LPCWSTR,)
        drive_type.restype = wintypes.UINT
        if not get_mount(path, mount, len(mount)) or drive_type(mount.value) != 3:
            raise HostResourceError("resource measurement requires a local fixed volume")
        if not get_name(mount.value, name, len(name)):
            raise HostResourceError("resource volume identity is unavailable")
        identity = name.value.casefold()
    else:
        identity = f"device:{info.st_dev}"
    return "volume-" + hashlib.sha256(identity.encode()).hexdigest()[:32], _bytes(shutil.disk_usage(path).free)


def snapshot_resources(
    claims,
    *,
    host_limit_bytes,
    cache_limits,
    now,
    verification_cache=None,
    cancelled: Callable[[], bool] | None = None,
    maximum_scan_seconds=30.0,
    maximum_entries=100000,
    maximum_hash_bytes=2**40,
    host_reserve_bytes=GIB,
    disk_reserve_bytes=GIB,
) -> ResourceSnapshot:
    """Measure existing roots without following links or giving partial-file credit.

    Time limits are cooperative around OS calls and chunks, not kernel-I/O
    deadlines. ``observed_at`` is the supplied scan-start time, conservatively.
    Hash/stat caches are never persisted; child artifact verification is required.
    Pending budgets confer no credit. Only unchanged partial hashes can resume.
    """
    _bytes(host_limit_bytes, positive=True)
    _bytes(host_reserve_bytes)
    _bytes(disk_reserve_bytes)
    _bytes(maximum_hash_bytes)
    if type(maximum_entries) is not int or not 1 <= maximum_entries <= 100000:
        raise ValueError("resource entry bound must be between 1 and 100000")
    for value in (now, maximum_scan_seconds):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("resource measurement times must be finite and non-negative")
    if not 0 < maximum_scan_seconds <= 300:
        raise ValueError("resource scan deadline must be within 300 seconds")
    if verification_cache is not None and not isinstance(verification_cache, VerificationCache):
        raise ValueError("resource verification cache has an invalid type")
    if cancelled is not None and not callable(cancelled):
        raise ValueError("resource cancellation must be callable")
    if not isinstance(claims, (tuple, list)) or len(claims) > 2 * MAX_WORKERS:
        raise ValueError("resource claims must contain at most 32 generations")
    if not isinstance(cache_limits, Mapping) or len(cache_limits) > 2 * MAX_WORKERS:
        raise ValueError("resource cache limits must be a bounded map")
    started = time.monotonic()
    epoch = None if verification_cache is None else verification_cache._generation()

    def check_cancelled():
        if cancelled is not None:
            try:
                stopped = cancelled()
            except Exception:
                raise HostResourceError("resource cancellation status is unavailable") from None
            if stopped:
                raise ResourceScanCancelled("resource verification was cancelled; retry with a new request")
        if verification_cache is not None:
            verification_cache._check_generation(epoch)

    def check_time():
        check_cancelled()
        if time.monotonic() - started > maximum_scan_seconds:
            raise ResourceScanPending("resource verification timed out; retry after preflight verification")

    hashed_bytes = 0

    def verify(path, fingerprint, artifact):
        nonlocal hashed_bytes
        key = (os.path.normcase(path), artifact.sha256, artifact.size_bytes, fingerprint)
        check_time()
        if verification_cache is not None and verification_cache._contains(key):
            return
        resumed = None if verification_cache is None else verification_cache._resume(key, epoch)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            stream = os.fdopen(descriptor, "rb", buffering=0)
        except BaseException:
            os.close(descriptor)
            raise
        with stream:
            opened = _fingerprint(os.fstat(stream.fileno()))
            if not _same_open_file(fingerprint, opened) or (resumed is not None and resumed[2] != opened):
                raise HostResourceError("resource artifact changed during verification")
            offset, digest = (0, hashlib.sha256()) if resumed is None else resumed[:2]
            stream.seek(offset)

            def unchanged():
                if (
                    _fingerprint(os.fstat(stream.fileno())) != opened
                    or _fingerprint(_checked_stat(path)) != fingerprint
                ):
                    raise HostResourceError("resource artifact changed during verification")

            try:
                while offset < artifact.size_bytes:
                    check_time()
                    remaining = maximum_hash_bytes - hashed_bytes
                    if remaining <= 0:
                        raise ResourceScanPending("resource verification reached its byte bound; retry verification")
                    chunk = stream.read(min(_CHUNK_BYTES, remaining, artifact.size_bytes - offset))
                    if not chunk:
                        raise HostResourceError("resource artifact changed during verification")
                    hashed_bytes += len(chunk)
                    offset += len(chunk)
                    digest.update(chunk)
                check_time()
                unchanged()
            except ResourceScanPending:
                # A timeout is resumable only after validating the still-open
                # descriptor and its current path. No descriptor survives return.
                unchanged()
                check_cancelled()
                if verification_cache is not None and offset:
                    verification_cache._save_partial(key, offset, digest, opened, epoch)
                raise
            if digest.hexdigest() != artifact.sha256:
                raise HostResourceError("resource artifact hash differs from its verified claim")
        check_cancelled()
        if verification_cache is not None:
            verification_cache._add(key, epoch)

    try:
        check_time()
        if os.name != "nt" and not sys.platform.startswith("linux"):
            raise HostResourceError("resource measurement supports native Windows and Linux volumes")
        roots = {}
        desired = {}
        for path, limit in cache_limits.items():
            check_time()
            root = canonical_cache_root(path)
            if root != path or root in roots:
                raise ValueError("resource cache keys must be unique canonical root strings")
            roots[root] = _bytes(limit, positive=True)
        for root in roots:
            check_time()
            if any(other != root and _inside(root, other) for other in roots):
                raise HostResourceError("resource cache roots overlap")
        mounts = _linux_mounts()
        local_filesystems = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "tmpfs", "ramfs", "overlay"}
        for mount_source, mount, filesystem in mounts:
            check_time()
            if any(
                (mount != root and _inside(mount, root)) or (mount_source != "/" and _inside(root, mount))
                for root in roots
            ):
                raise HostResourceError("resource cache has an ambiguous or nested mount")
            if filesystem not in local_filesystems and any(_inside(root, mount) for root in roots):
                raise HostResourceError("resource measurement requires a supported local filesystem")
        for claim in claims:
            check_time()
            if not isinstance(claim, WorkerResourceClaim):
                raise ValueError("resource claims must be WorkerResourceClaim instances")
            WorkerResourceClaim.__post_init__(claim)
            for artifact in claim.artifacts:
                check_time()
                ArtifactClaim.__post_init__(artifact)
                if artifact.cache_root not in roots:
                    raise ValueError("resource claim cache is missing its configured limit")
                previous = desired.setdefault(artifact.file_key, artifact)
                if (previous.sha256, previous.size_bytes) != (artifact.sha256, artifact.size_bytes):
                    raise ValueError("resource file has conflicting content claims")
        records = {}
        caches = []
        volumes = {}
        for root, limit in sorted(roots.items()):
            check_time()
            root_info = _checked_stat(root)
            root_device = root_info.st_dev
            seen_files = set()
            used = 0
            present = []
            pending = [root]
            while pending:
                check_time()
                path = pending.pop()
                info = _checked_stat(path)
                if info.st_dev != root_device or (path != root and os.path.ismount(path)):
                    raise HostResourceError("resource cache contains a nested volume")
                if len(records) >= maximum_entries:
                    raise HostResourceError("resource cache exceeds the entry measurement bound")
                fingerprint = _fingerprint(info)
                records[path] = fingerprint
                if stat.S_ISDIR(info.st_mode):
                    with os.scandir(path) as entries:
                        for entry in entries:
                            check_time()
                            if len(pending) + len(records) >= maximum_entries:
                                raise HostResourceError("resource cache exceeds the entry measurement bound")
                            pending.append(entry.path)
                    continue
                identity = info.st_dev, info.st_ino
                if identity not in seen_files:
                    used += _bytes(info.st_size)
                    seen_files.add(identity)
                relative = os.path.relpath(path, root).replace(os.sep, "/")
                artifact = desired.get((root, os.path.normcase(relative)))
                if artifact is None:
                    continue
                desired_path = os.path.join(root, *artifact.relative_path.split("/"))
                if _fingerprint(_checked_stat(desired_path)) != fingerprint:
                    raise HostResourceError("resource artifact path has an ambiguous file identity")
                if info.st_size != artifact.size_bytes:
                    raise HostResourceError("resource artifact size differs from its verified claim")
                verify(path, fingerprint, artifact)
                if _fingerprint(_checked_stat(path)) != fingerprint:
                    raise HostResourceError("resource artifact changed during verification")
                present.append(artifact)
            if len(present) > MAX_FILES:
                raise HostResourceError("verified resource inventory exceeds its bound")
            volume, free = _volume(root, root_info)
            available = max(0, free - disk_reserve_bytes)
            volumes[volume] = min(available, volumes.get(volume, available))
            caches.append(CacheSnapshot(root, volume, _bytes(used), limit, tuple(present)))
        # Detect mutations of unrelated files and directory membership as well as
        # desired files. No observation is a lock against subsequent writers.
        for path, fingerprint in records.items():
            check_time()
            if _fingerprint(_checked_stat(path)) != fingerprint:
                raise HostResourceError("resource cache changed during measurement; retry")
        for root in roots:
            check_time()
            if canonical_cache_root(root) != root:
                raise HostResourceError("resource cache physical path changed during measurement")
        if _linux_mounts() != mounts:
            raise HostResourceError("resource mount topology changed during measurement")
        available = _bytes(psutil.virtual_memory().available)
        result = ResourceSnapshot(
            now,
            host_limit_bytes,
            max(0, available - host_reserve_bytes),
            tuple(caches),
            tuple(VolumeSnapshot(volume, free) for volume, free in sorted(volumes.items())),
        )
        if verification_cache is not None:
            verification_cache._prune_missing({os.path.normcase(p): fp for p, fp in records.items()}, roots)
        check_time()
        return result
    except ResourceScanPending:
        raise
    except BaseException as error:
        if verification_cache is not None:
            verification_cache.discard_pending()
        if isinstance(error, (OSError, psutil.Error)):
            raise HostResourceError("resource measurement is unavailable or unreadable; retry") from None
        raise
