"""Validate and bind the exact Linux packaged runtime for the Qwen3.8 route.

This module is controller-side only.  It consumes an already extracted production
desktop bundle and its release attestations, revalidates the complete install
archive and onedir inventory, and emits one canonical record for the later
privileged host-stage operation.  It never downloads model data or invokes a
provider.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from desktop import build_desktop
from drift.model_manifest import ManifestError, ModelManifest
from scripts import gateq38_route_controller as controller

SCHEMA_VERSION = controller.RUNTIME_PACKAGE_SCHEMA_VERSION
SCOPE = controller.RUNTIME_PACKAGE_SCOPE
SOURCE_CONTEXT_SCOPE = "qwen3.8-runtime-package-source-context"
MAX_RECORD_BYTES = 262_144
HASH_CHUNK_BYTES = 1_048_576
NODE_ROOT = controller.RUNTIME_PACKAGE_NODE_ROOT
NODE_EXECUTABLE = controller.RUNTIME_PACKAGE_NODE_EXECUTABLE
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_WEIGHT_ROLES = {"converted_weight", "quantized_weight", "weight"}
ProtectionVerifier = Callable[[Path, bool], None]
_SOURCE_CONTEXT_FIELDS = {
    "schema_version",
    "scope",
    "source_commit",
    "source_tree",
    "source_bindings",
}


class Q38StagePackageError(RuntimeError):
    """The packaged runtime or its source binding failed closed."""


def _regular_bytes(path: Path, maximum: int | None = None) -> bytes:
    try:
        metadata = path.lstat()
        reparse = bool(getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        if reparse or path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise Q38StagePackageError(f"required regular file is unsafe: {path.name}")
        if maximum is not None and not 1 <= metadata.st_size <= maximum:
            raise Q38StagePackageError(f"required file size is invalid: {path.name}")
        payload = path.read_bytes()
    except Q38StagePackageError:
        raise
    except OSError as exc:
        raise Q38StagePackageError(f"required file could not be read: {path.name}") from exc
    if len(payload) != metadata.st_size:
        raise Q38StagePackageError(f"required file changed while read: {path.name}")
    return payload


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


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
        raise Q38StagePackageError("runtime package record is not canonical JSON") from exc


def _package_digest(value: Mapping[str, Any]) -> str:
    try:
        return controller._runtime_package_digest(value)
    except controller.RouteControllerError as exc:
        raise Q38StagePackageError(str(exc)) from exc


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _stream_binding(path: Path, expected_size: int) -> tuple[str, int]:
    descriptor: int | None = None
    try:
        before = path.lstat()
        reparse = bool(getattr(before, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        if (
            reparse
            or path.is_symlink()
            or not stat.S_ISREG(before.st_mode)
            or type(expected_size) is not int
            or expected_size <= 0
            or before.st_size != expected_size
        ):
            raise Q38StagePackageError("release archive identity is invalid")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _file_identity(opened) != _file_identity(before):
            raise Q38StagePackageError("release archive identity changed while opened")

        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, HASH_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > expected_size:
                raise Q38StagePackageError("release archive size changed while hashed")
            digest.update(chunk)

        after = os.fstat(descriptor)
        final = path.lstat()
        final_reparse = bool(getattr(final, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        if (
            total != expected_size
            or final_reparse
            or path.is_symlink()
            or not stat.S_ISREG(final.st_mode)
            or _file_identity(after) != _file_identity(opened)
            or _file_identity(final) != _file_identity(opened)
        ):
            raise Q38StagePackageError("release archive identity changed while hashed")
        return "sha256:" + digest.hexdigest(), total
    except Q38StagePackageError:
        raise
    except OSError as exc:
        raise Q38StagePackageError("release archive could not be hashed safely") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _assert_verifier_sources(
    source_root: Path,
    source_bindings: Sequence[Mapping[str, Any]],
) -> None:
    bindings = {item.get("relative_path"): item for item in source_bindings if isinstance(item, Mapping)}
    imported = {
        controller.DESKTOP_RELEASE_VERIFIER_SOURCE_PATH: Path(build_desktop.__file__).resolve(),
        controller.STAGE_PACKAGE_SOURCE_PATH: Path(__file__).resolve(),
    }
    root = source_root.resolve()
    for relative, module_path in imported.items():
        expected_path = (root / Path(*relative.split("/"))).resolve()
        binding = bindings.get(relative)
        if module_path != expected_path or not isinstance(binding, Mapping):
            raise Q38StagePackageError("runtime package verifier sources are not plan-bound")
        payload = _regular_bytes(expected_path)
        if binding.get("byte_size") != len(payload) or binding.get("sha256") != _sha256(payload):
            raise Q38StagePackageError("runtime package verifier source binding changed")


def _protected_tree_snapshot(
    root: Path,
    protection_verifier: ProtectionVerifier,
) -> tuple[tuple[Any, ...], ...]:
    try:
        supplied = root.lstat()
        supplied_reparse = bool(
            getattr(supplied, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )
        if supplied_reparse or root.is_symlink() or not stat.S_ISDIR(supplied.st_mode):
            raise Q38StagePackageError("runtime package root is unsafe")
        root = root.resolve(strict=True)
        protection_verifier(root, True)
        entries: list[tuple[Any, ...]] = []
        for child in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
            metadata = child.lstat()
            relative = child.relative_to(root).as_posix()
            reparse = bool(
                getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            )
            if stat.S_ISDIR(metadata.st_mode) and not reparse and not child.is_symlink():
                protection_verifier(child, True)
                kind = "directory"
            elif stat.S_ISREG(metadata.st_mode) and not reparse and not child.is_symlink():
                protection_verifier(child, False)
                kind = "file"
            elif stat.S_ISLNK(metadata.st_mode):
                kind = "symlink"
            else:
                raise Q38StagePackageError("runtime package tree contains an unsafe entry")
            entries.append(
                (
                    relative,
                    kind,
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_mode,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                )
            )
        return tuple(entries)
    except Q38StagePackageError:
        raise
    except OSError as exc:
        raise Q38StagePackageError("runtime package tree could not be snapshotted") from exc


def _is_model_weight(path: str, manifested_weight_names: set[str]) -> bool:
    normalized = path.casefold()
    name = normalized.rsplit("/", 1)[-1]
    return (
        name in manifested_weight_names
        or name in {"model.safetensors", "pytorch_model.bin"}
        or name.endswith((".safetensors", ".gguf", ".ckpt", ".pt", ".pth", ".onnx"))
        or (name.startswith("pytorch_model-") and name.endswith(".bin"))
        or "/model-cache/" in normalized
        or "/model_cache/" in normalized
        or "/models--" in normalized
    )


def validate_record(
    value: Any,
    *,
    expected_source_commit: str | None = None,
    expected_source_tree: str | None = None,
    expected_manifest_digest: str | None = None,
    expected_source_bindings: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    try:
        record = dict(
            controller.validate_runtime_package_record(
                value,
                expected_source_commit=expected_source_commit,
                expected_manifest_digest=expected_manifest_digest,
                expected_source_bindings=expected_source_bindings,
            )
        )
    except controller.RouteControllerError as exc:
        raise Q38StagePackageError(str(exc)) from exc
    if expected_source_commit is not None and record["source_commit"] != expected_source_commit:
        raise Q38StagePackageError("runtime package source commit changed")
    if expected_source_tree is not None and record["source_tree"] != expected_source_tree:
        raise Q38StagePackageError("runtime package source tree changed")
    if expected_manifest_digest is not None and record["manifest_digest"] != expected_manifest_digest:
        raise Q38StagePackageError("runtime package manifest binding changed")
    return record


def validate_release_root(
    release_root: Path,
    manifest_path: Path,
    *,
    expected_source_commit: str,
    expected_source_tree: str,
    source_root: Path,
    source_bindings: Sequence[Mapping[str, Any]],
    protection_verifier: ProtectionVerifier,
) -> dict[str, Any]:
    _assert_verifier_sources(source_root, source_bindings)
    if _COMMIT_RE.fullmatch(expected_source_commit) is None or _COMMIT_RE.fullmatch(expected_source_tree) is None:
        raise Q38StagePackageError("expected source identity is invalid")

    protection_verifier(manifest_path, False)
    manifest_payload = _regular_bytes(manifest_path, controller.MAX_JSON_BYTES)
    manifest_sha256 = _sha256(manifest_payload)
    manifest_bytes = len(manifest_payload)
    try:
        manifest = ModelManifest.from_dict(json.loads(manifest_payload.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, ManifestError) as exc:
        raise Q38StagePackageError("Qwen3.8 manifest is invalid") from exc
    if manifest.digest_id != controller.EXPECTED_MANIFEST_DIGEST:
        raise Q38StagePackageError("Qwen3.8 manifest identity is invalid")
    manifested_weight_names = {
        artifact.path.rsplit("/", 1)[-1].casefold() for artifact in manifest.artifacts if artifact.role in _WEIGHT_ROLES
    }

    before = _protected_tree_snapshot(release_root, protection_verifier)
    provenance_path = release_root / build_desktop.PROVENANCE_NAME
    checksums_path = release_root / build_desktop.CHECKSUMS_NAME
    metrics_path = release_root / build_desktop.DESKTOP_METRICS_NAME
    provenance_payload = _regular_bytes(provenance_path, MAX_RECORD_BYTES)
    checksums_payload = _regular_bytes(checksums_path, MAX_RECORD_BYTES)
    metrics_payload = _regular_bytes(metrics_path, MAX_RECORD_BYTES)
    try:
        provenance = json.loads(provenance_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Q38StagePackageError("release provenance is invalid") from exc
    if not isinstance(provenance, dict):
        raise Q38StagePackageError("release provenance is invalid")

    try:
        summary = build_desktop._verify_release_attestations(
            release_root,
            expected_source_commit=expected_source_commit,
            expected_source_tree=expected_source_tree,
            require_metrics=True,
        )
    except RuntimeError as exc:
        raise Q38StagePackageError("production release attestations are invalid") from exc
    if (
        _regular_bytes(provenance_path, MAX_RECORD_BYTES) != provenance_payload
        or _regular_bytes(checksums_path, MAX_RECORD_BYTES) != checksums_payload
        or _regular_bytes(metrics_path, MAX_RECORD_BYTES) != metrics_payload
    ):
        raise Q38StagePackageError("release attestations changed while validated")

    build_platform = provenance.get("build_platform")
    archive = summary.get("install_archive")
    if (
        not isinstance(build_platform, str)
        or not build_platform.casefold().startswith("linux")
        or not isinstance(archive, dict)
        or archive.get("platform") != "Linux"
        or archive.get("format") != "tar.gz"
        or archive.get("path") != "communityai-desktop-linux.tar.gz"
        or provenance.get("source_commit") != expected_source_commit
        or provenance.get("source_tree") != expected_source_tree
    ):
        raise Q38StagePackageError("release is not the exact Linux production package")

    artifacts = provenance.get("artifacts")
    if not isinstance(artifacts, list):
        raise Q38StagePackageError("release artifact inventory is invalid")
    node_entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    executable: dict[str, Any] | None = None
    for raw in artifacts:
        if not isinstance(raw, dict):
            raise Q38StagePackageError("release artifact inventory is invalid")
        path_value = raw.get("path")
        if not isinstance(path_value, str):
            raise Q38StagePackageError("release artifact path is invalid")
        if path_value == NODE_EXECUTABLE or path_value.startswith(NODE_ROOT + "/"):
            folded = path_value.casefold()
            if folded in seen:
                raise Q38StagePackageError("node runtime inventory has a case collision")
            seen.add(folded)
            if _is_model_weight(path_value, manifested_weight_names):
                raise Q38StagePackageError("node runtime contains model weights")
            entry = dict(raw)
            if entry.get("kind") == "symlink":
                target = entry.get("link_target")
                if not isinstance(target, str) or not target.startswith(NODE_ROOT + "/"):
                    raise Q38StagePackageError("node runtime symlink escapes its runtime root")
            node_entries.append(entry)
            if path_value == NODE_EXECUTABLE:
                executable = entry
    if executable is None or executable.get("kind") != "file":
        raise Q38StagePackageError("packaged node executable is missing")
    mode = executable.get("mode")
    if type(mode) is not int or mode != 0o755:
        raise Q38StagePackageError("packaged node executable mode is not 0755")
    if any(type(entry.get("size_bytes")) is not int or entry["size_bytes"] <= 0 for entry in node_entries):
        raise Q38StagePackageError("node runtime entry size is invalid")

    archive_path = release_root / str(archive["path"])
    archive_digest, archive_bytes = _stream_binding(archive_path, archive.get("size_bytes"))
    expected_archive_digest = "sha256:" + str(archive["sha256"])
    if archive_digest != expected_archive_digest or archive_bytes != archive["size_bytes"]:
        raise Q38StagePackageError("release archive binding changed")
    if _protected_tree_snapshot(release_root, protection_verifier) != before:
        raise Q38StagePackageError("runtime package tree changed while validated")
    if (
        _regular_bytes(provenance_path, MAX_RECORD_BYTES) != provenance_payload
        or _regular_bytes(checksums_path, MAX_RECORD_BYTES) != checksums_payload
        or _regular_bytes(metrics_path, MAX_RECORD_BYTES) != metrics_payload
        or _regular_bytes(manifest_path, controller.MAX_JSON_BYTES) != manifest_payload
    ):
        raise Q38StagePackageError("runtime package inputs changed while validated")

    inventory = sorted(node_entries, key=lambda item: str(item["path"]))
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "scope": SCOPE,
        "platform": "linux",
        "source_commit": expected_source_commit,
        "source_tree": expected_source_tree,
        "source_bindings_digest": controller._source_bindings_digest(source_bindings),
        "release_archive_name": archive["path"],
        "release_archive_sha256": archive_digest,
        "release_archive_bytes": archive_bytes,
        "checksums_sha256": _sha256(checksums_payload),
        "checksums_bytes": len(checksums_payload),
        "provenance_sha256": _sha256(provenance_payload),
        "provenance_bytes": len(provenance_payload),
        "desktop_metrics_sha256": _sha256(metrics_payload),
        "desktop_metrics_bytes": len(metrics_payload),
        "manifest_digest": manifest.digest_id,
        "manifest_sha256": manifest_sha256,
        "manifest_bytes": manifest_bytes,
        "node_root": NODE_ROOT,
        "node_executable": NODE_EXECUTABLE,
        "node_executable_sha256": "sha256:" + str(executable["sha256"]),
        "node_executable_bytes": executable["size_bytes"],
        "node_runtime_entry_count": len(inventory),
        "node_runtime_bytes": sum(int(entry["size_bytes"]) for entry in inventory),
        "node_runtime_inventory_digest": _sha256(_canonical_bytes(inventory)),
        "runtime_package_digest": "",
    }
    record["runtime_package_digest"] = _package_digest(record)
    return validate_record(
        record,
        expected_source_commit=expected_source_commit,
        expected_source_tree=expected_source_tree,
        expected_manifest_digest=controller.EXPECTED_MANIFEST_DIGEST,
        expected_source_bindings=source_bindings,
    )


def _assert_output_isolation(
    output: Path,
    *,
    release_root: Path,
    manifest_path: Path,
    source_context_path: Path,
    source_root: Path,
) -> None:
    try:
        candidate = output.resolve(strict=False)
        protected_files = {
            manifest_path.resolve(strict=True),
            source_context_path.resolve(strict=True),
        }
        protected_roots = {
            release_root.resolve(strict=True),
            source_root.resolve(strict=True),
        }
    except (OSError, RuntimeError) as exc:
        raise Q38StagePackageError("runtime package output isolation could not be resolved") from exc
    if candidate in protected_files:
        raise Q38StagePackageError("runtime package output aliases a protected input")
    for root in protected_roots:
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        raise Q38StagePackageError("runtime package output is inside a protected input root")


def _atomic_record(path: Path, record: Mapping[str, Any]) -> None:
    payload = _canonical_bytes(validate_record(record))
    if len(payload) > MAX_RECORD_BYTES:
        raise Q38StagePackageError("runtime package record is too large")
    path.parent.mkdir(parents=True, exist_ok=True)
    parent = path.parent.lstat()
    reparse = bool(getattr(parent, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if reparse or path.parent.is_symlink() or not stat.S_ISDIR(parent.st_mode):
        raise Q38StagePackageError("runtime package output parent is unsafe")
    if path.exists() or path.is_symlink():
        target = path.lstat()
        target_reparse = bool(
            getattr(target, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )
        if target_reparse or path.is_symlink() or not stat.S_ISREG(target.st_mode):
            raise Q38StagePackageError("runtime package output target is unsafe")
    handle, raw_temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise Q38StagePackageError("runtime package record could not be committed") from exc
    finally:
        temporary.unlink(missing_ok=True)


def load_source_context(path: Path, source_root: Path) -> dict[str, Any]:
    payload = _regular_bytes(path, MAX_RECORD_BYTES)
    try:
        raw = controller._mapping(
            controller._strict_json(payload),
            _SOURCE_CONTEXT_FIELDS,
            "runtime package source context",
        )
        if (
            type(raw["schema_version"]) is not int
            or raw["schema_version"] != SCHEMA_VERSION
            or raw["scope"] != SOURCE_CONTEXT_SCOPE
            or not isinstance(raw["source_commit"], str)
            or _COMMIT_RE.fullmatch(raw["source_commit"]) is None
            or not isinstance(raw["source_tree"], str)
            or _COMMIT_RE.fullmatch(raw["source_tree"]) is None
        ):
            raise Q38StagePackageError("runtime package source context identity is invalid")
        source_bindings = controller._validate_source_bindings(raw["source_bindings"], source_root)
    except controller.RouteControllerError as exc:
        raise Q38StagePackageError(str(exc)) from exc
    if {item["relative_path"] for item in source_bindings} != controller.REQUIRED_SOURCE_PATHS:
        raise Q38StagePackageError("runtime package source context is incomplete")
    try:
        controller._assert_protected_path_from_bindings(
            path.parent,
            source_bindings,
            source_root,
            directory=True,
        )
        controller._assert_protected_path_from_bindings(
            path,
            source_bindings,
            source_root,
            directory=False,
        )
    except controller.RouteControllerError as exc:
        raise Q38StagePackageError(str(exc)) from exc
    if _regular_bytes(path, MAX_RECORD_BYTES) != payload:
        raise Q38StagePackageError("runtime package source context changed while validated")
    _assert_verifier_sources(source_root, source_bindings)
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": SOURCE_CONTEXT_SCOPE,
        "source_commit": raw["source_commit"],
        "source_tree": raw["source_tree"],
        "source_bindings": source_bindings,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bind an exact Qwen3.8 Linux packaged runtime")
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source-context", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _assert_output_isolation(
            args.output,
            release_root=args.release_root,
            manifest_path=args.manifest,
            source_context_path=args.source_context,
            source_root=args.source_root,
        )
        context = load_source_context(args.source_context, args.source_root)
        source_bindings = context["source_bindings"]
        record = validate_release_root(
            args.release_root,
            args.manifest,
            expected_source_commit=context["source_commit"],
            expected_source_tree=context["source_tree"],
            source_root=args.source_root,
            source_bindings=source_bindings,
            protection_verifier=lambda path, directory: controller._assert_protected_path_from_bindings(
                path,
                source_bindings,
                args.source_root,
                directory=directory,
            ),
        )
        _atomic_record(args.output, record)
    except (Q38StagePackageError, controller.RouteControllerError) as exc:
        raise SystemExit(f"Qwen3.8 runtime package validation failed: {exc}") from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
