"""Materialize, verify, and promote one exact Gate 14 warm cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, Mapping, Sequence
from urllib.request import getproxies

import gate14_packaged_lifecycle as lifecycle

import drift
import drift.model_manifest as model_manifest_module
import drift.node.edge_acquisition as edge_acquisition_module
from drift.model_manifest import ManifestError, ModelManifest
from drift.node.edge_acquisition import acquire_client_artifacts

SCHEMA_VERSION = 1
PLAN_SCOPE = "gate14-cache-materialization-plan"
HANDOFF_SCOPE = "gate14-cache-materialization-handoff"
RESULT_SCOPE = "gate14-cache-materialization"
PLAN_NAME = "gate14-cache-plan.json"
TEMPLATE_NAME = "gate14-lifecycle-template.json"
CONFIG_NAME = "gate14-lifecycle.json"
CACHE_NAME = "gate14-warm-cache"
RECORD_NAME = lifecycle._MATERIALIZATION_RECORD_NAME
BINDING_NAME = "gate14-warm-cache-binding.json"
HANDOFF_NAME = "gate14-cache-handoff.json"
MAX_JSON_BYTES = lifecycle.MAX_MATERIALIZATION_RECORD_BYTES
MAX_SOURCE_BYTES = 8 * 1024 * 1024
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_MANIFEST_NAMES = {
    "windows": "qwen3.5-2b-bfloat16-eager.json",
    "linux": "gemma-4-e2b-it-bfloat16-eager.json",
}
_OVERRIDE_NAMES = (
    "HF_ENDPOINT",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)
_SOURCE_NAMES = (
    "gate14_cache_materializer.py",
    "gate14_packaged_lifecycle.py",
    "drift/node/edge_acquisition.py",
    "drift/model_manifest.py",
)
_PLAN_FIELDS = {
    "schema_version",
    "scope",
    "platform",
    "source_commit",
    "manifest_path",
    "work_root",
    "staging_root",
    "lifecycle_template_sha256",
    "sources",
}
_HANDOFF_FIELDS = {
    "schema_version",
    "scope",
    "plan_sha256",
    "platform",
    "source_commit",
    "model_id",
    "manifest_digest",
    "materializer_sources_sha256",
    "materialization_record_sha256",
    "materialization_record_bytes",
    "warm_cache_binding_sha256",
    "warm_cache_binding_file_sha256",
    "warm_cache_binding_bytes",
    "artifact_count",
    "artifact_bytes",
}


class Gate14CacheMaterializationError(RuntimeError):
    """Fresh-cache materialization, verification, or promotion failed closed."""


Acquirer = Callable[..., Mapping[str, Any]]
OwnershipVerifier = Callable[..., None]


@dataclass(frozen=True)
class CachePlan:
    platform: str
    source_commit: str
    manifest_path: Path
    work_root: Path
    staging_root: Path
    template_path: Path
    template_sha256: str
    sources: Mapping[str, str]
    sources_sha256: str
    plan_sha256: str


@dataclass
class CacheLease:
    handles: list[Any]
    identities: list[tuple[Path, os.stat_result, bool]]
    directory_entries: list[tuple[Path, frozenset[str]]]

    def assert_stable(self) -> None:
        for path, expected, directory in self.identities:
            try:
                observed = path.lstat()
            except OSError as exc:
                raise Gate14CacheMaterializationError("warm cache changed after verification") from exc
            if (
                _reparse(observed)
                or path.is_symlink()
                or bool(stat.S_ISDIR(observed.st_mode)) is not directory
                or _file_identity(observed) != _file_identity(expected)
            ):
                raise Gate14CacheMaterializationError("warm cache changed after verification")
        for path, expected_names in self.directory_entries:
            try:
                observed_names = frozenset(entry.name for entry in os.scandir(path))
            except OSError as exc:
                raise Gate14CacheMaterializationError("warm cache changed after verification") from exc
            if observed_names != expected_names:
                raise Gate14CacheMaterializationError("warm cache changed after verification")

    def close(self) -> None:
        while self.handles:
            handle = self.handles.pop()
            try:
                handle.close()
            except OSError:
                pass


class _RawHandle:
    def __init__(self, value: int, closer: Callable[[int], Any]):
        self.value = value
        self._closer = closer

    def close(self) -> None:
        if self.value:
            value, self.value = self.value, 0
            self._closer(value)


def _native_platform() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    raise Gate14CacheMaterializationError("unsupported materialization platform")


def _reparse(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _canonical(value: Mapping[str, Any]) -> bytes:
    try:
        payload = (
            json.dumps(
                value,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise Gate14CacheMaterializationError("JSON value is invalid") from exc
    if not 1 <= len(payload) <= MAX_JSON_BYTES:
        raise Gate14CacheMaterializationError("JSON value exceeded its bound")
    return payload


def _strict_canonical(payload: bytes, label: str) -> Mapping[str, Any]:
    try:
        value = lifecycle._strict_json(payload, MAX_JSON_BYTES)
    except lifecycle.Gate14LifecycleError as exc:
        raise Gate14CacheMaterializationError(f"{label} is invalid") from exc
    if _canonical(value) != payload:
        raise Gate14CacheMaterializationError(f"{label} is not canonical")
    return value


def _safe_directory(path: Path, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise Gate14CacheMaterializationError(f"{label} must be absolute")
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise Gate14CacheMaterializationError(f"{label} is unavailable") from exc
    if _reparse(metadata) or candidate.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise Gate14CacheMaterializationError(f"{label} is unsafe")
    return candidate.resolve(strict=True)


def _windows_open(path: Path, *, directory: bool) -> tuple[int, Callable[[int], Any]]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    generic_read = 0x80000000
    share_read_only = 0x00000001
    open_existing = 3
    open_reparse_point = 0x00200000
    backup_semantics = 0x02000000
    sequential_scan = 0x08000000
    flags = open_reparse_point | (backup_semantics if directory else sequential_scan)
    raw_handle = create_file(
        str(path),
        generic_read,
        share_read_only,
        None,
        open_existing,
        flags,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if raw_handle == invalid_handle:
        error = ctypes.get_last_error()
        raise OSError(error, "could not open locked cache entry", str(path))
    return int(raw_handle), close_handle


def _open_locked_regular(
    path: Path,
    maximum: int,
    *,
    minimum: int = 1,
) -> tuple[BinaryIO, os.stat_result]:
    candidate = Path(path)
    try:
        before = candidate.lstat()
    except OSError as exc:
        raise Gate14CacheMaterializationError("required file is unavailable") from exc
    if (
        _reparse(before)
        or candidate.is_symlink()
        or not stat.S_ISREG(before.st_mode)
        or not minimum <= before.st_size <= maximum
    ):
        raise Gate14CacheMaterializationError("required file is unsafe")

    handle: BinaryIO | None = None
    try:
        if os.name == "nt":
            import msvcrt

            raw_handle, close_handle = _windows_open(candidate, directory=False)
            try:
                descriptor = msvcrt.open_osfhandle(raw_handle, os.O_RDONLY)
            except BaseException:
                close_handle(raw_handle)
                raise
        else:
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(candidate, flags)
        handle = os.fdopen(descriptor, "rb", closefd=True)
        opened = os.fstat(handle.fileno())
        if _reparse(opened) or not stat.S_ISREG(opened.st_mode) or _file_identity(before) != _file_identity(opened):
            raise Gate14CacheMaterializationError("required file changed while opening")
        return handle, opened
    except Gate14CacheMaterializationError:
        if handle is not None:
            handle.close()
        raise
    except OSError as exc:
        if handle is not None:
            handle.close()
        raise Gate14CacheMaterializationError("required file is unreadable") from exc


def _read_locked_regular(
    path: Path,
    maximum: int,
    *,
    minimum: int = 1,
) -> bytes:
    handle, opened = _open_locked_regular(path, maximum, minimum=minimum)
    try:
        payload = handle.read(maximum + 1)
        after_handle = os.fstat(handle.fileno())
        after_path = Path(path).lstat()
    except OSError as exc:
        raise Gate14CacheMaterializationError("required file is unreadable") from exc
    finally:
        handle.close()
    if (
        len(payload) != opened.st_size
        or _file_identity(opened) != _file_identity(after_handle)
        or _file_identity(opened) != _file_identity(after_path)
        or _reparse(after_path)
        or Path(path).is_symlink()
    ):
        raise Gate14CacheMaterializationError("required file changed while reading")
    return payload


def _open_locked_directory(path: Path) -> tuple[Any, os.stat_result]:
    candidate = Path(path)
    try:
        before = candidate.lstat()
    except OSError as exc:
        raise Gate14CacheMaterializationError("warm cache directory is unavailable") from exc
    if _reparse(before) or candidate.is_symlink() or not stat.S_ISDIR(before.st_mode):
        raise Gate14CacheMaterializationError("warm cache contains an unsafe directory")
    try:
        if os.name == "nt":
            raw_handle, closer = _windows_open(candidate, directory=True)
            handle: Any = _RawHandle(raw_handle, closer)
        else:
            descriptor = os.open(
                candidate,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            handle = os.fdopen(descriptor, "rb", closefd=True)
            opened = os.fstat(handle.fileno())
            if _file_identity(before) != _file_identity(opened):
                raise Gate14CacheMaterializationError("warm cache directory changed while opening")
        after = candidate.lstat()
        if (
            _reparse(after)
            or candidate.is_symlink()
            or not stat.S_ISDIR(after.st_mode)
            or _file_identity(before) != _file_identity(after)
        ):
            raise Gate14CacheMaterializationError("warm cache directory changed while opening")
        return handle, before
    except Gate14CacheMaterializationError:
        try:
            handle.close()
        except (OSError, UnboundLocalError):
            pass
        raise
    except OSError as exc:
        raise Gate14CacheMaterializationError("warm cache directory is unreadable") from exc


def _source_paths() -> Mapping[str, Path]:
    return {
        "gate14_cache_materializer.py": Path(__file__),
        "gate14_packaged_lifecycle.py": Path(lifecycle.__file__),
        "drift/node/edge_acquisition.py": Path(edge_acquisition_module.__file__),
        "drift/model_manifest.py": Path(model_manifest_module.__file__),
    }


def current_source_bindings() -> Mapping[str, str]:
    result: dict[str, str] = {}
    for name, path in _source_paths().items():
        payload = _read_locked_regular(path, MAX_SOURCE_BYTES)
        normalized = payload.replace(b"\r\n", b"\n")
        if b"\r" in normalized:
            raise Gate14CacheMaterializationError("materializer source line endings are invalid")
        result[name] = "sha256:" + hashlib.sha256(normalized).hexdigest()
    return result


def _load_plan(
    plan_path: Path,
    *,
    ownership_verifier: OwnershipVerifier = lifecycle._assert_controller_owned,
) -> CachePlan:
    payload = _read_locked_regular(plan_path, lifecycle.MAX_CONFIG_BYTES)
    raw = _strict_canonical(payload, "cache materialization plan")
    if (
        set(raw) != _PLAN_FIELDS
        or type(raw.get("schema_version")) is not int
        or raw.get("schema_version") != SCHEMA_VERSION
        or raw.get("scope") != PLAN_SCOPE
        or raw.get("platform") not in _MANIFEST_NAMES
        or not isinstance(raw.get("source_commit"), str)
        or _COMMIT_RE.fullmatch(raw["source_commit"]) is None
        or not isinstance(raw.get("lifecycle_template_sha256"), str)
        or _DIGEST_RE.fullmatch(raw["lifecycle_template_sha256"]) is None
        or not isinstance(raw.get("sources"), dict)
        or set(raw["sources"]) != set(_SOURCE_NAMES)
        or any(not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None for value in raw["sources"].values())
    ):
        raise Gate14CacheMaterializationError("cache materialization plan binding is invalid")
    try:
        manifest_path = Path(raw["manifest_path"])
        work_root = Path(raw["work_root"])
        staging_root = Path(raw["staging_root"])
    except TypeError as exc:
        raise Gate14CacheMaterializationError("cache materialization plan path is invalid") from exc
    work = _safe_directory(work_root, "work root")
    staging = _safe_directory(staging_root, "staging root")
    if work == staging or work in staging.parents or staging in work.parents:
        raise Gate14CacheMaterializationError("materialization roots overlap")
    exact_plan = staging / PLAN_NAME
    exact_template = staging / TEMPLATE_NAME
    if (
        Path(os.path.abspath(os.fspath(plan_path))) != exact_plan
        or not manifest_path.is_absolute()
        or manifest_path.name != _MANIFEST_NAMES[raw["platform"]]
    ):
        raise Gate14CacheMaterializationError("cache materialization plan path binding is invalid")
    try:
        ownership_verifier(staging.parent, directory=True)
        ownership_verifier(staging, directory=True)
        ownership_verifier(exact_plan, directory=False)
        ownership_verifier(exact_template, directory=False)
    except lifecycle.Gate14LifecycleError as exc:
        raise Gate14CacheMaterializationError("cache materialization plan is not controller-owned") from exc
    sources = dict(raw["sources"])
    return CachePlan(
        platform=raw["platform"],
        source_commit=raw["source_commit"],
        manifest_path=manifest_path,
        work_root=work,
        staging_root=staging,
        template_path=exact_template,
        template_sha256=raw["lifecycle_template_sha256"],
        sources=sources,
        sources_sha256=lifecycle._digest(lifecycle._canonical(sources)),
        plan_sha256=lifecycle._digest(payload),
    )


def _verify_sources(plan: CachePlan) -> None:
    if current_source_bindings() != plan.sources:
        raise Gate14CacheMaterializationError("cache materializer source binding changed")


def _safe_manifest(path: Path, platform_name: str) -> ModelManifest:
    if not Path(path).is_absolute() or Path(path).name != _MANIFEST_NAMES[platform_name]:
        raise Gate14CacheMaterializationError("manifest path binding is invalid")
    payload = _read_locked_regular(path, lifecycle.MAX_CONFIG_BYTES)
    try:
        manifest = ModelManifest.from_json(payload.decode("utf-8"))
    except (UnicodeError, ManifestError) as exc:
        raise Gate14CacheMaterializationError("manifest is invalid") from exc
    return manifest


def _validate_manifest(
    manifest: ModelManifest,
    *,
    platform_name: str,
) -> tuple[str, str]:
    model_id = lifecycle.acceptance.EXPECTED_PLATFORM_MODELS[platform_name]
    profile = lifecycle.acceptance.MODEL_PROFILES[model_id]
    repository, dtype = lifecycle._MODEL_SOURCE[model_id]
    expected = lifecycle._GATE9_WARM_CACHE[platform_name]["artifacts"]
    observed = tuple(
        (item.path, item.role, "sha256:" + item.sha256, item.size)
        for item in sorted(manifest.artifacts, key=lambda value: value.path)
    )
    if (
        manifest.name != model_id
        or manifest.digest_id != profile["manifest_digest"]
        or manifest.source.repository != repository
        or manifest.source.revision != profile["revision_commit"]
        or manifest.runtime.dtype != dtype
        or manifest.runtime.adapter_profile != "none"
        or observed != expected
    ):
        raise Gate14CacheMaterializationError("manifest profile binding changed")
    try:
        manifest.validate_runtime(drift.__version__)
    except ManifestError as exc:
        raise Gate14CacheMaterializationError("manifest runtime binding changed") from exc
    return model_id, profile["manifest_digest"]


def _transport_overridden() -> bool:
    if any(os.environ.get(name) for name in _OVERRIDE_NAMES):
        return True
    try:
        proxies = getproxies()
    except OSError:
        return True
    return any(value for key, value in proxies.items() if key.casefold() not in {"no", "no_proxy"})


def _clear_acquisition_metadata(
    cache_root: Path,
    manifest_digest: str,
    artifact_paths: Sequence[str],
) -> None:
    manifest_root = cache_root / "manifest-artifacts" / manifest_digest.removeprefix("sha256:")
    partial = manifest_root / "partial"
    locks = manifest_root / "locks"
    if _entry_exists(partial):
        metadata = partial.lstat()
        if _reparse(metadata) or partial.is_symlink() or not stat.S_ISDIR(metadata.st_mode) or any(partial.iterdir()):
            raise Gate14CacheMaterializationError("acquisition retained partial material")
        partial.rmdir()
    if _entry_exists(locks):
        metadata = locks.lstat()
        if _reparse(metadata) or locks.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise Gate14CacheMaterializationError("acquisition lock directory is unsafe")
        expected = {hashlib.sha256(path.encode("utf-8")).hexdigest() + ".lock" for path in artifact_paths}
        entries = list(locks.iterdir())
        if {entry.name for entry in entries} != expected:
            raise Gate14CacheMaterializationError("acquisition lock inventory changed")
        for entry in entries:
            metadata = entry.lstat()
            if _reparse(metadata) or entry.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_size != 0:
                raise Gate14CacheMaterializationError("acquisition lock entry is unsafe")
            entry.unlink()
        locks.rmdir()


def _expected_cache(
    binding: Mapping[str, Any],
) -> tuple[dict[str, tuple[int, str]], set[str]]:
    artifacts = binding.get("artifacts")
    record_digest = binding.get("materialization_record_sha256")
    if (
        not isinstance(artifacts, list)
        or not isinstance(record_digest, str)
        or _DIGEST_RE.fullmatch(record_digest) is None
    ):
        raise Gate14CacheMaterializationError("warm-cache binding is invalid")
    manifest_digest = None
    for platform_name, model_id in lifecycle.acceptance.EXPECTED_PLATFORM_MODELS.items():
        profile = lifecycle.acceptance.MODEL_PROFILES[model_id]
        expected_artifacts = [
            {
                "path": path,
                "role": role,
                "sha256": digest,
                "size_bytes": size,
            }
            for path, role, digest, size in lifecycle._GATE9_WARM_CACHE[platform_name]["artifacts"]
        ]
        if expected_artifacts == artifacts:
            manifest_digest = profile["manifest_digest"].removeprefix("sha256:")
            break
    if manifest_digest is None:
        raise Gate14CacheMaterializationError("warm-cache artifact profile is unknown")
    expected_files: dict[str, tuple[int, str]] = {}
    expected_directories: set[str] = set()
    prefix = f"manifest-artifacts/{manifest_digest}/snapshot"
    for item in artifacts:
        relative = f"{prefix}/{item['path']}"
        expected_files[relative] = (
            item["size_bytes"],
            item["sha256"].removeprefix("sha256:"),
        )
        parts = relative.split("/")
        expected_directories.update("/".join(parts[:index]) for index in range(1, len(parts)))
    return expected_files, expected_directories


def _hash_open_file(
    handle: BinaryIO,
    opened: os.stat_result,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        size += len(chunk)
        digest.update(chunk)
    after = os.fstat(handle.fileno())
    if _file_identity(opened) != _file_identity(after):
        raise Gate14CacheMaterializationError("warm cache artifact changed while hashing")
    return size, digest.hexdigest()


def verify_exact_cache(
    cache_root: Path,
    binding: Mapping[str, Any],
) -> CacheLease:
    cache_root = Path(cache_root)
    expected_files, expected_directories = _expected_cache(binding)
    lease = CacheLease([], [], [])
    seen_files: set[str] = set()
    seen_directories: set[str] = set()
    seen_casefold: set[str] = set()
    pending = [cache_root]
    try:
        while pending:
            root_path = pending.pop()
            directory_handle, directory_metadata = _open_locked_directory(root_path)
            lease.handles.append(directory_handle)
            lease.identities.append((root_path, directory_metadata, True))
            try:
                entries = list(os.scandir(root_path))
            except OSError as exc:
                raise Gate14CacheMaterializationError("warm cache directory is unreadable") from exc
            lease.directory_entries.append((root_path, frozenset(entry.name for entry in entries)))
            for entry in entries:
                path = root_path / entry.name
                relative = path.relative_to(cache_root).as_posix()
                folded = relative.casefold()
                if folded in seen_casefold:
                    raise Gate14CacheMaterializationError("warm cache path collides")
                seen_casefold.add(folded)
                try:
                    metadata = path.lstat()
                except OSError as exc:
                    raise Gate14CacheMaterializationError("warm cache entry is unavailable") from exc
                if (
                    _reparse(metadata)
                    or path.is_symlink()
                    or not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode))
                ):
                    raise Gate14CacheMaterializationError("warm cache contains an unsafe entry")
                if stat.S_ISDIR(metadata.st_mode):
                    if relative not in expected_directories:
                        raise Gate14CacheMaterializationError("warm cache contains an unexpected directory")
                    seen_directories.add(relative)
                    pending.append(path)
                    continue
                if relative not in expected_files:
                    raise Gate14CacheMaterializationError("warm cache contains an unexpected file")
                handle, opened = _open_locked_regular(
                    path,
                    expected_files[relative][0],
                )
                lease.handles.append(handle)
                lease.identities.append((path, opened, False))
                size, digest = _hash_open_file(handle, opened)
                if (size, digest) != expected_files[relative]:
                    raise Gate14CacheMaterializationError("warm cache artifact verification failed")
                seen_files.add(relative)
        if seen_files != set(expected_files) or seen_directories != expected_directories:
            raise Gate14CacheMaterializationError("warm cache inventory is incomplete")
        lease.assert_stable()
        return lease
    except BaseException:
        lease.close()
        raise


def _entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise Gate14CacheMaterializationError("materialization output state is unavailable") from exc
    return True


def _unlink_retry(path: Path) -> None:
    last_error: OSError | None = None
    for _attempt in range(2):
        try:
            path.unlink()
            break
        except FileNotFoundError:
            break
        except OSError as exc:
            last_error = exc
    if _entry_exists(path):
        raise Gate14CacheMaterializationError("materialization output cleanup did not complete") from last_error


def _remove_tree_retry(path: Path) -> None:
    last_error: OSError | None = None
    for _attempt in range(2):
        if not _entry_exists(path):
            return
        try:
            metadata = path.lstat()
            if _reparse(metadata) or path.is_symlink():
                if stat.S_ISDIR(metadata.st_mode):
                    path.rmdir()
                else:
                    path.unlink()
            elif stat.S_ISDIR(metadata.st_mode):
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as exc:
            last_error = exc
    if _entry_exists(path):
        raise Gate14CacheMaterializationError("materialization cache cleanup did not complete") from last_error


def _write_new(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException as exc:
        try:
            _unlink_retry(path)
        except Gate14CacheMaterializationError as cleanup_exc:
            raise Gate14CacheMaterializationError("incomplete materialization output could not be removed") from exc
        raise


_WINDOWS_CONTROLLER_SDDL = "O:BAG:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)"


def _windows_protect_controller_output(path: Path) -> None:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    local_free = kernel32.LocalFree
    local_free.argtypes = (ctypes.c_void_p,)
    local_free.restype = ctypes.c_void_p

    descriptor = ctypes.c_void_p()
    descriptor_size = wintypes.DWORD()
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    )
    convert.restype = wintypes.BOOL
    if not convert(
        _WINDOWS_CONTROLLER_SDDL,
        1,
        ctypes.byref(descriptor),
        ctypes.byref(descriptor_size),
    ):
        error = ctypes.get_last_error()
        raise Gate14CacheMaterializationError(
            "controller output security descriptor could not be created"
        ) from OSError(error, "security descriptor conversion failed")

    try:
        owner = ctypes.c_void_p()
        owner_defaulted = wintypes.BOOL()
        get_owner = advapi32.GetSecurityDescriptorOwner
        get_owner.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.BOOL),
        )
        get_owner.restype = wintypes.BOOL
        present = wintypes.BOOL()
        dacl = ctypes.c_void_p()
        dacl_defaulted = wintypes.BOOL()
        get_dacl = advapi32.GetSecurityDescriptorDacl
        get_dacl.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.BOOL),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.BOOL),
        )
        get_dacl.restype = wintypes.BOOL
        if (
            not get_owner(
                descriptor,
                ctypes.byref(owner),
                ctypes.byref(owner_defaulted),
            )
            or not get_dacl(
                descriptor,
                ctypes.byref(present),
                ctypes.byref(dacl),
                ctypes.byref(dacl_defaulted),
            )
            or not owner.value
            or not present.value
            or not dacl.value
        ):
            error = ctypes.get_last_error()
            raise Gate14CacheMaterializationError("controller output security descriptor is invalid") from OSError(
                error, "security descriptor parsing failed"
            )

        set_security = advapi32.SetNamedSecurityInfoW
        set_security.argtypes = (
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        set_security.restype = wintypes.DWORD
        result = set_security(
            os.fspath(path),
            1,
            0x1 | 0x4 | 0x80000000,
            owner,
            None,
            dacl,
            None,
        )
        if result != 0:
            raise Gate14CacheMaterializationError(
                "controller output security descriptor could not be installed"
            ) from OSError(result, "security descriptor installation failed")
    finally:
        local_free(descriptor)


def _protect_promoted_output(
    path: Path,
    *,
    os_name: str | None = None,
) -> None:
    observed_os = os.name if os_name is None else os_name
    if observed_os == "nt":
        _windows_protect_controller_output(path)
    else:
        try:
            path.chmod(0o600)
        except OSError as exc:
            raise Gate14CacheMaterializationError("controller output permissions could not be installed") from exc
    try:
        lifecycle._assert_controller_managed(path, directory=False)
    except lifecycle.Gate14LifecycleError as exc:
        raise Gate14CacheMaterializationError("controller output protection is invalid") from exc


def _cleanup_materialization(
    *,
    outputs: Sequence[tuple[Path, bool]],
    cache: Path,
    cache_created: bool,
) -> None:
    failures = []
    for path, created in outputs:
        if not created:
            continue
        try:
            _unlink_retry(path)
        except Gate14CacheMaterializationError as exc:
            failures.append(exc)
    if cache_created:
        try:
            _remove_tree_retry(cache)
        except Gate14CacheMaterializationError as exc:
            failures.append(exc)
    if failures:
        raise Gate14CacheMaterializationError("materialization failed and cleanup did not complete") from failures[0]


def materialize(
    *,
    plan_path: Path,
    acquirer: Acquirer = acquire_client_artifacts,
    ownership_verifier: OwnershipVerifier = lifecycle._assert_controller_owned,
    cache_verifier: Callable[[Path, Mapping[str, Any]], CacheLease] = verify_exact_cache,
    native_platform: str | None = None,
) -> Mapping[str, Any]:
    plan = _load_plan(plan_path, ownership_verifier=ownership_verifier)
    observed_platform = _native_platform() if native_platform is None else native_platform
    if observed_platform != plan.platform:
        raise Gate14CacheMaterializationError("native materialization platform changed")
    _validate_template(
        plan=plan,
        template_payload=_read_locked_regular(
            plan.template_path,
            lifecycle.MAX_CONFIG_BYTES,
        ),
    )
    if _transport_overridden():
        raise Gate14CacheMaterializationError("official upstream transport is overridden")
    _verify_sources(plan)
    manifest = _safe_manifest(plan.manifest_path, plan.platform)
    model_id, manifest_digest = _validate_manifest(
        manifest,
        platform_name=plan.platform,
    )

    cache = plan.work_root / CACHE_NAME
    record_path = plan.work_root / RECORD_NAME
    binding_path = plan.work_root / BINDING_NAME
    handoff_path = plan.work_root / HANDOFF_NAME
    if any(_entry_exists(path) for path in (cache, record_path, binding_path, handoff_path)):
        raise Gate14CacheMaterializationError("materialization outputs are not fresh")

    cache_created = False
    record_created = False
    binding_created = False
    handoff_created = False
    lease: CacheLease | None = None
    try:
        cache.mkdir(mode=0o700)
        cache_created = True
        record = acquirer(
            manifest,
            cache_dir=cache,
            token=False,
            max_resumptions=3,
            require_direct_upstream=True,
        )
        record_payload = _canonical(record)
        binding = lifecycle.build_warm_cache_binding(
            record_payload,
            platform=plan.platform,
            source_commit=plan.source_commit,
            materialization_plan_sha256=plan.plan_sha256,
            materializer_sources_sha256=plan.sources_sha256,
            model_id=model_id,
            manifest_digest=manifest_digest,
        )
        _clear_acquisition_metadata(
            cache,
            manifest_digest,
            [item["path"] for item in binding["artifacts"]],
        )
        lease = cache_verifier(cache, binding)
        binding_payload = _canonical(binding)
        binding_sha256 = lifecycle._digest(lifecycle._canonical(binding))
        handoff = {
            "schema_version": SCHEMA_VERSION,
            "scope": HANDOFF_SCOPE,
            "plan_sha256": plan.plan_sha256,
            "platform": plan.platform,
            "source_commit": plan.source_commit,
            "model_id": model_id,
            "manifest_digest": manifest_digest,
            "materializer_sources_sha256": plan.sources_sha256,
            "materialization_record_sha256": binding["materialization_record_sha256"],
            "materialization_record_bytes": len(record_payload),
            "warm_cache_binding_sha256": binding_sha256,
            "warm_cache_binding_file_sha256": lifecycle._digest(binding_payload),
            "warm_cache_binding_bytes": len(binding_payload),
            "artifact_count": binding["artifact_count"],
            "artifact_bytes": binding["artifact_bytes"],
        }
        _write_new(record_path, record_payload)
        record_created = True
        _write_new(binding_path, binding_payload)
        binding_created = True
        _write_new(handoff_path, _canonical(handoff))
        handoff_created = True
        lease.assert_stable()
    except BaseException as exc:
        if lease is not None:
            lease.close()
        try:
            _cleanup_materialization(
                outputs=(
                    (handoff_path, handoff_created),
                    (binding_path, binding_created),
                    (record_path, record_created),
                ),
                cache=cache,
                cache_created=cache_created,
            )
        except Gate14CacheMaterializationError as cleanup_exc:
            raise cleanup_exc from exc
        raise
    finally:
        if lease is not None:
            lease.close()

    return {
        "schema_version": SCHEMA_VERSION,
        "scope": RESULT_SCOPE,
        "result": "passed",
        "phase": "materialized",
        "platform": plan.platform,
        "source_commit": plan.source_commit,
        "plan_sha256": plan.plan_sha256,
        "materializer_sources_sha256": plan.sources_sha256,
        "model_id": model_id,
        "manifest_digest": manifest_digest,
        "materialization_record_sha256": binding["materialization_record_sha256"],
        "artifact_count": binding["artifact_count"],
        "artifact_bytes": binding["artifact_bytes"],
        "warm_cache_binding_sha256": binding_sha256,
    }


def _validate_handoff(
    payload: bytes,
    *,
    plan: CachePlan,
    record_payload: bytes,
    binding_payload: bytes,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    handoff = _strict_canonical(payload, "cache materialization handoff")
    binding = _strict_canonical(binding_payload, "warm-cache binding")
    model_id = lifecycle.acceptance.EXPECTED_PLATFORM_MODELS[plan.platform]
    manifest_digest = lifecycle.acceptance.MODEL_PROFILES[model_id]["manifest_digest"]
    expected_binding = lifecycle.build_warm_cache_binding(
        record_payload,
        platform=plan.platform,
        source_commit=plan.source_commit,
        materialization_plan_sha256=plan.plan_sha256,
        materializer_sources_sha256=plan.sources_sha256,
        model_id=model_id,
        manifest_digest=manifest_digest,
    )
    expected_handoff = {
        "schema_version": SCHEMA_VERSION,
        "scope": HANDOFF_SCOPE,
        "plan_sha256": plan.plan_sha256,
        "platform": plan.platform,
        "source_commit": plan.source_commit,
        "model_id": model_id,
        "manifest_digest": manifest_digest,
        "materializer_sources_sha256": plan.sources_sha256,
        "materialization_record_sha256": expected_binding["materialization_record_sha256"],
        "materialization_record_bytes": len(record_payload),
        "warm_cache_binding_sha256": lifecycle._digest(lifecycle._canonical(expected_binding)),
        "warm_cache_binding_file_sha256": lifecycle._digest(binding_payload),
        "warm_cache_binding_bytes": len(binding_payload),
        "artifact_count": expected_binding["artifact_count"],
        "artifact_bytes": expected_binding["artifact_bytes"],
    }
    if set(handoff) != _HANDOFF_FIELDS or handoff != expected_handoff or binding != expected_binding:
        raise Gate14CacheMaterializationError("cache materialization handoff binding changed")
    return handoff, binding


def _validate_template(
    *,
    plan: CachePlan,
    template_payload: bytes,
) -> Mapping[str, Any]:
    if lifecycle._digest(template_payload) != plan.template_sha256:
        raise Gate14CacheMaterializationError("lifecycle template identity changed")
    template = _strict_canonical(template_payload, "lifecycle template")
    expected_model = lifecycle.acceptance.EXPECTED_PLATFORM_MODELS[plan.platform]
    expected_manifest = lifecycle.acceptance.MODEL_PROFILES[expected_model]["manifest_digest"]
    if (
        set(template) != lifecycle._CONFIG_FIELDS
        or template.get("warm_cache") is not None
        or template.get("platform") != plan.platform
        or template.get("source_commit") != plan.source_commit
        or template.get("model_id") != expected_model
        or template.get("manifest_digest") != expected_manifest
        or template.get("work_root") != str(plan.work_root)
        or template.get("staging_root") != str(plan.staging_root)
    ):
        raise Gate14CacheMaterializationError("lifecycle template binding changed")
    return template


def _final_config(
    *,
    plan: CachePlan,
    template_payload: bytes,
    binding: Mapping[str, Any],
) -> tuple[Mapping[str, Any], bytes]:
    value = dict(
        _validate_template(
            plan=plan,
            template_payload=template_payload,
        )
    )
    value["warm_cache"] = dict(binding)
    return value, _canonical(value)


def _load_promoted_config(path: Path) -> Any:
    return lifecycle.load_config(
        path,
        ownership_verifier=lifecycle._assert_controller_managed,
    )


def _validate_promoted_config(
    *,
    plan: CachePlan,
    config_path: Path,
    config_payload: bytes,
    record_payload: bytes,
    lifecycle_loader: Callable[[Path], Any],
) -> Any:
    loaded = lifecycle_loader(config_path)
    if (
        loaded.platform != plan.platform
        or loaded.source_commit != plan.source_commit
        or loaded.config_sha256 != lifecycle._digest(config_payload)
        or loaded.warm_cache.materialization_plan_sha256 != plan.plan_sha256
        or loaded.warm_cache.materializer_sources_sha256 != plan.sources_sha256
        or loaded.warm_cache.materialization_record_sha256 != lifecycle._digest(record_payload)
        or loaded.warm_cache.materialization_record_bytes != len(record_payload)
    ):
        raise Gate14CacheMaterializationError("promoted lifecycle configuration changed")
    return loaded


def _cleanup_promoted_handoff(paths: Sequence[Path]) -> None:
    failures = []
    for path in paths:
        try:
            _unlink_retry(path)
        except Gate14CacheMaterializationError as exc:
            failures.append(exc)
    if failures or any(_entry_exists(path) for path in paths):
        raise Gate14CacheMaterializationError(
            "promoted lifecycle inputs were committed but handoff cleanup did not complete"
        ) from (failures[0] if failures else None)


def _promotion_result(
    *,
    plan: CachePlan,
    config_payload: bytes,
    binding: Mapping[str, Any],
) -> Mapping[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": RESULT_SCOPE,
        "result": "passed",
        "phase": "promoted",
        "platform": plan.platform,
        "source_commit": plan.source_commit,
        "plan_sha256": plan.plan_sha256,
        "materializer_sources_sha256": plan.sources_sha256,
        "model_id": lifecycle.acceptance.EXPECTED_PLATFORM_MODELS[plan.platform],
        "manifest_digest": lifecycle.acceptance.MODEL_PROFILES[
            lifecycle.acceptance.EXPECTED_PLATFORM_MODELS[plan.platform]
        ]["manifest_digest"],
        "materialization_record_sha256": binding["materialization_record_sha256"],
        "artifact_count": binding["artifact_count"],
        "artifact_bytes": binding["artifact_bytes"],
        "warm_cache_binding_sha256": lifecycle._digest(lifecycle._canonical(binding)),
        "lifecycle_config_sha256": lifecycle._digest(config_payload),
    }


def promote(
    *,
    plan_path: Path,
    ownership_verifier: OwnershipVerifier = lifecycle._assert_controller_managed,
    cache_verifier: Callable[[Path, Mapping[str, Any]], CacheLease] = verify_exact_cache,
    lifecycle_loader: Callable[[Path], Any] = _load_promoted_config,
    output_protector: Callable[[Path], None] = _protect_promoted_output,
) -> Mapping[str, Any]:
    plan = _load_plan(plan_path, ownership_verifier=ownership_verifier)
    _verify_sources(plan)
    cache = plan.work_root / CACHE_NAME
    work_record = plan.work_root / RECORD_NAME
    work_binding = plan.work_root / BINDING_NAME
    work_handoff = plan.work_root / HANDOFF_NAME
    handoff_paths = (work_handoff, work_binding, work_record)
    staged_record = plan.staging_root / RECORD_NAME
    config_path = plan.staging_root / CONFIG_NAME
    template_payload = _read_locked_regular(
        plan.template_path,
        lifecycle.MAX_CONFIG_BYTES,
    )
    _validate_template(plan=plan, template_payload=template_payload)

    staged_state = (
        _entry_exists(staged_record),
        _entry_exists(config_path),
    )
    if any(staged_state) and not all(staged_state):
        raise Gate14CacheMaterializationError("promoted lifecycle outputs are incomplete")
    if all(staged_state):
        record_payload = _read_locked_regular(staged_record, MAX_JSON_BYTES)
        config_payload = _read_locked_regular(
            config_path,
            lifecycle.MAX_CONFIG_BYTES,
        )
        config = _strict_canonical(config_payload, "promoted lifecycle configuration")
        binding = config.get("warm_cache")
        if not isinstance(binding, dict):
            raise Gate14CacheMaterializationError("promoted lifecycle configuration changed")
        _validate_promoted_config(
            plan=plan,
            config_path=config_path,
            config_payload=config_payload,
            record_payload=record_payload,
            lifecycle_loader=lifecycle_loader,
        )
        lease = cache_verifier(cache, binding)
        try:
            lease.assert_stable()
            _cleanup_promoted_handoff(handoff_paths)
            lease.assert_stable()
        finally:
            lease.close()
        return _promotion_result(
            plan=plan,
            config_payload=config_payload,
            binding=binding,
        )

    record_payload = _read_locked_regular(work_record, MAX_JSON_BYTES)
    binding_payload = _read_locked_regular(work_binding, MAX_JSON_BYTES)
    handoff_payload = _read_locked_regular(work_handoff, MAX_JSON_BYTES)
    handoff, binding = _validate_handoff(
        handoff_payload,
        plan=plan,
        record_payload=record_payload,
        binding_payload=binding_payload,
    )
    _config, config_payload = _final_config(
        plan=plan,
        template_payload=template_payload,
        binding=binding,
    )

    record_created = False
    config_created = False
    committed = False
    lease: CacheLease | None = None
    try:
        lease = cache_verifier(cache, binding)
        _write_new(staged_record, record_payload)
        record_created = True
        output_protector(staged_record)
        _write_new(config_path, config_payload)
        config_created = True
        output_protector(config_path)
        loaded = _validate_promoted_config(
            plan=plan,
            config_path=config_path,
            config_payload=config_payload,
            record_payload=record_payload,
            lifecycle_loader=lifecycle_loader,
        )
        if loaded.warm_cache.binding_sha256 != handoff["warm_cache_binding_sha256"]:
            raise Gate14CacheMaterializationError("promoted lifecycle configuration changed")
        lease.assert_stable()
        committed = True
        _cleanup_promoted_handoff(handoff_paths)
        lease.assert_stable()
    except BaseException as exc:
        if lease is not None:
            lease.close()
        if committed:
            raise Gate14CacheMaterializationError(
                "promoted lifecycle outputs were committed; retry promotion cleanup"
            ) from exc
        failures = []
        for path, created in (
            (config_path, config_created),
            (staged_record, record_created),
        ):
            if not created:
                continue
            try:
                _unlink_retry(path)
            except Gate14CacheMaterializationError as cleanup_exc:
                failures.append(cleanup_exc)
        if failures:
            raise Gate14CacheMaterializationError(
                "promotion failed and protected-output cleanup did not complete"
            ) from exc
        raise
    finally:
        if lease is not None:
            lease.close()

    return _promotion_result(
        plan=plan,
        config_payload=config_payload,
        binding=binding,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("materialize", "promote"):
        command = commands.add_parser(name)
        command.add_argument("--plan", type=Path, required=True)
    commands.add_parser("source-bindings")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "source-bindings":
            result: Mapping[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "scope": "gate14-cache-materializer-sources",
                "sources": current_source_bindings(),
            }
        elif args.command == "materialize":
            result = materialize(plan_path=args.plan)
        else:
            result = promote(plan_path=args.plan)
    except (
        Gate14CacheMaterializationError,
        lifecycle.Gate14LifecycleError,
        ManifestError,
        OSError,
        ValueError,
    ) as exc:
        raise SystemExit(str(exc)) from exc
    print(
        json.dumps(
            result,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
