"""Atomic, revision-bound persistence for the node contribution policy."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from drift.node.config import ContributionPolicyConfig, NodeConfig, NodeConfigError
from drift.node.config_lock import NodeConfigWriteLockError, node_config_write_lock
from drift.node.worker_supervisor import WorkerSupervisor, WorkerSupervisorSettings

MAX_NODE_CONFIG_BYTES = 4 * 1024 * 1024
CONTRIBUTION_POLICY_SCHEMA_VERSION = 1


class ContributionPolicyConflictError(RuntimeError):
    """The on-disk node config no longer matches the caller's revision."""


class ContributionPolicyPersistenceError(RuntimeError):
    """The validated policy could not be durably persisted."""


def _revision(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(os.path, "isjunction", lambda candidate: False)(path))


def _safe_config_path(path: Path | str) -> Path:
    absolute = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    try:
        if _is_link_or_junction(absolute) or _is_link_or_junction(absolute.parent):
            raise NodeConfigError("node config policy persistence refuses links and junctions")
        if not absolute.is_file():
            raise NodeConfigError("node config policy persistence requires a regular file")
        resolved = absolute.resolve(strict=True)
        resolved_parent = absolute.parent.resolve(strict=True)
    except OSError as exc:
        raise NodeConfigError("node config policy persistence could not verify its target") from exc
    if os.path.normcase(os.fspath(resolved)) != os.path.normcase(os.fspath(absolute)):
        raise NodeConfigError("node config policy persistence refuses linked paths")
    if os.path.normcase(os.fspath(resolved_parent)) != os.path.normcase(os.fspath(absolute.parent)):
        raise NodeConfigError("node config policy persistence refuses linked parent paths")
    return absolute


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
        document, payload = self._read()
        config = NodeConfig.from_dict(document, base_dir=self.path.parent)
        if expected_config is not None and config != expected_config:
            raise NodeConfigError("node config changed while the contribution policy runtime was starting")
        self._policy = config.contribution_policy
        self._revision = _revision(payload)

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

    def update(self, source: Mapping[str, Any], *, expected_revision: str) -> dict[str, Any]:
        if not isinstance(expected_revision, str) or not expected_revision.startswith("sha256:"):
            raise ContributionPolicyConflictError("policy update has an invalid config revision")
        with self._lock:
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
