"""Durable, conservative admission for node-owned worker generations.

The supervisor acquires before Popen and releases only after contained cleanup.
An abandoned journal entry is deliberately never recovered using a PID alone.
Staging is summed for the entire generation, including after loading readiness;
the cooperative loading gate is not an RSS limit. Paths/tokens are private.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import tempfile
import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from drift.model_manifest import ModelManifest
from drift.node.host_resources import (
    ResourceScanCancelled,
    ResourceScanPending,
    VerificationCache,
    canonical_cache_root,
    snapshot_resources,
)
from drift.node.placement_memory import MAX_CONFIG_METADATA_BYTES, MAX_INDEX_METADATA_BYTES
from drift.node.placement_resources import (
    MAX_BYTES,
    MAX_WORKERS,
    ArtifactClaim,
    WorkerResourceClaim,
    evaluate_resources,
)
from drift.node.worker_loading import (
    LoadingBinding,
    cleanup_loading_binding,
    create_loading_binding,
    initialize_loading_gate,
    loading_claim_digest,
    loading_gate,
)

_ERROR = "shared resource admission is unavailable; retained reservations require verified cleanup"
_LIMIT = "shared host memory or cache storage is unavailable for this worker"
_MAX_JOURNAL_BYTES = 32 * 1024**2


class ResourceReservationError(RuntimeError):
    def __init__(self, message, *, category="unavailable"):
        super().__init__(message)
        self.category = category if category == "capacity" else "unavailable"


class _JournalBusy(RuntimeError):
    pass


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
    Journal loss/corruption and uncertain writes fail closed. Managed production
    recovery requires durable owner exclusion and the recorded native containment
    contract; legacy entries never gain authority from a missing PID.
    """

    def __init__(
        self,
        directory: Path,
        *,
        snapshot_provider=snapshot_resources,
        clock=time.time,
        loading_protocol=False,
        recovery_protocol=False,
        worker_cgroup_root=None,
        storage_binding=None,
    ):
        if type(loading_protocol) is not bool or type(recovery_protocol) is not bool:
            raise ValueError("resource protocols must be booleans")
        if recovery_protocol and not loading_protocol:
            raise ValueError("resource recovery requires the managed loading protocol")
        if worker_cgroup_root is not None:
            from drift.node.resource_recovery_config import normalize_worker_cgroup_root

            if not recovery_protocol:
                raise ValueError("worker cgroup containment requires resource recovery")
            worker_cgroup_root = normalize_worker_cgroup_root(worker_cgroup_root)
        self._worker_cgroup_root = worker_cgroup_root
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
        self._storage_binding = storage_binding
        self._uncertain = False
        self._acquisition_uncertain = False
        self._cache_roots = set()
        self._loading_protocol = loading_protocol
        self._loading_bindings = {}
        self._recovery_protocol = recovery_protocol
        self._owner_lease = None
        self._recovery_containments = {}
        self._journal_version = 2 if recovery_protocol else 1
        self._recovery_status = dict(state="checking", reason="checking", retryable=True)
        self._recovery_stop = threading.Event()
        self._recovery_wake = threading.Event()
        self._recovery_thread = None
        self._closed = False

    @property
    def recovery_protocol_enabled(self):
        return self._recovery_protocol

    def recovery_snapshot(self):
        """Return cached public state without journal, process or lock I/O."""
        return dict(self._recovery_status)

    def recovery_containment_for_token(self, token):
        """Pure lookup; the acquired generation already owns this OS boundary."""
        if token not in self._owned or token not in self._recovery_containments:
            raise ResourceReservationError(_ERROR)
        return self._recovery_containments[token]

    def _recovery_state(self, reason):
        allowed = {
            "none",
            "checking",
            "active_owner",
            "cleanup_pending",
            "legacy_state",
            "unverifiable_state",
            "unsupported_platform",
        }
        if reason not in allowed:
            reason = "unverifiable_state"
        self._recovery_status = dict(
            state="ready" if reason == "none" else "checking" if reason == "checking" else "blocked",
            reason=reason,
            retryable=reason in {"checking", "active_owner", "cleanup_pending"},
        )
        if self._recovery_status["retryable"]:
            self._recovery_wake.set()

    def start_recovery(self):
        """Start one bounded owner, including when sharing is disabled."""
        if not self._recovery_protocol or self._closed or self._recovery_thread is not None:
            return

        def run():
            while not self._recovery_stop.is_set():
                self._recovery_wake.wait()
                self._recovery_wake.clear()
                if self._recovery_stop.is_set():
                    return
                if not self.recover(cancelled=self._recovery_stop.is_set) and self._recovery_status["retryable"]:
                    self._recovery_stop.wait(0.2)
                    self._recovery_wake.set()

        runner = threading.Thread(target=run, name="resource-recovery", daemon=True)
        self._recovery_thread = runner
        self._recovery_wake.set()
        try:
            runner.start()
        except Exception:
            self._recovery_thread = None
            self._recovery_state("unverifiable_state")

    def close(self, timeout=2.0):
        """Stop recovery and acknowledge only a globally empty durable state.

        The journal is read while holding its native OS lock, after new work has
        been excluded. An explicit Linux profile also proves the complete
        worker subtree empty. False retains the lease and OS handles; callers
        must treat a lost reply or process exit as pending, never as success.
        """
        self._closed = True
        self._recovery_stop.set()
        self._recovery_wake.set()
        # Even timeout=0 retains one bounded nonblocking-style lock attempt.
        deadline = time.monotonic() + max(0.05, timeout)
        runner = self._recovery_thread
        if runner is not None and runner is not threading.current_thread():
            runner.join(max(0.0, deadline - time.monotonic()))
            if runner.is_alive():
                return False

        def deadline_reached():
            return time.monotonic() >= deadline

        while True:
            try:
                with self.drain_guard(cancelled=deadline_reached):
                    if self._owner_lease is not None:
                        self._owner_lease.close()
                        self._owner_lease = None
                    return True
            except _JournalBusy:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                time.sleep(min(0.05, remaining))
            except Exception:
                return False

    @contextmanager
    def drain_guard(self, *, cancelled=None):
        """Hold the global empty-journal proof while a caller commits its ack.

        This does not exclude a future node start. The caller separately owns
        node admission and complete node-tree death for the whole transaction.
        """
        with self._locked(cancelled):
            if self._read() or self._owned or self._pending_release or self._uncertain or self._recovery_containments:
                raise ResourceReservationError(_ERROR)
            if self._worker_cgroup_root is not None:
                from drift.node.linux_cgroup_recovery import verify_cgroup_tree_empty

                verify_cgroup_tree_empty(self._worker_cgroup_root)
            yield

    @staticmethod
    def _recovery_digest(entry, kind):
        payload = dict(
            schema_version=1,
            owner=entry["owner"],
            claim=asdict(entry["claim"]),
            host_limit=entry["host_limit"],
            disk_limit=entry["disk_limit"],
            loading=entry.get("loading"),
            kind=kind,
        )
        return (
            "sha256:"
            + hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
            ).hexdigest()
        )

    def _loading_from_entry(self, entry):
        value = entry.get("loading")
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) != {"nonce", "binding_digest"}:
            raise ValueError("invalid retained loading binding")
        return LoadingBinding(
            os.path.normcase(os.path.normpath(str(self._directory / "loading"))),
            entry["claim"].reservation_id,
            value["nonce"],
            value["binding_digest"],
        )

    def _ensure_recovery_owner(self):
        from drift.node.resource_recovery import open_owner_lease

        if self._closed:
            raise ResourceReservationError(_ERROR)
        if self._owner_lease is None:
            self._owner_lease = open_owner_lease(self._directory / "owners", self._owner)
        self._owner_lease.require_live()

    def _discard_unpublished(self, token):
        """Only before journal publication and before any spawn is possible."""
        try:
            containment = self._recovery_containments.get(token)
            if containment is not None:
                containment.close()
                self._recovery_containments.pop(token, None)
            loading = self._loading_bindings.get(token)
            if loading is not None:
                cleanup_loading_binding(loading)
                self._loading_bindings.pop(token, None)
        except BaseException:
            self._uncertain = self._acquisition_uncertain = True
            self._recovery_state("unverifiable_state")
            raise

    def _recover_locked(self, entries, cancelled=None):
        if not self._recovery_protocol:
            return entries
        from drift.node.linux_cgroup_recovery import recover_linux_cgroup, validate_cgroup_profile
        from drift.node.resource_recovery import GenerationRecoveryBinding, acquire_recovery_guard
        from drift.node.worker_recovery_containment import recover_windows_containment

        try:
            # Explicit profile selection is binding even with an empty journal:
            # metadata and workers cannot fall back after a capability failure.
            if self._worker_cgroup_root is not None:
                self._check_cancelled(cancelled)
                validate_cgroup_profile(self._worker_cgroup_root, require_unfrozen=False)
            self._ensure_recovery_owner()
            for entry in tuple(entries):
                self._check_cancelled(cancelled)
                if entry["owner"] == self._owner:
                    continue
                if entry.get("recovery") is None:
                    self._recovery_state("legacy_state")
                    raise ResourceReservationError(_ERROR)
                binding = GenerationRecoveryBinding.from_json(entry["recovery"])
                digest = self._recovery_digest(entry, binding.kind)
                with acquire_recovery_guard(
                    self._directory / "owners", binding, expected_claim_digest=digest, cancelled=cancelled
                ) as guard:
                    proof = guard.prove_empty(
                        windows_probe=recover_windows_containment, linux_probe=recover_linux_cgroup
                    )
                    self._check_cancelled(cancelled)
                    loading = self._loading_from_entry(entry)
                    if loading is not None:
                        cleanup_loading_binding(loading)
                    guard.require_proof(proof)
                    retained = [e for e in entries if e["claim"].reservation_id != entry["claim"].reservation_id]
                    self._write(retained)
                    entries = retained
            # Freezing must never prevent orphan cleanup. After that cleanup,
            # admission remains unavailable until the explicit root can run
            # the native child's inherited-lease closure handshake.
            if self._worker_cgroup_root is not None:
                self._check_cancelled(cancelled)
                validate_cgroup_profile(self._worker_cgroup_root)
            self._recovery_state("none")
            return entries
        except ResourceScanCancelled:
            raise
        except ResourceReservationError:
            raise
        except Exception as exc:
            self._recovery_state(getattr(exc, "reason", "unverifiable_state"))
            raise ResourceReservationError(_ERROR) from None

    def recover(self, *, cancelled=None):
        """Reconcile only generations whose complete death is proven under exclusion."""
        if not self._recovery_protocol:
            return True
        try:
            with self._locked(cancelled):
                if self._closed or self._uncertain:
                    raise ValueError("resource state cannot be recovered by this owner")
                self._recover_locked(self._read(), cancelled)
            return True
        except ResourceScanCancelled:
            return False
        except _JournalBusy:
            self._recovery_state("checking")
            return False
        except ResourceReservationError:
            return False
        except Exception:
            self._recovery_state("unverifiable_state")
            return False

    @property
    def loading_protocol_enabled(self):
        return self._loading_protocol

    def loading_binding_for_token(self, token):
        """Pure in-memory lookup after successful acquisition; no lock or OS I/O.

        The supervisor owns the token until certified cleanup. Publication occurs
        before acquire returns; cleanup only removes it after release succeeds.
        """
        if not self._loading_protocol or token not in self._owned or token not in self._loading_bindings:
            raise ResourceReservationError(_ERROR)
        return self._loading_bindings[token]

    def _check_cancelled(self, cancelled):
        if cancelled is not None and cancelled():
            self._verification_cache.discard_pending()
            raise ResourceScanCancelled("resource admission was cancelled")

    @contextmanager
    def _local_lock(self, cancelled):
        # A different admitted generation may be blocked in kernel filesystem
        # I/O. Cancellation must not wait for that operation's Python mutex.
        self._check_cancelled(cancelled)
        while not self._mutex.acquire(timeout=0.05):
            self._check_cancelled(cancelled)
        try:
            self._check_cancelled(cancelled)
            yield
        finally:
            self._mutex.release()

    @contextmanager
    def _locked(self, cancelled=None):
        # A stable lock inode is never removed or replaced during journal writes.
        with self._local_lock(cancelled):
            if self._storage_binding is None:
                self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            else:
                self._validate_storage()
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
            if self._storage_binding is not None:
                descriptor = os.open(lock_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
                created_lock = False
            else:
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
                if self._storage_binding is None:
                    directory = self._directory.stat()
                    self._storage_binding = dict(
                        directory=(directory.st_dev, directory.st_ino), lease=(opened.st_dev, opened.st_ino)
                    )
                self._validate_storage()
                try:
                    if os.name == "nt":
                        import msvcrt

                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        if self._recovery_protocol:
                            self._recovery_state("checking")
                        raise _JournalBusy() from None
                    raise
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
                # Creating the initialization marker commits us to establishing
                # its empty journal; cancellation must not leave a lost-journal
                # state in an otherwise brand-new node directory.
                self._check_cancelled(cancelled)
                yield
                self._validate_storage()
            finally:
                if locked:
                    if os.name == "nt":
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def _validate_storage(self):
        directory = self._directory.lstat()
        if not stat.S_ISDIR(directory.st_mode) or self._directory.is_symlink():
            raise ValueError("resource directory changed")
        lease = _regular(self._directory / "admission.lock")
        observed = dict(directory=(directory.st_dev, directory.st_ino), lease=(lease.st_dev, lease.st_ino))
        if observed != self._storage_binding:
            raise ValueError("resource storage identity changed")

    @property
    def _path(self):
        return self._directory / "generations.json"

    def _read(self):
        try:
            return self._read_journal()
        except Exception:
            if self._recovery_protocol:
                self._recovery_state("unverifiable_state")
            raise

    def _read_journal(self):
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
        if type(document["schema_version"]) is not int or document["schema_version"] not in (1, 2):
            raise ValueError("invalid resource journal version")
        version = document["schema_version"]
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
            expected = {"owner", "claim", "host_limit", "disk_limit"}
            if version == 2:
                expected |= {"recovery", "loading"}
            if not isinstance(entry, dict) or set(entry) != expected:
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
            parsed = {**entry, "claim": claim}
            loading = self._loading_from_entry(parsed)
            if version == 2 and entry["recovery"] is not None:
                from drift.node.resource_recovery import GenerationRecoveryBinding

                binding = GenerationRecoveryBinding.from_json(entry["recovery"])
                if (
                    binding.owner.owner_id != entry["owner"]
                    or binding.reservation_id != claim.reservation_id
                    or binding.claim_digest != self._recovery_digest(parsed, binding.kind)
                    or (binding.kind == "metadata") != (loading is None)
                ):
                    raise ValueError("resource recovery binding does not match its claim")
            elif loading is not None:
                raise ValueError("loading cleanup lacks recovery authority")
            result.append(parsed)
            _positive(entry["host_limit"])
            _positive(entry["disk_limit"])
        if len({entry["claim"].reservation_id for entry in result}) != len(result):
            raise ValueError("duplicate resource reservation")
        if any(artifact.cache_root not in roots for entry in result for artifact in entry["claim"].artifacts):
            raise ValueError("reservation cache root is not recorded")
        self._cache_roots = set(roots)
        self._journal_version = max(self._journal_version, version)
        self._seen_journal = True
        return result

    def _write(self, entries):
        raw = json.dumps(
            {
                "schema_version": self._journal_version,
                "cache_roots": sorted(self._cache_roots),
                "reservations": [
                    {
                        **e,
                        "claim": asdict(e["claim"]),
                        **(
                            {"recovery": e.get("recovery"), "loading": e.get("loading")}
                            if self._journal_version == 2
                            else {}
                        ),
                    }
                    for e in entries
                ],
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
            if self._recovery_protocol:
                self._recovery_state("unverifiable_state")
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

    def register_cache_roots(self, roots, *, cancelled=None):
        """Durably remember existing physical roots before materialization.

        This records storage ownership, not permission to download or spawn.
        Use metadata_admission before synchronous config/index materialization.
        """
        try:
            with self._locked(cancelled):
                if self._uncertain:
                    raise ValueError("uncertain resource state")
                entries = self._recover_locked(self._read(), cancelled)
                self._check_cancelled(cancelled)
                self._remember_roots_locked(roots, entries)
        except ResourceScanCancelled:
            if cancelled is not None:
                raise
            raise ResourceReservationError(_ERROR) from None
        except Exception:
            raise ResourceReservationError(_ERROR) from None

    @contextmanager
    def metadata_admission(self, manifest, *, cache_dir, host_limit_bytes, disk_limit_bytes, cancelled=None):
        """Reserve exact metadata growth before yielding synchronous load permission.

        The caller creates/validates its trusted root first and must not leave
        downloads or other work running after the body exits. The temporary
        512 MiB + 32x metadata staging allowance is an estimate for parsing work,
        not a cap or accounting for metadata retained in later parent caches.
        No weights or tokenizer paths receive admission here.
        """
        try:
            self._check_cancelled(cancelled)
            root = canonical_cache_root(cache_dir)
            self.register_cache_roots((root,), cancelled=cancelled)
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
        self.prepare(launch, cancelled=cancelled)
        token = self._acquire(launch, cancelled=cancelled, prepare_loading=False)
        try:
            self._check_cancelled(cancelled)
            gate = (
                loading_gate(self._directory / "loading", cancelled=cancelled)
                if self._loading_protocol
                else nullcontext()
            )
            with gate:
                self._check_cancelled(cancelled)
                yield
        finally:
            # Unlike Popen there is no ambiguous child handle: the synchronous
            # body has returned/raised before its loading reservation is freed.
            self.release(token)

    def _snapshot(self, entries, *, seconds, known_roots, cancelled=None):
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
            **({"cancelled": cancelled} if cancelled is not None else {}),
        )

    def prepare(self, launch, *, cancelled=None):
        """Warm file verifications outside supervisor/store transition locks.

        This makes no reservation and confers no permission to spawn. A fresh
        bounded snapshot and the entire journal are rechecked by acquire().
        """
        try:
            with self._locked(cancelled):
                if self._uncertain:
                    raise ValueError("uncertain resource state")
                entries = self._recover_locked(self._read(), cancelled)
                entry = self._entry(launch)
                self._check_cancelled(cancelled)
                self._remember_roots_locked(tuple({a.cache_root for a in entry["claim"].artifacts}), entries)
                known_roots = tuple(self._cache_roots)
            self._snapshot([*entries, entry], seconds=30.0, known_roots=known_roots, cancelled=cancelled)
            self._check_cancelled(cancelled)
        except (ResourceScanPending, ResourceScanCancelled):
            if cancelled is not None:
                raise
            raise ResourceReservationError(_ERROR) from None
        except Exception:
            raise ResourceReservationError(_ERROR) from None

    def acquire(self, launch, *, cancelled=None):
        return self._acquire(launch, cancelled=cancelled, prepare_loading=self._loading_protocol)

    def _acquire(self, launch, *, cancelled=None, prepare_loading=False):
        try:
            with self._locked(cancelled):
                if self._uncertain:
                    raise ValueError("uncertain resource state")
                entries = self._recover_locked(self._read(), cancelled)
                entry = self._entry(launch)
                self._check_cancelled(cancelled)
                self._remember_roots_locked(tuple({a.cache_root for a in entry["claim"].artifacts}), entries)
                if len(entries) >= MAX_WORKERS:
                    raise ResourceReservationError(_LIMIT, category="capacity")
                snapshot = self._snapshot(
                    [*entries, entry], seconds=2.0, known_roots=tuple(self._cache_roots), cancelled=cancelled
                )
                self._check_cancelled(cancelled)
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
                self._check_cancelled(cancelled)
                token = entry["claim"].reservation_id
                if prepare_loading:
                    digest = loading_claim_digest(
                        manifest_digest=launch.placement_manifest_digest,
                        block_indices=launch.block_indices,
                        artifact_bytes=launch.placement_artifact_bytes,
                        artifact_set_digest=launch.placement_artifact_set_digest,
                        cache_root=launch.placement_cache_root,
                    )
                if self._loading_protocol:
                    try:
                        # Metadata and child admission initialize under the same
                        # journal lock; neither may race the first gate marker.
                        initialize_loading_gate(self._directory / "loading")
                    except Exception:
                        self._uncertain = self._acquisition_uncertain = True
                        if self._recovery_protocol:
                            self._recovery_state("unverifiable_state")
                        raise
                if prepare_loading:
                    try:
                        binding = create_loading_binding(self._directory / "loading", token, digest)
                        self._loading_bindings[token] = binding
                    except Exception:
                        self._uncertain = self._acquisition_uncertain = True
                        if self._recovery_protocol:
                            self._recovery_state("unverifiable_state")
                        raise
                    try:
                        self._check_cancelled(cancelled)
                    except BaseException:
                        # No journal publication/Popen occurred; this generation
                        # can be removed even if its cancellation arrived late.
                        try:
                            cleanup_loading_binding(binding)
                            self._loading_bindings.pop(token, None)
                        except Exception:
                            self._uncertain = self._acquisition_uncertain = True
                        raise
                if self._recovery_protocol:
                    from drift.node.resource_recovery import make_generation_binding
                    from drift.node.worker_recovery_containment import create_recovery_containment

                    kind = "worker" if prepare_loading else "metadata"
                    entry["loading"] = (
                        {"nonce": binding.nonce, "binding_digest": binding.binding_digest} if prepare_loading else None
                    )
                    try:
                        prepared = None
                        if prepare_loading and self._worker_cgroup_root is not None:
                            from drift.node.linux_cgroup_recovery import prepare_generation

                            prepared = prepare_generation(
                                self._worker_cgroup_root, self._owner_lease.owner_binding, token
                            )
                            self._recovery_containments[token] = prepared
                        recovery = make_generation_binding(
                            self._owner_lease.owner_binding,
                            token,
                            kind=kind,
                            claim_digest=self._recovery_digest(entry, kind),
                            **({"linux_cgroup": prepared.identity} if prepared is not None else {}),
                        )
                        entry["recovery"] = recovery.to_json()
                        if prepared is not None:
                            prepared.bind(recovery)
                        elif prepare_loading:
                            self._recovery_containments[token] = create_recovery_containment(recovery)
                        self._check_cancelled(cancelled)
                    except BaseException as exc:
                        # No journal publication or child creation has occurred.
                        self._discard_unpublished(token)
                        if not isinstance(exc, ResourceScanCancelled):
                            self._recovery_state(getattr(exc, "reason", "unverifiable_state"))
                        raise
                try:
                    self._write([*entries, entry])
                except Exception:
                    self._uncertain = self._acquisition_uncertain = True
                    raise
                self._owned.add(token)
                # Once publication succeeds, always hand back the token even if
                # cancellation raced the write. The asynchronous owner must
                # release it; raising here would lose a known cleanup handle.
                return token
        except (ResourceScanPending, ResourceScanCancelled):
            if cancelled is not None:
                raise
            raise ResourceReservationError(_ERROR) from None
        except ResourceReservationError:
            raise
        except Exception:
            raise ResourceReservationError(_ERROR) from None

    def acquire_cancellable(self, launch, cancel_event):
        """Run only on a supervisor-owned background operation.

        Retry incomplete cooperative verification, retaining bounded hash
        progress. Each attempt rereads the journal and samples live resources;
        errors and capacity denials are never converted into retry success.
        Cancellation can interrupt mutex waits and user-space scanning, not a
        kernel call already in progress. No additional thread/process is made.
        """
        if not isinstance(cancel_event, threading.Event):
            raise TypeError("resource admission requires a cancellation event")
        prepared = False
        try:
            while True:
                self._check_cancelled(cancel_event.is_set)
                try:
                    if not prepared:
                        self.prepare(launch, cancelled=cancel_event.is_set)
                        prepared = True
                    return self.acquire(launch, cancelled=cancel_event.is_set)
                except ResourceScanPending:
                    cancel_event.wait(0.05)
                except ResourceScanCancelled:
                    # Another worker can invalidate this node's shared partial
                    # cache epoch. That discards evidence, not this worker's
                    # Start intent. Only our own Event authorizes cancellation.
                    self._check_cancelled(cancel_event.is_set)
                    prepared = False
                    cancel_event.wait(0.05)
        except ResourceScanCancelled:
            self._verification_cache.discard_pending()
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
                binding = self._loading_bindings.get(token)
                if binding is not None:
                    cleanup_loading_binding(binding)
                containment = self._recovery_containments.get(token)
                if containment is not None:
                    containment.close()
                # A preceding release may have published its removal before
                # fsync failed. Rewrite durably on retry; this is permitted only
                # for this owner's known cleanup-certified release attempt.
                self._write([e for e in entries if e["claim"].reservation_id != token])
                self._owned.remove(token)
                self._loading_bindings.pop(token, None)
                self._recovery_containments.pop(token, None)
                self._pending_release.discard(token)
                if len(self._released) >= 1024:
                    self._released.pop(next(iter(self._released)))
                self._released[token] = True
                self._uncertain = self._acquisition_uncertain or bool(self._pending_release)
        except Exception:
            raise ResourceReservationError(_ERROR) from None
