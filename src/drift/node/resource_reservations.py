"""Durable, conservative admission for node-owned worker generations.

The supervisor acquires before Popen and releases only after contained cleanup.
An abandoned journal entry is deliberately never recovered using a PID alone.
Staging is summed for the entire generation; this is not a loading gate or an
RSS limit. All paths/tokens/errors in this module are private control state.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from drift.model_manifest import ModelManifest
from drift.node.host_resources import VerificationCache, canonical_cache_root, snapshot_resources
from drift.node.placement_memory import MAX_CONFIG_METADATA_BYTES, MAX_INDEX_METADATA_BYTES
from drift.node.placement_resources import (
    MAX_BYTES,
    MAX_WORKERS,
    ArtifactClaim,
    WorkerResourceClaim,
    evaluate_resources,
)

_ERROR = "shared resource admission is unavailable; retained reservations require verified cleanup"
_LIMIT = "shared host memory or cache storage is unavailable for this worker"
_MAX_JOURNAL_BYTES = 32 * 1024**2


class ResourceReservationError(RuntimeError):
    def __init__(self, message, *, category="unavailable"):
        super().__init__(message)
        self.category = category if category == "capacity" else "unavailable"


def _positive(value):
    if type(value) is not int or not 0 < value <= MAX_BYTES:
        raise ValueError("invalid resource ceiling")
    return value


def _regular(path):
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & 0x400
        or info.st_nlink != 1
        or (os.name != "nt" and info.st_mode & 0o077)
    ):
        raise ValueError("unsafe private resource state")
    return info


def _fingerprint(info):
    # Reading the file can legitimately change atime (including Linux relatime).
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate resource state key")
        result[key] = value
    return result


class ResourceReservationManager:
    """One journal/OS lock for every managed worker in one node data directory.

    Admission reserves all estimates against fresh available RAM, including
    existing reservations. This intentionally double-counts resident usage rather
    than assuming a journal entry has already materialized in the OS sample.
    Journal loss/corruption and uncertain writes fail closed. There is no automatic
    stale-entry deletion: a restarted manager cannot certify old descendants.
    """

    def __init__(self, directory: Path, *, snapshot_provider=snapshot_resources, clock=time.time):
        self._directory = Path(directory).absolute()
        self._snapshot_provider = snapshot_provider
        self._clock = clock
        self._verification_cache = VerificationCache()
        self._mutex = threading.RLock()
        self._owner = uuid4().hex
        self._owned = set()
        self._pending_release = set()
        self._released = {}
        self._seen_journal = False
        self._uncertain = False
        self._acquisition_uncertain = False
        self._cache_roots = set()

    @contextmanager
    def _locked(self):
        # A stable lock inode is never removed or replaced during journal writes.
        with self._mutex:
            self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            canonical = canonical_cache_root(self._directory)
            if os.path.normcase(str(self._directory)) != canonical:
                raise ValueError("unsafe resource directory")
            if os.name != "nt" and self._directory.stat().st_mode & 0o077:
                raise ValueError("resource directory must be private")
            lock_path = self._directory / "admission.lock"
            try:
                _regular(lock_path)
            except FileNotFoundError:
                pass
            try:
                descriptor = os.open(
                    lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
                )
                created_lock = True
            except FileExistsError:
                descriptor = os.open(lock_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
                created_lock = False
            locked = False
            try:
                opened = os.fstat(descriptor)
                observed = _regular(lock_path)
                if (opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino):
                    raise ValueError("resource lock identity changed")
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                # The stable lock inode is also the durable initialization
                # marker. Once it exists, missing journal state is never empty,
                # including after a process restart. Only its exclusive creator
                # may initialize the first empty journal, under this OS lock.
                if created_lock:
                    try:
                        _regular(self._path)
                    except FileNotFoundError:
                        self._write([])
                yield
            finally:
                if locked:
                    if os.name == "nt":
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    @property
    def _path(self):
        return self._directory / "generations.json"

    def _read(self):
        try:
            before = _regular(self._path)
        except FileNotFoundError:
            raise ValueError("resource journal disappeared") from None
        if before.st_size > _MAX_JOURNAL_BYTES:
            raise ValueError("resource journal is too large")
        with self._path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ValueError("resource journal identity changed")
            raw = handle.read(_MAX_JOURNAL_BYTES + 1)
        if len(raw) > _MAX_JOURNAL_BYTES or _fingerprint(_regular(self._path)) != _fingerprint(before):
            raise ValueError("resource journal changed while reading")
        document = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(document, dict) or set(document) != {"schema_version", "reservations", "cache_roots"}:
            raise ValueError("invalid resource journal")
        if type(document["schema_version"]) is not int or document["schema_version"] != 1:
            raise ValueError("invalid resource journal version")
        entries = document["reservations"]
        roots = document["cache_roots"]
        if not isinstance(roots, list) or len(roots) > 32:
            raise ValueError("invalid remembered cache roots")
        if any(
            not isinstance(root, str)
            or not root
            or len(root) > 4096
            or not os.path.isabs(root)
            or os.path.normcase(os.path.normpath(root)) != root
            or "\0" in root
            for root in roots
        ):
            raise ValueError("invalid remembered cache path")
        if len(set(roots)) != len(roots):
            raise ValueError("duplicate remembered cache root")
        if not isinstance(entries, list) or len(entries) > MAX_WORKERS:
            raise ValueError("invalid resource journal entries")
        result = []
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"owner", "claim", "host_limit", "disk_limit"}:
                raise ValueError("invalid resource reservation")
            if not isinstance(entry["owner"], str) or len(entry["owner"]) != 32:
                raise ValueError("invalid resource owner")
            claim = entry["claim"]
            if not isinstance(claim, dict) or set(claim) != {
                "reservation_id",
                "worker_id",
                "persistent_host_bytes",
                "staging_host_bytes",
                "artifacts",
            }:
                raise ValueError("invalid resource claim")
            if not isinstance(claim["artifacts"], list) or len(claim["artifacts"]) > 4096:
                raise ValueError("invalid resource artifacts")
            claim = WorkerResourceClaim(**{**claim, "artifacts": tuple(ArtifactClaim(**a) for a in claim["artifacts"])})
            result.append({**entry, "claim": claim})
            _positive(entry["host_limit"])
            _positive(entry["disk_limit"])
        if len({entry["claim"].reservation_id for entry in result}) != len(result):
            raise ValueError("duplicate resource reservation")
        if any(artifact.cache_root not in roots for entry in result for artifact in entry["claim"].artifacts):
            raise ValueError("reservation cache root is not recorded")
        self._cache_roots = set(roots)
        self._seen_journal = True
        return result

    def _write(self, entries):
        raw = json.dumps(
            {
                "schema_version": 1,
                "cache_roots": sorted(self._cache_roots),
                "reservations": [{**e, "claim": asdict(e["claim"])} for e in entries],
            },
            sort_keys=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(raw) > _MAX_JOURNAL_BYTES:
            raise ValueError("resource journal is too large")
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=".generation-", dir=self._directory)
            with os.fdopen(descriptor, "wb") as handle:
                os.chmod(temporary, 0o600)
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
            temporary = None
            self._seen_journal = True
            if os.name != "nt":
                descriptor = os.open(self._directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except Exception:
            # Even a reported write failure may have published a complete entry.
            self._uncertain = True
            raise
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _entry(self, launch):
        if not isinstance(launch.resource_claim, WorkerResourceClaim):
            raise ValueError("worker has no resource claim")
        if launch.resource_claim.worker_id.casefold() != launch.worker_id.casefold():
            raise ValueError("resource claim worker mismatch")
        roots = {a.cache_root for a in launch.resource_claim.artifacts}
        if len(roots) != 1 or launch.placement_cache_root is None:
            raise ValueError("managed worker requires one cache root")
        if roots != {canonical_cache_root(launch.placement_cache_root)}:
            raise ValueError("resource claim cache mismatch")
        return dict(
            owner=self._owner,
            claim=replace(launch.resource_claim, reservation_id=uuid4().hex),
            host_limit=_positive(launch.max_host_memory_bytes),
            disk_limit=_positive(launch.max_disk_bytes),
        )

    def _remember_roots_locked(self, roots, entries):
        if not isinstance(roots, (tuple, list)) or len(roots) > 32:
            raise ValueError("invalid cache root registration")
        remembered = self._cache_roots | {canonical_cache_root(root) for root in roots}
        if len(remembered) > 32:
            raise ValueError("too many remembered cache roots")
        ordered = sorted(remembered)
        for index, root in enumerate(ordered):
            for other in ordered[index + 1 :]:
                try:
                    common = os.path.commonpath((root, other))
                except ValueError:
                    continue  # Distinct native drives cannot overlap.
                if common in (root, other):
                    raise ValueError("remembered cache roots overlap")
        if remembered != self._cache_roots:
            self._cache_roots = remembered
            # This inventory publication precedes snapshots, capacity denials
            # and any caller-authorized metadata writes. Never roll it back.
            try:
                self._write(entries)
            except Exception:
                # Cleanup of a different owned generation cannot certify an
                # uncertain inventory publication or reopen new admission.
                self._acquisition_uncertain = True
                raise

    def register_cache_roots(self, roots):
        """Durably remember existing physical roots before materialization.

        This records storage ownership, not permission to download or spawn.
        Use metadata_admission before synchronous config/index materialization.
        """
        try:
            with self._locked():
                if self._uncertain:
                    raise ValueError("uncertain resource state")
                entries = self._read()
                self._remember_roots_locked(roots, entries)
        except Exception:
            raise ResourceReservationError(_ERROR) from None

    @contextmanager
    def metadata_admission(self, manifest, *, cache_dir, host_limit_bytes, disk_limit_bytes):
        """Reserve exact metadata growth before yielding synchronous load permission.

        The caller creates/validates its trusted root first and must not leave
        downloads or other work running after the body exits. The temporary
        512 MiB + 32x metadata staging allowance is an estimate for parsing work,
        not a cap or accounting for metadata retained in later parent caches.
        No weights or tokenizer paths receive admission here.
        """
        try:
            root = canonical_cache_root(cache_dir)
            self.register_cache_roots((root,))
            if not isinstance(manifest, ModelManifest):
                raise ValueError("metadata admission requires a verified manifest")
            configs = manifest.artifacts_for_roles({"config"})
            indices = manifest.artifacts_for_roles({"weight_index"})
            if (
                len(configs) != 1
                or configs[0].path != "config.json"
                or len(indices) > 1
                or configs[0].size > MAX_CONFIG_METADATA_BYTES
                or any(item.size > MAX_INDEX_METADATA_BYTES for item in indices)
            ):
                raise ValueError("metadata admission exceeds the supported footprint")
            artifacts = tuple(
                ArtifactClaim(
                    root,
                    f"manifest-artifacts/{manifest.digest}/snapshot/{item.path}",
                    item.sha256,
                    item.size,
                )
                for item in configs + indices
            )
            worker = "metadata-" + manifest.digest
            launch = SimpleNamespace(
                worker_id=worker,
                resource_claim=WorkerResourceClaim(
                    "planned-" + worker,
                    worker,
                    0,
                    512 * 1024**2 + 32 * sum(item.size_bytes for item in artifacts),
                    artifacts,
                ),
                placement_cache_root=root,
                max_host_memory_bytes=_positive(host_limit_bytes),
                max_disk_bytes=_positive(disk_limit_bytes),
            )
        except Exception:
            raise ResourceReservationError(_ERROR) from None
        # Prewarm completed-file hashes outside the supervisor transition lock;
        # acquire still samples and reserves under the journal OS lock.
        self.prepare(launch)
        token = self.acquire(launch)
        try:
            yield
        finally:
            # Unlike Popen there is no ambiguous child handle: the synchronous
            # body has returned/raised before its loading reservation is freed.
            self.release(token)

    def _snapshot(self, entries, *, seconds, known_roots):
        shared_limit = min(entry["disk_limit"] for entry in entries)
        roots = {root: shared_limit for root in known_roots}
        for entry in entries:
            for artifact in entry["claim"].artifacts:
                roots[artifact.cache_root] = min(roots.get(artifact.cache_root, MAX_BYTES), entry["disk_limit"])
        return self._snapshot_provider(
            tuple(entry["claim"] for entry in entries),
            host_limit_bytes=min(entry["host_limit"] for entry in entries),
            cache_limits=roots,
            now=self._clock(),
            verification_cache=self._verification_cache,
            maximum_scan_seconds=seconds,
        )

    def prepare(self, launch):
        """Warm file verifications outside supervisor/store transition locks.

        This makes no reservation and confers no permission to spawn. A fresh
        bounded snapshot and the entire journal are rechecked by acquire().
        """
        try:
            with self._locked():
                if self._uncertain:
                    raise ValueError("uncertain resource state")
                entries = self._read()
                entry = self._entry(launch)
                self._remember_roots_locked(tuple({a.cache_root for a in entry["claim"].artifacts}), entries)
                known_roots = tuple(self._cache_roots)
            self._snapshot([*entries, entry], seconds=30.0, known_roots=known_roots)
        except Exception:
            raise ResourceReservationError(_ERROR) from None

    def acquire(self, launch):
        try:
            with self._locked():
                if self._uncertain:
                    raise ValueError("uncertain resource state")
                entries = self._read()
                entry = self._entry(launch)
                self._remember_roots_locked(tuple({a.cache_root for a in entry["claim"].artifacts}), entries)
                if len(entries) >= MAX_WORKERS:
                    raise ResourceReservationError(_LIMIT, category="capacity")
                snapshot = self._snapshot([*entries, entry], seconds=2.0, known_roots=tuple(self._cache_roots))
                # Old entries can be waiting to spawn or orphaned. Do not assume
                # their persistent estimate is included in measured usage yet.
                snapshot = replace(
                    snapshot,
                    host_available_bytes=max(
                        0, snapshot.host_available_bytes - sum(e["claim"].persistent_host_bytes for e in entries)
                    ),
                )
                feasible = evaluate_resources(
                    (entry["claim"],),
                    retained_claims=tuple(e["claim"] for e in entries),
                    snapshot=snapshot,
                    now=self._clock(),
                    maximum_age_seconds=5.0,
                    serialized_loading=False,
                )
                # max_disk_space is one node allowance, even when workers use
                # distinct roots or volumes. Stopped generations leave cached
                # files, so the durable root inventory remains part of scans.
                shared_disk_limit = min(e["disk_limit"] for e in [*entries, entry])
                if (
                    not feasible.admitted
                    or sum(value for _, value in feasible.cache_projected_bytes) > shared_disk_limit
                ):
                    raise ResourceReservationError(_LIMIT, category="capacity")
                self._cache_roots.update(cache.root for cache in snapshot.caches)
                if len(self._cache_roots) > 32:
                    raise ResourceReservationError(_ERROR)
                try:
                    self._write([*entries, entry])
                except Exception:
                    self._acquisition_uncertain = True
                    raise
                token = entry["claim"].reservation_id
                self._owned.add(token)
                return token
        except ResourceReservationError:
            raise
        except Exception:
            raise ResourceReservationError(_ERROR) from None

    def release(self, token):
        """Caller certifies contained cleanup; foreign/unknown tokens never clear state."""
        try:
            with self._locked():
                if token in self._released:
                    return
                if token not in self._owned:
                    raise ValueError("unknown resource reservation")
                entries = self._read()
                matches = [e for e in entries if e["claim"].reservation_id == token and e["owner"] == self._owner]
                if len(matches) > 1 or (not matches and token not in self._pending_release):
                    raise ValueError("resource reservation is missing")
                self._pending_release.add(token)
                # A preceding release may have published its removal before
                # fsync failed. Rewrite durably on retry; this is permitted only
                # for this owner's known cleanup-certified release attempt.
                self._write([e for e in entries if e["claim"].reservation_id != token])
                self._owned.remove(token)
                self._pending_release.discard(token)
                if len(self._released) >= 1024:
                    self._released.pop(next(iter(self._released)))
                self._released[token] = True
                self._uncertain = self._acquisition_uncertain or bool(self._pending_release)
        except Exception:
            raise ResourceReservationError(_ERROR) from None
