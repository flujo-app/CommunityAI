"""Shared, fail-closed Gate 14 packaged lifecycle boundary.

Platform adapters perform the real desktop, control-API, process, cache, and
measurement operations. This module owns the source/package configuration,
immutable challenge-ready checkpoint, challenge ordering, strict observation
validation, privacy cleanup boundary, and final host-probe invocation. The
configuration deliberately has no fields for claimed passes, suspension
results, calibration samples, or cleanup success.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

import gate14_calibration_challenge as challenge_contract
import gate14_hardware_acceptance as acceptance
import gate14_host_probe as host_probe

SCHEMA_VERSION = 1
SCOPE = "gate14-packaged-lifecycle"
PREPARED_SCOPE = "gate14-prepared-host-observations"
CHECKPOINT_SCOPE = "gate14-host-lifecycle-checkpoint"
CLEANUP_SCOPE = "gate14-host-lifecycle-cleanup"
MAX_CONFIG_BYTES = 65_536
MAX_PRIVATE_JSON_BYTES = 262_144
MAX_RELEASE_METADATA_BYTES = host_probe.MAX_JSON_BYTES
MAX_PACKAGE_BYTES = 8 * 1024**3
MAX_CHALLENGE_WAIT_SECONDS = 1_200.0

_RUN_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")

_CONFIG_FIELDS = {
    "schema_version",
    "scope",
    "run_id",
    "platform",
    "attempt_ordinal",
    "source_commit",
    "package_path",
    "package_sha256",
    "package_bytes",
    "release_metadata_path",
    "release_metadata_sha256",
    "model_id",
    "manifest_digest",
    "gate13_evidence_sha256",
    "staging_root",
    "work_root",
    "challenge_path",
    "checkpoint_path",
    "facts_path",
    "evidence_path",
    "disk_bytes",
    "vram_bytes",
    "bandwidth_mbps",
    "power_watts",
    "pause_timeout_seconds",
    "sample_interval_seconds",
    "max_challenge_wait_seconds",
}
_PREPARED_FIELDS = {
    "schema_version",
    "scope",
    "run_id",
    "platform",
    "attempt_ordinal",
    "source_commit",
    "package_sha256",
    "model",
    "cache",
    "placement",
    "limits",
    "recovery",
    "pause",
    "restart",
    "unsupported_telemetry",
}
_CHECKPOINT_FIELDS = {
    "schema_version",
    "scope",
    "run_id",
    "platform",
    "attempt_ordinal",
    "source_commit",
    "lifecycle_config_sha256",
    "package_sha256",
    "release_metadata_sha256",
    "prepared_facts_sha256",
    "phase",
    "created_at_unix",
}
_CLEANUP_FIELDS = {
    "schema_version",
    "scope",
    "run_id",
    "platform",
    "attempt_ordinal",
    "processes_absent",
    "credentials_removed",
    "action_temporaries_removed",
}
_PACKAGE_NAMES = {
    "windows": "communityai-desktop-windows.zip",
    "linux": "communityai-desktop-linux.tar.gz",
}
_OUTPUT_NAMES = {
    "checkpoint_path": "gate14-checkpoint.json",
    "facts_path": "gate14-facts.json",
    "evidence_path": "gate14-platform-evidence.json",
}
_CHALLENGE_NAME = "gate14-challenge.json"
_PENDING_EVIDENCE_NAME = ".gate14-platform-evidence.pending.json"
_RELEASE_METADATA = {
    "schema_version": 1,
    "product": "CommunityAI",
    "package": "communityai-desktop",
    "release_channel": "public-alpha",
    "warning": (
        "Unsigned public-alpha engineering bundle: verify SHA256SUMS before use. "
        "No publisher signature or authenticated automatic update is provided."
    ),
    "unsigned": True,
    "publisher_signature": False,
    "automatic_updates": False,
    "supported_platforms": ["Windows", "Linux"],
    "macos_supported": False,
    "credits_enabled": False,
    "complete_release_qualification": False,
    "artifact_root": "CommunityAI",
    "artifact_inventory": "regular-files-and-relative-internal-file-symlinks-with-file-modes",
    "checksum_manifest": "SHA256SUMS",
    "install_archive_required": True,
    "install_archive_provenance": "provenance.json#install_archive",
    "desktop_metrics": "desktop-metrics.json",
    "provenance": "provenance.json",
}


class Gate14LifecycleError(ValueError):
    """A packaged lifecycle input or observation failed closed."""


class LifecycleActions(Protocol):
    """Source-bound platform actions used by the shared sequencer."""

    def prepare(self, config: "LifecycleConfig") -> Mapping[str, Any]:
        """Perform and observe every non-calibration qualification drill."""

    def calibrate(
        self,
        config: "LifecycleConfig",
        challenge: Mapping[str, Any],
    ) -> Sequence[Mapping[str, Any]]:
        """Measure the three physical suspension and recovery windows."""

    def cleanup(self, config: "LifecycleConfig") -> Mapping[str, Any]:
        """Stop product processes and remove credentials/action temporaries."""


HardwareProbe = Callable[[str], Mapping[str, Any]]


@dataclass(frozen=True)
class LifecycleConfig:
    run_id: str
    platform: str
    attempt_ordinal: int
    source_commit: str
    config_sha256: str
    config_path: Path
    package_path: Path
    package_sha256: str
    package_bytes: int
    release_metadata_path: Path
    release_metadata_sha256: str
    model_id: str
    manifest_digest: str
    gate13_evidence_sha256: str
    staging_root: Path
    work_root: Path
    challenge_path: Path
    checkpoint_path: Path
    facts_path: Path
    evidence_path: Path
    disk_bytes: int
    vram_bytes: int
    bandwidth_mbps: float
    power_watts: float
    pause_timeout_seconds: float
    sample_interval_seconds: float
    max_challenge_wait_seconds: float


def _reject_constant(_value: str) -> None:
    raise Gate14LifecycleError("non-finite JSON value")


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Gate14LifecycleError("duplicate JSON field")
        result[key] = value
    return result


def _canonical(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise Gate14LifecycleError("observation is not canonical JSON") from exc


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _open_regular(
    path: Path,
    maximum: int,
    *,
    minimum: int = 1,
):
    """Open one exact regular file without following a raced path."""

    path = Path(path)
    try:
        before = path.lstat()
    except OSError as exc:
        raise Gate14LifecycleError("required file is unavailable") from exc
    reparse = bool(getattr(before, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if (
        reparse
        or path.is_symlink()
        or not stat.S_ISREG(before.st_mode)
        or (os.name != "nt" and before.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
        or not minimum <= before.st_size <= maximum
    ):
        raise Gate14LifecycleError("required file is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        handle = os.fdopen(descriptor, "rb")
    except OSError as exc:
        raise Gate14LifecycleError("required file is unreadable") from exc
    try:
        opened = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or not minimum <= opened.st_size <= maximum
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise Gate14LifecycleError("required file changed while opening")
        return handle, opened
    except BaseException:
        handle.close()
        raise


def _regular_metadata(
    path: Path,
    maximum: int,
    *,
    minimum: int = 1,
) -> os.stat_result:
    handle, metadata = _open_regular(path, maximum, minimum=minimum)
    handle.close()
    return metadata


def _regular_bytes(path: Path, maximum: int) -> bytes:
    handle, metadata = _open_regular(path, maximum)
    try:
        payload = handle.read(maximum + 1)
        after = os.fstat(handle.fileno())
    except OSError as exc:
        raise Gate14LifecycleError("required file is unreadable") from exc
    finally:
        handle.close()
    if len(payload) != metadata.st_size or (after.st_dev, after.st_ino, after.st_size) != (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
    ):
        raise Gate14LifecycleError("required file changed while reading")
    return payload


def _strict_json(payload: bytes, maximum: int) -> Mapping[str, Any]:
    if not 1 <= len(payload) <= maximum:
        raise Gate14LifecycleError("JSON size is invalid")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Gate14LifecycleError("JSON is invalid") from exc
    if not isinstance(value, dict):
        raise Gate14LifecycleError("JSON root is invalid")
    return value


def _absolute_path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise Gate14LifecycleError(f"{label} is invalid")
    path = Path(value)
    if not path.is_absolute():
        raise Gate14LifecycleError(f"{label} is not absolute")
    return Path(os.path.abspath(os.fspath(path)))


def _bounded_number(
    value: Any,
    label: str,
    minimum: float,
    maximum: float,
) -> float:
    if type(value) not in (int, float):
        raise Gate14LifecycleError(f"{label} is invalid")
    rendered = float(value)
    if not math.isfinite(rendered) or not minimum <= rendered <= maximum:
        raise Gate14LifecycleError(f"{label} is invalid")
    return rendered


def _bounded_integer(
    value: Any,
    label: str,
    minimum: int,
    maximum: int,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise Gate14LifecycleError(f"{label} is invalid")
    return value


def _work_root(path: Path, *, controller_owned: bool = False) -> Path:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise Gate14LifecycleError("work root is unavailable") from exc
    reparse = bool(getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if (
        reparse
        or path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or (controller_owned and os.name != "nt" and metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
    ):
        raise Gate14LifecycleError("work root is unsafe")
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise Gate14LifecycleError("work root is unavailable") from exc


def _assert_windows_access_denied(
    *,
    directory: bool,
    opener: Callable[[int], Any],
    closer: Callable[[Any], Any],
    invalid_handle: Any,
    get_last_error: Callable[[], int],
) -> None:
    masks = [0x00010000, 0x00040000, 0x00080000, 0x40000000]
    if directory:
        masks.extend((0x00000002, 0x00000004, 0x00000040))
    for access_mask in masks:
        handle = opener(access_mask)
        if handle != invalid_handle:
            closer(handle)
            raise Gate14LifecycleError("controller staging is writable by the qualification process")
        if get_last_error() != 5:
            raise Gate14LifecycleError("controller staging write access could not be disproved")


def _windows_controller_owned(path: Path, *, directory: bool) -> None:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    local_free = kernel32.LocalFree
    local_free.argtypes = (ctypes.c_void_p,)
    local_free.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    get_security = advapi32.GetNamedSecurityInfoW
    get_security.argtypes = (
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    )
    get_security.restype = wintypes.DWORD
    result = get_security(
        os.fspath(path),
        1,
        0x1 | 0x4,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if result != 0:
        raise Gate14LifecycleError("controller staging ACL is unavailable")
    try:
        if not owner.value or not dacl.value:
            raise Gate14LifecycleError("controller staging ACL is unsafe")
        owner_text = wintypes.LPWSTR()
        convert_sid = advapi32.ConvertSidToStringSidW
        convert_sid.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))
        convert_sid.restype = wintypes.BOOL
        if not convert_sid(owner, ctypes.byref(owner_text)):
            raise Gate14LifecycleError("controller staging owner is unavailable")
        try:
            if owner_text.value not in {"S-1-5-18", "S-1-5-32-544"}:
                raise Gate14LifecycleError("controller staging owner is unsafe")
        finally:
            local_free(ctypes.cast(owner_text, ctypes.c_void_p))
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        get_control = advapi32.GetSecurityDescriptorControl
        get_control.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.WORD),
            ctypes.POINTER(wintypes.DWORD),
        )
        get_control.restype = wintypes.BOOL
        if not get_control(descriptor, ctypes.byref(control), ctypes.byref(revision)) or not control.value & 0x1000:
            raise Gate14LifecycleError("controller staging DACL is not protected")
    finally:
        local_free(descriptor)

    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE

    def open_with_access(access_mask: int):
        ctypes.set_last_error(0)
        return create_file(
            os.fspath(path),
            access_mask,
            0x1 | 0x2 | 0x4,
            None,
            3,
            0x02000000 if directory else 0,
            None,
        )

    _assert_windows_access_denied(
        directory=directory,
        opener=open_with_access,
        closer=close_handle,
        invalid_handle=ctypes.c_void_p(-1).value,
        get_last_error=ctypes.get_last_error,
    )


def _assert_controller_owned(path: Path, *, directory: bool) -> None:
    path = Path(path)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise Gate14LifecycleError("controller staging is unavailable") from exc
    reparse = bool(getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    expected_type = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if reparse or path.is_symlink() or not expected_type:
        raise Gate14LifecycleError("controller staging type is unsafe")
    if os.name == "nt":
        _windows_controller_owned(path, directory=directory)
        return
    if (
        metadata.st_uid != 0
        or os.geteuid() == 0
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or os.access(path, os.W_OK, effective_ids=True)
    ):
        raise Gate14LifecycleError("controller staging ownership is unsafe")


def _path_under_root(path: Path, root: Path, expected_name: str, label: str) -> Path:
    path = _absolute_path(os.fspath(path), label)
    if path.name != expected_name:
        raise Gate14LifecycleError("private lifecycle filename is invalid")
    try:
        parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise Gate14LifecycleError("private lifecycle parent is unavailable") from exc
    if parent != root:
        raise Gate14LifecycleError(f"{label} escaped its root")
    if path.exists():
        metadata = path.lstat()
        reparse = bool(getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        if reparse or path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise Gate14LifecycleError(f"{label} is unsafe")
    return path


def _private_path(raw: Mapping[str, Any], field: str, root: Path) -> Path:
    return _path_under_root(
        _absolute_path(raw[field], field.replace("_", " ")),
        root,
        _OUTPUT_NAMES[field],
        "private lifecycle path",
    )


def load_config(path: Path) -> LifecycleConfig:
    payload = _regular_bytes(Path(path), MAX_CONFIG_BYTES)
    raw = _strict_json(payload, MAX_CONFIG_BYTES)
    if set(raw) != _CONFIG_FIELDS or raw.get("schema_version") != SCHEMA_VERSION or raw.get("scope") != SCOPE:
        raise Gate14LifecycleError("lifecycle configuration schema is invalid")

    run_id = raw["run_id"]
    platform = raw["platform"]
    source_commit = raw["source_commit"]
    package_sha256 = raw["package_sha256"]
    model_id = raw["model_id"]
    manifest_digest = raw["manifest_digest"]
    gate13_digest = raw["gate13_evidence_sha256"]
    release_metadata_sha256 = raw["release_metadata_sha256"]
    if not isinstance(run_id, str) or _RUN_RE.fullmatch(run_id) is None:
        raise Gate14LifecycleError("run ID is invalid")
    if platform not in acceptance.EXPECTED_PLATFORM_MODELS:
        raise Gate14LifecycleError("platform is invalid")
    if (
        not isinstance(source_commit, str)
        or _COMMIT_RE.fullmatch(source_commit) is None
        or not isinstance(package_sha256, str)
        or _DIGEST_RE.fullmatch(package_sha256) is None
        or not isinstance(manifest_digest, str)
        or _DIGEST_RE.fullmatch(manifest_digest) is None
        or not isinstance(release_metadata_sha256, str)
        or _DIGEST_RE.fullmatch(release_metadata_sha256) is None
        or gate13_digest != acceptance.EXPECTED_GATE13_EVIDENCE_SHA256
    ):
        raise Gate14LifecycleError("source or evidence binding is invalid")

    expected_model = acceptance.EXPECTED_PLATFORM_MODELS[platform]
    profile = acceptance.MODEL_PROFILES[expected_model]
    if model_id != expected_model or manifest_digest != profile["manifest_digest"]:
        raise Gate14LifecycleError("model binding is invalid")

    staging_root = _work_root(
        _absolute_path(raw["staging_root"], "staging root"),
        controller_owned=True,
    )
    root = _work_root(_absolute_path(raw["work_root"], "work root"))
    if staging_root == root:
        raise Gate14LifecycleError("staging and work roots overlap")
    config_path = _path_under_root(
        Path(path),
        staging_root,
        "gate14-lifecycle.json",
        "lifecycle configuration",
    )
    if _digest(_regular_bytes(config_path, MAX_CONFIG_BYTES)) != _digest(payload):
        raise Gate14LifecycleError("lifecycle configuration changed")

    package_path = _path_under_root(
        _absolute_path(raw["package_path"], "package path"),
        staging_root,
        _PACKAGE_NAMES[platform],
        "production package",
    )
    if package_path.name != _PACKAGE_NAMES[platform]:
        raise Gate14LifecycleError("production package filename is invalid")
    package_bytes = _bounded_integer(
        raw["package_bytes"],
        "package size",
        1,
        MAX_PACKAGE_BYTES,
    )
    if _regular_metadata(package_path, MAX_PACKAGE_BYTES).st_size != package_bytes:
        raise Gate14LifecycleError("package size changed")
    release_metadata_path = _path_under_root(
        _absolute_path(raw["release_metadata_path"], "release metadata path"),
        staging_root,
        "release-metadata.json",
        "release metadata",
    )
    metadata_payload = _regular_bytes(
        release_metadata_path,
        MAX_RELEASE_METADATA_BYTES,
    )
    if (
        _digest(metadata_payload) != release_metadata_sha256
        or _strict_json(metadata_payload, MAX_RELEASE_METADATA_BYTES) != _RELEASE_METADATA
    ):
        raise Gate14LifecycleError("release metadata binding is invalid")

    challenge_path = _path_under_root(
        _absolute_path(raw["challenge_path"], "challenge path"),
        staging_root,
        _CHALLENGE_NAME,
        "controller challenge",
    )
    _assert_controller_owned(staging_root.parent, directory=True)
    _assert_controller_owned(staging_root, directory=True)
    for staged_path in (config_path, package_path, release_metadata_path):
        _assert_controller_owned(staged_path, directory=False)
    if challenge_path.exists():
        _assert_controller_owned(challenge_path, directory=False)
    private = {field: _private_path(raw, field, root) for field in _OUTPUT_NAMES}
    if len(set(private.values())) != len(private):
        raise Gate14LifecycleError("private lifecycle paths overlap")

    disk_bytes = _bounded_integer(
        raw["disk_bytes"],
        "disk limit",
        profile["selected_artifact_bytes"],
        acceptance.MAX_BYTES,
    )
    vram_bytes = _bounded_integer(
        raw["vram_bytes"],
        "VRAM limit",
        1,
        32 * 1024**3 - 1,
    )
    bandwidth = _bounded_number(
        raw["bandwidth_mbps"],
        "bandwidth limit",
        0.001,
        1_000_000.0,
    )
    power = _bounded_number(
        raw["power_watts"],
        "power limit",
        0.001,
        1_000.0,
    )
    pause = _bounded_number(
        raw["pause_timeout_seconds"],
        "pause timeout",
        1.0,
        300.0,
    )
    sample_interval = _bounded_number(
        raw["sample_interval_seconds"],
        "sample interval",
        0.05,
        30.0,
    )
    challenge_wait = _bounded_number(
        raw["max_challenge_wait_seconds"],
        "challenge wait",
        1.0,
        MAX_CHALLENGE_WAIT_SECONDS,
    )

    return LifecycleConfig(
        run_id=run_id,
        platform=platform,
        attempt_ordinal=_bounded_integer(
            raw["attempt_ordinal"],
            "attempt ordinal",
            1,
            100,
        ),
        source_commit=source_commit,
        config_sha256=_digest(payload),
        config_path=config_path,
        package_path=package_path,
        package_sha256=package_sha256,
        package_bytes=package_bytes,
        release_metadata_path=release_metadata_path,
        release_metadata_sha256=release_metadata_sha256,
        model_id=model_id,
        manifest_digest=manifest_digest,
        gate13_evidence_sha256=gate13_digest,
        staging_root=staging_root,
        work_root=root,
        challenge_path=challenge_path,
        checkpoint_path=private["checkpoint_path"],
        facts_path=private["facts_path"],
        evidence_path=private["evidence_path"],
        disk_bytes=disk_bytes,
        vram_bytes=vram_bytes,
        bandwidth_mbps=bandwidth,
        power_watts=power,
        pause_timeout_seconds=pause,
        sample_interval_seconds=sample_interval,
        max_challenge_wait_seconds=challenge_wait,
    )


def _hash_package(config: LifecycleConfig) -> None:
    stream, metadata = _open_regular(config.package_path, MAX_PACKAGE_BYTES)
    digest = hashlib.sha256()
    try:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    except OSError as exc:
        raise Gate14LifecycleError("package is unreadable") from exc
    finally:
        stream.close()
    if (
        metadata.st_size != config.package_bytes
        or (after.st_dev, after.st_ino, after.st_size) != (metadata.st_dev, metadata.st_ino, metadata.st_size)
        or "sha256:" + digest.hexdigest() != config.package_sha256
    ):
        raise Gate14LifecycleError("package digest changed")


def _verify_staged_inputs(config: LifecycleConfig) -> None:
    _assert_controller_owned(config.staging_root.parent, directory=True)
    _assert_controller_owned(config.staging_root, directory=True)
    for staged_path in (
        config.config_path,
        config.package_path,
        config.release_metadata_path,
    ):
        _assert_controller_owned(staged_path, directory=False)
    if config.challenge_path.exists():
        _assert_controller_owned(config.challenge_path, directory=False)
    config_payload = _regular_bytes(config.config_path, MAX_CONFIG_BYTES)
    if _digest(config_payload) != config.config_sha256:
        raise Gate14LifecycleError("lifecycle configuration binding changed")
    _hash_package(config)
    metadata = _regular_bytes(
        config.release_metadata_path,
        MAX_RELEASE_METADATA_BYTES,
    )
    if (
        _digest(metadata) != config.release_metadata_sha256
        or _strict_json(metadata, MAX_RELEASE_METADATA_BYTES) != _RELEASE_METADATA
    ):
        raise Gate14LifecycleError("release metadata binding changed")


def validate_prepared(
    value: Mapping[str, Any],
    config: LifecycleConfig,
) -> Mapping[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != _PREPARED_FIELDS
        or value["schema_version"] != SCHEMA_VERSION
        or value["scope"] != PREPARED_SCOPE
        or value["run_id"] != config.run_id
        or value["platform"] != config.platform
        or value["attempt_ordinal"] != config.attempt_ordinal
        or value["source_commit"] != config.source_commit
        or value["package_sha256"] != config.package_sha256
    ):
        raise Gate14LifecycleError("prepared observation schema or binding is invalid")
    try:
        model = acceptance._validate_model(value["model"], config.platform)
        if model["id"] != config.model_id or model["manifest_digest"] != config.manifest_digest:
            raise Gate14LifecycleError("prepared model binding changed")
        acceptance._validate_cache(
            value["cache"],
            model["selected_artifact_bytes"],
        )
        acceptance._validate_placement(
            value["placement"],
            model["total_blocks"],
        )
        acceptance._validate_limits(
            value["limits"],
            model["selected_artifact_bytes"],
            32 * 1024**3,
        )
        acceptance._validate_recovery(value["recovery"])
        acceptance._validate_pause(value["pause"])
        acceptance._validate_restart(value["restart"])
        acceptance._validate_unsupported(value["unsupported_telemetry"])
    except acceptance.Gate14EvidenceError as exc:
        raise Gate14LifecycleError("prepared observation is invalid") from exc

    limits = value["limits"]
    if (
        limits["disk_bytes"] != config.disk_bytes
        or limits["vram_bytes"] != config.vram_bytes
        or float(limits["bandwidth_mbps"]) != config.bandwidth_mbps
        or float(limits["power_watts"]) != config.power_watts
        or limits["schedule_timezone"] != "UTC"
    ):
        raise Gate14LifecycleError("prepared resource limits changed")
    return dict(value)


def _checkpoint_value(
    config: LifecycleConfig,
    prepared: Mapping[str, Any],
    created_at_unix: int,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": CHECKPOINT_SCOPE,
        "run_id": config.run_id,
        "platform": config.platform,
        "attempt_ordinal": config.attempt_ordinal,
        "source_commit": config.source_commit,
        "lifecycle_config_sha256": config.config_sha256,
        "package_sha256": config.package_sha256,
        "release_metadata_sha256": config.release_metadata_sha256,
        "prepared_facts_sha256": _digest(_canonical(prepared)),
        "phase": "challenge-ready",
        "created_at_unix": created_at_unix,
    }


def validate_checkpoint(
    value: Mapping[str, Any],
    config: LifecycleConfig,
    prepared: Mapping[str, Any],
    *,
    now_unix: float,
) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != _CHECKPOINT_FIELDS:
        raise Gate14LifecycleError("checkpoint schema is invalid")
    expected = _checkpoint_value(
        config,
        prepared,
        value.get("created_at_unix"),
    )
    if value != expected:
        raise Gate14LifecycleError("checkpoint binding is invalid")
    created = value["created_at_unix"]
    if (
        type(created) is not int
        or type(now_unix) not in (int, float)
        or not math.isfinite(float(now_unix))
        or not 0 <= created <= float(now_unix)
    ):
        raise Gate14LifecycleError("checkpoint time is invalid")
    return dict(value)


def checkpoint_digest(value: Mapping[str, Any]) -> str:
    if not isinstance(value, dict) or set(value) != _CHECKPOINT_FIELDS:
        raise Gate14LifecycleError("checkpoint schema is invalid")
    return _digest(_canonical(value))


def load_checkpoint_for_controller(
    path: Path,
    *,
    run_id: str,
    platform: str,
    source_commit: str,
    package_sha256: str,
    now_unix: float,
) -> Mapping[str, Any]:
    value = _strict_json(
        _regular_bytes(Path(path), MAX_PRIVATE_JSON_BYTES),
        MAX_PRIVATE_JSON_BYTES,
    )
    if (
        set(value) != _CHECKPOINT_FIELDS
        or value.get("schema_version") != SCHEMA_VERSION
        or value.get("scope") != CHECKPOINT_SCOPE
        or value.get("run_id") != run_id
        or value.get("platform") != platform
        or value.get("source_commit") != source_commit
        or value.get("package_sha256") != package_sha256
        or value.get("phase") != "challenge-ready"
        or type(value.get("attempt_ordinal")) is not int
        or not 1 <= value["attempt_ordinal"] <= 100
        or not isinstance(value.get("lifecycle_config_sha256"), str)
        or _DIGEST_RE.fullmatch(value["lifecycle_config_sha256"]) is None
        or not isinstance(value.get("release_metadata_sha256"), str)
        or _DIGEST_RE.fullmatch(value["release_metadata_sha256"]) is None
        or not isinstance(value.get("prepared_facts_sha256"), str)
        or _DIGEST_RE.fullmatch(value["prepared_facts_sha256"]) is None
        or type(value.get("created_at_unix")) is not int
        or type(now_unix) not in (int, float)
        or not math.isfinite(float(now_unix))
        or not 0 <= value["created_at_unix"] <= float(now_unix)
    ):
        raise Gate14LifecycleError("checkpoint controller binding is invalid")
    return dict(value)


def _write_new(path: Path, value: Mapping[str, Any]) -> None:
    if path.exists():
        raise Gate14LifecycleError("private lifecycle output already exists")
    payload = _canonical(value) + os.linesep.encode("ascii")
    if len(payload) > MAX_PRIVATE_JSON_BYTES:
        raise Gate14LifecycleError("private lifecycle output is too large")
    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise Gate14LifecycleError("private lifecycle output already exists") from exc
        temporary.unlink()
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def write_or_load_checkpoint(
    config: LifecycleConfig,
    prepared: Mapping[str, Any],
    *,
    now_unix: float,
) -> Mapping[str, Any]:
    prepared = validate_prepared(prepared, config)
    if config.checkpoint_path.exists():
        existing = _strict_json(
            _regular_bytes(
                config.checkpoint_path,
                MAX_PRIVATE_JSON_BYTES,
            ),
            MAX_PRIVATE_JSON_BYTES,
        )
        return validate_checkpoint(
            existing,
            config,
            prepared,
            now_unix=now_unix,
        )
    if type(now_unix) not in (int, float) or not math.isfinite(float(now_unix)) or float(now_unix) < 0:
        raise Gate14LifecycleError("checkpoint clock is invalid")
    value = _checkpoint_value(config, prepared, int(now_unix))
    _write_new(config.checkpoint_path, value)
    return value


def wait_for_challenge(
    config: LifecycleConfig,
    checkpoint: Mapping[str, Any],
    *,
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> Mapping[str, Any]:
    started = monotonic()
    deadline = started + config.max_challenge_wait_seconds
    while True:
        now = clock()
        if config.challenge_path.exists():
            try:
                _assert_controller_owned(
                    config.challenge_path,
                    directory=False,
                )
                value = challenge_contract.validate(
                    challenge_contract.load(config.challenge_path),
                    run_id=config.run_id,
                    platform=config.platform,
                    source_commit=config.source_commit,
                    package_sha256=config.package_sha256,
                    checkpoint_sha256=checkpoint_digest(checkpoint),
                    now_unix=now,
                )
            except challenge_contract.Gate14ChallengeError as exc:
                raise Gate14LifecycleError("calibration challenge is invalid") from exc
            if value["issued_at_unix"] < checkpoint["created_at_unix"] or value[
                "checkpoint_sha256"
            ] != checkpoint_digest(checkpoint):
                raise Gate14LifecycleError("calibration challenge predates readiness")
            return value
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise Gate14LifecycleError("calibration challenge timed out")
        sleeper(min(config.sample_interval_seconds, remaining))


def validate_suspensions(
    value: Sequence[Mapping[str, Any]],
    prepared: Mapping[str, Any],
    challenge: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise Gate14LifecycleError("calibration observations are invalid")
    rendered = [dict(item) if isinstance(item, Mapping) else item for item in value]
    challenge_summary = {
        "challenge_sha256": challenge_contract.digest(challenge),
        "controller_state_revision": challenge["controller_state_revision"],
        "issued_at_unix": challenge["issued_at_unix"],
        "expires_at_unix": challenge["expires_at_unix"],
    }
    try:
        acceptance._validate_suspensions(
            rendered,
            prepared["limits"],
            challenge_summary,
        )
    except acceptance.Gate14EvidenceError as exc:
        raise Gate14LifecycleError("calibration observations are invalid") from exc
    return rendered


def validate_cleanup(
    value: Mapping[str, Any],
    config: LifecycleConfig,
) -> Mapping[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != _CLEANUP_FIELDS
        or value["schema_version"] != SCHEMA_VERSION
        or value["scope"] != CLEANUP_SCOPE
        or value["run_id"] != config.run_id
        or value["platform"] != config.platform
        or value["attempt_ordinal"] != config.attempt_ordinal
        or any(
            value[field] is not True
            for field in (
                "processes_absent",
                "credentials_removed",
                "action_temporaries_removed",
            )
        )
    ):
        raise Gate14LifecycleError("lifecycle cleanup is incomplete")
    return dict(value)


def _facts(
    config: LifecycleConfig,
    prepared: Mapping[str, Any],
    suspensions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": host_probe.FACT_SCOPE,
        "run_id": config.run_id,
        "platform": config.platform,
        "source_commit": config.source_commit,
        "gate13_evidence_sha256": config.gate13_evidence_sha256,
        "expected_package_sha256": config.package_sha256,
        "model": prepared["model"],
        "cache": prepared["cache"],
        "placement": prepared["placement"],
        "limits": prepared["limits"],
        "suspensions": list(suspensions),
        "recovery": prepared["recovery"],
        "pause": prepared["pause"],
        "restart": prepared["restart"],
        "unsupported_telemetry": prepared["unsupported_telemetry"],
        "qualification_temporaries_removed": True,
    }


def _remove_output(path: Path) -> None:
    try:
        Path(path).unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise Gate14LifecycleError("private lifecycle output cleanup failed") from exc


def _load_platform_output(path: Path) -> Mapping[str, Any]:
    return _strict_json(
        _regular_bytes(path, host_probe.MAX_JSON_BYTES),
        host_probe.MAX_JSON_BYTES,
    )


def run_lifecycle(
    config: LifecycleConfig,
    actions: LifecycleActions,
    *,
    hardware_probe: HardwareProbe = host_probe.probe_hardware,
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> Mapping[str, Any]:
    """Run the shared ordering contract and emit one strict platform document."""

    pending_path = config.work_root / _PENDING_EVIDENCE_NAME
    owns_outputs = False
    try:
        if any(
            path.exists()
            for path in (
                config.challenge_path,
                config.checkpoint_path,
                config.facts_path,
                config.evidence_path,
                pending_path,
            )
        ):
            raise Gate14LifecycleError("fresh lifecycle outputs are required")
        owns_outputs = True
        _verify_staged_inputs(config)
        prepared = validate_prepared(actions.prepare(config), config)
        _verify_staged_inputs(config)
        if config.challenge_path.exists():
            raise Gate14LifecycleError("calibration challenge arrived before readiness")
        checkpoint = write_or_load_checkpoint(
            config,
            prepared,
            now_unix=clock(),
        )
        challenge = wait_for_challenge(
            config,
            checkpoint,
            clock=clock,
            monotonic=monotonic,
            sleeper=sleeper,
        )
        suspensions = validate_suspensions(
            actions.calibrate(config, challenge),
            prepared,
            challenge,
        )
        _verify_staged_inputs(config)
        current_challenge = challenge_contract.validate(
            challenge_contract.load(config.challenge_path),
            run_id=config.run_id,
            platform=config.platform,
            source_commit=config.source_commit,
            package_sha256=config.package_sha256,
            checkpoint_sha256=checkpoint_digest(checkpoint),
            now_unix=clock(),
        )
        if current_challenge != challenge:
            raise Gate14LifecycleError("calibration challenge changed")
        validate_cleanup(actions.cleanup(config), config)
        _verify_staged_inputs(config)

        _write_new(config.facts_path, _facts(config, prepared, suspensions))
        document = host_probe.run_probe(
            platform_name=config.platform,
            facts_path=config.facts_path,
            challenge_path=config.challenge_path,
            package_path=config.package_path,
            release_metadata_path=config.release_metadata_path,
            output_path=pending_path,
            hardware_probe=hardware_probe,
            now_unix=clock(),
        )
        _remove_output(config.facts_path)
        _verify_staged_inputs(config)
        if challenge_contract.load(config.challenge_path) != challenge:
            raise Gate14LifecycleError("calibration challenge changed")
        acceptance.validate_platform_document(document)
        persisted = _load_platform_output(pending_path)
        acceptance.validate_platform_document(persisted)
        if (
            _canonical(persisted) != _canonical(document)
            or persisted["package"]["release_metadata_sha256"] != config.release_metadata_sha256
        ):
            raise Gate14LifecycleError("persisted platform evidence changed")
        _write_new(config.evidence_path, persisted)
        _remove_output(pending_path)
        published = _load_platform_output(config.evidence_path)
        acceptance.validate_platform_document(published)
        if _canonical(published) != _canonical(document):
            raise Gate14LifecycleError("published platform evidence changed")
        return published
    except BaseException as exc:
        removal_error = None
        if owns_outputs:
            for path in (config.facts_path, pending_path, config.evidence_path):
                try:
                    _remove_output(path)
                except Gate14LifecycleError as cleanup_exc:
                    removal_error = cleanup_exc
        try:
            validate_cleanup(actions.cleanup(config), config)
        except BaseException as cleanup_exc:
            raise Gate14LifecycleError("lifecycle failed and cleanup did not complete") from cleanup_exc
        if removal_error is not None:
            raise Gate14LifecycleError(
                "lifecycle failed and private output cleanup did not complete"
            ) from removal_error
        raise
