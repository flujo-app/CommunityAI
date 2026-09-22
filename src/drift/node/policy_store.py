"""Atomic, revision-bound persistence for the node contribution policy."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from drift.node.config import ContributionPolicyConfig, NodeConfig, NodeConfigError
from drift.node.config_lock import NodeConfigWriteLockError, node_config_write_lock
from drift.node.device_binding import DeviceBindingError, DeviceBindingStore
from drift.node.gpu_selection_tokens import GpuSelectionChangedError, GpuSelectionTokens
from drift.node.gpu_worker_selection import candidate_gpu_selection, validate_gpu_selection_request
from drift.node.hardware_status import MAX_VISIBLE_ACCELERATORS
from drift.node.worker_selection import candidate_selection, enroll_selection, validate_selection_request
from drift.node.worker_supervisor import WorkerReconfigurationBusyError, WorkerSupervisor, WorkerSupervisorSettings

MAX_NODE_CONFIG_BYTES = 4 * 1024 * 1024
CONTRIBUTION_POLICY_SCHEMA_VERSION = 1


class ContributionPolicyConflictError(RuntimeError):
    """The on-disk node config no longer matches the caller's revision."""


class ContributionPolicyPersistenceError(RuntimeError):
    """The validated policy could not be durably persisted."""


class GpuSelectionRuntimeUnavailableError(NodeConfigError):
    """A saved draft requires joint automatic runtime that is not implemented yet."""


class ManagedGpuSelectionRequiredError(NodeConfigError):
    """Managed GPU membership requires the complete revision-bound batch API."""


def _selection_device(value: Any) -> str | None:
    if value == "cuda":
        return "cuda:0"
    if isinstance(value, str) and re.fullmatch(r"cuda:(0|[1-9][0-9]?)", value):
        return value if int(value.split(":")[1]) < MAX_VISIBLE_ACCELERATORS else None
    return None


def _binding_store(worker) -> DeviceBindingStore:
    return DeviceBindingStore(worker.identity_path.with_name(f".{worker.identity_path.name}.device-binding"))


def _gpu_ownership_reason(config: NodeConfig) -> str:
    if len(config.workers) > MAX_VISIBLE_ACCELERATORS:
        return "This configuration exceeds the supported GPU editing worker limit."
    managed_devices = set()
    manual = []
    for worker in config.workers:
        if worker.managed_by != "desktop_gpu":
            if worker.model.casefold() == "auto":
                return "Resolve legacy automatic worker ownership before editing GPU selections."
            if worker.device != "cpu" and _selection_device(worker.device) is None:
                return "Configure an explicit supported device for every custom worker before editing GPU selections."
            manual.append(worker)
            continue
        device = _selection_device(worker.device)
        if device is None or device != worker.device or device in managed_devices:
            return "Resolve ambiguous managed GPU ownership before editing GPU selections."
        managed_devices.add(device)
    if manual and config.contribution_policy.processing_scope != "per_device":
        return "Migrate custom worker processing settings before editing GPU selections."
    return ""


def _revision(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(os.path, "isjunction", lambda candidate: False)(path))


def _safe_config_path(path: Path | str) -> Path:
    absolute = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    try:
        if any(_is_link_or_junction(candidate) for candidate in (absolute, *absolute.parents)):
            raise NodeConfigError("node config policy persistence refuses links and junctions")
        if not absolute.is_file():
            raise NodeConfigError("node config policy persistence requires a regular file")
        resolved = absolute.resolve(strict=True)
    except OSError as exc:
        raise NodeConfigError("node config policy persistence could not verify its target") from exc
    return resolved


def _exchange_paths(replacement: Path, target: Path) -> Path:
    """Atomically exchange a candidate with its target and return the displaced path."""
    if os.name == "nt":
        import ctypes

        descriptor, backup_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".previous",
            dir=target.parent,
        )
        os.close(descriptor)
        backup = Path(backup_name)
        backup.unlink()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        replace_file = kernel32.ReplaceFileW
        replace_file.argtypes = (
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        replace_file.restype = ctypes.c_int
        if not replace_file(str(target), str(replacement), str(backup), 0, None, None):
            error = ctypes.get_last_error()
            if backup.is_file() and not target.exists():
                try:
                    os.replace(backup, target)
                except OSError as restore_exc:
                    # ERROR_UNABLE_TO_MOVE_REPLACEMENT_2 can leave the original
                    # only at the backup path. Never delete that recovery copy.
                    raise OSError(
                        error,
                        "atomic node config exchange failed; original remains in recovery backup",
                    ) from restore_exc
            raise OSError(error, "atomic node config exchange failed")
        return backup

    if os.name == "posix":
        import ctypes
        import errno

        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise OSError(errno.ENOTSUP, "atomic node config exchange is unavailable")
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        at_fdcwd = -100
        rename_exchange = 2
        if renameat2(
            at_fdcwd,
            os.fsencode(replacement),
            at_fdcwd,
            os.fsencode(target),
            rename_exchange,
        ):
            error = ctypes.get_errno()
            raise OSError(error, "atomic node config exchange failed")
        return replacement

    raise OSError("atomic node config exchange is unsupported on this platform")


def _parse_document(payload: bytes) -> dict[str, Any]:
    if len(payload) > MAX_NODE_CONFIG_BYTES:
        raise NodeConfigError("node config exceeds the policy persistence size limit")
    try:
        source = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NodeConfigError("node config must be UTF-8 JSON") from exc

    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise NodeConfigError(f"Node config JSON contains duplicate object key {key!r}")
            result[key] = value
        return result

    def reject_non_finite(value):
        raise NodeConfigError(f"Node config JSON contains non-finite number {value}")

    try:
        document = json.loads(
            source,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_non_finite,
        )
    except json.JSONDecodeError as exc:
        raise NodeConfigError(f"Invalid node config JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise NodeConfigError("node config must be a JSON object")
    return document


class ContributionPolicyStore:
    """Own one persistent node config and transact its policy with a supervisor."""

    def __init__(
        self,
        config_path: Path | str,
        supervisor: WorkerSupervisor,
        prepare: Callable[[NodeConfig], WorkerSupervisorSettings],
        *,
        expected_config: NodeConfig | None = None,
    ) -> None:
        self.path = _safe_config_path(config_path)
        self._supervisor = supervisor
        self._prepare = prepare
        self._lock = threading.Lock()
        self._restart_pending = False
        self._gpu_selection_tokens = GpuSelectionTokens()
        document, payload = self._read()
        config = NodeConfig.from_dict(document, base_dir=self.path.parent)
        if expected_config is not None and config != expected_config:
            raise NodeConfigError("node config changed while the contribution policy runtime was starting")
        self._policy = config.contribution_policy
        self._revision = _revision(payload)
        # These markers describe the running worker set until the node reloads.
        self._runtime_worker_provenance = {
            worker.worker_id.casefold(): worker.managed_by for worker in config.workers if worker.managed_by is not None
        }

    def _read(self) -> tuple[dict[str, Any], bytes]:
        _safe_config_path(self.path)
        try:
            payload = self.path.read_bytes()
        except OSError as exc:
            raise ContributionPolicyPersistenceError("node config could not be read safely") from exc
        return _parse_document(payload), payload

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema_version": CONTRIBUTION_POLICY_SCHEMA_VERSION,
                "config_revision": self._revision,
                "policy": dict(self._policy.to_dict()),
            }

    def _atomic_replace(self, payload: bytes, *, expected_revision: str) -> None:
        try:
            with node_config_write_lock(self.path):
                self._atomic_replace_locked(payload, expected_revision=expected_revision)
        except NodeConfigWriteLockError as exc:
            raise ContributionPolicyConflictError(
                "another node config writer is active; refresh the policy before saving"
            ) from exc

    def _atomic_replace_locked(self, payload: bytes, *, expected_revision: str) -> None:
        _safe_config_path(self.path)
        try:
            original = self.path.read_bytes()
            original_stat = self.path.stat()
        except OSError as exc:
            raise ContributionPolicyPersistenceError("node config could not be checked before persistence") from exc
        if _revision(original) != expected_revision:
            raise ContributionPolicyConflictError("node config changed; refresh the policy before saving")

        descriptor = None
        temporary = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
            )
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = None
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, stat.S_IMODE(original_stat.st_mode))
            _safe_config_path(self.path)
            if _revision(self.path.read_bytes()) != expected_revision:
                raise ContributionPolicyConflictError("node config changed; refresh the policy before saving")

            displaced = _exchange_paths(temporary, self.path)
            temporary = displaced
            if _revision(displaced.read_bytes()) != expected_revision:
                try:
                    candidate = _exchange_paths(displaced, self.path)
                except OSError as exc:
                    # Preserve the displaced concurrent document for recovery if
                    # restoring it atomically is itself impossible.
                    temporary = None
                    raise ContributionPolicyPersistenceError(
                        "node config changed during persistence and could not be restored"
                    ) from exc
                temporary = candidate
                raise ContributionPolicyConflictError("node config changed; refresh the policy before saving")

            try:
                displaced.unlink()
            except OSError:
                # The target is already durably committed. A stale secret-free
                # displaced config must not make active and disk policy diverge.
                pass
            temporary = None
            if os.name != "nt":
                # The rename is already the committed transaction. A filesystem that
                # cannot fsync directories must not make disk and active policy diverge.
                try:
                    directory_fd = os.open(self.path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    pass
        except ContributionPolicyConflictError:
            raise
        except OSError as exc:
            raise ContributionPolicyPersistenceError("node config policy persistence failed") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    def update_inference_mode(self, mode: str, *, expected_revision: str) -> dict[str, Any]:
        if mode not in ("auto", "local_only"):
            raise NodeConfigError("inference mode must be auto or local_only")
        with self._lock:
            self._require_no_restart()
            if expected_revision != self._revision:
                raise ContributionPolicyConflictError("node config changed; refresh before saving")
            document, payload = self._read()
            if _revision(payload) != self._revision:
                raise ContributionPolicyConflictError("node config changed; refresh before saving")
            document["inference_mode"] = mode
            NodeConfig.from_dict(document, base_dir=self.path.parent)
            encoded = (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
            self._atomic_replace(encoded, expected_revision=self._revision)
            self._revision = _revision(encoded)
            return {"inference_mode": mode, "config_revision": self._revision}

    def update(self, source: Mapping[str, Any], *, expected_revision: str) -> dict[str, Any]:
        if not isinstance(expected_revision, str) or not expected_revision.startswith("sha256:"):
            raise ContributionPolicyConflictError("policy update has an invalid config revision")
        with self._lock:
            self._require_no_restart()
            if expected_revision != self._revision:
                raise ContributionPolicyConflictError("node config changed; refresh the policy before saving")
            document, payload = self._read()
            disk_revision = _revision(payload)
            if disk_revision != self._revision:
                raise ContributionPolicyConflictError("node config changed; refresh the policy before saving")

            policy = ContributionPolicyConfig.from_dict(source)
            candidate_document = dict(document)
            candidate_document["contribution_policy"] = policy.to_dict()
            candidate_config = NodeConfig.from_dict(candidate_document, base_dir=self.path.parent)
            settings = self._prepare(candidate_config)
            encoded = (
                json.dumps(
                    candidate_document,
                    ensure_ascii=False,
                    indent=2,
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
            self._supervisor.reconfigure(
                settings,
                persist=lambda: self._atomic_replace(encoded, expected_revision=disk_revision),
            )
            self._policy = candidate_config.contribution_policy
            self._revision = _revision(encoded)
            return {
                "schema_version": CONTRIBUTION_POLICY_SCHEMA_VERSION,
                "config_revision": self._revision,
                "policy": dict(self._policy.to_dict()),
            }

    def _require_no_restart(self) -> None:
        if self._restart_pending:
            raise WorkerReconfigurationBusyError("node configuration restart is pending")

    def gpu_selection_snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._require_no_restart()
            return self._gpu_selection_tokens.snapshot(self._revision)

    def worker_provenance(self) -> dict[str, str]:
        with self._lock:
            return dict(self._runtime_worker_provenance)

    def _read_current_config(self):
        document, payload = self._read()
        if _revision(payload) != self._revision:
            raise ContributionPolicyConflictError("node config changed; refresh before saving")
        return document, NodeConfig.from_dict(document, base_dir=self.path.parent)

    def _selection_idle_reason(self, manager) -> str:
        if self._restart_pending or self._supervisor.configuration_restart_pending:
            return "The saved configuration is waiting for the node to restart."
        workers = self._supervisor.snapshots()
        if any(
            not item.get("operator_paused")
            or item.get("desired_running")
            or item.get("pid") is not None
            or item.get("schedule_suspended")
            or item.get("resource_suspended")
            or item.get("state") != "paused"
            for item in workers
        ):
            return "Pause sharing before editing GPU selections."
        if manager.closed or any(
            item.active_requests or item.state.value in ("loading", "unloading") for item in manager.snapshots()
        ):
            return "Finish active inference before editing GPU selections."
        return ""

    def _gpu_selection_state_locked(self, config, *, hardware_status, manager) -> dict[str, Any]:
        # Capture the hardware view and all tokens against this one policy/revision.
        # Neither reading inventory nor reading an existing pin enrolls a device.
        try:
            hardware = {} if hardware_status is None else hardware_status(dict(config.contribution_policy.to_dict()))
        except Exception:
            hardware = {}
        raw_inventory = hardware.get("gpus", []) if isinstance(hardware, dict) else []
        raw_inventory = raw_inventory[:MAX_VISIBLE_ACCELERATORS] if isinstance(raw_inventory, (list, tuple)) else []
        token_rows = self._gpu_selection_tokens.snapshot(self._revision)["devices"]
        tokens = {row["device"]: row["selection_token"] for row in token_rows}
        inventory = {}
        for raw in raw_inventory:
            if not isinstance(raw, dict):
                continue
            device = raw.get("device")
            cuda_device = _selection_device(device)
            supported = cuda_device is not None and device == cuda_device
            if not supported and not (
                device == "mps"
                or isinstance(device, str)
                and re.fullmatch(r"xpu:(0|[1-9][0-9]?)", device)
                and int(device.split(":")[1]) < MAX_VISIBLE_ACCELERATORS
            ):
                continue
            if device in inventory:
                continue
            name = raw.get("name")
            name = " ".join(name.split())[:128] if isinstance(name, str) else "GPU"
            if (
                not name
                or not name.isprintable()
                or re.search(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}", name)
            ):
                name = "GPU"
            total = raw.get("total_bytes")
            total = total if type(total) is int and 0 < total <= 2**63 - 1 else None
            status = "unsupported" if not supported else "unavailable"
            if supported and raw.get("status") == "available" and total is not None and device in tokens:
                status = "available"
            inventory[device] = {"device": device, "name": name, "total_bytes": total, "status": status}
            if status == "available":
                inventory[device]["selection_token"] = tokens[device]

        visible_workers = config.workers[:MAX_VISIBLE_ACCELERATORS]
        managed = {worker.device: worker for worker in visible_workers if worker.managed_by == "desktop_gpu"}
        manual = [worker for worker in visible_workers if worker.managed_by != "desktop_gpu"]
        manual_devices = {_selection_device(worker.device) for worker in manual}
        manual_physical = set()
        for worker in manual:
            device = _selection_device(worker.device)
            if device is None:
                continue
            try:
                manual_physical.add(_binding_store(worker).load_existing(worker.worker_id, device).cuda_visible_devices)
            except DeviceBindingError:
                pass  # PUT checks every retained manual pin before any enrollment is committed.
        for device, row in inventory.items():
            owned = device in manual_devices
            token = row.get("selection_token")
            if token is not None:
                try:
                    owned |= (
                        self._gpu_selection_tokens.verify_identity(device, self._revision, token) in manual_physical
                    )
                except GpuSelectionChangedError:
                    row["status"] = "unavailable"
                    row.pop("selection_token", None)
            if owned:
                row["status"] = "unsupported"
                row.pop("selection_token", None)

        for device, worker in managed.items():
            if _selection_device(device) != device:
                continue
            row = inventory.setdefault(
                device, {"device": device, "name": "GPU", "total_bytes": None, "status": "unavailable"}
            )
            try:
                binding = _binding_store(worker).load_existing(worker.worker_id, device)
                token = row.get("selection_token")
                if token is None:
                    raise GpuSelectionChangedError("selected GPU is unavailable")
                self._gpu_selection_tokens.verify_enrolled(device, self._revision, token, binding.cuda_visible_devices)
            except (DeviceBindingError, GpuSelectionChangedError):
                row["status"] = "unavailable"
                row.pop("selection_token", None)

        # Saved missing cards take precedence over informational unsupported devices
        # at the bound, so a selected lost card always remains deselectable.
        order = sorted(
            inventory,
            key=lambda value: (
                value not in managed,
                value.split(":")[0],
                int(value.split(":")[1]) if ":" in value else 0,
            ),
        )
        inventory_rows = [inventory[device] for device in order[:MAX_VISIBLE_ACCELERATORS]]
        rows = []
        for item in inventory_rows:
            device = item["device"]
            if _selection_device(device) != device:
                continue
            worker = managed.get(device)
            rows.append(
                {
                    "device": device,
                    "selected": worker is not None,
                    "max_vram": (worker.max_vram or config.contribution_policy.max_vram or "100%")
                    if worker
                    else "100%",
                    "max_processing_percent": (
                        worker.max_processing_percent
                        if worker.max_processing_percent is not None
                        else config.contribution_policy.max_processing_percent
                    )
                    if worker
                    else 100,
                }
            )
        ownership_reason = _gpu_ownership_reason(config)
        idle_reason = self._selection_idle_reason(manager)
        missing = any(item["status"] != "available" for item in inventory_rows if item["device"] in managed)
        restart = self._restart_pending or self._supervisor.configuration_restart_pending
        reason = ownership_reason or idle_reason
        if not reason and missing:
            reason = "A selected GPU changed or is missing; deselect it before saving."
        return {
            "schema_version": 1,
            "config_revision": self._revision,
            "editable": not bool(ownership_reason or idle_reason),
            "inventory": inventory_rows,
            "rows": rows,
            "restart_required": restart,
            "runtime_ready": not restart and not missing,
            "reason": reason or "Multiple automatic GPUs are not supported by the current runtime.",
        }

    def gpu_selection_state(self, *, hardware_status, manager) -> dict[str, Any]:
        """Read a coherent whole-set draft; readiness describes this supported config."""
        with self._lock:
            _, config = self._read_current_config()
            result = self._gpu_selection_state_locked(config, hardware_status=hardware_status, manager=manager)
            self._read_current_config()  # Detect external edits during hardware or pin probes.
            return result

    def _verify_gpu_enrollments(self, config, enrollments, *, original_ids, enroll_new):
        workers = {worker.worker_id.casefold(): worker for worker in config.workers}
        selected_physical = set()
        for enrollment in enrollments:
            worker = workers[enrollment["worker_id"].casefold()]
            device, token = enrollment["device"], enrollment["selection_token"]
            self._gpu_selection_tokens.verify_identity(device, self._revision, token)
            if worker.worker_id.casefold() in original_ids or not enroll_new:
                binding = _binding_store(worker).load_existing(worker.worker_id, device)
            else:
                binding = enroll_selection(config, worker.worker_id)
            self._gpu_selection_tokens.verify_enrolled(device, self._revision, token, binding.cuda_visible_devices)
            if binding.cuda_visible_devices in selected_physical:
                raise GpuSelectionChangedError("GPU selections resolve to the same physical device")
            selected_physical.add(binding.cuda_visible_devices)
        # Retained manual pins must remain valid even when removing the final
        # managed card: preparation may otherwise enroll a missing manual pin.
        for worker in config.workers:
            if worker.managed_by == "desktop_gpu" or _selection_device(worker.device) is None:
                continue
            binding = _binding_store(worker).load_existing(worker.worker_id, _selection_device(worker.device))
            if binding.cuda_visible_devices in selected_physical:
                raise GpuSelectionChangedError("GPU selection conflicts with a custom worker")

    def update_gpu_selection(self, request: dict, *, manager, hardware_status) -> dict[str, Any]:
        """Persist a complete paused managed set without relaxing automatic runtime admission."""
        request = validate_gpu_selection_request(request)
        with self._lock:
            self._require_no_restart()
            if request["expected_config_revision"] != self._revision:
                raise ContributionPolicyConflictError("node config changed; refresh before saving")
            document, current = self._read_current_config()
            ownership_reason = _gpu_ownership_reason(current)
            if ownership_reason:
                raise NodeConfigError(ownership_reason)
            idle_reason = self._selection_idle_reason(manager)
            if idle_reason:
                raise WorkerReconfigurationBusyError(idle_reason)
            state = self._gpu_selection_state_locked(current, hardware_status=hardware_status, manager=manager)
            available = {item["device"] for item in state["inventory"] if item["status"] == "available"}
            physical_devices = {}
            for row in request["rows"]:
                if row["selected"]:
                    if row["device"] not in available:
                        raise GpuSelectionChangedError("selected GPU is unavailable or belongs to a custom worker")
                    physical_devices[row["device"]] = self._gpu_selection_tokens.verify_identity(
                        row["device"], self._revision, row["selection_token"]
                    )
            candidate, enrollments, changed = candidate_gpu_selection(
                document, request, base_dir=self.path.parent, physical_devices=physical_devices
            )
            if sum(worker["model"].casefold() == "auto" for worker in candidate.get("workers", [])) > 1:
                raise GpuSelectionRuntimeUnavailableError("joint automatic GPU runtime is not available")
            config = NodeConfig.from_dict(candidate, base_dir=self.path.parent)
            original_ids = {worker.worker_id.casefold() for worker in current.workers}
            if not changed:
                self._verify_gpu_enrollments(config, enrollments, original_ids=original_ids, enroll_new=False)
                self._read_current_config()
                return {
                    "schema_version": 1,
                    "config_revision": self._revision,
                    "restart_required": False,
                    "runtime_ready": state["runtime_ready"],
                }
            encoded = (json.dumps(candidate, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
            if len(encoded) > MAX_NODE_CONFIG_BYTES:
                raise NodeConfigError("resulting node config exceeds the size limit")

            def persist():
                self._verify_gpu_enrollments(config, enrollments, original_ids=original_ids, enroll_new=True)
                self._prepare(config)
                self._verify_gpu_enrollments(config, enrollments, original_ids=original_ids, enroll_new=False)
                self._atomic_replace(encoded, expected_revision=self._revision)

            def commit_while_idle():
                if not manager.commit_idle_restart(persist):
                    raise WorkerReconfigurationBusyError("finish active inference before changing GPU selections")

            self._supervisor.commit_configuration_restart(commit_while_idle)
            self._revision = _revision(encoded)
            self._policy = config.contribution_policy
            self._restart_pending = True
            return {
                "schema_version": 1,
                "config_revision": self._revision,
                "restart_required": True,
                "runtime_ready": False,
            }

    def update_worker_selection(self, request: dict, *, manager) -> dict[str, Any]:
        """Commit paused worker membership only while execution is quiescent.

        Lock order is store, supervisor, model admission, config write lock.
        Rebuilding the whole node avoids stale planners and telemetry closures.
        """
        request = validate_selection_request(request)
        with self._lock:
            self._require_no_restart()
            if request["expected_config_revision"] != self._revision:
                raise ContributionPolicyConflictError("node config changed; refresh before saving")
            document, payload = self._read()
            if _revision(payload) != self._revision:
                raise ContributionPolicyConflictError("node config changed; refresh before saving")
            # An older client must not rotate/remove managed identities or move
            # a retained custom worker onto a managed card outside whole-set
            # token, physical-ownership and enrollment validation. This applies
            # to every mutation in a mixed configuration, including same-device
            # reselection (which would otherwise allocate a new private identity).
            if any(worker.get("managed_by") == "desktop_gpu" for worker in document.get("workers", [])):
                raise ManagedGpuSelectionRequiredError(
                    "Use the GPU selection batch control for managed GPU configurations"
                )
            candidate, worker_id, device = candidate_selection(document, request, base_dir=self.path.parent)
            config = NodeConfig.from_dict(candidate, base_dir=self.path.parent)
            encoded = (json.dumps(candidate, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
            if len(encoded) > MAX_NODE_CONFIG_BYTES:
                raise NodeConfigError("resulting node config exceeds the size limit")

            def persist():
                if device is not None:
                    if "selection_token" in request:
                        self._gpu_selection_tokens.verify(device, self._revision, request["selection_token"])
                    # Never publish a config whose first physical pin is deferred
                    # until restart: a reordered ordinal could select another card.
                    # A failed save may retain a private, never-reused orphan pin.
                    binding = enroll_selection(config, worker_id)
                    if "selection_token" in request:
                        self._gpu_selection_tokens.verify_enrolled(
                            device, self._revision, request["selection_token"], binding.cuda_visible_devices
                        )
                self._prepare(config)
                self._atomic_replace(encoded, expected_revision=self._revision)

            def commit_while_idle():
                if not manager.commit_idle_restart(persist):
                    raise WorkerReconfigurationBusyError("finish active inference before changing GPU selections")

            self._supervisor.commit_configuration_restart(commit_while_idle)
            self._revision = _revision(encoded)
            self._restart_pending = True
            return {
                "schema_version": 1,
                "config_revision": self._revision,
                "restart_required": True,
                "worker_id": worker_id,
                "device": device,
            }


def parse_policy_update_request(payload: bytes) -> tuple[str, Mapping[str, Any]]:
    """Strictly decode the bounded versioned whole-policy replacement request."""
    if len(payload) > 256 * 1024:
        raise NodeConfigError("contribution policy request exceeds the size limit")
    try:
        source = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NodeConfigError("contribution policy request must be UTF-8 JSON") from exc

    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise NodeConfigError(f"contribution policy request contains duplicate field {key!r}")
            result[key] = value
        return result

    def reject_non_finite(value):
        raise NodeConfigError(f"contribution policy request contains non-finite number {value}")

    try:
        request = json.loads(
            source,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_non_finite,
        )
    except json.JSONDecodeError as exc:
        raise NodeConfigError("contribution policy request is invalid JSON") from exc
    if not isinstance(request, dict):
        raise NodeConfigError("contribution policy request must be a JSON object")
    expected_fields = {"schema_version", "expected_config_revision", "policy"}
    if set(request) != expected_fields:
        raise NodeConfigError("contribution policy request has missing or unknown fields")
    if request["schema_version"] != CONTRIBUTION_POLICY_SCHEMA_VERSION:
        raise NodeConfigError("unsupported contribution policy request schema")
    revision = request["expected_config_revision"]
    if not isinstance(revision, str) or len(revision) != 71 or not revision.startswith("sha256:"):
        raise NodeConfigError("contribution policy request has an invalid config revision")
    try:
        int(revision[7:], 16)
    except ValueError as exc:
        raise NodeConfigError("contribution policy request has an invalid config revision") from exc
    policy = request["policy"]
    if not isinstance(policy, dict):
        raise NodeConfigError("contribution policy must be a JSON object")
    return revision, policy
