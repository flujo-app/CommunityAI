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
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
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
MAX_RELEASE_PROVENANCE_BYTES = 8 * 1024**2
MAX_DESKTOP_METRICS_BYTES = 1024**2
MAX_RELEASE_CHECKSUMS_BYTES = 4 * 1024**2
MAX_RELEASE_AUDIT_BYTES = 32 * 1024**2
MAX_MATERIALIZATION_RECORD_BYTES = 2 * 1024**2
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
    "release_audit",
    "warm_cache",
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
    "release_audit_sha256",
    "warm_cache_sha256",
    "materialization_record_sha256",
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
_RELEASE_AUDIT_ARCHIVE_NAME = "release-audit.zip"
_RELEASE_AUDIT_DIRECTORY_NAME = "release-audit"
_MATERIALIZATION_RECORD_NAME = "gate14-cache-materialization.json"
_RELEASE_AUDIT_MEMBERS = (
    "SHA256SUMS",
    "desktop-metrics.json",
    "provenance.json",
    "release-metadata.json",
)
_MODEL_SOURCE = {
    "Qwen3.5 2B": ("Qwen/Qwen3.5-2B", "bfloat16"),
    "Gemma 4 E2B IT": ("google/gemma-4-E2B-it", "bfloat16"),
}
_GATE9_WARM_CACHE = {
    "windows": {
        "gate9_acquisition_record_sha256": "sha256:557c9a5a5441d095f780bfe20620450502a1b941fd7e33fe72b83bec5e147c52",
        "gate9_resource_envelope_sha256": "sha256:cd68afb67d9b0f3cb8c82db0d3314ad89b558c20880998ea4d8c4493e9f4bc9f",
        "artifacts": (
            (
                "chat_template.jinja",
                "chat_template",
                "sha256:273d8e0e683b885071fb17e08d71e5f2a5ddfb5309756181681de4f5a1822d80",
                7755,
            ),
            ("config.json", "config", "sha256:ed1c1723241f23f7f4e23430759cbd7dcfb4103cbdfe052bfe7626b57c2615b4", 2908),
            (
                "merges.txt",
                "tokenizer",
                "sha256:a9d356d7bdf1ef4949e3e748e95b8e10ad9d4e2e838eddc38a0a7b6b94d1db8d",
                3353259,
            ),
            (
                "model.safetensors-00001-of-00001.safetensors",
                "weight",
                "sha256:aa33250c4fc64891ddfaba3a314fd9542ea371843c387178b425fbcc5ed680b1",
                4548221488,
            ),
            (
                "model.safetensors.index.json",
                "weight_index",
                "sha256:aca8afed9da75b0f050b408d270766fd77627f1af401e240f61c3b47d0db02f9",
                64460,
            ),
            (
                "tokenizer.json",
                "tokenizer",
                "sha256:5f9e4d4901a92b997e463c1f46055088b6cca5ca61a6522d1b9f64c4bb81cb42",
                12807982,
            ),
            (
                "tokenizer_config.json",
                "tokenizer",
                "sha256:49e2b6e395f959f077f1e992b338919c0d4a9732fc6e613995e06557f843500c",
                16709,
            ),
            (
                "vocab.json",
                "tokenizer",
                "sha256:ce99b4cb2983d118806ce0a8b777a35b093e2000a503ebde25853284c9dfa003",
                6722759,
            ),
        ),
    },
    "linux": {
        "gate9_acquisition_record_sha256": "sha256:1628f87f1baaa2f562ca6c7340d2863034cdb0d17dbdf8995e38d9ae792fe0b5",
        "gate9_resource_envelope_sha256": "sha256:2eb0bcf6419ba085665fad34310453a1b9dc2e89d90e9177f41566df012996c8",
        "artifacts": (
            (
                "chat_template.jinja",
                "chat_template",
                "sha256:0a2c8073c878ab1da004bee933a998606537bbb62016310352c7285c3f01c5b5",
                18569,
            ),
            ("config.json", "config", "sha256:1b28f3d2c3100f6c594754b81107428bd7b822a7f48272ca681dae9d2ec38330", 4954),
            (
                "model.safetensors",
                "weight",
                "sha256:2db5482b20d746879bb3ef79b5203e9075a2e2b98f54ec7c2f281c1477ddc550",
                10246621918,
            ),
            (
                "tokenizer.json",
                "tokenizer",
                "sha256:cc8d3a0ce36466ccc1278bf987df5f71db1719b9ca6b4118264f45cb627bfe0f",
                32169626,
            ),
            (
                "tokenizer_config.json",
                "tokenizer",
                "sha256:9f4fec4b1dc6ecddf8f4a92e9caea5971c0e67d81309f3f9066a2bee8c362633",
                3082,
            ),
        ),
    },
}
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
class AuditMemberBinding:
    name: str
    sha256: str
    size_bytes: int
    path: Path


@dataclass(frozen=True)
class ReleaseAuditBinding:
    artifact_name: str
    artifact_sha256: str
    artifact_bytes: int
    archive_path: Path
    members: tuple[AuditMemberBinding, ...]
    binding_sha256: str


@dataclass(frozen=True)
class CacheArtifactBinding:
    path: str
    role: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class WarmCacheBinding:
    gate9_acquisition_record_sha256: str
    gate9_resource_envelope_sha256: str
    source_commit: str
    materialization_plan_sha256: str
    materializer_sources_sha256: str
    materialization_record_sha256: str
    materialization_record_bytes: int
    materialization_record_path: Path
    artifact_count: int
    artifact_bytes: int
    artifacts: tuple[CacheArtifactBinding, ...]
    binding_sha256: str


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
    release_audit: ReleaseAuditBinding
    warm_cache: WarmCacheBinding
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


def _exact_equal(value: Any, expected: Any) -> bool:
    if type(value) is not type(expected):
        return False
    if isinstance(expected, dict):
        return set(value) == set(expected) and all(_exact_equal(value[key], expected[key]) for key in expected)
    if isinstance(expected, list):
        return len(value) == len(expected) and all(
            _exact_equal(observed, required) for observed, required in zip(value, expected)
        )
    return value == expected


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


def _windows_controller_owned(
    path: Path,
    *,
    directory: bool,
    require_qualification_denied: bool = True,
) -> None:
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

    if not require_qualification_denied:
        return

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


def _assert_controller_managed(path: Path, *, directory: bool) -> None:
    """Prove structural controller ownership without constraining the caller token."""
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
        _windows_controller_owned(
            path,
            directory=directory,
            require_qualification_denied=False,
        )
        return
    if metadata.st_uid != 0 or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise Gate14LifecycleError("controller staging ownership is unsafe")


def _assert_controller_owned(path: Path, *, directory: bool) -> None:
    _assert_controller_managed(path, directory=directory)
    if os.name == "nt":
        _windows_controller_owned(path, directory=directory)
        return
    if os.geteuid() == 0 or os.access(path, os.W_OK, effective_ids=True):
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


def _exact_staged_path(path: Path, expected: Path, label: str, *, directory: bool) -> Path:
    candidate = _absolute_path(os.fspath(path), label)
    expected = Path(os.path.abspath(os.fspath(expected)))
    if candidate != expected:
        raise Gate14LifecycleError(f"{label} escaped its fixed location")
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise Gate14LifecycleError(f"{label} is unavailable") from exc
    reparse = bool(getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    expected_type = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if reparse or candidate.is_symlink() or not expected_type:
        raise Gate14LifecycleError(f"{label} is unsafe")
    return candidate


def _public_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or any(ord(character) < 32 for character in value):
        raise Gate14LifecycleError(f"{label} is unsafe")
    pure = PurePosixPath(value)
    if pure.is_absolute() or pure.as_posix() != value or any(part in ("", ".", "..") for part in pure.parts):
        raise Gate14LifecycleError(f"{label} is unsafe")
    return value


def _digest_field(value: Any, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise Gate14LifecycleError(f"{label} is invalid")
    return value


def _audit_member_payloads(
    binding: ReleaseAuditBinding,
    *,
    ownership_verifier: Callable[..., None] | None = None,
) -> dict[str, bytes]:
    if ownership_verifier is None:
        ownership_verifier = _assert_controller_owned
    ownership_verifier(binding.archive_path, directory=False)
    archive = _regular_bytes(binding.archive_path, MAX_RELEASE_AUDIT_BYTES)
    if len(archive) != binding.artifact_bytes or _digest(archive) != binding.artifact_sha256:
        raise Gate14LifecycleError("release audit artifact binding changed")

    expected = {member.name: member for member in binding.members}
    payloads: dict[str, bytes] = {}
    try:
        import io

        with zipfile.ZipFile(io.BytesIO(archive), "r") as source:
            infos = source.infolist()
            names = [info.filename for info in infos]
            if names != list(_RELEASE_AUDIT_MEMBERS) or any(
                info.is_dir() or info.flag_bits & 0x1 or stat.S_IFMT(info.external_attr >> 16) not in {0, stat.S_IFREG}
                for info in infos
            ):
                raise Gate14LifecycleError("release audit archive members are invalid")
            for info in infos:
                member = expected[info.filename]
                if info.file_size != member.size_bytes:
                    raise Gate14LifecycleError("release audit archive member size changed")
                payload = source.read(info)
                if len(payload) != member.size_bytes or _digest(payload) != member.sha256:
                    raise Gate14LifecycleError("release audit archive member digest changed")
                payloads[member.name] = payload
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise Gate14LifecycleError("release audit archive is unreadable") from exc

    for member in binding.members:
        ownership_verifier(member.path, directory=False)
        staged = _regular_bytes(
            member.path,
            {
                "provenance.json": MAX_RELEASE_PROVENANCE_BYTES,
                "desktop-metrics.json": MAX_DESKTOP_METRICS_BYTES,
                "SHA256SUMS": MAX_RELEASE_CHECKSUMS_BYTES,
            }.get(member.name, MAX_RELEASE_METADATA_BYTES),
        )
        if staged != payloads[member.name]:
            raise Gate14LifecycleError("release audit extracted member changed")
    return payloads


def _validate_release_semantics(
    payloads: Mapping[str, bytes],
    *,
    platform: str,
    source_commit: str,
    package_sha256: str,
    package_bytes: int,
) -> None:
    provenance = _strict_json(
        payloads["provenance.json"],
        MAX_RELEASE_PROVENANCE_BYTES,
    )
    metrics = _strict_json(
        payloads["desktop-metrics.json"],
        MAX_DESKTOP_METRICS_BYTES,
    )
    release_metadata = _strict_json(
        payloads["release-metadata.json"],
        MAX_RELEASE_METADATA_BYTES,
    )
    if not _exact_equal(release_metadata, _RELEASE_METADATA):
        raise Gate14LifecycleError("release audit metadata claims are invalid")

    provenance_fields = {
        "schema_version",
        "product",
        "package",
        "release_channel",
        "source_commit",
        "source_tree",
        "build_workflow",
        "build_platform",
        "build_python",
        "build_pyinstaller",
        "artifact_root",
        "checksum_manifest",
        "artifacts",
        "install_archive",
        "desktop_metrics",
        "catalog_publication_bundle",
        "unsigned",
        "publisher_signature",
        "automatic_updates",
        "complete_release_qualification",
    }
    title = platform.title()
    archive = provenance.get("install_archive")
    metrics_record = provenance.get("desktop_metrics")
    artifacts = provenance.get("artifacts")
    if (
        set(provenance) != provenance_fields
        or type(provenance.get("schema_version")) is not int
        or provenance.get("schema_version") != 1
        or provenance.get("product") != "CommunityAI"
        or provenance.get("package") != "communityai-desktop"
        or provenance.get("release_channel") != "public-alpha"
        or provenance.get("source_commit") != source_commit
        or not isinstance(provenance.get("source_tree"), str)
        or _COMMIT_RE.fullmatch(provenance["source_tree"]) is None
        or not isinstance(provenance.get("build_platform"), str)
        or not provenance["build_platform"].startswith(title)
        or provenance.get("artifact_root") != "CommunityAI"
        or provenance.get("checksum_manifest") != "SHA256SUMS"
        or provenance.get("unsigned") is not True
        or provenance.get("publisher_signature") is not False
        or provenance.get("automatic_updates") is not False
        or provenance.get("complete_release_qualification") is not False
        or not isinstance(artifacts, list)
        or not artifacts
        or not isinstance(archive, dict)
        or not isinstance(metrics_record, dict)
    ):
        raise Gate14LifecycleError("release provenance binding is invalid")

    expected_archive = {
        "schema_version": 1,
        "path": _PACKAGE_NAMES[platform],
        "format": "zip" if platform == "windows" else "tar.gz",
        "platform": title,
        "artifact_root": "CommunityAI",
        "sha256": package_sha256.removeprefix("sha256:"),
        "size_bytes": package_bytes,
        "entry_count": archive.get("entry_count"),
        "preserves_executable_modes": platform == "linux",
        "preserves_internal_file_symlinks": platform == "linux",
    }
    if (
        set(archive) != set(expected_archive)
        or type(archive.get("schema_version")) is not int
        or type(archive.get("entry_count")) is not int
        or archive["entry_count"] < 1
        or not _exact_equal(archive, expected_archive)
    ):
        raise Gate14LifecycleError("release archive provenance is invalid")

    metrics_payload = payloads["desktop-metrics.json"]
    if not _exact_equal(
        metrics_record,
        {
            "schema_version": 1,
            "path": "desktop-metrics.json",
            "sha256": hashlib.sha256(metrics_payload).hexdigest(),
            "size_bytes": len(metrics_payload),
        },
    ):
        raise Gate14LifecycleError("desktop metrics provenance is invalid")

    artifact_paths: list[str] = []
    artifact_kinds: dict[str, str] = {}
    link_targets: dict[str, str] = {}
    checksum_lines: list[str] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise Gate14LifecycleError("release artifact inventory is invalid")
        kind = artifact.get("kind")
        expected_fields = {"path", "kind", "sha256", "size_bytes"}
        expected_fields.add("mode" if kind == "file" else "link_target")
        path = _public_path(artifact.get("path"), "release artifact path")
        digest = artifact.get("sha256")
        size = artifact.get("size_bytes")
        if (
            set(artifact) != expected_fields
            or not path.startswith("CommunityAI/")
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or type(size) is not int
            or size < 0
            or kind not in {"file", "symlink"}
        ):
            raise Gate14LifecycleError("release artifact inventory is invalid")
        if kind == "file":
            if type(artifact.get("mode")) is not int or not 0 <= artifact["mode"] <= 0o7777:
                raise Gate14LifecycleError("release artifact mode is invalid")
        else:
            target = _public_path(artifact.get("link_target"), "release link target")
            if target == path or not target.startswith("CommunityAI/"):
                raise Gate14LifecycleError("release artifact link is invalid")
            link_targets[path] = target
        artifact_paths.append(path)
        artifact_kinds[path] = kind
        checksum_lines.append(f"{digest}  {path}\n")
    if any(artifact_kinds.get(target) != "file" for target in link_targets.values()):
        raise Gate14LifecycleError("release artifact link target is invalid")
    if (
        artifact_paths != sorted(artifact_paths)
        or len({item.casefold() for item in artifact_paths}) != len(artifact_paths)
        or payloads["SHA256SUMS"] != "".join(checksum_lines).encode("utf-8")
    ):
        raise Gate14LifecycleError("release checksum inventory is invalid")

    release_artifacts = metrics.get("release_artifacts")
    node_sidecar = metrics.get("node_sidecar")
    if (
        type(metrics.get("schema_version")) is not int
        or metrics.get("schema_version") != 1
        or metrics.get("application") != "CommunityAI"
        or metrics.get("package") != "communityai-desktop"
        or not isinstance(metrics.get("platform"), str)
        or not metrics["platform"].startswith(title)
        or metrics.get("signed") is not False
        or metrics.get("catalog_bootstrap_bundled") is not True
        or not _exact_equal(
            metrics.get("catalog_publication_bundle"),
            provenance.get("catalog_publication_bundle"),
        )
        or not isinstance(release_artifacts, dict)
        or set(release_artifacts)
        != {
            "schema_version",
            "artifact_count",
            "artifact_bytes",
            "checksums_sha256",
            "source_commit",
            "source_tree",
            "unsigned",
            "complete_release_qualification",
            "install_archive",
        }
        or type(release_artifacts.get("schema_version")) is not int
        or release_artifacts.get("schema_version") != 1
        or type(release_artifacts.get("artifact_count")) is not int
        or release_artifacts.get("artifact_count") != len(artifacts)
        or type(release_artifacts.get("artifact_bytes")) is not int
        or release_artifacts.get("artifact_bytes") != sum(item["size_bytes"] for item in artifacts)
        or release_artifacts.get("checksums_sha256") != hashlib.sha256(payloads["SHA256SUMS"]).hexdigest()
        or release_artifacts.get("source_commit") != source_commit
        or release_artifacts.get("source_tree") != provenance.get("source_tree")
        or release_artifacts.get("unsigned") is not True
        or not _exact_equal(release_artifacts.get("install_archive"), archive)
        or release_artifacts.get("complete_release_qualification") is not False
        or not isinstance(node_sidecar, dict)
        or node_sidecar.get("self_test_passed") is not True
        or node_sidecar.get("node_entrypoint_smoke_passed") is not True
        or node_sidecar.get("worker_entrypoint_smoke_passed") is not True
        or node_sidecar.get("worker_self_test_passed") is not True
    ):
        raise Gate14LifecycleError("desktop metrics binding is invalid")


def _load_release_audit(
    value: Any,
    *,
    staging_root: Path,
    ownership_verifier: Callable[..., None],
    release_metadata_path: Path,
    release_metadata_sha256: str,
    platform: str,
    source_commit: str,
    package_sha256: str,
    package_bytes: int,
) -> ReleaseAuditBinding:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "artifact_name",
        "artifact_sha256",
        "artifact_bytes",
        "members",
    }:
        raise Gate14LifecycleError("release audit binding schema is invalid")
    expected_artifact_name = f"communityai-desktop-audit-{platform}"
    raw_members = value.get("members")
    if (
        type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or value.get("artifact_name") != expected_artifact_name
        or not isinstance(raw_members, list)
        or len(raw_members) != len(_RELEASE_AUDIT_MEMBERS)
    ):
        raise Gate14LifecycleError("release audit identity is invalid")
    artifact_sha256 = _digest_field(
        value.get("artifact_sha256"),
        "release audit artifact digest",
    )
    artifact_bytes = _bounded_integer(
        value.get("artifact_bytes"),
        "release audit artifact size",
        1,
        MAX_RELEASE_AUDIT_BYTES,
    )

    audit_directory = _exact_staged_path(
        staging_root / _RELEASE_AUDIT_DIRECTORY_NAME,
        staging_root / _RELEASE_AUDIT_DIRECTORY_NAME,
        "release audit directory",
        directory=True,
    )
    ownership_verifier(audit_directory, directory=True)
    members: list[AuditMemberBinding] = []
    for index, raw in enumerate(raw_members):
        if not isinstance(raw, dict) or set(raw) != {"name", "sha256", "size_bytes"}:
            raise Gate14LifecycleError("release audit member schema is invalid")
        name = raw.get("name")
        if name != _RELEASE_AUDIT_MEMBERS[index]:
            raise Gate14LifecycleError("release audit members are not exact and sorted")
        maximum = {
            "provenance.json": MAX_RELEASE_PROVENANCE_BYTES,
            "desktop-metrics.json": MAX_DESKTOP_METRICS_BYTES,
            "SHA256SUMS": MAX_RELEASE_CHECKSUMS_BYTES,
        }.get(name, MAX_RELEASE_METADATA_BYTES)
        member = AuditMemberBinding(
            name=name,
            sha256=_digest_field(raw.get("sha256"), "release audit member digest"),
            size_bytes=_bounded_integer(
                raw.get("size_bytes"),
                "release audit member size",
                1,
                maximum,
            ),
            path=_exact_staged_path(
                audit_directory / name,
                audit_directory / name,
                "release audit member",
                directory=False,
            ),
        )
        members.append(member)
    metadata_member = next(item for item in members if item.name == "release-metadata.json")
    if metadata_member.path != release_metadata_path or metadata_member.sha256 != release_metadata_sha256:
        raise Gate14LifecycleError("release metadata is not bound to the full audit")

    binding = ReleaseAuditBinding(
        artifact_name=expected_artifact_name,
        artifact_sha256=artifact_sha256,
        artifact_bytes=artifact_bytes,
        archive_path=_exact_staged_path(
            staging_root / _RELEASE_AUDIT_ARCHIVE_NAME,
            staging_root / _RELEASE_AUDIT_ARCHIVE_NAME,
            "release audit archive",
            directory=False,
        ),
        members=tuple(members),
        binding_sha256=_digest(_canonical(value)),
    )
    payloads = _audit_member_payloads(
        binding,
        ownership_verifier=ownership_verifier,
    )
    _validate_release_semantics(
        payloads,
        platform=platform,
        source_commit=source_commit,
        package_sha256=package_sha256,
        package_bytes=package_bytes,
    )
    return binding


def _validate_cache_artifact(value: Any) -> CacheArtifactBinding:
    if not isinstance(value, dict) or set(value) != {
        "path",
        "role",
        "sha256",
        "size_bytes",
    }:
        raise Gate14LifecycleError("warm-cache artifact schema is invalid")
    path = _public_path(value.get("path"), "warm-cache artifact path")
    role = value.get("role")
    if role not in {"chat_template", "config", "tokenizer", "weight", "weight_index"}:
        raise Gate14LifecycleError("warm-cache artifact role is invalid")
    return CacheArtifactBinding(
        path=path,
        role=role,
        sha256=_digest_field(value.get("sha256"), "warm-cache artifact digest"),
        size_bytes=_bounded_integer(
            value.get("size_bytes"),
            "warm-cache artifact size",
            1,
            acceptance.MAX_BYTES,
        ),
    )


def _validate_materialization_record(
    payload: bytes,
    *,
    platform: str,
    model_id: str,
    manifest_digest: str,
    warm_cache: WarmCacheBinding,
) -> None:
    raw = _strict_json(payload, MAX_MATERIALIZATION_RECORD_BYTES)
    expected_fields = {
        "schema_version",
        "acquired_at_unix",
        "runtime",
        "model",
        "selection",
        "artifacts",
        "transfer",
        "storage",
        "privacy",
    }
    runtime = raw.get("runtime")
    model = raw.get("model")
    selection = raw.get("selection")
    transfer = raw.get("transfer")
    storage = raw.get("storage")
    privacy = raw.get("privacy")
    artifacts = raw.get("artifacts")
    profile = acceptance.MODEL_PROFILES[model_id]
    repository, dtype = _MODEL_SOURCE[model_id]
    nested = (runtime, model, selection, transfer, storage, privacy)
    runtime_values = runtime.values() if isinstance(runtime, dict) else ()
    if (
        set(raw) != expected_fields
        or type(raw.get("schema_version")) is not int
        or raw.get("schema_version") != 1
        or type(raw.get("acquired_at_unix")) is not int
        or raw["acquired_at_unix"] < 1
        or not all(isinstance(item, dict) for item in nested)
        or set(runtime) != {"python", "platform", "drift"}
        or any(
            not isinstance(item, str) or not item or len(item) > 256 or any(ord(character) < 32 for character in item)
            for item in runtime_values
        )
        or not (
            runtime.get("platform", "").casefold() == platform
            or runtime.get("platform", "").casefold().startswith(platform + "-")
        )
        or set(model) != {"id", "manifest_digest", "repository", "revision", "dtype"}
        or set(selection)
        != {
            "startup_artifact_paths",
            "weight_artifact_paths",
            "artifact_count",
            "artifact_bytes",
            "weight_artifact_bytes",
        }
        or set(transfer)
        != {
            "direct_upstream_transfer",
            "mirror_used",
            "source_class_verified",
            "transport_override_present",
            "elapsed_seconds",
            "max_resumptions",
            "resumptions",
            "completed",
        }
        or not isinstance(artifacts, list)
        or model.get("id") != model_id
        or model.get("manifest_digest") != manifest_digest
        or model.get("repository") != repository
        or model.get("revision") != profile["revision_commit"]
        or model.get("dtype") != dtype
    ):
        raise Gate14LifecycleError("cache materialization record binding is invalid")

    expected_artifacts = [
        {
            "path": item.path,
            "role": item.role,
            "sha256": item.sha256.removeprefix("sha256:"),
            "size_bytes": item.size_bytes,
        }
        for item in warm_cache.artifacts
    ]
    observed_artifacts: list[dict[str, Any]] = []
    total_resumptions = 0
    for artifact in artifacts:
        if not isinstance(artifact, dict) or set(artifact) != {
            "path",
            "role",
            "sha256",
            "size_bytes",
            "materialization_attempts",
            "resumptions",
            "resumed_from_bytes",
            "elapsed_seconds",
        }:
            raise Gate14LifecycleError("cache materialization artifact schema is invalid")
        observed = {
            "path": artifact.get("path"),
            "role": artifact.get("role"),
            "sha256": artifact.get("sha256"),
            "size_bytes": artifact.get("size_bytes"),
        }
        attempts = artifact.get("materialization_attempts")
        resumptions = artifact.get("resumptions")
        resumed = artifact.get("resumed_from_bytes")
        elapsed = artifact.get("elapsed_seconds")
        if (
            type(attempts) is not int
            or attempts < 1
            or type(resumptions) is not int
            or resumptions < 0
            or not isinstance(resumed, list)
            or len(resumed) != resumptions
            or any(type(item) is not int or item < 1 for item in resumed)
            or type(elapsed) not in (int, float)
            or not math.isfinite(float(elapsed))
            or elapsed < 0
        ):
            raise Gate14LifecycleError("cache materialization artifact proof is invalid")
        observed_artifacts.append(observed)
        total_resumptions += resumptions
    if not _exact_equal(observed_artifacts, expected_artifacts):
        raise Gate14LifecycleError("cache materialization artifacts changed")

    startup_paths = [
        item.path
        for item in warm_cache.artifacts
        if item.role in {"chat_template", "config", "tokenizer", "weight_index"}
    ]
    weight_paths = [item.path for item in warm_cache.artifacts if item.role == "weight"]
    if (
        selection.get("startup_artifact_paths") != sorted(startup_paths)
        or selection.get("weight_artifact_paths") != sorted(weight_paths)
        or type(selection.get("artifact_count")) is not int
        or selection.get("artifact_count") != warm_cache.artifact_count
        or type(selection.get("artifact_bytes")) is not int
        or selection.get("artifact_bytes") != warm_cache.artifact_bytes
        or type(selection.get("weight_artifact_bytes")) is not int
        or selection["weight_artifact_bytes"]
        != sum(item.size_bytes for item in warm_cache.artifacts if item.role == "weight")
        or type(transfer.get("elapsed_seconds")) not in (int, float)
        or not math.isfinite(float(transfer["elapsed_seconds"]))
        or transfer["elapsed_seconds"] < 0
        or transfer.get("direct_upstream_transfer") is not True
        or transfer.get("mirror_used") is not False
        or transfer.get("source_class_verified") is not True
        or transfer.get("transport_override_present") is not False
        or transfer.get("completed") is not True
        or type(transfer.get("max_resumptions")) is not int
        or transfer.get("max_resumptions") != 3
        or type(transfer.get("resumptions")) is not int
        or transfer.get("resumptions") != total_resumptions
        or not 0 <= total_resumptions <= 3
        or not _exact_equal(
            storage,
            {
                "cold_start": True,
                "cache_bytes_before": 0,
                "cache_bytes_after": warm_cache.artifact_bytes,
                "cache_growth_bytes": warm_cache.artifact_bytes,
                "verified": True,
            },
        )
        or not _exact_equal(
            privacy,
            {
                "credentials_retained": False,
                "local_paths_retained": False,
                "response_bodies_retained": False,
                "urls_retained": False,
            },
        )
    ):
        raise Gate14LifecycleError("cache materialization proof is invalid")


def build_warm_cache_binding(
    payload: bytes,
    *,
    platform: str,
    source_commit: str,
    materialization_plan_sha256: str,
    materializer_sources_sha256: str,
    model_id: str,
    manifest_digest: str,
) -> dict[str, Any]:
    """Build the exact lifecycle fragment for one verified fresh materialization."""
    expected_model = acceptance.EXPECTED_PLATFORM_MODELS.get(platform)
    profile = acceptance.MODEL_PROFILES.get(model_id)
    expected = _GATE9_WARM_CACHE.get(platform)
    if (
        expected_model != model_id
        or profile is None
        or expected is None
        or profile["manifest_digest"] != manifest_digest
        or not isinstance(source_commit, str)
        or _COMMIT_RE.fullmatch(source_commit) is None
        or not isinstance(materialization_plan_sha256, str)
        or _DIGEST_RE.fullmatch(materialization_plan_sha256) is None
        or not isinstance(materializer_sources_sha256, str)
        or _DIGEST_RE.fullmatch(materializer_sources_sha256) is None
    ):
        raise Gate14LifecycleError("cache materialization profile is invalid")
    artifacts = tuple(CacheArtifactBinding(*item) for item in expected["artifacts"])
    value = {
        "schema_version": 1,
        "layout": "manifest-artifacts-v1",
        "gate9_acquisition_record_sha256": expected["gate9_acquisition_record_sha256"],
        "gate9_resource_envelope_sha256": expected["gate9_resource_envelope_sha256"],
        "source_commit": source_commit,
        "materialization_plan_sha256": materialization_plan_sha256,
        "materializer_sources_sha256": materializer_sources_sha256,
        "materialization_record_sha256": _digest(payload),
        "materialization_record_bytes": len(payload),
        "artifact_count": len(artifacts),
        "artifact_bytes": sum(item.size_bytes for item in artifacts),
        "artifacts": [
            {
                "path": item.path,
                "role": item.role,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
            }
            for item in artifacts
        ],
    }
    binding = WarmCacheBinding(
        gate9_acquisition_record_sha256=value["gate9_acquisition_record_sha256"],
        gate9_resource_envelope_sha256=value["gate9_resource_envelope_sha256"],
        source_commit=value["source_commit"],
        materialization_plan_sha256=value["materialization_plan_sha256"],
        materializer_sources_sha256=value["materializer_sources_sha256"],
        materialization_record_sha256=value["materialization_record_sha256"],
        materialization_record_bytes=value["materialization_record_bytes"],
        materialization_record_path=Path(_MATERIALIZATION_RECORD_NAME),
        artifact_count=value["artifact_count"],
        artifact_bytes=value["artifact_bytes"],
        artifacts=artifacts,
        binding_sha256=_digest(_canonical(value)),
    )
    _validate_materialization_record(
        payload,
        platform=platform,
        model_id=model_id,
        manifest_digest=manifest_digest,
        warm_cache=binding,
    )
    return value


def _load_warm_cache(
    value: Any,
    *,
    staging_root: Path,
    ownership_verifier: Callable[..., None],
    platform: str,
    source_commit: str,
    model_id: str,
    manifest_digest: str,
) -> WarmCacheBinding:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "layout",
        "gate9_acquisition_record_sha256",
        "gate9_resource_envelope_sha256",
        "source_commit",
        "materialization_plan_sha256",
        "materializer_sources_sha256",
        "materialization_record_sha256",
        "materialization_record_bytes",
        "artifact_count",
        "artifact_bytes",
        "artifacts",
    }:
        raise Gate14LifecycleError("warm-cache binding schema is invalid")
    expected = _GATE9_WARM_CACHE[platform]
    raw_artifacts = value.get("artifacts")
    if (
        type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or value.get("layout") != "manifest-artifacts-v1"
        or value.get("gate9_acquisition_record_sha256") != expected["gate9_acquisition_record_sha256"]
        or value.get("gate9_resource_envelope_sha256") != expected["gate9_resource_envelope_sha256"]
        or not isinstance(raw_artifacts, list)
    ):
        raise Gate14LifecycleError("warm-cache Gate 9 identity is invalid")
    artifacts = tuple(_validate_cache_artifact(item) for item in raw_artifacts)
    expected_artifacts = tuple(CacheArtifactBinding(*item) for item in expected["artifacts"])
    profile = acceptance.MODEL_PROFILES[model_id]
    artifact_count = _bounded_integer(
        value.get("artifact_count"),
        "warm-cache artifact count",
        1,
        100,
    )
    artifact_bytes = _bounded_integer(
        value.get("artifact_bytes"),
        "warm-cache artifact bytes",
        1,
        acceptance.MAX_BYTES,
    )
    if (
        artifacts != expected_artifacts
        or artifact_count != len(artifacts)
        or artifact_count != profile["selected_artifact_count"]
        or artifact_bytes != sum(item.size_bytes for item in artifacts)
        or artifact_bytes != profile["selected_artifact_bytes"]
        or [item.path for item in artifacts] != sorted(item.path for item in artifacts)
        or len({item.path.casefold() for item in artifacts}) != len(artifacts)
        or manifest_digest != profile["manifest_digest"]
    ):
        raise Gate14LifecycleError("warm-cache artifact identity is invalid")

    record_path = _exact_staged_path(
        staging_root / _MATERIALIZATION_RECORD_NAME,
        staging_root / _MATERIALIZATION_RECORD_NAME,
        "cache materialization record",
        directory=False,
    )
    binding = WarmCacheBinding(
        gate9_acquisition_record_sha256=expected["gate9_acquisition_record_sha256"],
        gate9_resource_envelope_sha256=expected["gate9_resource_envelope_sha256"],
        source_commit=value.get("source_commit"),
        materialization_plan_sha256=_digest_field(
            value.get("materialization_plan_sha256"),
            "cache materialization plan digest",
        ),
        materializer_sources_sha256=_digest_field(
            value.get("materializer_sources_sha256"),
            "cache materializer sources digest",
        ),
        materialization_record_sha256=_digest_field(
            value.get("materialization_record_sha256"),
            "cache materialization record digest",
        ),
        materialization_record_bytes=_bounded_integer(
            value.get("materialization_record_bytes"),
            "cache materialization record size",
            1,
            MAX_MATERIALIZATION_RECORD_BYTES,
        ),
        materialization_record_path=record_path,
        artifact_count=artifact_count,
        artifact_bytes=artifact_bytes,
        artifacts=artifacts,
        binding_sha256=_digest(_canonical(value)),
    )
    if binding.source_commit != source_commit:
        raise Gate14LifecycleError("cache materialization source binding changed")
    ownership_verifier(record_path, directory=False)
    record = _regular_bytes(record_path, MAX_MATERIALIZATION_RECORD_BYTES)
    if len(record) != binding.materialization_record_bytes or _digest(record) != binding.materialization_record_sha256:
        raise Gate14LifecycleError("cache materialization record identity changed")
    _validate_materialization_record(
        record,
        platform=platform,
        model_id=model_id,
        manifest_digest=manifest_digest,
        warm_cache=binding,
    )
    return binding


def load_config(
    path: Path,
    *,
    ownership_verifier: Callable[..., None] | None = None,
) -> LifecycleConfig:
    if ownership_verifier is None:
        ownership_verifier = _assert_controller_owned
    payload = _regular_bytes(Path(path), MAX_CONFIG_BYTES)
    raw = _strict_json(payload, MAX_CONFIG_BYTES)
    if (
        set(raw) != _CONFIG_FIELDS
        or type(raw.get("schema_version")) is not int
        or raw.get("schema_version") != SCHEMA_VERSION
        or raw.get("scope") != SCOPE
    ):
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
    release_metadata_path = _exact_staged_path(
        _absolute_path(raw["release_metadata_path"], "release metadata path"),
        staging_root / _RELEASE_AUDIT_DIRECTORY_NAME / "release-metadata.json",
        "release metadata",
        directory=False,
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
    release_audit = _load_release_audit(
        raw["release_audit"],
        staging_root=staging_root,
        ownership_verifier=ownership_verifier,
        release_metadata_path=release_metadata_path,
        release_metadata_sha256=release_metadata_sha256,
        platform=platform,
        source_commit=source_commit,
        package_sha256=package_sha256,
        package_bytes=package_bytes,
    )
    warm_cache = _load_warm_cache(
        raw["warm_cache"],
        staging_root=staging_root,
        ownership_verifier=ownership_verifier,
        platform=platform,
        source_commit=source_commit,
        model_id=model_id,
        manifest_digest=manifest_digest,
    )

    challenge_path = _path_under_root(
        _absolute_path(raw["challenge_path"], "challenge path"),
        staging_root,
        _CHALLENGE_NAME,
        "controller challenge",
    )
    ownership_verifier(staging_root.parent, directory=True)
    ownership_verifier(staging_root, directory=True)
    for staged_path in (config_path, package_path, release_metadata_path):
        ownership_verifier(staged_path, directory=False)
    if challenge_path.exists():
        ownership_verifier(challenge_path, directory=False)
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
        release_audit=release_audit,
        warm_cache=warm_cache,
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
    if _digest(metadata) != config.release_metadata_sha256 or not _exact_equal(
        _strict_json(metadata, MAX_RELEASE_METADATA_BYTES),
        _RELEASE_METADATA,
    ):
        raise Gate14LifecycleError("release metadata binding changed")

    audit_payloads = _audit_member_payloads(config.release_audit)
    _validate_release_semantics(
        audit_payloads,
        platform=config.platform,
        source_commit=config.source_commit,
        package_sha256=config.package_sha256,
        package_bytes=config.package_bytes,
    )
    _assert_controller_owned(
        config.warm_cache.materialization_record_path,
        directory=False,
    )
    materialization = _regular_bytes(
        config.warm_cache.materialization_record_path,
        MAX_MATERIALIZATION_RECORD_BYTES,
    )
    if (
        len(materialization) != config.warm_cache.materialization_record_bytes
        or _digest(materialization) != config.warm_cache.materialization_record_sha256
    ):
        raise Gate14LifecycleError("cache materialization record identity changed")
    _validate_materialization_record(
        materialization,
        platform=config.platform,
        model_id=config.model_id,
        manifest_digest=config.manifest_digest,
        warm_cache=config.warm_cache,
    )


def validate_prepared(
    value: Mapping[str, Any],
    config: LifecycleConfig,
) -> Mapping[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != _PREPARED_FIELDS
        or type(value["schema_version"]) is not int
        or value["schema_version"] != SCHEMA_VERSION
        or value["scope"] != PREPARED_SCOPE
        or value["run_id"] != config.run_id
        or value["platform"] != config.platform
        or type(value["attempt_ordinal"]) is not int
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
        "release_audit_sha256": config.release_audit.binding_sha256,
        "warm_cache_sha256": config.warm_cache.binding_sha256,
        "materialization_record_sha256": (config.warm_cache.materialization_record_sha256),
        "prepared_facts_sha256": _digest(_canonical(prepared)),
        "phase": "challenge-ready",
        "created_at_unix": created_at_unix,
    }


def _validate_checkpoint_shape(value: Any) -> None:
    digest_fields = (
        "lifecycle_config_sha256",
        "package_sha256",
        "release_metadata_sha256",
        "release_audit_sha256",
        "warm_cache_sha256",
        "materialization_record_sha256",
        "prepared_facts_sha256",
    )
    if (
        not isinstance(value, dict)
        or set(value) != _CHECKPOINT_FIELDS
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != SCHEMA_VERSION
        or value.get("scope") != CHECKPOINT_SCOPE
        or not isinstance(value.get("run_id"), str)
        or _RUN_RE.fullmatch(value["run_id"]) is None
        or value.get("platform") not in {"windows", "linux"}
        or type(value.get("attempt_ordinal")) is not int
        or value.get("attempt_ordinal") < 1
        or not isinstance(value.get("source_commit"), str)
        or _COMMIT_RE.fullmatch(value["source_commit"]) is None
        or any(
            not isinstance(value.get(field), str) or _DIGEST_RE.fullmatch(value[field]) is None
            for field in digest_fields
        )
        or value.get("phase") != "challenge-ready"
        or type(value.get("created_at_unix")) is not int
        or value.get("created_at_unix") < 0
    ):
        raise Gate14LifecycleError("checkpoint schema is invalid")


def validate_checkpoint(
    value: Mapping[str, Any],
    config: LifecycleConfig,
    prepared: Mapping[str, Any],
    *,
    now_unix: float,
) -> Mapping[str, Any]:
    _validate_checkpoint_shape(value)
    expected = _checkpoint_value(
        config,
        prepared,
        value.get("created_at_unix"),
    )
    if not _exact_equal(value, expected):
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
    _validate_checkpoint_shape(value)
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
        or type(value.get("schema_version")) is not int
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
        or not isinstance(value.get("release_audit_sha256"), str)
        or _DIGEST_RE.fullmatch(value["release_audit_sha256"]) is None
        or not isinstance(value.get("warm_cache_sha256"), str)
        or _DIGEST_RE.fullmatch(value["warm_cache_sha256"]) is None
        or not isinstance(value.get("materialization_record_sha256"), str)
        or _DIGEST_RE.fullmatch(value["materialization_record_sha256"]) is None
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
        or type(value["schema_version"]) is not int
        or value["schema_version"] != SCHEMA_VERSION
        or value["scope"] != CLEANUP_SCOPE
        or value["run_id"] != config.run_id
        or value["platform"] != config.platform
        or type(value["attempt_ordinal"]) is not int
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
