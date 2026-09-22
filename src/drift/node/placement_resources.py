"""Pure aggregate resource feasibility, not a runtime reservation or load lock.

Callers supply freshly measured, physically canonical cache roots and volume IDs.
Only lexical canonicality is checked here: no filesystem, hardware or network I/O
is performed. Keep old claims in ``retained_claims`` until containment cleanup is
verified. Stopping a worker never deletes cache usage from an observation.

Host staging is summed unless a caller has enforced one cross-process loading
lock across ALL included workers, including retained loads. Cache read/write
locks alone do not provide that host serialization. Fresh measurements must be
rechecked under the eventual reservation/load coordinator before downloading or
spawning. This module neither enforces bandwidth nor treats samples as rate caps.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass

MAX_WORKERS = 16
MAX_FILES = 4096
MAX_BYTES = 2**63 - 1


def _bytes(value, name):
    if type(value) is not int or not 0 <= value <= MAX_BYTES:
        raise ValueError(f"{name} must be a bounded non-negative integer")
    return value


def _label(value, name):
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is None:
        raise ValueError(f"{name} must be a bounded identifier")


def _root(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or "\0" in value
        or not os.path.isabs(value)
        or os.path.normcase(os.path.normpath(value)) != value
    ):
        raise ValueError("cache root must be a canonical absolute native path")


def _items(value, kind, limit, name):
    if not isinstance(value, (tuple, list)) or len(value) > limit or any(not isinstance(v, kind) for v in value):
        raise ValueError(f"{name} must be a bounded sequence of {kind.__name__}")
    return tuple(value)


def _time(value, name):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite non-negative timestamp or duration")


@dataclass(frozen=True)
class ArtifactClaim:
    cache_root: str
    relative_path: str
    sha256: str
    size_bytes: int

    def __post_init__(self):
        _root(self.cache_root)
        path = self.relative_path
        if (
            not isinstance(path, str)
            or not path
            or len(path) > 1024
            or any(ord(character) < 32 or character in '\\:<>"|?*' for character in path)
            or any(part in ("", ".", "..") or part.endswith((".", " ")) for part in path.split("/"))
            or any(
                re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])", part.split(".")[0]) for part in path.split("/")
            )
        ):
            raise ValueError("artifact path must be canonical and relative to its cache root")
        if not isinstance(self.sha256, str) or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None:
            raise ValueError("artifact SHA256 must be lowercase hexadecimal")
        _bytes(self.size_bytes, "artifact size")

    @property
    def file_key(self):
        # A hash does not prove two physical copies share storage. Only the same
        # target path is deduplicated before materialization. Verified hardlinks
        # already on disk are reflected in measured used_bytes, not assumed here.
        return self.cache_root, os.path.normcase(self.relative_path)


@dataclass(frozen=True)
class WorkerResourceClaim:
    reservation_id: str
    worker_id: str
    persistent_host_bytes: int
    staging_host_bytes: int
    artifacts: tuple[ArtifactClaim, ...] = ()

    def __post_init__(self):
        _label(self.reservation_id, "reservation_id")
        _label(self.worker_id, "worker_id")
        _bytes(self.persistent_host_bytes, "persistent host bytes")
        _bytes(self.staging_host_bytes, "staging host bytes")
        object.__setattr__(self, "artifacts", _items(self.artifacts, ArtifactClaim, MAX_FILES, "artifacts"))


@dataclass(frozen=True)
class CacheSnapshot:
    root: str
    volume_id: str
    used_bytes: int
    configured_limit_bytes: int
    verified_present_artifacts: tuple[ArtifactClaim, ...] = ()

    def __post_init__(self):
        _root(self.root)
        _label(self.volume_id, "volume_id")
        _bytes(self.used_bytes, "cache usage")
        _bytes(self.configured_limit_bytes, "cache limit")
        present = _items(self.verified_present_artifacts, ArtifactClaim, MAX_FILES, "verified artifacts")
        if any(item.cache_root != self.root for item in present):
            raise ValueError("verified artifact must belong to the observed cache root")
        object.__setattr__(self, "verified_present_artifacts", present)


@dataclass(frozen=True)
class VolumeSnapshot:
    volume_id: str
    available_bytes: int

    def __post_init__(self):
        _label(self.volume_id, "volume_id")
        _bytes(self.available_bytes, "volume availability")


@dataclass(frozen=True)
class ResourceSnapshot:
    observed_at: float
    host_configured_limit_bytes: int
    host_available_bytes: int
    caches: tuple[CacheSnapshot, ...]
    volumes: tuple[VolumeSnapshot, ...]

    def __post_init__(self):
        _time(self.observed_at, "observed_at")
        _bytes(self.host_configured_limit_bytes, "host limit")
        _bytes(self.host_available_bytes, "host availability")
        object.__setattr__(self, "caches", _items(self.caches, CacheSnapshot, MAX_WORKERS * 2, "caches"))
        object.__setattr__(self, "volumes", _items(self.volumes, VolumeSnapshot, MAX_WORKERS * 2, "volumes"))


@dataclass(frozen=True)
class ResourceFeasibility:
    admitted: bool
    reasons: tuple[str, ...]
    persistent_host_bytes: int
    staging_host_bytes: int
    additional_host_bytes: int
    artifact_union_bytes: int
    cache_growth_bytes: tuple[tuple[str, int], ...]
    cache_projected_bytes: tuple[tuple[str, int], ...]
    volume_growth_bytes: tuple[tuple[str, int], ...]


def _union(files):
    result = {}
    for artifact in files:
        old = result.setdefault(artifact.file_key, artifact)
        if (old.sha256, old.size_bytes) != (artifact.sha256, artifact.size_bytes):
            raise ValueError("one cache file has conflicting hash or size claims")
    return result


def evaluate_resources(
    final_claims,
    *,
    retained_claims=(),
    snapshot: ResourceSnapshot,
    now: float,
    maximum_age_seconds: float,
    serialized_loading: bool = False,
) -> ResourceFeasibility:
    """Evaluate final plus still-reserved resources, without changing any state.

    Reservation IDs identify launch generations, not merely workers. Identical
    final/retained claims share an ID; changed claims must use new IDs and overlap
    until old cleanup is proven. Retained persistent memory is assumed already
    charged to observed availability; all new persistent memory and ALL staging
    are charged again. No speculative freed-memory or cache-eviction credit.

    Available bytes must already exclude operator/OS safety reserves. Verified
    present files require exact size/hash evidence, not mere existence. Existing
    partials stay in used_bytes; absent final files reserve their full size, a
    conservative bound even when a loader can resume a partial. Different roots
    on one volume share the same availability ceiling.
    """
    final = _items(final_claims, WorkerResourceClaim, MAX_WORKERS, "final claims")
    retained = _items(retained_claims, WorkerResourceClaim, MAX_WORKERS, "retained claims")
    if not isinstance(snapshot, ResourceSnapshot) or type(serialized_loading) is not bool:
        raise ValueError("resource snapshot and explicit serialization flag are required")
    _time(now, "now")
    _time(maximum_age_seconds, "maximum_age_seconds")
    if not 0 <= now - snapshot.observed_at <= maximum_age_seconds:
        raise ValueError("resource snapshot is stale or from the future")
    if len({claim.worker_id.casefold() for claim in final}) != len(final):
        raise ValueError("final worker IDs must be unique")
    claims = {}
    for group in (retained, final):
        if len({claim.reservation_id for claim in group}) != len(group):
            raise ValueError("reservation IDs must be unique within each map")
        for claim in group:
            if claims.setdefault(claim.reservation_id, claim) != claim:
                raise ValueError("a retained reservation cannot be replaced before verified release")
    caches = {cache.root: cache for cache in snapshot.caches}
    volumes = {volume.volume_id: volume for volume in snapshot.volumes}
    if len(caches) != len(snapshot.caches) or len(volumes) != len(snapshot.volumes):
        raise ValueError("cache roots and volume IDs must be unique")
    roots = sorted(caches)
    for index, root in enumerate(roots):
        for other in roots[index + 1 :]:
            try:
                common = os.path.commonpath((root, other))
            except ValueError:  # Different native drives.
                continue
            if common in (root, other):
                raise ValueError("cache roots must not overlap")
    if any(cache.volume_id not in volumes for cache in caches.values()):
        raise ValueError("cache volume availability is missing")
    files = _union(artifact for claim in claims.values() for artifact in claim.artifacts)
    present = _union(artifact for cache in caches.values() for artifact in cache.verified_present_artifacts)
    _union((*files.values(), *present.values()))  # Verify stored and desired content agree.
    for root, cache in caches.items():
        # Even verified hardlink aliases cannot make different content occupy
        # the same file. Measured logical file usage must cover this lower bound;
        # no extra copy is assumed for an existing same-content alias.
        content_sizes = {}
        for artifact in cache.verified_present_artifacts:
            if content_sizes.setdefault(artifact.sha256, artifact.size_bytes) != artifact.size_bytes:
                raise ValueError("verified content hash has conflicting sizes")
        if sum(content_sizes.values()) > cache.used_bytes:
            raise ValueError("cache usage is below its verified content inventory")
    if any(artifact.cache_root not in caches for artifact in files.values()):
        raise ValueError("artifact cache observation is missing")
    growth = {root: 0 for root in caches}
    for key, artifact in files.items():
        if key not in present:
            growth[artifact.cache_root] += artifact.size_bytes
    projected = {root: cache.used_bytes + growth[root] for root, cache in caches.items()}
    volume_growth = {volume: 0 for volume in volumes}
    for root, cache in caches.items():
        volume_growth[cache.volume_id] += growth[root]
    persistent = sum(claim.persistent_host_bytes for claim in claims.values())
    staging_sizes = [claim.staging_host_bytes for claim in claims.values()]
    staging = max(staging_sizes, default=0) if serialized_loading else sum(staging_sizes)
    retained_ids = {claim.reservation_id for claim in retained}
    additional_host = staging + sum(
        claim.persistent_host_bytes for key, claim in claims.items() if key not in retained_ids
    )
    reasons = []
    if persistent + staging > snapshot.host_configured_limit_bytes:
        reasons.append("configured host memory ceiling exceeded")
    if additional_host > snapshot.host_available_bytes:
        reasons.append("fresh available host memory exceeded")
    if any(projected[root] > cache.configured_limit_bytes for root, cache in caches.items()):
        reasons.append("configured cache storage ceiling exceeded")
    if any(volume_growth[key] > volume.available_bytes for key, volume in volumes.items()):
        reasons.append("fresh available volume storage exceeded")
    return ResourceFeasibility(
        not reasons,
        tuple(reasons),
        persistent,
        staging,
        additional_host,
        sum(artifact.size_bytes for artifact in files.values()),
        tuple(sorted(growth.items())),
        tuple(sorted(projected.items())),
        tuple(sorted(volume_growth.items())),
    )
