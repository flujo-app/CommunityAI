"""Privileged Linux package preparation for the Qwen3.8 complete-route attempt.

This host-only helper consumes the controller-protected final plan and exact provider
action. It verifies and extracts the plan-bound packaged node, protects it from the
ordinary qualification identity, performs one offline help preflight, and emits a
digest-only prepared record. It never provisions resources or downloads model data.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable, Iterator, Mapping, Sequence

from scripts import gateq38_linux_host_transport as transport, gateq38_route_controller as controller

SCHEMA_VERSION = 1
PREPARED_SCOPE = "qwen3.8-linux-host-runtime-prepared"
CLEANUP_SCOPE = "qwen3.8-linux-host-runtime-cleanup-terminal"
PUBLICATION_SCOPE = "qwen3.8-linux-host-status-publication"
DELIVERY_INSTALL_SCOPE = "qwen3.8-linux-instance-delivery-install"
QUALIFICATION_USER = "communityai-q38"
MAX_JSON_BYTES = controller.MAX_JSON_BYTES
MAX_PROVENANCE_BYTES = controller.MAX_RELEASE_PROVENANCE_BYTES
MAX_CHECKSUMS_BYTES = controller.MAX_RELEASE_CHECKSUMS_BYTES
MAX_METRICS_BYTES = controller.MAX_RELEASE_METRICS_BYTES
MAX_OUTPUT_BYTES = 1_048_576
MAX_ARCHIVE_ENTRIES = 100_000
MAX_ARCHIVE_BYTES = 8 * 1024**3
MAX_EXPANDED_BYTES = 16 * 1024**3
HASH_CHUNK_BYTES = 1_048_576
KILL_SIGNAL = getattr(signal, "SIGKILL", 9)
RUNTIME_BASE = Path("/opt/communityai-q38")
INPUT_BASE = Path("/var/lib/communityai-q38/input")
WORK_BASE = Path("/var/lib/communityai-q38/work")
STATE_BASE = Path("/var/lib/communityai-q38/state")
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
METADATA_HOST = "169.254.169.254"
METADATA_PORT = 80
GUEST_ATTRIBUTE_PATH = "/computeMetadata/v1/instance/guest-attributes/communityai-q38/status-v1"
METADATA_TIMEOUT_SECONDS = 5.0
MAX_METADATA_RESPONSE_BYTES = 4_096
_HEX_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_PREPARED_FIELDS = {
    "schema_version",
    "scope",
    "run_id",
    "source_commit",
    "plan_digest",
    "execution_inventory_digest",
    "start_action_id",
    "instance_context_digest",
    "resource_name",
    "resource_kind",
    "worker_id",
    "instance_generation_digest",
    "boot_id",
    "runtime_package_digest",
    "release_archive_sha256",
    "node_executable_sha256",
    "node_runtime_inventory_digest",
    "node_runtime_entry_count",
    "node_runtime_bytes",
    "qualification_user",
    "qualification_uid",
    "qualification_gid",
    "preflight_returncode",
    "preflight_stdout_sha256",
    "preflight_stdout_bytes",
    "preflight_stderr_sha256",
    "preflight_stderr_bytes",
    "prepared_record_digest",
}


class Q38LinuxHostRuntimeError(RuntimeError):
    """The protected package, host action, preflight, or cleanup failed closed."""


@dataclass(frozen=True)
class HostPaths:
    plan: Path
    start_action: Path
    cleanup_action: Path
    source_root: Path
    release_root: Path
    manifest: Path
    runtime_base: Path
    work_base: Path
    prepared_record: Path
    instance_context: Path
    transport_key: Path
    status_envelope: Path
    boot_id: Path
    transport_bundle: Path | None = None

    @classmethod
    def production(cls) -> "HostPaths":
        return cls(
            plan=INPUT_BASE / "route-plan.json",
            start_action=INPUT_BASE / "start-action.json",
            cleanup_action=INPUT_BASE / "cleanup-action.json",
            source_root=INPUT_BASE / "source",
            release_root=INPUT_BASE / "release",
            manifest=INPUT_BASE / "manifest.json",
            runtime_base=RUNTIME_BASE,
            work_base=WORK_BASE,
            prepared_record=STATE_BASE / "prepared.json",
            instance_context=INPUT_BASE / "instance-context.json",
            transport_key=INPUT_BASE / "host-status.key",
            status_envelope=STATE_BASE / "host-status.json",
            boot_id=BOOT_ID_PATH,
            transport_bundle=INPUT_BASE / "instance-delivery.bin",
        )


@dataclass(frozen=True)
class Artifact:
    path: str
    kind: str
    sha256: str
    size_bytes: int
    mode: int | None
    link_target: str | None


@dataclass(frozen=True)
class QualificationIdentity:
    name: str
    uid: int
    gid: int


@dataclass(frozen=True)
class PreflightResult:
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class TransportInputs:
    context: dict[str, Any]
    key: bytes
    boot_id: str


def _reject_constant(_value: str) -> None:
    raise Q38LinuxHostRuntimeError("non-finite JSON value")


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Q38LinuxHostRuntimeError("duplicate JSON field")
        result[key] = value
    return result


def _strict_json(payload: bytes, *, maximum: int = MAX_JSON_BYTES) -> dict[str, Any]:
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= maximum:
        raise Q38LinuxHostRuntimeError("JSON payload exceeded its bound")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Q38LinuxHostRuntimeError("JSON payload is invalid") from exc
    if not isinstance(value, dict):
        raise Q38LinuxHostRuntimeError("JSON root must be an object")
    return value


def _canonical_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise Q38LinuxHostRuntimeError("value is not canonical JSON") from exc


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _sha256_stream(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(HASH_CHUNK_BYTES), b""):
                digest.update(chunk)
    except OSError as exc:
        raise Q38LinuxHostRuntimeError("runtime file is unreadable") from exc
    return "sha256:" + digest.hexdigest()


def _same_json_value(actual: Any, expected: Any) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(actual, dict):
        return set(actual) == set(expected) and all(_same_json_value(actual[key], expected[key]) for key in actual)
    if isinstance(actual, list):
        return len(actual) == len(expected) and all(
            _same_json_value(left, right) for left, right in zip(actual, expected)
        )
    return actual == expected


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        metadata.st_size,
        getattr(metadata, "st_mtime_ns", int(metadata.st_mtime * 1_000_000_000)),
    )


def _is_reparse(path: Path, metadata: os.stat_result) -> bool:
    return (
        bool(getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        or path.is_symlink()
    )


@contextmanager
def _verified_file(
    path: Path,
    *,
    expected_size: int | None = None,
    expected_digest: str | None = None,
    maximum: int | None = None,
) -> Iterator[tuple[BinaryIO, bytes | None]]:
    descriptor: int | None = None
    try:
        before = path.lstat()
        if (
            _is_reparse(path, before)
            or not stat.S_ISREG(before.st_mode)
            or (expected_size is not None and before.st_size != expected_size)
            or (maximum is not None and not 1 <= before.st_size <= maximum)
        ):
            raise Q38LinuxHostRuntimeError(f"required file is unsafe: {path.name}")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _file_identity(opened) != _file_identity(before):
            raise Q38LinuxHostRuntimeError("required file identity changed while opened")
        stream = os.fdopen(descriptor, "rb", closefd=False)
        payload: bytes | None = None
        if maximum is not None:
            payload = stream.read(maximum + 1)
            if len(payload) != opened.st_size:
                raise Q38LinuxHostRuntimeError("required file changed while read")
            stream.seek(0)
        if expected_digest is not None:
            digest = hashlib.sha256()
            total = 0
            for chunk in iter(lambda: stream.read(HASH_CHUNK_BYTES), b""):
                total += len(chunk)
                if expected_size is not None and total > expected_size:
                    raise Q38LinuxHostRuntimeError("required file grew while hashed")
                digest.update(chunk)
            if (
                expected_size is not None
                and total != expected_size
                or "sha256:" + digest.hexdigest() != expected_digest
            ):
                raise Q38LinuxHostRuntimeError("required file binding changed")
            stream.seek(0)
        after = os.fstat(descriptor)
        final = path.lstat()
        if (
            _is_reparse(path, final)
            or not stat.S_ISREG(final.st_mode)
            or _file_identity(after) != _file_identity(opened)
            or _file_identity(final) != _file_identity(opened)
        ):
            raise Q38LinuxHostRuntimeError("required file identity changed while verified")
        yield stream, payload
    except Q38LinuxHostRuntimeError:
        raise
    except OSError as exc:
        raise Q38LinuxHostRuntimeError(f"required file is unreadable: {path.name}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _regular_bytes(path: Path, *, maximum: int = MAX_JSON_BYTES) -> bytes:
    with _verified_file(path, maximum=maximum) as (_stream, payload):
        if payload is None:
            raise AssertionError("bounded read omitted its payload")
        return payload


def _assert_root_private_file(path: Path) -> None:
    _assert_root_managed(path, directory=False)
    if os.name != "posix":
        return
    metadata = path.lstat()
    if (
        _is_reparse(path, metadata)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise Q38LinuxHostRuntimeError(f"private host input is unsafe: {path.name}")


def _read_boot_id(path: Path) -> str:
    descriptor: int | None = None
    try:
        before = path.lstat()
        if _is_reparse(path, before) or not stat.S_ISREG(before.st_mode):
            raise Q38LinuxHostRuntimeError("Linux boot identity source is unsafe")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _file_identity(opened) != _file_identity(before):
            raise Q38LinuxHostRuntimeError("Linux boot identity changed while opened")
        payload = os.read(descriptor, 129)
        after = os.fstat(descriptor)
        final = path.lstat()
        if (
            _is_reparse(path, final)
            or _file_identity(after) != _file_identity(opened)
            or _file_identity(final) != _file_identity(opened)
        ):
            raise Q38LinuxHostRuntimeError("Linux boot identity changed while read")
        try:
            value = payload.decode("ascii")
        except UnicodeDecodeError as exc:
            raise Q38LinuxHostRuntimeError("Linux boot identity is invalid") from exc
        if len(payload) != 37 or not value.endswith("\n") or transport._BOOT_ID_RE.fullmatch(value[:-1]) is None:
            raise Q38LinuxHostRuntimeError("Linux boot identity is invalid")
        return value[:-1]
    except Q38LinuxHostRuntimeError:
        raise
    except OSError as exc:
        raise Q38LinuxHostRuntimeError("Linux boot identity is unreadable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _load_authenticated_context(
    plan: controller.RoutePlan,
    paths: HostPaths,
    *,
    expected_resource_name: str,
    expected_generation_digest: str,
    now_unix: int,
    allow_expired_for_cleanup: bool,
) -> tuple[dict[str, Any], bytes]:
    bundle_path = paths.transport_bundle
    if bundle_path is not None:
        if bundle_path.parent != paths.plan.parent:
            raise Q38LinuxHostRuntimeError("transport bundle is outside the protected input boundary")
        _assert_root_managed(bundle_path.parent, directory=True)
        _assert_root_private_file(bundle_path)
        try:
            payload = _regular_bytes(
                bundle_path,
                maximum=transport.MAX_DELIVERY_BYTES,
            )
            _record, context, key = transport.decode_instance_delivery(
                payload,
                plan,
                now_unix=now_unix,
                expected_resource_name=expected_resource_name,
                expected_generation_digest=expected_generation_digest,
                allow_expired_for_cleanup=allow_expired_for_cleanup,
            )
        except transport.Q38LinuxHostTransportError as exc:
            raise Q38LinuxHostRuntimeError(str(exc)) from exc
        return context, key
    if (
        paths.instance_context.parent != paths.transport_key.parent
        or paths.instance_context.parent != paths.plan.parent
    ):
        raise Q38LinuxHostRuntimeError("transport inputs are outside the protected input boundary")
    _assert_root_managed(paths.instance_context.parent, directory=True)
    _assert_root_private_file(paths.instance_context)
    _assert_root_private_file(paths.transport_key)
    try:
        with _verified_file(paths.instance_context, maximum=transport.MAX_ENVELOPE_BYTES,) as (
            _context_stream,
            context_payload,
        ), _verified_file(paths.transport_key, expected_size=transport.KEY_BYTES,) as (key_stream, _key_payload):
            if context_payload is None:
                raise AssertionError("instance context payload was not read")
            key = key_stream.read(transport.KEY_BYTES + 1)
            if len(key) != transport.KEY_BYTES:
                raise Q38LinuxHostRuntimeError("transport key is invalid")
            context = transport.decode_instance_context(context_payload)
            validated = transport.validate_instance_context(
                context,
                plan,
                key=key,
                now_unix=now_unix,
                expected_resource_name=expected_resource_name,
                expected_generation_digest=expected_generation_digest,
                _allow_expired_for_cleanup=allow_expired_for_cleanup,
            )
    except transport.Q38LinuxHostTransportError as exc:
        raise Q38LinuxHostRuntimeError(str(exc)) from exc
    return validated, key


def _load_transport_inputs(
    plan: controller.RoutePlan,
    paths: HostPaths,
    *,
    expected_resource_name: str,
    expected_generation_digest: str,
    now_unix: int,
) -> TransportInputs:
    context, key = _load_authenticated_context(
        plan,
        paths,
        expected_resource_name=expected_resource_name,
        expected_generation_digest=expected_generation_digest,
        now_unix=now_unix,
        allow_expired_for_cleanup=False,
    )
    return TransportInputs(context=context, key=key, boot_id=_read_boot_id(paths.boot_id))


def _safe_member_path(raw: Any, *, allow_root: bool = False) -> str:
    if not isinstance(raw, str) or not raw or "\\" in raw or any(ord(character) < 32 for character in raw):
        raise Q38LinuxHostRuntimeError("package path is unsafe")
    normalized = raw[:-1] if raw.endswith("/") else raw
    pure = PurePosixPath(normalized)
    if (
        pure.is_absolute()
        or pure.as_posix() != normalized
        or any(part in {"", ".", ".."} for part in pure.parts)
        or not pure.parts
        or pure.parts[0] != "CommunityAI"
        or (len(pure.parts) == 1 and not allow_root)
    ):
        raise Q38LinuxHostRuntimeError("package path is unsafe")
    return normalized


def _canonical_link_target(member_path: str, raw_target: Any) -> str:
    if (
        not isinstance(raw_target, str)
        or not raw_target
        or raw_target.startswith("/")
        or "\\" in raw_target
        or any(ord(character) < 32 for character in raw_target)
    ):
        raise Q38LinuxHostRuntimeError("package symlink is unsafe")
    parts: list[str] = []
    for part in (PurePosixPath(member_path).parent / PurePosixPath(raw_target)).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise Q38LinuxHostRuntimeError("package symlink escapes its root")
            parts.pop()
        else:
            parts.append(part)
    return _safe_member_path(PurePosixPath(*parts).as_posix())


def _digest_field(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:") or _HEX_DIGEST_RE.fullmatch(value[7:]) is None:
        raise Q38LinuxHostRuntimeError(f"{field} is invalid")
    return value


def _positive_integer(value: Any, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise Q38LinuxHostRuntimeError(f"{field} is invalid")
    return value


def _artifact(raw: Any) -> Artifact:
    if not isinstance(raw, dict):
        raise Q38LinuxHostRuntimeError("release artifact is invalid")
    kind = raw.get("kind")
    expected = (
        {"path", "kind", "mode", "sha256", "size_bytes"}
        if kind == "file"
        else {"path", "kind", "link_target", "sha256", "size_bytes"}
        if kind == "symlink"
        else set()
    )
    if not expected or set(raw) != expected:
        raise Q38LinuxHostRuntimeError("release artifact schema is invalid")
    path = _safe_member_path(raw["path"])
    digest = raw["sha256"]
    if not isinstance(digest, str) or _HEX_DIGEST_RE.fullmatch(digest) is None:
        raise Q38LinuxHostRuntimeError("release artifact digest is invalid")
    size = _positive_integer(raw["size_bytes"], "release artifact size")
    if kind == "file":
        mode = raw["mode"]
        if type(mode) is not int or mode not in {0o644, 0o755}:
            raise Q38LinuxHostRuntimeError("release artifact mode is unsafe")
        return Artifact(path, kind, digest, size, mode, None)
    target = _safe_member_path(raw["link_target"])
    return Artifact(path, kind, digest, size, None, target)


def _assert_root_managed(path: Path, *, directory: bool) -> None:
    if not path.is_absolute():
        raise Q38LinuxHostRuntimeError("protected path is not absolute")
    try:
        target = path.lstat()
    except OSError as exc:
        raise Q38LinuxHostRuntimeError("protected path is unavailable") from exc
    if _is_reparse(path, target):
        raise Q38LinuxHostRuntimeError("protected path is linked")
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(target.st_mode):
        raise Q38LinuxHostRuntimeError("protected path type is invalid")
    if target.st_uid != 0 or target.st_gid != 0 or stat.S_IMODE(target.st_mode) & 0o022:
        raise Q38LinuxHostRuntimeError("protected path is writable by the qualification identity")
    current = path.parent
    while current != current.parent:
        metadata = current.lstat()
        if (
            _is_reparse(current, metadata)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise Q38LinuxHostRuntimeError("protected path parent is unsafe")
        current = current.parent


def _assert_qualification_traversal(path: Path) -> None:
    current = path
    while current != current.parent:
        metadata = current.lstat()
        if (
            _is_reparse(current, metadata)
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) & 0o001 == 0
        ):
            raise Q38LinuxHostRuntimeError("preflight parent is not traversable by the qualification identity")
        current = current.parent


def _assert_source_bound(plan: controller.RoutePlan, source_root: Path) -> None:
    root = source_root.resolve(strict=True)
    bindings = {item["relative_path"]: item for item in plan.source_bindings}
    imported = {
        controller.LINUX_HOST_RUNTIME_SOURCE_PATH: Path(__file__).resolve(),
        controller.LINUX_HOST_TRANSPORT_SOURCE_PATH: Path(transport.__file__).resolve(),
        "scripts/gateq38_route_controller.py": Path(controller.__file__).resolve(),
    }
    _assert_root_managed(root, directory=True)
    for relative, module in imported.items():
        expected = (root / Path(*PurePosixPath(relative).parts)).resolve(strict=True)
        binding = bindings.get(relative)
        if module != expected or binding is None:
            raise Q38LinuxHostRuntimeError("imported host sources are not plan-bound")
        _assert_root_managed(expected, directory=False)
        payload = _regular_bytes(expected)
        if len(payload) != binding["byte_size"] or _sha256(payload) != binding["sha256"]:
            raise Q38LinuxHostRuntimeError("host source binding changed")


def _load_plan_and_action(
    plan_path: Path,
    action_path: Path,
    source_root: Path,
    *,
    expected_action: str,
    now_unix: int,
) -> tuple[controller.RoutePlan, dict[str, Any]]:
    if expected_action not in {"start_route", "cleanup_route"}:
        raise Q38LinuxHostRuntimeError("host action is invalid")
    initial_plan = _regular_bytes(plan_path)
    try:
        plan = controller.load_plan(plan_path, source_root)
    except controller.RouteControllerError as exc:
        raise Q38LinuxHostRuntimeError(str(exc)) from exc
    _assert_root_managed(plan_path.parent, directory=True)
    _assert_root_managed(plan_path, directory=False)
    if _regular_bytes(plan_path) != initial_plan:
        raise Q38LinuxHostRuntimeError("route plan changed while loaded")
    _assert_source_bound(plan, source_root)
    if type(now_unix) is not int or now_unix <= 0:
        raise Q38LinuxHostRuntimeError("trusted current time is invalid")
    if expected_action == "start_route" and now_unix >= plan.deadline_unix:
        raise Q38LinuxHostRuntimeError("route plan is expired")
    if expected_action == "start_route" and any(
        plan.authorization[field] is not True
        for field in (
            "reservation_recorded",
            "native_auth_revalidated",
            "inventory_revalidated",
            "pricing_revalidated",
            "provisioning_authorized",
        )
    ):
        raise Q38LinuxHostRuntimeError("route start is not fully authorized")

    action_payload = _regular_bytes(action_path)
    _assert_root_managed(action_path.parent, directory=True)
    _assert_root_managed(action_path, directory=False)
    if _regular_bytes(action_path) != action_payload:
        raise Q38LinuxHostRuntimeError("host action changed while loaded")
    action = _strict_json(action_payload)
    revision = action.get("revision")
    if type(revision) is not int or revision < 0:
        raise Q38LinuxHostRuntimeError("host action revision is invalid")
    expected = controller.action_record(
        {"revision": revision, "next_action": expected_action},
        plan,
    )
    if not _same_json_value(action, expected):
        raise Q38LinuxHostRuntimeError("host action is not the exact controller action")
    return plan, action


def _load_release_inventory(
    plan: controller.RoutePlan,
    paths: HostPaths,
) -> tuple[list[Artifact], list[Artifact]]:
    package = plan.runtime_package
    _assert_root_managed(paths.release_root, directory=True)
    bound_files = {
        "SHA256SUMS": (
            package["checksums_bytes"],
            package["checksums_sha256"],
            MAX_CHECKSUMS_BYTES,
        ),
        "provenance.json": (
            package["provenance_bytes"],
            package["provenance_sha256"],
            MAX_PROVENANCE_BYTES,
        ),
        "desktop-metrics.json": (
            package["desktop_metrics_bytes"],
            package["desktop_metrics_sha256"],
            MAX_METRICS_BYTES,
        ),
    }
    payloads: dict[str, bytes] = {}
    for name, (size, digest, maximum) in bound_files.items():
        candidate = paths.release_root / name
        _assert_root_managed(candidate, directory=False)
        with _verified_file(candidate, expected_size=size, expected_digest=digest, maximum=maximum) as (
            _stream,
            payload,
        ):
            if payload is None:
                raise AssertionError("release companion payload was not read")
            payloads[name] = payload
    _assert_root_managed(paths.manifest.parent, directory=True)
    _assert_root_managed(paths.manifest, directory=False)
    with _verified_file(
        paths.manifest,
        expected_size=package["manifest_bytes"],
        expected_digest=package["manifest_sha256"],
        maximum=MAX_JSON_BYTES,
    ) as (_stream, manifest_payload):
        if manifest_payload is None:
            raise AssertionError("manifest payload was not read")
    manifest = _strict_json(manifest_payload)
    source = manifest.get("source")
    model = manifest.get("model")
    if (
        type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != 1
        or not isinstance(source, dict)
        or source.get("revision") != plan.model_revision
        or not isinstance(model, dict)
        or model.get("num_blocks") != 64
    ):
        raise Q38LinuxHostRuntimeError("manifest physical identity is inconsistent")

    provenance = _strict_json(payloads["provenance.json"], maximum=MAX_PROVENANCE_BYTES)
    if provenance.get("source_commit") != plan.source_commit or provenance.get("source_tree") != package["source_tree"]:
        raise Q38LinuxHostRuntimeError("release provenance source identity changed")
    raw_artifacts = provenance.get("artifacts")
    if not isinstance(raw_artifacts, list) or not raw_artifacts:
        raise Q38LinuxHostRuntimeError("release artifact inventory is invalid")
    artifacts = [_artifact(value) for value in raw_artifacts]
    paths_list = [item.path for item in artifacts]
    if paths_list != sorted(paths_list) or len({item.casefold() for item in paths_list}) != len(paths_list):
        raise Q38LinuxHostRuntimeError("release artifact paths are not canonical")
    artifact_map = {item.path: item for item in artifacts}
    for item in artifacts:
        if item.kind == "symlink":
            if item.link_target not in artifact_map or artifact_map[item.link_target].kind != "file":
                raise Q38LinuxHostRuntimeError("release symlink target is invalid")
    expected_checksums = "".join(f"{item.sha256}  {item.path}\n" for item in artifacts).encode("utf-8")
    if payloads["SHA256SUMS"] != expected_checksums:
        raise Q38LinuxHostRuntimeError("release checksum inventory changed")

    node_prefix = package["node_root"] + "/"
    node = [item for item in artifacts if item.path == package["node_executable"] or item.path.startswith(node_prefix)]
    raw_node = [value for value in raw_artifacts if value.get("path") in {item.path for item in node}]
    raw_node.sort(key=lambda item: item["path"])
    if (
        len(node) != package["node_runtime_entry_count"]
        or sum(item.size_bytes for item in node) != package["node_runtime_bytes"]
        or _sha256(_canonical_bytes(raw_node)) != package["node_runtime_inventory_digest"]
    ):
        raise Q38LinuxHostRuntimeError("node runtime inventory changed")
    executable = artifact_map.get(package["node_executable"])
    if (
        executable is None
        or executable.kind != "file"
        or executable.sha256 != package["node_executable_sha256"][7:]
        or executable.size_bytes != package["node_executable_bytes"]
        or executable.mode != 0o755
    ):
        raise Q38LinuxHostRuntimeError("node executable identity changed")
    return artifacts, node


def _audit_members(
    source: tarfile.TarFile,
    artifacts: Sequence[Artifact],
) -> dict[str, tarfile.TarInfo]:
    artifact_map = {item.path: item for item in artifacts}
    members: dict[str, tarfile.TarInfo] = {}
    folded_paths: set[str] = set()
    total_bytes = 0
    for member in source:
        if len(members) >= MAX_ARCHIVE_ENTRIES:
            raise Q38LinuxHostRuntimeError("archive entry count exceeded its bound")
        path = _safe_member_path(member.name, allow_root=True)
        folded = path.casefold()
        if folded in folded_paths:
            raise Q38LinuxHostRuntimeError("archive contains duplicate members")
        folded_paths.add(folded)
        if (
            member.islnk()
            or member.isdev()
            or member.isfifo()
            or getattr(member, "sparse", None)
            or not (member.isdir() or member.isfile() or member.issym())
        ):
            raise Q38LinuxHostRuntimeError("archive member type is unsafe")
        if stat.S_IMODE(member.mode) & 0o7000 or (not member.isfile() and member.size != 0):
            raise Q38LinuxHostRuntimeError("archive member mode or size is unsafe")
        members[path] = member
        if member.isfile():
            total_bytes += member.size
            if total_bytes > MAX_EXPANDED_BYTES:
                raise Q38LinuxHostRuntimeError("archive expanded size exceeded its bound")
    payload_members = {path: member for path, member in members.items() if not member.isdir()}
    if set(payload_members) != set(artifact_map):
        raise Q38LinuxHostRuntimeError("archive payload inventory changed")
    if total_bytes != sum(item.size_bytes for item in artifacts if item.kind == "file"):
        raise Q38LinuxHostRuntimeError("archive expanded size changed")
    for path, artifact in artifact_map.items():
        member = payload_members[path]
        if artifact.kind == "file":
            if not member.isfile() or member.size != artifact.size_bytes or stat.S_IMODE(member.mode) != artifact.mode:
                raise Q38LinuxHostRuntimeError("archive file identity changed")
            stream = source.extractfile(member)
            if stream is None:
                raise Q38LinuxHostRuntimeError("archive file is unreadable")
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(HASH_CHUNK_BYTES), b""):
                digest.update(chunk)
            if digest.hexdigest() != artifact.sha256:
                raise Q38LinuxHostRuntimeError("archive file digest changed")
        elif not member.issym() or _canonical_link_target(path, member.linkname) != artifact.link_target:
            raise Q38LinuxHostRuntimeError("archive symlink identity changed")
    return members


def _remove_tree_strict(path: Path, message: str) -> None:
    try:
        if path.is_symlink():
            raise Q38LinuxHostRuntimeError(message)
        if path.exists():
            shutil.rmtree(path)
        if path.exists() or path.is_symlink():
            raise Q38LinuxHostRuntimeError(message)
    except Q38LinuxHostRuntimeError:
        raise
    except OSError as exc:
        raise Q38LinuxHostRuntimeError(message) from exc


def _extract_verified_archive(
    archive: Path,
    package: Mapping[str, Any],
    artifacts: Sequence[Artifact],
    node: Sequence[Artifact],
    destination: Path,
) -> None:
    if destination.exists() or destination.is_symlink():
        raise Q38LinuxHostRuntimeError("runtime staging destination is not empty")
    if (
        type(package["release_archive_bytes"]) is not int
        or not 1 <= package["release_archive_bytes"] <= MAX_ARCHIVE_BYTES
        or sum(item.size_bytes for item in artifacts) > MAX_EXPANDED_BYTES
    ):
        raise Q38LinuxHostRuntimeError("archive size exceeds the host-stage bound")
    _assert_root_managed(archive, directory=False)
    destination.mkdir(mode=0o700, parents=False)
    node_paths = {item.path for item in node}
    try:
        with _verified_file(
            archive,
            expected_size=package["release_archive_bytes"],
            expected_digest=package["release_archive_sha256"],
        ) as (handle, _payload):
            with tarfile.open(fileobj=handle, mode="r:gz") as source:
                _audit_members(source, artifacts)
            handle.seek(0)
            node_map = {item.path: item for item in node}
            pending_links: list[tuple[Artifact, str]] = []
            with tarfile.open(fileobj=handle, mode="r:gz") as source:
                for member in source:
                    path = _safe_member_path(member.name, allow_root=True)
                    artifact = node_map.get(path)
                    if member.isdir() and (
                        path == "CommunityAI"
                        or path == package["node_root"]
                        or package["node_root"].startswith(path + "/")
                    ):
                        target = destination.joinpath(*PurePosixPath(path).parts)
                        target.mkdir(mode=0o755, parents=True, exist_ok=True)
                    elif artifact is not None and artifact.kind == "file":
                        stream = source.extractfile(member)
                        if stream is None:
                            raise Q38LinuxHostRuntimeError("archive file is unreadable")
                        target = destination.joinpath(*PurePosixPath(path).parts)
                        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                        with target.open("xb") as output:
                            shutil.copyfileobj(stream, output, length=HASH_CHUNK_BYTES)
                            output.flush()
                            os.fsync(output.fileno())
                        os.chmod(target, artifact.mode or 0o644)
                    elif artifact is not None and artifact.kind == "symlink":
                        if artifact.link_target not in node_paths:
                            raise Q38LinuxHostRuntimeError("node symlink leaves the runtime inventory")
                        pending_links.append((artifact, member.linkname))
            for artifact, linkname in pending_links:
                target = destination.joinpath(*PurePosixPath(artifact.path).parts)
                target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                target.symlink_to(linkname)
    except BaseException:
        _remove_tree_strict(destination, "runtime staging cleanup is incomplete")
        raise


def _verify_runtime_tree(
    root: Path,
    node: Sequence[Artifact],
    *,
    protected: bool,
) -> tuple[int, int]:
    product = root / "CommunityAI"
    runtime = product / "node"
    for directory in (root, product, runtime):
        if not directory.is_dir() or directory.is_symlink():
            raise Q38LinuxHostRuntimeError("installed node runtime is unsafe")
        metadata = directory.lstat()
        if protected and (metadata.st_uid != 0 or metadata.st_gid != 0 or stat.S_IMODE(metadata.st_mode) != 0o755):
            raise Q38LinuxHostRuntimeError("runtime ancestor protection changed")
    if {item.name for item in root.iterdir()} != {"CommunityAI"} or {item.name for item in product.iterdir()} != {
        "node"
    }:
        raise Q38LinuxHostRuntimeError("runtime ancestor inventory changed")
    expected = {item.path: item for item in node}
    observed: set[str] = set()
    folded_observed: set[str] = set()
    resolved_root = runtime.resolve(strict=True)
    for directory, names, files in os.walk(runtime, topdown=True, followlinks=False):
        base = Path(directory)
        metadata = base.lstat()
        if base.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise Q38LinuxHostRuntimeError("runtime directory is unsafe")
        if protected and (metadata.st_uid != 0 or metadata.st_gid != 0 or stat.S_IMODE(metadata.st_mode) != 0o755):
            raise Q38LinuxHostRuntimeError("runtime directory protection changed")
        for name in names:
            child = base / name
            child_metadata = child.lstat()
            if child.is_symlink() or not stat.S_ISDIR(child_metadata.st_mode):
                raise Q38LinuxHostRuntimeError("runtime contains an unsafe directory")
        for name in files:
            candidate = base / name
            relative = "CommunityAI/node/" + candidate.relative_to(runtime).as_posix()
            artifact = expected.get(relative)
            folded = relative.casefold()
            if artifact is None or folded in folded_observed:
                raise Q38LinuxHostRuntimeError("runtime contains an extra or duplicate entry")
            observed.add(relative)
            folded_observed.add(folded)
            metadata = candidate.lstat()
            if artifact.kind == "file":
                if (
                    candidate.is_symlink()
                    or not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_size != artifact.size_bytes
                    or _sha256_stream(candidate) != "sha256:" + artifact.sha256
                    or protected
                    and (
                        metadata.st_uid != 0 or metadata.st_gid != 0 or stat.S_IMODE(metadata.st_mode) != artifact.mode
                    )
                ):
                    raise Q38LinuxHostRuntimeError("runtime file identity changed")
            else:
                if not candidate.is_symlink():
                    raise Q38LinuxHostRuntimeError("runtime symlink identity changed")
                try:
                    resolved = candidate.resolve(strict=True)
                    resolved.relative_to(resolved_root)
                except (OSError, RuntimeError, ValueError) as exc:
                    raise Q38LinuxHostRuntimeError("runtime symlink escaped its root") from exc
                if (
                    _canonical_link_target(relative, os.readlink(candidate)) != artifact.link_target
                    or _sha256_stream(resolved) != "sha256:" + artifact.sha256
                    or protected
                    and (metadata.st_uid != 0 or metadata.st_gid != 0)
                ):
                    raise Q38LinuxHostRuntimeError("runtime symlink target changed")
    if observed != set(expected):
        raise Q38LinuxHostRuntimeError("runtime entry inventory is incomplete")
    return len(observed), sum(item.size_bytes for item in node)


def _protect_runtime(root: Path, node: Sequence[Artifact]) -> None:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise Q38LinuxHostRuntimeError("runtime protection requires root")
    artifact_map = {item.path: item for item in node}
    for directory, names, files in os.walk(root, topdown=False, followlinks=False):
        base = Path(directory)
        for name in files:
            candidate = base / name
            relative = candidate.relative_to(root).as_posix()
            if candidate.is_symlink():
                os.chown(candidate, 0, 0, follow_symlinks=False)
            else:
                artifact = artifact_map.get(relative)
                if artifact is None or artifact.mode is None:
                    raise Q38LinuxHostRuntimeError("runtime protection target is unbound")
                os.chown(candidate, 0, 0)
                os.chmod(candidate, artifact.mode)
        for name in names:
            candidate = base / name
            if candidate.is_symlink():
                raise Q38LinuxHostRuntimeError("runtime contains a directory symlink")
            os.chown(candidate, 0, 0)
            os.chmod(candidate, 0o755)
        os.chown(base, 0, 0)
        os.chmod(base, 0o755)


def _runtime_key(plan: controller.RoutePlan) -> str:
    package_digest = plan.runtime_package["runtime_package_digest"]
    if (
        not isinstance(package_digest, str)
        or not package_digest.startswith("sha256:")
        or _HEX_DIGEST_RE.fullmatch(package_digest[7:]) is None
    ):
        raise Q38LinuxHostRuntimeError("runtime package destination digest is invalid")
    return hashlib.sha256(f"{plan.plan_digest}\0{package_digest}".encode("ascii")).hexdigest()


def _runtime_destination(plan: controller.RoutePlan, paths: HostPaths) -> Path:
    return paths.runtime_base / _runtime_key(plan)


def _qualification_identity() -> QualificationIdentity:
    try:
        import pwd

        account = pwd.getpwnam(QUALIFICATION_USER)
    except (ImportError, KeyError) as exc:
        raise Q38LinuxHostRuntimeError("qualification identity is unavailable") from exc
    if account.pw_uid <= 0 or account.pw_gid <= 0:
        raise Q38LinuxHostRuntimeError("qualification identity must be non-root")
    return QualificationIdentity(QUALIFICATION_USER, account.pw_uid, account.pw_gid)


def _empty_tree(path: Path) -> bool:
    return path.is_dir() and not any(path.rglob("*"))


def _assert_executable_handle(
    supplied: os.stat_result,
    opened: os.stat_result,
) -> None:
    if (
        _file_identity(opened) != _file_identity(supplied)
        or opened.st_uid != 0
        or opened.st_gid != 0
        or stat.S_IMODE(opened.st_mode) != 0o755
    ):
        raise Q38LinuxHostRuntimeError("packaged node executable protection changed")


def _preflight_child(identity: QualificationIdentity) -> Callable[[], None]:
    def configure() -> None:
        import resource

        os.setsid()
        os.setgroups([])
        os.setgid(identity.gid)
        os.setuid(identity.uid)
        os.umask(0o077)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))

    return configure


def _kill_process_group(process_group: int) -> None:
    try:
        os.killpg(process_group, KILL_SIGNAL)
    except ProcessLookupError:
        return
    except OSError as exc:
        raise Q38LinuxHostRuntimeError("preflight process group could not be killed") from exc


def _prove_process_group_empty(
    process_group: int,
    *,
    timeout: float = 5.0,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    deadline = clock() + timeout
    while clock() < deadline:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return
        except OSError as exc:
            raise Q38LinuxHostRuntimeError("preflight process group could not be inspected") from exc
        sleeper(0.05)
    raise Q38LinuxHostRuntimeError("preflight process group cleanup is incomplete")


def _cleanup_started_process(process: Any, *, leader_reaped: bool) -> None:
    _kill_process_group(process.pid)
    if not leader_reaped:
        try:
            process.wait(timeout=30)
        except BaseException as exc:
            raise Q38LinuxHostRuntimeError("packaged preflight leader could not be reaped") from exc
    _prove_process_group_empty(process.pid)


def _remove_preflight_work(work: Path) -> None:
    _remove_tree_strict(work, "preflight work cleanup is incomplete")


def _create_preflight_work(
    work: Path,
    identity: QualificationIdentity,
) -> tuple[Path, Path, Path]:
    created = False
    try:
        work.mkdir(mode=0o711, parents=False, exist_ok=False)
        created = True
        os.chown(work, 0, 0)
        os.chmod(work, 0o711)
        _assert_root_managed(work, directory=True)
        _assert_qualification_traversal(work)
        home = work / "home"
        cache = work / "cache"
        temporary = work / "tmp"
        for candidate in (home, cache, temporary):
            candidate.mkdir(mode=0o700, parents=False, exist_ok=False)
            os.chown(candidate, identity.uid, identity.gid)
            os.chmod(candidate, 0o700)
        return home, cache, temporary
    except BaseException:
        if created:
            _remove_preflight_work(work)
        raise


def _run_packaged_preflight(
    plan: controller.RoutePlan,
    runtime: Path,
    node: Sequence[Artifact],
    paths: HostPaths,
    identity: QualificationIdentity,
    *,
    popen_factory: Callable[..., Any] = subprocess.Popen,
) -> PreflightResult:
    _verify_runtime_tree(runtime, node, protected=True)
    executable = runtime.joinpath(*PurePosixPath(plan.runtime_package["node_executable"]).parts)
    work = paths.work_base / _runtime_key(plan)
    if work.exists() or work.is_symlink():
        raise Q38LinuxHostRuntimeError("preflight work root is not empty")
    paths.work_base.mkdir(mode=0o711, parents=True, exist_ok=True)
    _assert_root_managed(paths.work_base, directory=True)
    os.chown(paths.work_base, 0, 0)
    os.chmod(paths.work_base, 0o711)
    _assert_qualification_traversal(paths.work_base)
    home, cache, temporary = _create_preflight_work(work, identity)
    environment = {
        "HOME": str(home),
        "XDG_CACHE_HOME": str(cache),
        "TMPDIR": str(temporary),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "NO_PROXY": "*",
        "no_proxy": "*",
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
    }
    descriptor: int | None = None
    process: Any | None = None
    leader_reaped = False
    group_clean = False
    try:
        metadata = executable.lstat()
        if executable.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise Q38LinuxHostRuntimeError("packaged node executable is unsafe")
        descriptor = os.open(executable, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        _assert_executable_handle(metadata, opened)
        digest = hashlib.sha256()
        for chunk in iter(lambda: os.read(descriptor, HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
        if "sha256:" + digest.hexdigest() != plan.runtime_package["node_executable_sha256"]:
            raise Q38LinuxHostRuntimeError("packaged node executable digest changed")
        os.lseek(descriptor, 0, os.SEEK_SET)
        with tempfile.TemporaryFile(dir=work) as stdout_file, tempfile.TemporaryFile(dir=work) as stderr_file:
            argv = (str(executable), "edge-acquire", "--help")
            process = popen_factory(
                argv,
                executable=f"/proc/self/fd/{descriptor}",
                cwd=runtime / "CommunityAI" / "node",
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                shell=False,
                close_fds=True,
                pass_fds=(descriptor,),
                preexec_fn=_preflight_child(identity),
            )
            try:
                returncode = process.wait(timeout=180)
            except subprocess.TimeoutExpired as exc:
                raise Q38LinuxHostRuntimeError("packaged preflight timed out") from exc
            leader_reaped = True
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                group_clean = True
            else:
                raise Q38LinuxHostRuntimeError("packaged preflight left descendants")
            stdout_file.seek(0, os.SEEK_END)
            stderr_file.seek(0, os.SEEK_END)
            stdout_bytes = stdout_file.tell()
            stderr_bytes = stderr_file.tell()
            if stdout_bytes > MAX_OUTPUT_BYTES or stderr_bytes > MAX_OUTPUT_BYTES:
                raise Q38LinuxHostRuntimeError("packaged preflight output exceeded its bound")
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read()
            stderr = stderr_file.read()
        if returncode != 0 or b"edge-acquire" not in stdout.lower():
            raise Q38LinuxHostRuntimeError("packaged edge-acquire help preflight failed")
        if not _empty_tree(home) or not _empty_tree(cache) or not _empty_tree(temporary):
            raise Q38LinuxHostRuntimeError("packaged preflight wrote to its isolated state")
        _verify_runtime_tree(runtime, node, protected=True)
        return PreflightResult(returncode, stdout, stderr)
    except OSError as exc:
        raise Q38LinuxHostRuntimeError("packaged preflight could not start") from exc
    finally:
        try:
            if process is not None and not group_clean:
                _cleanup_started_process(process, leader_reaped=leader_reaped)
        finally:
            try:
                if descriptor is not None:
                    os.close(descriptor)
            finally:
                _remove_preflight_work(work)


def _prepared_record(
    plan: controller.RoutePlan,
    action: Mapping[str, Any],
    identity: QualificationIdentity,
    result: PreflightResult,
    context: Mapping[str, Any],
    boot_id: str,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "scope": PREPARED_SCOPE,
        "run_id": plan.run_id,
        "source_commit": plan.source_commit,
        "plan_digest": plan.plan_digest,
        "execution_inventory_digest": plan.execution_inventory_digest,
        "start_action_id": action["action_id"],
        "instance_context_digest": context["context_digest"],
        "resource_name": context["resource_name"],
        "resource_kind": context["resource_kind"],
        "worker_id": context["worker_id"],
        "instance_generation_digest": context["instance_generation_digest"],
        "boot_id": boot_id,
        "runtime_package_digest": plan.runtime_package["runtime_package_digest"],
        "release_archive_sha256": plan.runtime_package["release_archive_sha256"],
        "node_executable_sha256": plan.runtime_package["node_executable_sha256"],
        "node_runtime_inventory_digest": plan.runtime_package["node_runtime_inventory_digest"],
        "node_runtime_entry_count": plan.runtime_package["node_runtime_entry_count"],
        "node_runtime_bytes": plan.runtime_package["node_runtime_bytes"],
        "qualification_user": identity.name,
        "qualification_uid": identity.uid,
        "qualification_gid": identity.gid,
        "preflight_returncode": result.returncode,
        "preflight_stdout_sha256": _sha256(result.stdout),
        "preflight_stdout_bytes": len(result.stdout),
        "preflight_stderr_sha256": _sha256(result.stderr),
        "preflight_stderr_bytes": len(result.stderr),
        "prepared_record_digest": "",
    }
    record["prepared_record_digest"] = _prepared_digest(record)
    return record


def _prepared_digest(record: Mapping[str, Any]) -> str:
    unsigned = dict(record)
    unsigned.pop("prepared_record_digest", None)
    return _sha256(_canonical_bytes(unsigned))


def validate_prepared_record(
    value: Any,
    plan: controller.RoutePlan,
    *,
    instance_context: Mapping[str, Any] | None = None,
    boot_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _PREPARED_FIELDS:
        raise Q38LinuxHostRuntimeError("prepared record schema is invalid")
    resource = plan.resource_by_name.get(value.get("resource_name"))
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != SCHEMA_VERSION
        or value["scope"] != PREPARED_SCOPE
        or value["run_id"] != plan.run_id
        or value["source_commit"] != plan.source_commit
        or value["plan_digest"] != plan.plan_digest
        or value["execution_inventory_digest"] != plan.execution_inventory_digest
        or value["start_action_id"] != controller._action_id(plan, "start_route")
        or resource is None
        or not resource.kind.endswith("instance")
        or value["resource_kind"] != resource.kind
        or value["worker_id"] != resource.worker_id
        or not isinstance(value["boot_id"], str)
        or transport._BOOT_ID_RE.fullmatch(value["boot_id"]) is None
        or value["runtime_package_digest"] != plan.runtime_package["runtime_package_digest"]
        or value["release_archive_sha256"] != plan.runtime_package["release_archive_sha256"]
        or value["node_executable_sha256"] != plan.runtime_package["node_executable_sha256"]
        or value["node_runtime_inventory_digest"] != plan.runtime_package["node_runtime_inventory_digest"]
        or value["node_runtime_entry_count"] != plan.runtime_package["node_runtime_entry_count"]
        or value["node_runtime_bytes"] != plan.runtime_package["node_runtime_bytes"]
        or value["qualification_user"] != QUALIFICATION_USER
        or type(value["qualification_uid"]) is not int
        or value["qualification_uid"] <= 0
        or type(value["qualification_gid"]) is not int
        or value["qualification_gid"] <= 0
        or type(value["preflight_returncode"]) is not int
        or value["preflight_returncode"] != 0
        or type(value["preflight_stdout_bytes"]) is not int
        or not 1 <= value["preflight_stdout_bytes"] <= MAX_OUTPUT_BYTES
        or type(value["preflight_stderr_bytes"]) is not int
        or not 0 <= value["preflight_stderr_bytes"] <= MAX_OUTPUT_BYTES
    ):
        raise Q38LinuxHostRuntimeError("prepared record identity is invalid")
    for field in (
        "instance_context_digest",
        "instance_generation_digest",
        "preflight_stdout_sha256",
        "preflight_stderr_sha256",
        "prepared_record_digest",
    ):
        _digest_field(value[field], field)
    if instance_context is not None:
        expected_context = {
            "instance_context_digest": instance_context.get("context_digest"),
            "resource_name": instance_context.get("resource_name"),
            "resource_kind": instance_context.get("resource_kind"),
            "worker_id": instance_context.get("worker_id"),
            "instance_generation_digest": instance_context.get("instance_generation_digest"),
        }
        if any(value[field] != expected for field, expected in expected_context.items()):
            raise Q38LinuxHostRuntimeError("prepared record instance binding changed")
    if boot_id is not None and value["boot_id"] != boot_id:
        raise Q38LinuxHostRuntimeError("prepared record boot identity changed")
    if value["prepared_record_digest"] != _prepared_digest(value):
        raise Q38LinuxHostRuntimeError("prepared record digest changed")
    return dict(value)


def _load_prepared_record(
    path: Path,
    plan: controller.RoutePlan,
    *,
    instance_context: Mapping[str, Any],
    boot_id: str,
) -> dict[str, Any]:
    _assert_root_private_file(path)
    value = _strict_json(_regular_bytes(path))
    return validate_prepared_record(
        value,
        plan,
        instance_context=instance_context,
        boot_id=boot_id,
    )


def build_prepared_status_envelope(
    prepared_record: Mapping[str, Any],
    inputs: TransportInputs,
    plan: controller.RoutePlan,
    *,
    expected_resource_name: str,
    expected_generation_digest: str,
    revision: int,
    published_at_unix: int,
) -> dict[str, Any]:
    try:
        context = transport.validate_instance_context(
            inputs.context,
            plan,
            key=inputs.key,
            now_unix=published_at_unix,
            expected_resource_name=expected_resource_name,
            expected_generation_digest=expected_generation_digest,
        )
        prepared = validate_prepared_record(
            dict(prepared_record),
            plan,
            instance_context=context,
            boot_id=inputs.boot_id,
        )
        envelope = transport.build_status_envelope(
            context,
            transport.initial_status_payload(context, plan),
            plan,
            key=inputs.key,
            boot_id=inputs.boot_id,
            revision=revision,
            published_at_unix=published_at_unix,
            prepared_record_digest=prepared["prepared_record_digest"],
        )
    except transport.Q38LinuxHostTransportError as exc:
        raise Q38LinuxHostRuntimeError(str(exc)) from exc
    if envelope["prepared_record_digest"] != prepared["prepared_record_digest"]:
        raise Q38LinuxHostRuntimeError("host status does not bind the protected prepared record")
    return envelope


@contextmanager
def _prepared_state_lock(parent: Path) -> Iterator[None]:
    if os.name != "posix":
        yield
        return
    import fcntl

    lock = parent / ".prepared.lock"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            lock,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, 0o600)
        opened = os.fstat(descriptor)
        current = lock.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or _file_identity(opened) != _file_identity(current)
            or opened.st_uid != 0
            or opened.st_gid != 0
            or stat.S_IMODE(opened.st_mode) != 0o600
        ):
            raise Q38LinuxHostRuntimeError("prepared state lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except Q38LinuxHostRuntimeError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise Q38LinuxHostRuntimeError("prepared state lock is unavailable") from exc
    try:
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _remove_stale_prepared_temporaries(parent: Path) -> None:
    for candidate in parent.iterdir():
        if not candidate.name.startswith(".prepared.") or not candidate.name.endswith(".tmp"):
            continue
        metadata = candidate.lstat()
        if (
            candidate.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or os.name == "posix"
            and (metadata.st_uid != 0 or metadata.st_gid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600)
        ):
            raise Q38LinuxHostRuntimeError("stale prepared temporary is unsafe")
        candidate.unlink()
    if any(
        candidate.name.startswith(".prepared.") and candidate.name.endswith(".tmp") for candidate in parent.iterdir()
    ):
        raise Q38LinuxHostRuntimeError("stale prepared cleanup is incomplete")


def _accept_existing_prepared(
    path: Path,
    value: Mapping[str, Any],
    plan: controller.RoutePlan,
) -> None:
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise Q38LinuxHostRuntimeError("prepared record target is unsafe")
    _assert_root_managed(path, directory=False)
    existing = validate_prepared_record(
        _strict_json(_regular_bytes(path)),
        plan,
    )
    if not _same_json_value(existing, dict(value)):
        raise Q38LinuxHostRuntimeError("prepared record already binds another result")


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_prepared_locked(
    path: Path,
    value: Mapping[str, Any],
    plan: controller.RoutePlan,
) -> bool:
    payload = _canonical_bytes(value)
    _remove_stale_prepared_temporaries(path.parent)
    if path.exists() or path.is_symlink():
        _accept_existing_prepared(path, value, plan)
        return False
    descriptor, raw = tempfile.mkstemp(prefix=".prepared.", suffix=".tmp", dir=path.parent)
    temporary = Path(raw)
    linked = False
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.chown(temporary, 0, 0)
        os.chmod(temporary, 0o600)
        try:
            os.link(temporary, path)
        except FileExistsError:
            _accept_existing_prepared(path, value, plan)
            return False
        linked = True
        _accept_existing_prepared(path, value, plan)
        temporary.unlink()
        linked = False
        return True
    except Q38LinuxHostRuntimeError:
        raise
    except OSError as exc:
        raise Q38LinuxHostRuntimeError("prepared record could not be committed") from exc
    finally:
        temporary.unlink(missing_ok=True)
        if linked:
            _remove_exact_published_file(path, payload, "prepared record")


def _atomic_prepared(
    path: Path,
    value: Mapping[str, Any],
    plan: controller.RoutePlan,
) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _assert_root_managed(path.parent, directory=True)
    with _prepared_state_lock(path.parent):
        _atomic_prepared_locked(path, value, plan)
        _fsync_directory(path.parent)


def _remove_stale_status_temporaries(parent: Path) -> None:
    for candidate in parent.iterdir():
        if not candidate.name.startswith(".status.") or not candidate.name.endswith(".tmp"):
            continue
        metadata = candidate.lstat()
        if (
            candidate.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or os.name == "posix"
            and (metadata.st_uid != 0 or metadata.st_gid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600)
        ):
            raise Q38LinuxHostRuntimeError("stale status temporary is unsafe")
        candidate.unlink()
    if any(candidate.name.startswith(".status.") and candidate.name.endswith(".tmp") for candidate in parent.iterdir()):
        raise Q38LinuxHostRuntimeError("stale status cleanup is incomplete")


def _accept_existing_status(
    path: Path,
    intended: Mapping[str, Any],
    plan: controller.RoutePlan,
    inputs: TransportInputs,
    *,
    expected_resource_name: str,
    expected_generation_digest: str,
    now_unix: int,
) -> dict[str, Any]:
    _assert_root_private_file(path)
    try:
        existing = transport.decode_status_envelope(_regular_bytes(path, maximum=transport.MAX_ENVELOPE_BYTES))
        validated = transport.validate_status_envelope(
            existing,
            plan,
            key=inputs.key,
            now_unix=now_unix,
            expected_resource_name=expected_resource_name,
            expected_generation_digest=expected_generation_digest,
            expected_boot_id=inputs.boot_id,
        )
    except transport.Q38LinuxHostTransportError as exc:
        raise Q38LinuxHostRuntimeError(str(exc)) from exc
    stable_fields = (
        "context",
        "boot_id",
        "revision",
        "prepared_record_digest",
        "payload",
        "payload_digest",
    )
    if any(not _same_json_value(validated[field], intended[field]) for field in stable_fields):
        raise Q38LinuxHostRuntimeError("host status already binds another result")
    return validated


def _atomic_status_locked(
    path: Path,
    value: Mapping[str, Any],
    plan: controller.RoutePlan,
    inputs: TransportInputs,
    *,
    expected_resource_name: str,
    expected_generation_digest: str,
    now_unix: int,
) -> tuple[dict[str, Any], bool]:
    payload = transport.encode_status_envelope(value)
    _remove_stale_status_temporaries(path.parent)
    if path.exists() or path.is_symlink():
        return (
            _accept_existing_status(
                path,
                value,
                plan,
                inputs,
                expected_resource_name=expected_resource_name,
                expected_generation_digest=expected_generation_digest,
                now_unix=now_unix,
            ),
            False,
        )
    descriptor, raw = tempfile.mkstemp(prefix=".status.", suffix=".tmp", dir=path.parent)
    temporary = Path(raw)
    linked = False
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.chown(temporary, 0, 0)
        os.chmod(temporary, 0o600)
        try:
            os.link(temporary, path)
        except FileExistsError:
            return (
                _accept_existing_status(
                    path,
                    value,
                    plan,
                    inputs,
                    expected_resource_name=expected_resource_name,
                    expected_generation_digest=expected_generation_digest,
                    now_unix=now_unix,
                ),
                False,
            )
        linked = True
        result = _accept_existing_status(
            path,
            value,
            plan,
            inputs,
            expected_resource_name=expected_resource_name,
            expected_generation_digest=expected_generation_digest,
            now_unix=now_unix,
        )
        temporary.unlink()
        linked = False
        return result, True
    except Q38LinuxHostRuntimeError:
        raise
    except OSError as exc:
        raise Q38LinuxHostRuntimeError("host status could not be committed") from exc
    finally:
        temporary.unlink(missing_ok=True)
        if linked:
            _remove_exact_published_file(path, payload, "host status")


def _atomic_status(
    path: Path,
    value: Mapping[str, Any],
    plan: controller.RoutePlan,
    inputs: TransportInputs,
    *,
    expected_resource_name: str,
    expected_generation_digest: str,
    now_unix: int,
) -> dict[str, Any]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _assert_root_managed(path.parent, directory=True)
    with _prepared_state_lock(path.parent):
        result, _created = _atomic_status_locked(
            path,
            value,
            plan,
            inputs,
            expected_resource_name=expected_resource_name,
            expected_generation_digest=expected_generation_digest,
            now_unix=now_unix,
        )
        _fsync_directory(path.parent)
        return result


def _remove_exact_published_file(path: Path, expected: bytes, label: str) -> None:
    if not path.exists() and not path.is_symlink():
        return
    _assert_root_private_file(path)
    if _regular_bytes(path, maximum=max(len(expected), 1)) != expected:
        raise Q38LinuxHostRuntimeError(f"{label} rollback target changed")
    path.unlink()


def _publish_prepared_status_locked(
    paths: HostPaths,
    record: Mapping[str, Any],
    plan: controller.RoutePlan,
    inputs: TransportInputs,
    *,
    expected_resource_name: str,
    expected_generation_digest: str,
    now_unix: int,
) -> dict[str, Any]:
    parent = paths.prepared_record.parent
    prepared_payload = _canonical_bytes(record)
    prepared_created = False
    status_created = False
    status_payload: bytes | None = None
    try:
        prepared_created = _atomic_prepared_locked(paths.prepared_record, record, plan)
        persisted = _load_prepared_record(
            paths.prepared_record,
            plan,
            instance_context=inputs.context,
            boot_id=inputs.boot_id,
        )
        envelope = build_prepared_status_envelope(
            persisted,
            inputs,
            plan,
            expected_resource_name=expected_resource_name,
            expected_generation_digest=expected_generation_digest,
            revision=1,
            published_at_unix=now_unix,
        )
        status_payload = transport.encode_status_envelope(envelope)
        _status, status_created = _atomic_status_locked(
            paths.status_envelope,
            envelope,
            plan,
            inputs,
            expected_resource_name=expected_resource_name,
            expected_generation_digest=expected_generation_digest,
            now_unix=now_unix,
        )
        _fsync_directory(parent)
        return persisted
    except BaseException:
        if status_created and status_payload is not None:
            _remove_exact_published_file(paths.status_envelope, status_payload, "host status")
        if prepared_created:
            _remove_exact_published_file(paths.prepared_record, prepared_payload, "prepared record")
        _remove_stale_status_temporaries(parent)
        _remove_stale_prepared_temporaries(parent)
        _fsync_directory(parent)
        raise


def _state_parent(paths: HostPaths) -> Path:
    if paths.status_envelope.parent != paths.prepared_record.parent:
        raise Q38LinuxHostRuntimeError("host status is outside the protected state boundary")
    parent = paths.prepared_record.parent
    created = False
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=False)
        created = True
    except FileExistsError:
        pass
    except OSError as exc:
        raise Q38LinuxHostRuntimeError("protected host state is unavailable") from exc
    if created:
        os.chown(parent, 0, 0)
        os.chmod(parent, 0o700)
    _assert_root_managed(parent, directory=True)
    return parent


def _cleanup_marker_value(
    plan: controller.RoutePlan,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": CLEANUP_SCOPE,
        "run_id": plan.run_id,
        "source_commit": plan.source_commit,
        "plan_digest": plan.plan_digest,
        "execution_inventory_digest": plan.execution_inventory_digest,
        "cleanup_action_id": controller._action_id(plan, "cleanup_route"),
        "instance_context_digest": context["context_digest"],
        "resource_name": context["resource_name"],
        "resource_kind": context["resource_kind"],
        "worker_id": context["worker_id"],
        "instance_generation_digest": context["instance_generation_digest"],
        "runtime_key": _runtime_key(plan),
    }


def _cleanup_marker_path_for_generation(
    paths: HostPaths,
    generation_digest: str,
) -> Path:
    if not isinstance(generation_digest, str) or controller._DIGEST_RE.fullmatch(generation_digest) is None:
        raise Q38LinuxHostRuntimeError("cleanup generation digest is invalid")
    return paths.prepared_record.parent / f"cleaned-{generation_digest.removeprefix('sha256:')}.json"


def _cleanup_marker_path(paths: HostPaths, context: Mapping[str, Any]) -> Path:
    return _cleanup_marker_path_for_generation(
        paths,
        str(context["instance_generation_digest"]),
    )


def _validate_cleanup_marker_generation(
    path: Path,
    plan: controller.RoutePlan,
    *,
    expected_resource_name: str,
    expected_generation_digest: str,
) -> dict[str, Any]:
    resource = plan.resource_by_name.get(expected_resource_name)
    if resource is None or not resource.kind.endswith("instance"):
        raise Q38LinuxHostRuntimeError("cleanup marker resource is invalid")
    _assert_root_private_file(path)
    value = _strict_json(_regular_bytes(path))
    fixed = {
        "schema_version": SCHEMA_VERSION,
        "scope": CLEANUP_SCOPE,
        "run_id": plan.run_id,
        "source_commit": plan.source_commit,
        "plan_digest": plan.plan_digest,
        "execution_inventory_digest": plan.execution_inventory_digest,
        "cleanup_action_id": controller._action_id(plan, "cleanup_route"),
        "resource_name": resource.name,
        "resource_kind": resource.kind,
        "worker_id": resource.worker_id,
        "instance_generation_digest": expected_generation_digest,
        "runtime_key": _runtime_key(plan),
    }
    if set(value) != {*fixed, "instance_context_digest"} or any(
        value[field] != expected for field, expected in fixed.items()
    ):
        raise Q38LinuxHostRuntimeError("cleanup marker binds another host generation")
    context_digest = value["instance_context_digest"]
    if not isinstance(context_digest, str) or controller._DIGEST_RE.fullmatch(context_digest) is None:
        raise Q38LinuxHostRuntimeError("cleanup marker context is invalid")
    return value


def _remove_stale_cleanup_temporaries(parent: Path) -> None:
    for candidate in parent.iterdir():
        if not candidate.name.startswith(".cleanup.") or not candidate.name.endswith(".tmp"):
            continue
        _assert_root_private_file(candidate)
        candidate.unlink()
    if any(
        candidate.name.startswith(".cleanup.") and candidate.name.endswith(".tmp") for candidate in parent.iterdir()
    ):
        raise Q38LinuxHostRuntimeError("stale cleanup marker cleanup is incomplete")


def _accept_existing_cleanup_marker(path: Path, value: Mapping[str, Any]) -> None:
    _assert_root_private_file(path)
    if not _same_json_value(_strict_json(_regular_bytes(path)), dict(value)):
        raise Q38LinuxHostRuntimeError("cleanup marker binds another host generation")


def _atomic_cleanup_marker_locked(path: Path, value: Mapping[str, Any]) -> None:
    payload = _canonical_bytes(value)
    _remove_stale_cleanup_temporaries(path.parent)
    if path.exists() or path.is_symlink():
        _accept_existing_cleanup_marker(path, value)
        return
    descriptor, raw = tempfile.mkstemp(prefix=".cleanup.", suffix=".tmp", dir=path.parent)
    temporary = Path(raw)
    linked = False
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.chown(temporary, 0, 0)
        os.chmod(temporary, 0o600)
        try:
            os.link(temporary, path)
        except FileExistsError:
            _accept_existing_cleanup_marker(path, value)
            return
        linked = True
        _accept_existing_cleanup_marker(path, value)
        temporary.unlink()
        linked = False
    except Q38LinuxHostRuntimeError:
        raise
    except OSError as exc:
        raise Q38LinuxHostRuntimeError("cleanup marker could not be committed") from exc
    finally:
        temporary.unlink(missing_ok=True)
        if linked:
            _remove_exact_published_file(path, payload, "cleanup marker")


def _reject_cleaned_generation_locked(path: Path, value: Mapping[str, Any]) -> None:
    _remove_stale_cleanup_temporaries(path.parent)
    if not path.exists() and not path.is_symlink():
        return
    _accept_existing_cleanup_marker(path, value)
    raise Q38LinuxHostRuntimeError("host generation cleanup is terminal")


def _remove_exact_tree(path: Path, parent: Path) -> None:
    if path.parent != parent or not path.is_absolute() or path in {Path("/"), Path.home()}:
        raise Q38LinuxHostRuntimeError("cleanup target is outside the runtime boundary")
    if not path.exists() and not path.is_symlink():
        return
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise Q38LinuxHostRuntimeError("cleanup target is unsafe")
    shutil.rmtree(path)
    if path.exists() or path.is_symlink():
        raise Q38LinuxHostRuntimeError("runtime cleanup is incomplete")


def _remove_stale_delivery_temporaries(parent: Path) -> None:
    for candidate in parent.iterdir():
        if not candidate.name.startswith(".delivery.") or not candidate.name.endswith(".tmp"):
            continue
        _assert_root_private_file(candidate)
        candidate.unlink()
    if any(
        candidate.name.startswith(".delivery.") and candidate.name.endswith(".tmp") for candidate in parent.iterdir()
    ):
        raise Q38LinuxHostRuntimeError("stale delivery cleanup is incomplete")


def _delivery_path(paths: HostPaths) -> Path:
    path = paths.transport_bundle
    if path is None or path.parent != paths.plan.parent or path.name != "instance-delivery.bin":
        raise Q38LinuxHostRuntimeError("transport delivery target is invalid")
    _assert_root_managed(path.parent, directory=True)
    return path


def _decode_delivery(
    payload: bytes,
    plan: controller.RoutePlan,
    *,
    expected_resource_name: str,
    expected_generation_digest: str,
    now_unix: int,
) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    try:
        return transport.decode_instance_delivery(
            payload,
            plan,
            now_unix=now_unix,
            expected_resource_name=expected_resource_name,
            expected_generation_digest=expected_generation_digest,
        )
    except transport.Q38LinuxHostTransportError as exc:
        raise Q38LinuxHostRuntimeError(str(exc)) from exc


def install_instance_delivery(
    paths: HostPaths,
    payload: bytes,
    *,
    expected_resource_name: str,
    expected_generation_digest: str,
    now_unix: int | None = None,
) -> dict[str, Any]:
    """Atomically install one authenticated context/key bundle under the lifecycle lock."""

    installation_time = int(time.time()) if now_unix is None else now_unix
    plan, _action = _load_plan_and_action(
        paths.plan,
        paths.start_action,
        paths.source_root,
        expected_action="start_route",
        now_unix=installation_time,
    )
    record, context, _key = _decode_delivery(
        payload,
        plan,
        expected_resource_name=expected_resource_name,
        expected_generation_digest=expected_generation_digest,
        now_unix=installation_time,
    )
    delivery = transport.InstanceDelivery(record, payload)
    target = _delivery_path(paths)
    state_parent = _state_parent(paths)
    cleanup_marker = _cleanup_marker_path(paths, context)
    cleanup_value = _cleanup_marker_value(plan, context)
    with _prepared_state_lock(state_parent):
        _reject_cleaned_generation_locked(cleanup_marker, cleanup_value)
        _remove_stale_delivery_temporaries(target.parent)
        if target.exists() or target.is_symlink():
            _assert_root_private_file(target)
            current_payload = _regular_bytes(
                target,
                maximum=transport.MAX_DELIVERY_BYTES,
            )
            current_record, _current_context, _current_key = _decode_delivery(
                current_payload,
                plan,
                expected_resource_name=expected_resource_name,
                expected_generation_digest=expected_generation_digest,
                now_unix=installation_time,
            )
            if current_payload == payload:
                return transport.build_instance_delivery_receipt(
                    delivery,
                    plan,
                    installed_at_unix=installation_time,
                )
            if (
                record["key_epoch"] != current_record["key_epoch"] + 1
                or record["previous_key_record_digest"] != current_record["key_record_digest"]
            ):
                raise Q38LinuxHostRuntimeError("instance delivery rotation is stale or discontinuous")
        elif record["key_epoch"] != 1 or record["previous_key_record_digest"] is not None:
            raise Q38LinuxHostRuntimeError("initial instance delivery does not begin at key epoch one")
        descriptor, raw = tempfile.mkstemp(
            prefix=".delivery.",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary = Path(raw)
        replaced = False
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.chown(temporary, 0, 0)
            os.chmod(temporary, 0o600)
            _assert_root_private_file(temporary)
            os.replace(temporary, target)
            replaced = True
            _assert_root_private_file(target)
            installed_payload = _regular_bytes(
                target,
                maximum=transport.MAX_DELIVERY_BYTES,
            )
            installed_record, _installed_context, _installed_key = _decode_delivery(
                installed_payload,
                plan,
                expected_resource_name=expected_resource_name,
                expected_generation_digest=expected_generation_digest,
                now_unix=installation_time,
            )
            if installed_payload != payload or installed_record != record:
                raise Q38LinuxHostRuntimeError("installed instance delivery changed")
            _fsync_directory(target.parent)
        except Q38LinuxHostRuntimeError:
            raise
        except OSError as exc:
            raise Q38LinuxHostRuntimeError("instance delivery could not be installed") from exc
        finally:
            if not replaced:
                temporary.unlink(missing_ok=True)
        return transport.build_instance_delivery_receipt(
            delivery,
            plan,
            installed_at_unix=installation_time,
        )


def prepare(
    paths: HostPaths,
    *,
    expected_resource_name: str,
    expected_generation_digest: str,
    now_unix: int | None = None,
    identity: QualificationIdentity | None = None,
    protector: Callable[[Path, Sequence[Artifact]], None] = _protect_runtime,
    preflight: Callable[
        [controller.RoutePlan, Path, Sequence[Artifact], HostPaths, QualificationIdentity],
        PreflightResult,
    ] = _run_packaged_preflight,
) -> dict[str, Any]:
    entry_time = int(time.time()) if now_unix is None else now_unix
    plan, action = _load_plan_and_action(
        paths.plan,
        paths.start_action,
        paths.source_root,
        expected_action="start_route",
        now_unix=entry_time,
    )
    inputs = _load_transport_inputs(
        plan,
        paths,
        expected_resource_name=expected_resource_name,
        expected_generation_digest=expected_generation_digest,
        now_unix=entry_time,
    )
    artifacts, node = _load_release_inventory(plan, paths)
    state_parent = _state_parent(paths)
    cleanup_marker = _cleanup_marker_path(paths, inputs.context)
    cleanup_value = _cleanup_marker_value(plan, inputs.context)
    with _prepared_state_lock(state_parent):
        _reject_cleaned_generation_locked(cleanup_marker, cleanup_value)
        paths.runtime_base.mkdir(mode=0o755, parents=True, exist_ok=True)
        paths.work_base.mkdir(mode=0o711, parents=True, exist_ok=True)
        _assert_root_managed(paths.runtime_base, directory=True)
        _assert_root_managed(paths.work_base, directory=True)
        os.chown(paths.runtime_base, 0, 0)
        os.chmod(paths.runtime_base, 0o755)
        _assert_qualification_traversal(paths.runtime_base)
        os.chown(paths.work_base, 0, 0)
        os.chmod(paths.work_base, 0o711)
        destination = _runtime_destination(plan, paths)
        created = False
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_dir():
                raise Q38LinuxHostRuntimeError("runtime destination is foreign")
            _verify_runtime_tree(destination, node, protected=True)
        else:
            temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=paths.runtime_base))
            shutil.rmtree(temporary)
            try:
                _extract_verified_archive(
                    paths.release_root / plan.runtime_package["release_archive_name"],
                    plan.runtime_package,
                    artifacts,
                    node,
                    temporary,
                )
                protector(temporary, node)
                _verify_runtime_tree(temporary, node, protected=True)
                os.replace(temporary, destination)
                created = True
            finally:
                if temporary.exists() or temporary.is_symlink():
                    _remove_tree_strict(temporary, "runtime staging cleanup is incomplete")
        try:
            resolved_identity = _qualification_identity() if identity is None else identity
            result = preflight(plan, destination, node, paths, resolved_identity)
            publication_time = int(time.time()) if now_unix is None else now_unix
            refreshed = _load_transport_inputs(
                plan,
                paths,
                expected_resource_name=expected_resource_name,
                expected_generation_digest=expected_generation_digest,
                now_unix=publication_time,
            )
            if (
                not _same_json_value(refreshed.context, inputs.context)
                or refreshed.key != inputs.key
                or refreshed.boot_id != inputs.boot_id
            ):
                raise Q38LinuxHostRuntimeError("transport inputs changed during host preparation")
            record = _prepared_record(
                plan,
                action,
                resolved_identity,
                result,
                refreshed.context,
                refreshed.boot_id,
            )
            validate_prepared_record(
                record,
                plan,
                instance_context=refreshed.context,
                boot_id=refreshed.boot_id,
            )
            return _publish_prepared_status_locked(
                paths,
                record,
                plan,
                refreshed,
                expected_resource_name=expected_resource_name,
                expected_generation_digest=expected_generation_digest,
                now_unix=publication_time,
            )
        except BaseException:
            if created:
                _remove_exact_tree(destination, paths.runtime_base)
            raise


def _publish_guest_attribute(
    payload: bytes,
    *,
    connection_factory: Callable[..., Any] = http.client.HTTPConnection,
) -> None:
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= transport.MAX_ENVELOPE_BYTES:
        raise Q38LinuxHostRuntimeError("host status publication payload is invalid")
    connection: Any | None = None
    try:
        connection = connection_factory(
            METADATA_HOST,
            METADATA_PORT,
            timeout=METADATA_TIMEOUT_SECONDS,
        )
        connection.request(
            "PUT",
            GUEST_ATTRIBUTE_PATH,
            body=payload,
            headers={
                "Metadata-Flavor": "Google",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(payload)),
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        declared_length = response.getheader("Content-Length")
        declared: int | None = None
        if declared_length is not None:
            if not isinstance(declared_length, str) or re.fullmatch(r"[0-9]+", declared_length) is None:
                raise Q38LinuxHostRuntimeError("metadata publication response length is invalid")
            if len(declared_length) > len(str(MAX_METADATA_RESPONSE_BYTES)):
                raise Q38LinuxHostRuntimeError("metadata publication response exceeded its size bound")
            declared = int(declared_length, 10)
            if declared > MAX_METADATA_RESPONSE_BYTES:
                raise Q38LinuxHostRuntimeError("metadata publication response exceeded its size bound")
        response_payload = response.read(MAX_METADATA_RESPONSE_BYTES + 1)
        if not isinstance(response_payload, bytes) or len(response_payload) > MAX_METADATA_RESPONSE_BYTES:
            raise Q38LinuxHostRuntimeError("metadata publication response exceeded its size bound")
        if declared is not None and len(response_payload) != declared:
            raise Q38LinuxHostRuntimeError("metadata publication response length changed")
        response_flavor = response.getheader("Metadata-Flavor")
        if (
            type(response.status) is not int
            or response.status != 200
            or not isinstance(response_flavor, str)
            or response_flavor != "Google"
        ):
            raise Q38LinuxHostRuntimeError("metadata publication was not acknowledged")
    except Q38LinuxHostRuntimeError:
        raise
    except (OSError, http.client.HTTPException) as exc:
        raise Q38LinuxHostRuntimeError("metadata publication failed") from exc
    finally:
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass


def publish_status(
    paths: HostPaths,
    *,
    expected_resource_name: str,
    expected_generation_digest: str,
    now_unix: int | None = None,
    sender: Callable[[bytes], None] = _publish_guest_attribute,
) -> dict[str, Any]:
    if paths.status_envelope.parent != paths.prepared_record.parent:
        raise Q38LinuxHostRuntimeError("host status is outside the protected state boundary")
    state_parent = paths.prepared_record.parent
    if state_parent.is_symlink() or not state_parent.is_dir():
        raise Q38LinuxHostRuntimeError("protected host state is unavailable")
    _assert_root_managed(state_parent, directory=True)
    with _prepared_state_lock(state_parent):
        verification_time = int(time.time()) if now_unix is None else now_unix
        plan, _action = _load_plan_and_action(
            paths.plan,
            paths.start_action,
            paths.source_root,
            expected_action="start_route",
            now_unix=verification_time,
        )
        inputs = _load_transport_inputs(
            plan,
            paths,
            expected_resource_name=expected_resource_name,
            expected_generation_digest=expected_generation_digest,
            now_unix=verification_time,
        )
        cleanup_marker = _cleanup_marker_path(paths, inputs.context)
        _reject_cleaned_generation_locked(
            cleanup_marker,
            _cleanup_marker_value(plan, inputs.context),
        )
        if not paths.prepared_record.exists() or paths.prepared_record.is_symlink():
            raise Q38LinuxHostRuntimeError("protected prepared record is unavailable")
        prepared = _load_prepared_record(
            paths.prepared_record,
            plan,
            instance_context=inputs.context,
            boot_id=inputs.boot_id,
        )
        _assert_root_private_file(paths.status_envelope)
        raw_envelope = _regular_bytes(
            paths.status_envelope,
            maximum=transport.MAX_ENVELOPE_BYTES,
        )
        try:
            envelope = transport.validate_status_envelope(
                transport.decode_status_envelope(raw_envelope),
                plan,
                key=inputs.key,
                now_unix=verification_time,
                expected_resource_name=expected_resource_name,
                expected_generation_digest=expected_generation_digest,
                expected_boot_id=inputs.boot_id,
            )
        except transport.Q38LinuxHostTransportError as exc:
            raise Q38LinuxHostRuntimeError(str(exc)) from exc
        if (
            envelope["prepared_record_digest"] != prepared["prepared_record_digest"]
            or transport.encode_status_envelope(envelope) != raw_envelope
        ):
            raise Q38LinuxHostRuntimeError("host status does not bind the protected prepared record")
        sender(raw_envelope)
        return {
            "schema_version": SCHEMA_VERSION,
            "scope": PUBLICATION_SCOPE,
            "run_id": plan.run_id,
            "source_commit": plan.source_commit,
            "plan_digest": plan.plan_digest,
            "resource_name": expected_resource_name,
            "instance_generation_digest": expected_generation_digest,
            "context_digest": inputs.context["context_digest"],
            "boot_id": inputs.boot_id,
            "revision": envelope["revision"],
            "prepared_record_digest": prepared["prepared_record_digest"],
            "envelope_sha256": "sha256:" + hashlib.sha256(raw_envelope).hexdigest(),
            "envelope_bytes": len(raw_envelope),
        }


def cleanup(
    paths: HostPaths,
    *,
    expected_resource_name: str,
    expected_generation_digest: str,
    now_unix: int | None = None,
) -> None:
    verification_time = int(time.time()) if now_unix is None else now_unix
    plan, _action = _load_plan_and_action(
        paths.plan,
        paths.cleanup_action,
        paths.source_root,
        expected_action="cleanup_route",
        now_unix=verification_time,
    )
    destination = _runtime_destination(plan, paths)
    work = paths.work_base / _runtime_key(plan)
    state_parent = _state_parent(paths)
    cleanup_marker = _cleanup_marker_path_for_generation(
        paths,
        expected_generation_digest,
    )
    bundle_path = paths.transport_bundle
    with _prepared_state_lock(state_parent):
        _remove_stale_prepared_temporaries(state_parent)
        _remove_stale_status_temporaries(state_parent)
        _remove_stale_cleanup_temporaries(state_parent)
        if bundle_path is not None:
            _delivery_path(paths)
            _remove_stale_delivery_temporaries(bundle_path.parent)
        if bundle_path is not None and not bundle_path.exists() and not bundle_path.is_symlink():
            if not cleanup_marker.exists() and not cleanup_marker.is_symlink():
                raise Q38LinuxHostRuntimeError("transport delivery is unavailable before terminal cleanup")
            _validate_cleanup_marker_generation(
                cleanup_marker,
                plan,
                expected_resource_name=expected_resource_name,
                expected_generation_digest=expected_generation_digest,
            )
            _remove_exact_tree(destination, paths.runtime_base)
            _remove_exact_tree(work, paths.work_base)
            for target in (paths.status_envelope, paths.prepared_record):
                if target.exists() or target.is_symlink():
                    _assert_root_private_file(target)
                    target.unlink()
            _fsync_directory(state_parent)
        else:
            context, _key = _load_authenticated_context(
                plan,
                paths,
                expected_resource_name=expected_resource_name,
                expected_generation_digest=expected_generation_digest,
                now_unix=verification_time,
                allow_expired_for_cleanup=True,
            )
            cleanup_value = _cleanup_marker_value(plan, context)
            if cleanup_marker.exists() or cleanup_marker.is_symlink():
                _accept_existing_cleanup_marker(cleanup_marker, cleanup_value)
            prepared: dict[str, Any] | None = None
            if paths.prepared_record.exists() or paths.prepared_record.is_symlink():
                _assert_root_private_file(paths.prepared_record)
                prepared = validate_prepared_record(
                    _strict_json(_regular_bytes(paths.prepared_record)),
                    plan,
                    instance_context=context,
                )
            if paths.status_envelope.exists() or paths.status_envelope.is_symlink():
                if prepared is None:
                    raise Q38LinuxHostRuntimeError("host status has no protected prepared record")
                _assert_root_private_file(paths.status_envelope)
                try:
                    status = transport.decode_status_envelope(
                        _regular_bytes(
                            paths.status_envelope,
                            maximum=transport.MAX_ENVELOPE_BYTES,
                        )
                    )
                except transport.Q38LinuxHostTransportError as exc:
                    raise Q38LinuxHostRuntimeError(str(exc)) from exc
                status_context = status.get("context")
                if (
                    not isinstance(status_context, dict)
                    or status_context.get("context_digest") != context["context_digest"]
                    or status_context.get("resource_name") != expected_resource_name
                    or status_context.get("instance_generation_digest") != expected_generation_digest
                    or status.get("boot_id") != prepared["boot_id"]
                    or status.get("prepared_record_digest") != prepared["prepared_record_digest"]
                ):
                    raise Q38LinuxHostRuntimeError("host status cleanup generation changed")
            _atomic_cleanup_marker_locked(cleanup_marker, cleanup_value)
            _fsync_directory(state_parent)
            _remove_exact_tree(destination, paths.runtime_base)
            _remove_exact_tree(work, paths.work_base)
            if paths.status_envelope.exists():
                paths.status_envelope.unlink()
            if prepared is not None:
                paths.prepared_record.unlink()
            if bundle_path is not None:
                _assert_root_private_file(bundle_path)
                installed = _regular_bytes(
                    bundle_path,
                    maximum=transport.MAX_DELIVERY_BYTES,
                )
                _record, installed_context, _installed_key = transport.decode_instance_delivery(
                    installed,
                    plan,
                    now_unix=verification_time,
                    expected_resource_name=expected_resource_name,
                    expected_generation_digest=expected_generation_digest,
                    allow_expired_for_cleanup=True,
                )
                if installed_context["context_digest"] != context["context_digest"]:
                    raise Q38LinuxHostRuntimeError("transport delivery cleanup generation changed")
                bundle_path.unlink()
                _fsync_directory(bundle_path.parent)
            _fsync_directory(state_parent)
    stale = [
        *state_parent.glob(".prepared.*.tmp"),
        *state_parent.glob(".status.*.tmp"),
        *state_parent.glob(".cleanup.*.tmp"),
    ]
    if bundle_path is not None:
        stale.extend(bundle_path.parent.glob(".delivery.*.tmp"))
    if (
        destination.exists()
        or work.exists()
        or paths.prepared_record.exists()
        or paths.status_envelope.exists()
        or bundle_path is not None
        and (bundle_path.exists() or bundle_path.is_symlink())
        or stale
    ):
        raise Q38LinuxHostRuntimeError("host runtime cleanup is incomplete")
    _validate_cleanup_marker_generation(
        cleanup_marker,
        plan,
        expected_resource_name=expected_resource_name,
        expected_generation_digest=expected_generation_digest,
    )


def _require_linux_root() -> None:
    if not sys.platform.startswith("linux") or not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise Q38LinuxHostRuntimeError("Qwen3.8 host runtime requires Linux root")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install, prepare, publish, or clean the protected Qwen3.8 Linux runtime"
    )
    parser.add_argument(
        "operation",
        choices=("install-delivery", "prepare", "publish-status", "cleanup"),
    )
    parser.add_argument("--resource-name", required=True)
    parser.add_argument("--instance-generation-digest", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _require_linux_root()
        paths = HostPaths.production()
        arguments = {
            "expected_resource_name": args.resource_name,
            "expected_generation_digest": args.instance_generation_digest,
        }
        if args.operation == "install-delivery":
            payload = sys.stdin.buffer.read(transport.MAX_DELIVERY_BYTES + 1)
            receipt = install_instance_delivery(paths, payload, **arguments)
            print(
                json.dumps(
                    receipt,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        elif args.operation == "prepare":
            record = prepare(paths, **arguments)
            print(json.dumps(record, allow_nan=False, sort_keys=True, separators=(",", ":")))
        elif args.operation == "publish-status":
            receipt = publish_status(paths, **arguments)
            print(json.dumps(receipt, allow_nan=False, sort_keys=True, separators=(",", ":")))
        else:
            cleanup(paths, **arguments)
    except (
        Q38LinuxHostRuntimeError,
        controller.RouteControllerError,
        transport.Q38LinuxHostTransportError,
    ) as exc:
        raise SystemExit(f"Qwen3.8 Linux host runtime failed: {exc}") from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
