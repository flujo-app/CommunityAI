"""Fixed, offline first-use preparation under the anchor's Start exclusion.

No wire-supplied paths, reset, network fetch, migration or credential rotation.
The caller owns the lifetime and admission locks throughout bind/prepare. This
is cooperative same-UID exclusion, not a sandbox or replacement-anchor recovery.
"""

from __future__ import annotations

import copy
import ctypes
import hashlib
import json
import os
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from drift.catalog_release import (
    MAX_CATALOG_PUBLICATION_BUNDLE_MEMBER_BYTES,
    _bundle_member_bytes,
    catalog_publication_bundle_index_digest,
    load_catalog_publication_bundle,
)
from drift.model_catalog import CatalogRollbackGuard, SignedModelCatalog
from drift.model_manifest import ModelManifest
from drift.node import linux_anchor as anchor, worker_loading as private
from drift.node.catalog_bootstrap import CatalogBootstrapConfig, CatalogBootstrapInstaller
from drift.node.config import NodeConfig
from drift.node.config_lock import node_config_lock_path
from drift.node.linux_anchor_entry import bootstrap_catalog_binding, catalog_discriminator
from drift.node.linux_anchor_state import _sync_directory
from drift.node.resource_recovery import RecoverableStateError

MAX_OUTPUTS = 128


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(value):
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False) + "\n"
    ).encode()


@dataclass(frozen=True)
class BootstrapPlan:
    bundle_digest: str
    bootstrap: CatalogBootstrapConfig
    envelope: SignedModelCatalog
    outputs: tuple[tuple[str, bytes], ...]
    digest: str
    initialization_admitted_at_ms: int | None = None

    def validate_current(self, now=None):
        self.envelope.verify(self.bootstrap.trust_root, now=now)


def build_bootstrap_plan(bundle, profile, *, initialize=False, now=None):
    """Pure bounded plan; authenticate all bytes before explicit root creation."""
    import drift
    from drift.node.loading import validate_manifest_execution

    bundle = Path(bundle)
    # Historical authentication is only for reopening the exact journal-bound
    # package. First Start admission checks current time before durable intent;
    # exact admitted work can finish after expiry, without admitting a new plan.
    raw = _bundle_member_bytes(bundle, "catalog.signed.json", maximum_bytes=MAX_CATALOG_PUBLICATION_BUNDLE_MEMBER_BYTES)
    envelope = SignedModelCatalog.from_json(raw.decode("utf-8"))
    index = load_catalog_publication_bundle(bundle, now=envelope.signed.issued_at_ms / 1000)
    anchor._require(len(index["files"]) <= MAX_OUTPUTS - 3)
    members = {}
    for entry in index["files"]:
        value = _bundle_member_bytes(bundle, entry["path"], maximum_bytes=MAX_CATALOG_PUBLICATION_BUNDLE_MEMBER_BYTES)
        anchor._require(len(value) == entry["size"] and "sha256:" + _digest(value) == entry["sha256"])
        members[entry["path"]] = value
    anchor._require(raw == members["catalog.signed.json"])
    bootstrap = CatalogBootstrapConfig.from_json(members["catalog-bootstrap.json"].decode("utf-8"))
    catalog = envelope.verify(bootstrap.trust_root, now=envelope.signed.issued_at_ms / 1000)
    installer = CatalogBootstrapInstaller(bootstrap, data_dir=profile.data_dir, config_path=profile.config_path)
    outputs, manifests, selectors = [], [], {}
    for model in catalog.models:
        relative = f"manifests/{model.manifest_digest.removeprefix('sha256:')}.json"
        manifest = ModelManifest.from_json(members[relative].decode("utf-8"))
        manifest.validate_runtime(drift.__version__)
        validate_manifest_execution(manifest, model.execution or "distributed")
        for selector in (manifest.name, *manifest.aliases):
            previous = selectors.setdefault(selector.casefold(), manifest.digest_id)
            anchor._require(previous == manifest.digest_id)
        manifests.append(profile.data_dir / relative)
        outputs.append(("node/" + relative, members[relative]))
    guard = CatalogRollbackGuard()
    guard.check(catalog)
    config = installer._render_node_config(catalog, tuple(manifests)).encode("utf-8")
    for path, value in (
        (installer.catalog_dir / f"{catalog.sequence}-{catalog.digest.removeprefix('sha256:')}.signed.json", raw),
        (installer.installed_bootstrap_path, members["catalog-bootstrap.json"]),
        (installer.cached_catalog_path, raw),
        (installer.rollback_path, _json(guard.to_dict())),
        (profile.config_path, config),  # Activation is always last.
    ):
        outputs.append((path.relative_to(profile.root).as_posix(), value))
    anchor._require(len(outputs) <= MAX_OUTPUTS and len({name for name, _ in outputs}) == len(outputs))
    for name, value in outputs:
        anchor._require(name.startswith("node/") and ".." not in Path(name).parts)
        anchor._require(0 < len(value) <= MAX_CATALOG_PUBLICATION_BUNDLE_MEMBER_BYTES)
    description = [(name, len(value), _digest(value), 0o600) for name, value in outputs]
    admitted = int((time.time() if now is None else now) * 1000) if initialize else None
    plan = BootstrapPlan(
        catalog_publication_bundle_index_digest(index),
        bootstrap,
        envelope,
        tuple(outputs),
        _digest(_json(description)),
        admitted,
    )
    if initialize:
        plan.validate_current(admitted / 1000)
    return plan


def _rename_new(directory_fd, source, destination):
    """Atomic Linux no-replace publication. No check-then-overwrite fallback."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(directory_fd, os.fsencode(source), directory_fd, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _probe_rename(directory):
    """Exercise this libc/kernel/filesystem before credential or product writes.

    Explicit enrollment owns these fixed probe names. On failure retain
    evidence; successful cleanup unlinks only the exact inode just created.
    """
    identity = private._identity(private._stat(directory, directory=True))
    parent = anchor.cg._open_root(str(directory))
    descriptor = None
    source, target = ".bootstrap-rename-probe", ".bootstrap-rename-probe-complete"
    try:
        anchor._require(anchor.cg._identity(parent) == identity)
        descriptor = os.open(
            source, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600, dir_fd=parent
        )
        os.fsync(descriptor)
        _rename_new(parent, source, target)
        os.fsync(parent)
        observed = os.stat(target, dir_fd=parent, follow_symlinks=False)
        anchor._require(anchor._lock_identity(observed) == anchor._lock_identity(os.fstat(descriptor)))
        anchor._require(private._identity(private._stat(directory, directory=True)) == identity)
        os.unlink(target, dir_fd=parent)
        os.fsync(parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def validate_bootstrap_record(value, *, plan, binding, service, account, parents, lock_names):
    """Pure strict codec; it neither inspects storage nor grants recovery authority."""
    bootstrap_catalog_binding(value)
    anchor._require(
        value["binding"] == binding and value["bundle"] == plan.bundle_digest and value["plan"] == plan.digest
    )
    anchor._require(
        type(value["transaction"]) is str and anchor.re.fullmatch("[0-9a-f]{32}", value["transaction"]) is not None
    )
    anchor._require(value["service"] == service and value["account"] == account)
    anchor._require(type(value["directories"]) is dict and set(value["directories"]) == parents)
    for name, identity in value["directories"].items():
        anchor._require(
            type(identity) is list
            and len(identity) == 2
            and all(type(item) is int and 0 <= item < 2**64 for item in identity)
            and identity[1] > 0
        )
    anchor._require(type(value["locks"]) is dict and set(value["locks"]) == set(lock_names))
    for name, identity in value["locks"].items():
        anchor._require(
            type(identity) is list
            and len(identity) == 2
            and all(type(item) is int and 0 <= item < 2**64 for item in identity)
        )
    count = value["progress"]
    anchor._require(type(count) is int and 0 <= count <= len(plan.outputs))
    anchor._require(type(value["pending"]) is bool and (not value["pending"] or count < len(plan.outputs)))
    anchor._require(
        type(value["ready"]) is bool
        and (
            not value["ready"]
            or (count == len(plan.outputs) and not value["pending"] and value["credential"] == "ready")
        )
    )
    anchor._require(value["credential"] in {"absent", "pending", "ready"})
    digest = value["credential_digest"]
    anchor._require((digest is None) == (value["credential"] == "absent"))
    anchor._require(digest is None or (type(digest) is str and anchor.re.fullmatch("[0-9a-f]{64}", digest) is not None))
    attempt = value["attempt"]
    admitted = value["admitted_at_ms"]
    anchor._require((admitted is None) == (attempt is None))
    anchor._require(
        admitted is None
        or (
            type(admitted) is int and plan.envelope.signed.issued_at_ms <= admitted < plan.envelope.signed.expires_at_ms
        )
    )
    anchor._require(
        attempt is None
        or (
            type(attempt) is dict
            and set(attempt) == {"request_id", "generation"}
            and all(
                type(item) is str and anchor.re.fullmatch("[0-9a-f]{32}", item) is not None for item in attempt.values()
            )
        )
    )
    anchor._require(
        attempt is not None
        or (count == 0 and not value["pending"] and value["credential"] == "absent" and not value["ready"])
    )
    anchor._require((count == 0 and not value["pending"]) or value["credential"] == "ready")


def derive_rebound_bootstrap_record(value, *, plan, binding, new_binding, service, account, parents, lock_names):
    """Purely derive a validated v2 record for a separately authorized rebind.

    This does not inspect or mutate storage and grants no recovery authority. The
    caller must publish it inside the recovery transaction while holding the
    original evidence. Every field except the dynamic binding/schema extension
    is retained exactly, including the immutable catalog discriminator.
    """
    validate_bootstrap_record(
        value,
        plan=plan,
        binding=binding,
        service=service,
        account=account,
        parents=parents,
        lock_names=lock_names,
    )
    anchor._require(
        type(new_binding) is str
        and anchor.re.fullmatch("[0-9a-f]{64}", new_binding) is not None
        and new_binding != binding
    )
    proposed = copy.deepcopy(value)
    proposed["schema_version"] = 2
    proposed["binding"] = new_binding
    proposed["catalog_binding"] = bootstrap_catalog_binding(value)
    validate_bootstrap_record(
        proposed,
        plan=plan,
        binding=new_binding,
        service=service,
        account=account,
        parents=parents,
        lock_names=lock_names,
    )
    return proposed


class AnchorBootstrap:
    """Single anchor-owner transaction. Uncertain effects poison this owner.

    A native keyring call is synchronous and may block. Cancellation never
    releases ownership or acknowledges Drain while such a call is outstanding.
    Installed keyring timing/replacement-service recovery remain qualification
    requirements, not guarantees provided by this source transaction.
    """

    def __init__(self, plan, profile, credential_store):
        self.plan, self.profile, self.store = plan, profile, credential_store
        self.root = profile.root
        self.path = self.root / "anchor" / "bootstrap.json"
        self.poisoned = False
        self.retryable = False
        self.value = self.fingerprint = None
        self._ownership = None

    def bind(self, state, ownership, *, initialize=False):
        anchor.cg._platform()
        self._ownership = ownership
        self.binding = _digest(_json(state.binding))
        self.parents = {
            "anchor",
            "node",
            "node/catalogs",
            "node/manifests",
            "node/catalogs/" + self.plan.bootstrap.trust_root.catalog_id,
        }
        self.lock_names = (
            "node/.catalog-bootstrap.lock",
            node_config_lock_path(self.profile.config_path).relative_to(self.root).as_posix(),
        )
        ownership()
        if initialize:
            anchor._require(type(self.plan.initialization_admitted_at_ms) is int)
            self.plan.validate_current(self.plan.initialization_admitted_at_ms / 1000)
            _probe_rename(self.path.parent)
            for relative in sorted(self.parents, key=lambda name: (name.count("/"), name)):
                path = self.root / relative
                if relative not in {"anchor", "node"}:
                    path.mkdir(mode=0o700)  # No adoption during explicit enrollment.
                    parent_identity = private._identity(private._stat(path.parent, directory=True))
                    _sync_directory(path.parent, parent_identity)
                private._directory(path)
            anchor._require(all(not os.path.lexists(self.root / name) for name, _ in self.plan.outputs))
            directories = {
                name: list(private._identity(private._stat(self.root / name, directory=True)))
                for name in sorted(self.parents)
            }
            locks = {}
            for name in self.lock_names:
                path = self.root / name
                private._exclusive(
                    path, catalog_discriminator(self.root, self.binding) if name == self.lock_names[0] else {}
                )
                _sync_directory(path.parent, tuple(directories["node"]))
                locks[name] = list(anchor._lock_identity(path.lstat()))
            value = dict(
                schema_version=2,
                binding=self.binding,
                catalog_binding=self.binding,
                transaction=uuid4().hex,
                bundle=self.plan.bundle_digest,
                plan=self.plan.digest,
                directories=directories,
                locks=locks,
                service=self.store.service,
                account=self.store.account,
                attempt=None,
                admitted_at_ms=None,
                progress=0,
                pending=False,
                credential="absent",
                credential_digest=None,
                ready=False,
            )
            private._exclusive(self.path, value)
            _sync_directory(self.path.parent, tuple(directories["anchor"]))
        self.value = private._read(self.path)
        self.fingerprint = private._fingerprint(private._stat(self.path))
        self.validate()

    def validate(self):
        try:
            anchor._require(not self.poisoned)
            self._ownership()
            anchor._require(private._fingerprint(private._stat(self.path)) == self.fingerprint)
            anchor._require(private._read(self.path) == self.value)
            self._validate_value(self.value)
        except Exception:
            self.poisoned = True
            raise RecoverableStateError() from None

    def _validate_value(self, value):
        validate_bootstrap_record(
            value,
            plan=self.plan,
            binding=self.binding,
            service=self.store.service,
            account=self.store.account,
            parents=self.parents,
            lock_names=self.lock_names,
        )
        for name, identity in value["directories"].items():
            private._directory(self.root / name)
            anchor._require(list(private._identity(private._stat(self.root / name, directory=True))) == identity)
        for name, identity in value["locks"].items():
            anchor._require(list(anchor._lock_identity((self.root / name).lstat())) == identity)
        anchor._require(
            private._read(self.root / self.lock_names[0])
            == catalog_discriminator(self.root, bootstrap_catalog_binding(value))
        )

    def _write(self, **changes):
        self.validate()
        proposed = dict(self.value, **changes)
        self._validate_value(proposed)  # Invalid local transitions never reach disk.
        directory = descriptor = None
        try:
            payload = private._encode(proposed)
            directory = self._parent(self.path)
            temporary = ".bootstrap-journal-" + uuid4().hex + ".tmp"
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600, dir_fd=directory
            )
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                anchor._require(written > 0)
                offset += written
            os.fsync(descriptor)
            self.validate()
            anchor._require(
                private._fingerprint(os.stat(self.path.name, dir_fd=directory, follow_symlinks=False))
                == self.fingerprint
            )
            os.replace(temporary, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
            self._ownership()
            anchor._require(
                list(private._identity(private._stat(self.path.parent, directory=True)))
                == self.value["directories"]["anchor"]
            )
            anchor._require(private._read(self.path) == proposed)
            self.value = copy.deepcopy(proposed)
            self.fingerprint = private._fingerprint(private._stat(self.path))
            self.validate()
        except Exception:
            self.poisoned = True
            raise RecoverableStateError() from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if directory is not None:
                os.close(directory)

    def _parent(self, path):
        self.validate()
        relative = path.parent.relative_to(self.root).as_posix()
        identity = tuple(self.value["directories"][relative])
        descriptor = anchor.cg._open_root(str(path.parent))
        try:
            anchor._require(anchor.cg._identity(descriptor) == identity)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _read_file(self, path, *, maximum=MAX_CATALOG_PUBLICATION_BUNDLE_MEMBER_BYTES, sync=False):
        directory = self._parent(path)
        descriptor = None
        try:
            before = private._stat(path)
            anchor._require(0 < before.st_size <= maximum)
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
            anchor._require(private._same_opened(before, os.fstat(descriptor)))
            value = bytearray()
            while len(value) <= maximum:
                part = os.read(descriptor, min(65536, maximum + 1 - len(value)))
                if not part:
                    break
                value.extend(part)
            anchor._require(len(value) == before.st_size)
            anchor._require(private._fingerprint(os.fstat(descriptor)) == private._fingerprint(before))
            anchor._require(private._fingerprint(private._stat(path)) == private._fingerprint(before))
            if sync:
                os.fsync(descriptor)
                os.fsync(directory)
            self.validate()
            return bytes(value)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(directory)

    def _temporary(self, index):
        name, _ = self.plan.outputs[index]
        return (self.root / name).with_name(f".anchor-bootstrap-{self.value['transaction']}-{index}.tmp")

    def _inventory(self):
        # An acknowledged output cannot disappear. Only the exact durably
        # pending entry can have appeared without its progress acknowledgement.
        for index, (name, payload) in enumerate(self.plan.outputs):
            path, temporary = self.root / name, self._temporary(index)
            committed = index < self.value["progress"]
            pending = index == self.value["progress"] and self.value["pending"]
            if committed or (pending and os.path.lexists(path)):
                anchor._require(self._read_file(path) == payload)
            else:
                anchor._require(not os.path.lexists(path))
            if pending and os.path.lexists(temporary):
                anchor._require(not os.path.lexists(path) and self._read_file(temporary) == payload)
            else:
                anchor._require(not os.path.lexists(temporary))

    def _commit(self, index):
        name, payload = self.plan.outputs[index]
        path, temporary = self.root / name, self._temporary(index)
        try:
            if not self.value["pending"]:
                anchor._require(not os.path.lexists(path) and not os.path.lexists(temporary))
                self._write(pending=True)
            if not os.path.lexists(path):
                directory = self._parent(path)
                try:
                    if not os.path.lexists(temporary):
                        descriptor = os.open(
                            temporary.name,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                            0o600,
                            dir_fd=directory,
                        )
                        try:
                            offset = 0
                            while offset < len(payload):
                                written = os.write(descriptor, payload[offset:])
                                anchor._require(written > 0)
                                offset += written
                            os.fsync(descriptor)
                        finally:
                            os.close(descriptor)
                    anchor._require(self._read_file(temporary, sync=True) == payload)
                    self.validate()
                    _rename_new(directory, temporary.name, path.name)
                    os.fsync(directory)
                finally:
                    os.close(directory)
            anchor._require(self._read_file(path, sync=True) == payload)
            anchor._require(not os.path.lexists(temporary))
            self._write(progress=index + 1, pending=False)
        except Exception:
            self.poisoned = True
            raise RecoverableStateError() from None

    def _credential(self, boundary, *, credential_call=None, cancelled=lambda: False):
        from communityai_desktop.credentials import CredentialMissingError

        from communityai_anchor.linux_anchor_credentials import TRANSACTION_SECONDS

        deadline = time.monotonic() + TRANSACTION_SECONDS

        def call(operation, secret=None, *, reconcile=False):
            if credential_call is not None:
                return credential_call(
                    operation,
                    secret,
                    self.value,
                    deadline=deadline,
                    reconcile=reconcile,
                    cancelled=(lambda: False) if reconcile else cancelled,
                )
            # Internal in-memory fixture seam. Production supplies an identity
            # without get/set methods, so loss of its executor cannot fall back
            # to synchronous parent keyring access.
            if operation == "set":
                self.store.set(secret)
                return None
            try:
                return _digest(self.store.get().encode())
            except CredentialMissingError:
                return None

        # This first read has no effect, even when a previous attempt durably
        # recorded a digest. Unavailability never grants a new set; retry only
        # rereads that same digest. Authoritative absence/mismatch stays fatal.
        try:
            existing = call("get")
        except Exception:
            self.retryable = True
            raise RecoverableStateError() from None
        boundary()
        try:
            if self.value["credential"] != "absent":
                anchor._require(existing is not None and existing == self.value["credential_digest"])
                if self.value["credential"] == "pending":
                    self._write(credential="ready")
                return
            anchor._require(existing is None and not os.path.lexists(self.profile.data_dir / "control-api.key"))
            secret = "drift_control_" + secrets.token_urlsafe(32)
            self._write(credential="pending", credential_digest=_digest(secret.encode()))
            # Once creation intent is durable, reconcile even if cancellation
            # or set failure occurs. Never record or expose the secret itself.
            try:
                call("set", secret)
            except Exception:
                pass
            anchor._require(call("get", reconcile=True) == self.value["credential_digest"])
            self._write(credential="ready")
        except Exception:
            self.poisoned = True
            raise RecoverableStateError() from None

    def _ready(self):
        # The running child is a separate trusted catalog-refresh/config writer.
        # Preserve settings; never restore the first-use config over user edits.
        self.profile.validate_config()
        config = NodeConfig.from_json(
            self._read_file(self.profile.config_path).decode(), base_dir=self.profile.data_dir
        )
        anchor._require(config.catalog_path is not None and config.catalog_bootstrap_path is not None)
        installer = CatalogBootstrapInstaller(
            self.plan.bootstrap, data_dir=self.profile.data_dir, config_path=self.profile.config_path
        )
        anchor._require(config.catalog_bootstrap_path == installer.installed_bootstrap_path)
        bootstrap = CatalogBootstrapConfig.from_json(self._read_file(config.catalog_bootstrap_path).decode())
        anchor._require(bootstrap == self.plan.bootstrap)  # Root replacement needs checked maintenance.
        envelope = SignedModelCatalog.from_json(self._read_file(config.catalog_path).decode())
        catalog = envelope.verify(bootstrap.trust_root, now=envelope.signed.issued_at_ms / 1000)
        anchor._require(
            config.catalog_path
            == installer.catalog_dir / f"{catalog.sequence}-{catalog.digest.removeprefix('sha256:')}.signed.json"
        )
        floor = self.plan.envelope.signed
        anchor._require(catalog.catalog_id == floor.catalog_id and catalog.sequence >= floor.sequence)
        anchor._require(catalog.sequence != floor.sequence or catalog.digest == floor.digest)
        rollback = installer.rollback_path
        guard = CatalogRollbackGuard.from_dict(json.loads(self._read_file(rollback), object_pairs_hook=private._unique))
        anchor._require(guard.latest.get(catalog.catalog_id) == (catalog.sequence, catalog.digest))
        import drift
        from drift.node.loading import validate_manifest_execution

        selectors = {}
        for model in catalog.models:
            path = self.profile.data_dir / "manifests" / (model.manifest_digest.removeprefix("sha256:") + ".json")
            manifest = ModelManifest.from_json(self._read_file(path).decode())
            anchor._require(manifest.digest_id == model.manifest_digest)
            manifest.validate_runtime(drift.__version__)
            validate_manifest_execution(manifest, model.execution or "distributed")
            for selector in (manifest.name, *manifest.aliases):
                anchor._require(selectors.setdefault(selector.casefold(), manifest.digest_id) == manifest.digest_id)

    @contextmanager
    def _writers(self):
        import fcntl

        descriptors = []
        try:
            self.validate()
            for name in self.lock_names:  # Shared writer order: catalog, config.
                descriptor = os.open(self.root / name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
                descriptors.append(descriptor)
                anchor._require(list(anchor._lock_identity(os.fstat(descriptor))) == self.value["locks"][name])
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    self.retryable = True
                    raise RecoverableStateError("active_owner") from None
                self.validate()
            yield
            self.validate()
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def prepare(self, state, *, cancelled, credential_call=None):
        self.retryable = False
        with self._writers():
            return self._prepare(state, cancelled=cancelled, credential_call=credential_call)

    def _prepare(self, state, *, cancelled, credential_call=None):
        self.validate()
        generation = state["generation"]
        anchor._require(
            state["phase"] == "starting"
            and state["operation"] == "start"
            and generation is not None
            and (credential_call is None or generation["cgroup"] is not None)
            and generation["pid"] is None
            and generation.get("start_ticks") is None
        )

        def boundary():
            self.validate()
            if cancelled():
                raise RecoverableStateError("cleanup_pending")

        boundary()
        admitted = self.value["admitted_at_ms"]
        if admitted is None:
            try:
                admitted = int(time.time() * 1000)
                self.plan.validate_current(admitted / 1000)
            except Exception:
                self.retryable = True
                raise RecoverableStateError() from None
        if not self.value["ready"]:
            try:
                self._inventory()
            except Exception:
                self.poisoned = True
                raise RecoverableStateError() from None
        boundary()
        self._write(attempt=dict(request_id=state["request_id"], generation=generation["id"]), admitted_at_ms=admitted)
        boundary()
        self._credential(boundary, credential_call=credential_call, cancelled=cancelled)
        boundary()
        if self.value["ready"]:
            try:
                self._ready()
            except Exception:
                self.poisoned = True
                raise RecoverableStateError() from None
            boundary()
            return
        while self.value["progress"] < len(self.plan.outputs):
            self._commit(self.value["progress"])
            boundary()
        try:
            self._inventory()
            self._ready()
            self._write(ready=True)
        except Exception:
            self.poisoned = True
            raise RecoverableStateError() from None
        boundary()
