"""Prepare and verify credential-free exact-snapshot qualification image inputs.

The prepare command hashes every manifested artifact, rejects extra or linked files,
and emits a small build contract plus a shell-free Docker Buildx command. It never
calls Docker or a provider. The same module is copied into the image and run during
the build to re-verify the copied snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from drift.model_manifest import ManifestError, ModelManifest

SCHEMA_VERSION = 1
PLATFORM = "linux/amd64"
PYTHON_IMAGE = "python:3.12.13-slim-bookworm" "@sha256:6e13e65c55e33adf203d77ee371cf8bf5d81bd4902ef07565721f46bf44917af"
UV_IMAGE = "ghcr.io/astral-sh/uv:0.11.21" "@sha256:6f1fa8fc4040ad7197d7e652057219871e5f6640abfe2b790f1419fdb2319e6b"
MAX_MANIFEST_BYTES = 1_000_000
MAX_CONTRACT_BYTES = 256_000
MAX_SNAPSHOT_FILES = 256
MAX_SNAPSHOT_BYTES = 20_000_000_000
MAX_RELATIVE_PATH_CHARS = 512
MAX_IMAGE_NAME_CHARS = 255
MAX_SOURCE_FILES = 1_000
MAX_SOURCE_BYTES = 100_000_000
_SOURCE_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_TAG_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9.-]{0,62})(?::[0-9]{1,5})?/)?"
    r"(?:[a-z0-9]+(?:[._-][a-z0-9]+)*/)*"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$"
)
_CANDIDATES = {
    "qwen3.5-2b": "manifests/candidates/qwen3.5-2b-bfloat16-eager.json",
    "gemma-4-e2b": "manifests/candidates/gemma-4-e2b-it-bfloat16-eager.json",
}
_CANDIDATE_MANIFEST_FILES = set(_CANDIDATES.values())
_SOURCE_ARCHIVE_PATHS = (
    "Dockerfile.qualification",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "src",
    "scripts/fly_qualification_node.py",
    "scripts/qualification_image_contract.py",
    *_CANDIDATE_MANIFEST_FILES,
)
_SOURCE_ROOT_FILES = {
    "Dockerfile.qualification",
    "pyproject.toml",
    "uv.lock",
    "README.md",
}
_SOURCE_SCRIPT_FILES = {
    "scripts/fly_qualification_node.py",
    "scripts/qualification_image_contract.py",
}
_CONTRACT_KEYS = {
    "schema_version",
    "scope",
    "candidate",
    "source_commit",
    "source_tree_digest",
    "source_files",
    "dockerfile_digest",
    "model_repository",
    "model_revision",
    "manifest_digest",
    "manifest_filename",
    "artifact_count",
    "declared_artifact_bytes",
    "artifact_paths",
    "platform",
    "python_image",
    "uv_image",
    "image_tag",
    "remote_manifest",
    "remote_cache_dir",
    "artifact_hashes_verified",
    "image_built",
    "image_published",
    "contract_digest",
}


class QualificationImageError(ValueError):
    """The proposed image input is incomplete, mutable, or unsafe."""


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _contract_digest(contract: Mapping[str, Any]) -> str:
    unsigned = {key: value for key, value in contract.items() if key != "contract_digest"}
    return "sha256:" + hashlib.sha256(_canonical_json(unsigned)).hexdigest()


def _load_bounded_json(path: Path, maximum_bytes: int, description: str) -> Mapping[str, Any]:
    try:
        if _is_link_or_junction(path) or not path.is_file() or path.stat().st_size > maximum_bytes:
            raise QualificationImageError(f"{description} is missing, linked, or oversized")
        value = json.loads(path.read_text(encoding="utf-8"))
    except QualificationImageError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise QualificationImageError(f"{description} is not readable strict JSON") from None
    if not isinstance(value, dict):
        raise QualificationImageError(f"{description} must be a JSON object")
    return value


def _require_source_commit(source_commit: str, repository_commit: str | None) -> None:
    if not isinstance(source_commit, str) or not _SOURCE_COMMIT_RE.fullmatch(source_commit):
        raise QualificationImageError("source commit must be an exact lowercase 40-character SHA-1")
    if repository_commit is None or source_commit != repository_commit:
        raise QualificationImageError("source commit does not match the checked-out repository")


def _require_image_tag(image_tag: str, source_commit: str | None = None) -> None:
    image_name = image_tag.rsplit(":", 1)[0] if isinstance(image_tag, str) else ""
    if (
        not isinstance(image_tag, str)
        or not _IMAGE_TAG_RE.fullmatch(image_tag)
        or len(image_name) > MAX_IMAGE_NAME_CHARS
        or "://" in image_tag
        or "@" in image_tag
        or any(character in image_tag for character in "\x00\r\n")
    ):
        raise QualificationImageError("image tag must be a bounded credential-free tagged reference")
    if source_commit is not None and image_tag.rsplit(":", 1)[1] != f"source-{source_commit}":
        raise QualificationImageError("image tag must be bound to the exact source commit")


def _is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(os.path, "isjunction", lambda candidate: False)
    return path.is_symlink() or bool(is_junction(path))


def _source_inventory(files: Mapping[str, bytes]) -> dict[str, dict[str, Any]]:
    return {
        name: {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}
        for name, payload in sorted(files.items())
    }


def _source_tree_digest(inventory: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(inventory)).hexdigest()


def _run_git(repository_root: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repository_root,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise QualificationImageError("exact repository source could not be read with Git") from None
    if completed.returncode != 0:
        raise QualificationImageError("exact repository source could not be read with Git")
    return completed


def _infer_repository_commit(repository_root: Path) -> str:
    raw_commit = _run_git(repository_root, ["rev-parse", "HEAD"]).stdout
    try:
        commit = raw_commit.decode("ascii", errors="strict").strip()
    except UnicodeError:
        raise QualificationImageError("checked-out repository commit is invalid") from None
    if not _SOURCE_COMMIT_RE.fullmatch(commit):
        raise QualificationImageError("checked-out repository commit is invalid")
    return commit


def _archive_repository_source(
    repository_root: Path, source_commit: str, repository_commit: str | None
) -> dict[str, bytes]:
    """Return only build inputs committed at the requested source commit."""

    _require_source_commit(source_commit, repository_commit)
    if not repository_root.is_absolute() or _is_link_or_junction(repository_root) or not repository_root.is_dir():
        raise QualificationImageError("repository root must be an absolute unlinked Git directory")
    top_level = _run_git(repository_root, ["rev-parse", "--show-toplevel"]).stdout
    try:
        reported_root = Path(top_level.decode("utf-8", errors="strict").strip()).resolve()
    except (OSError, UnicodeError):
        raise QualificationImageError("Git repository root is invalid") from None
    if reported_root != repository_root.resolve():
        raise QualificationImageError("repository root must be the exact Git top-level directory")

    tree = _run_git(
        repository_root,
        ["ls-tree", "-r", "-z", source_commit, "--", *_SOURCE_ARCHIVE_PATHS],
    ).stdout
    files: dict[str, bytes] = {}
    total_bytes = 0
    for raw_entry in tree.split(b"\x00"):
        if not raw_entry:
            continue
        try:
            metadata, raw_name = raw_entry.split(b"\t", 1)
            mode, object_type, object_id = metadata.decode("ascii", errors="strict").split(" ")
            name = raw_name.decode("utf-8", errors="strict")
        except (UnicodeError, ValueError):
            raise QualificationImageError("committed source tree inventory is invalid") from None
        path = PurePosixPath(name)
        if (
            mode not in {"100644", "100755"}
            or object_type != "blob"
            or not re.fullmatch(r"[0-9a-f]{40,64}", object_id)
            or path.is_absolute()
            or ".." in path.parts
            or len(name) > MAX_RELATIVE_PATH_CHARS
            or any(character in name for character in "\x00\r\n")
            or not (
                name in _SOURCE_ROOT_FILES
                or name in _SOURCE_SCRIPT_FILES
                or name in _CANDIDATE_MANIFEST_FILES
                or name.startswith("src/")
            )
        ):
            raise QualificationImageError("committed source tree contains a linked, special, or unsafe file")
        payload = _run_git(repository_root, ["cat-file", "blob", object_id]).stdout
        total_bytes += len(payload)
        files[name] = payload
        if len(files) > MAX_SOURCE_FILES or total_bytes > MAX_SOURCE_BYTES:
            raise QualificationImageError("committed source tree exceeds its bounded inventory")

    if (
        not _SOURCE_ROOT_FILES.issubset(files)
        or not _SOURCE_SCRIPT_FILES.issubset(files)
        or not _CANDIDATE_MANIFEST_FILES.issubset(files)
    ):
        raise QualificationImageError("exact repository source is missing required build inputs")
    if not any(name.startswith("src/") for name in files):
        raise QualificationImageError("exact repository source contains no package source")
    return files


def _load_committed_source(
    repository_root: Path,
    source_commit: str,
    repository_commit: str | None,
    candidate: str,
    manifest_bytes: bytes,
) -> dict[str, bytes]:
    files = _archive_repository_source(repository_root, source_commit, repository_commit)
    committed_manifest = files.get(_CANDIDATES[candidate])
    if committed_manifest != manifest_bytes:
        raise QualificationImageError("candidate manifest does not match the exact source commit")
    for candidate_manifest in _CANDIDATE_MANIFEST_FILES:
        files.pop(candidate_manifest, None)
    return files


def _load_manifest(path: Path) -> tuple[ModelManifest, bytes]:
    try:
        if _is_link_or_junction(path) or not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
            raise QualificationImageError("candidate manifest is missing, linked, or oversized")
        raw = path.read_bytes()
        manifest = ModelManifest.load(path)
    except QualificationImageError:
        raise
    except (ManifestError, OSError):
        raise QualificationImageError("candidate manifest is invalid or unreadable") from None
    if len(manifest.artifacts) > MAX_SNAPSHOT_FILES:
        raise QualificationImageError("candidate manifest declares too many artifacts")
    declared_bytes = sum(artifact.size for artifact in manifest.artifacts)
    if declared_bytes > MAX_SNAPSHOT_BYTES:
        raise QualificationImageError("candidate snapshot exceeds the 20 GB image-input ceiling")
    return manifest, raw


def _walk_snapshot(root: Path) -> tuple[dict[str, int], set[str]]:
    if not root.is_absolute() or _is_link_or_junction(root) or not root.is_dir():
        raise QualificationImageError("snapshot root must be an absolute unlinked directory")
    files: dict[str, int] = {}
    directories: set[str] = set()
    stack: list[tuple[Path, PurePosixPath]] = [(root, PurePosixPath())]
    while stack:
        current, prefix = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda entry: entry.name)
        except OSError:
            raise QualificationImageError("snapshot tree is not readable") from None
        for entry in entries:
            relative = prefix / entry.name
            relative_name = relative.as_posix()
            if (
                len(relative_name) > MAX_RELATIVE_PATH_CHARS
                or "\x00" in relative_name
                or "\r" in relative_name
                or "\n" in relative_name
            ):
                raise QualificationImageError("snapshot contains an unsafe relative path")
            entry_path = Path(entry.path)
            if entry.is_symlink() or _is_link_or_junction(entry_path):
                raise QualificationImageError("snapshot must not contain symbolic links or junctions")
            try:
                if entry.is_dir(follow_symlinks=False):
                    directories.add(relative_name)
                    stack.append((entry_path, relative))
                elif entry.is_file(follow_symlinks=False):
                    files[relative_name] = entry.stat(follow_symlinks=False).st_size
                    if len(files) > MAX_SNAPSHOT_FILES:
                        raise QualificationImageError("snapshot contains too many files")
                else:
                    raise QualificationImageError("snapshot must contain only regular files and directories")
            except OSError:
                raise QualificationImageError("snapshot tree changed while it was inspected") from None
    return files, directories


def _expected_directories(paths: set[str]) -> set[str]:
    directories: set[str] = set()
    for path in paths:
        parent = PurePosixPath(path).parent
        while parent != PurePosixPath("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return directories


def verify_snapshot(manifest: ModelManifest, snapshot_root: Path) -> Mapping[str, Any]:
    files, directories = _walk_snapshot(snapshot_root)
    expected_sizes = {artifact.path: artifact.size for artifact in manifest.artifacts}
    if files != expected_sizes or directories != _expected_directories(set(expected_sizes)):
        raise QualificationImageError("snapshot contains missing, extra, or incorrectly sized entries")
    try:
        manifest.verify_artifacts(snapshot_root)
    except (ManifestError, OSError):
        raise QualificationImageError("snapshot artifact digest does not match the exact manifest") from None
    return {
        "artifact_count": len(manifest.artifacts),
        "declared_artifact_bytes": sum(artifact.size for artifact in manifest.artifacts),
        "artifact_paths": sorted(expected_sizes),
        "artifact_hashes_verified": True,
    }


def _validate_source_inventory(inventory: Any) -> Mapping[str, Mapping[str, Any]]:
    if not isinstance(inventory, dict) or not inventory or len(inventory) > MAX_SOURCE_FILES:
        raise QualificationImageError("source inventory is missing or oversized")
    total_bytes = 0
    for name, metadata in inventory.items():
        if (
            not isinstance(name, str)
            or len(name) > MAX_RELATIVE_PATH_CHARS
            or any(character in name for character in "\x00\r\n")
            or PurePosixPath(name).is_absolute()
            or ".." in PurePosixPath(name).parts
            or not (name in _SOURCE_ROOT_FILES or name in _SOURCE_SCRIPT_FILES or name.startswith("src/"))
        ):
            raise QualificationImageError("source inventory contains an unsafe path")
        if (
            not isinstance(metadata, dict)
            or set(metadata) != {"sha256", "size"}
            or not isinstance(metadata["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", metadata["sha256"])
            or not isinstance(metadata["size"], int)
            or isinstance(metadata["size"], bool)
            or metadata["size"] < 0
        ):
            raise QualificationImageError("source inventory contains invalid file metadata")
        total_bytes += metadata["size"]
    if total_bytes > MAX_SOURCE_BYTES:
        raise QualificationImageError("source inventory exceeds its bounded size")
    if not _SOURCE_ROOT_FILES.issubset(inventory) or not _SOURCE_SCRIPT_FILES.issubset(inventory):
        raise QualificationImageError("source inventory is missing required build inputs")
    if not any(name.startswith("src/") for name in inventory):
        raise QualificationImageError("source inventory contains no package source")
    return inventory


def _walk_source_directory(root: Path, prefix: str) -> tuple[dict[str, int], set[str]]:
    files: dict[str, int] = {}
    directories: set[str] = set()
    stack: list[tuple[Path, PurePosixPath]] = [(root, PurePosixPath(prefix))]
    while stack:
        current, current_prefix = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda entry: entry.name)
        except OSError:
            raise QualificationImageError("copied source tree is not readable") from None
        for entry in entries:
            relative = current_prefix / entry.name
            name = relative.as_posix()
            entry_path = Path(entry.path)
            if len(name) > MAX_RELATIVE_PATH_CHARS or any(character in name for character in "\x00\r\n"):
                raise QualificationImageError("copied source tree contains an unsafe path")
            if entry.is_symlink() or _is_link_or_junction(entry_path):
                raise QualificationImageError("copied source tree contains a link or junction")
            try:
                if entry.is_dir(follow_symlinks=False):
                    directories.add(name)
                    stack.append((entry_path, relative))
                elif entry.is_file(follow_symlinks=False):
                    files[name] = entry.stat(follow_symlinks=False).st_size
                    if len(files) > MAX_SOURCE_FILES:
                        raise QualificationImageError("copied source tree contains too many files")
                else:
                    raise QualificationImageError("copied source tree contains a special file")
            except OSError:
                raise QualificationImageError("copied source tree changed while it was inspected") from None
    return files, directories


def verify_source_tree(inventory: Any, source_root: Path) -> Mapping[str, Any]:
    inventory = _validate_source_inventory(inventory)
    if not source_root.is_absolute() or _is_link_or_junction(source_root) or not source_root.is_dir():
        raise QualificationImageError("source tree root must be an absolute unlinked directory")

    actual_files: dict[str, int] = {}
    actual_directories: set[str] = set()
    for directory in ("src", "scripts"):
        files, directories = _walk_source_directory(source_root / directory, directory)
        actual_files.update(files)
        actual_directories.update(directories)
        actual_directories.add(directory)

    expected_nested = {
        name: metadata["size"]
        for name, metadata in inventory.items()
        if name.startswith("src/") or name.startswith("scripts/")
    }
    if actual_files != expected_nested or actual_directories != _expected_directories(set(expected_nested)):
        raise QualificationImageError("copied source tree contains missing, extra, or incorrectly sized entries")

    for name in _SOURCE_ROOT_FILES:
        path = source_root.joinpath(*PurePosixPath(name).parts)
        metadata = inventory[name]
        try:
            if _is_link_or_junction(path) or not path.is_file() or path.stat().st_size != metadata["size"]:
                raise QualificationImageError("copied source root file is missing, linked, or incorrectly sized")
        except OSError:
            raise QualificationImageError("copied source root file is unreadable") from None

    for name, metadata in inventory.items():
        path = source_root.joinpath(*PurePosixPath(name).parts)
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            raise QualificationImageError("copied source file is unreadable") from None
        if digest != metadata["sha256"]:
            raise QualificationImageError("copied source file digest does not match the exact commit")

    source_digest = _source_tree_digest(inventory)
    return {
        "source_file_count": len(inventory),
        "source_tree_digest": source_digest,
        "dockerfile_digest": "sha256:" + inventory["Dockerfile.qualification"]["sha256"],
        "source_hashes_verified": True,
    }


def _validate_contract(
    contract: Mapping[str, Any],
    *,
    expected_source_commit: str,
    expected_candidate: str,
    manifest: ModelManifest,
) -> None:
    if set(contract) != _CONTRACT_KEYS:
        raise QualificationImageError("image contract fields are incomplete or unexpected")
    if contract["schema_version"] != SCHEMA_VERSION or contract["scope"] != "qualification-image-input":
        raise QualificationImageError("image contract schema or scope is invalid")
    if contract["candidate"] != expected_candidate or contract["source_commit"] != expected_source_commit:
        raise QualificationImageError("image contract candidate or source commit does not match the build")
    if contract["manifest_digest"] != manifest.digest_id:
        raise QualificationImageError("image contract manifest digest does not match")
    source_inventory = _validate_source_inventory(contract["source_files"])
    if contract["source_tree_digest"] != _source_tree_digest(source_inventory):
        raise QualificationImageError("image contract source tree digest does not match")
    expected_dockerfile_digest = "sha256:" + source_inventory["Dockerfile.qualification"]["sha256"]
    if contract["dockerfile_digest"] != expected_dockerfile_digest:
        raise QualificationImageError("image contract Dockerfile digest does not match")
    if (
        contract["model_repository"] != manifest.source.repository
        or contract["model_revision"] != manifest.source.revision
    ):
        raise QualificationImageError("image contract model source does not match the manifest")
    if contract["manifest_filename"] != "model-manifest.json":
        raise QualificationImageError("image contract manifest filename is invalid")
    if contract["platform"] != PLATFORM or contract["python_image"] != PYTHON_IMAGE or contract["uv_image"] != UV_IMAGE:
        raise QualificationImageError("image contract base image or platform is not pinned")
    if contract["remote_manifest"] != "/workspace/qualification/model-manifest.json":
        raise QualificationImageError("image contract remote manifest path is invalid")
    if contract["remote_cache_dir"] != "/cache/model":
        raise QualificationImageError("image contract remote cache path is invalid")
    if contract["artifact_hashes_verified"] is not True:
        raise QualificationImageError("image contract does not attest artifact hash verification")
    if contract["image_built"] is not False or contract["image_published"] is not False:
        raise QualificationImageError("input contract cannot claim a built or published image")
    _require_image_tag(str(contract["image_tag"]), expected_source_commit)
    if contract["contract_digest"] != _contract_digest(contract):
        raise QualificationImageError("image contract digest does not match its contents")


def verify_contract(
    *,
    contract_path: Path,
    manifest_path: Path,
    snapshot_root: Path,
    source_tree_root: Path,
    source_commit: str,
    candidate: str,
    expected_manifest_digest: str,
    expected_declared_artifact_bytes: int,
    expected_source_tree_digest: str,
    expected_dockerfile_digest: str,
) -> Mapping[str, Any]:
    _require_source_commit(source_commit, source_commit)
    contract = _load_bounded_json(contract_path, MAX_CONTRACT_BYTES, "image contract")
    manifest, _ = _load_manifest(manifest_path)
    if (
        not isinstance(expected_manifest_digest, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_manifest_digest)
        or expected_manifest_digest != manifest.digest_id
    ):
        raise QualificationImageError("build manifest digest does not match the exact manifest")
    if (
        not isinstance(expected_declared_artifact_bytes, int)
        or isinstance(expected_declared_artifact_bytes, bool)
        or expected_declared_artifact_bytes < 0
    ):
        raise QualificationImageError("build declared artifact bytes must be a non-negative integer")
    _validate_contract(
        contract,
        expected_source_commit=source_commit,
        expected_candidate=candidate,
        manifest=manifest,
    )
    if (
        contract["manifest_digest"] != expected_manifest_digest
        or contract["declared_artifact_bytes"] != expected_declared_artifact_bytes
        or contract["source_tree_digest"] != expected_source_tree_digest
        or contract["dockerfile_digest"] != expected_dockerfile_digest
    ):
        raise QualificationImageError("image contract does not match the exact build arguments")
    source = verify_source_tree(contract["source_files"], source_tree_root)
    if (
        source["source_tree_digest"] != expected_source_tree_digest
        or source["dockerfile_digest"] != expected_dockerfile_digest
    ):
        raise QualificationImageError("copied source tree does not match the exact build arguments")
    snapshot = verify_snapshot(manifest, snapshot_root)
    if (
        contract["artifact_count"] != snapshot["artifact_count"]
        or contract["declared_artifact_bytes"] != snapshot["declared_artifact_bytes"]
        or contract["artifact_paths"] != snapshot["artifact_paths"]
    ):
        raise QualificationImageError("image contract artifact inventory does not match the copied snapshot")
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": "qualification-image-verification",
        "result": "passed",
        "candidate": candidate,
        "source_commit": source_commit,
        "manifest_digest": manifest.digest_id,
        **source,
        **snapshot,
        "qualification_evidence": False,
        "complete_release_qualification": False,
    }


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if len(payload.encode("utf-8")) > MAX_CONTRACT_BYTES:
        raise QualificationImageError("generated image contract exceeds its bounded size")
    path.write_text(payload, encoding="utf-8", newline="\n")


def prepare_contract(
    *,
    candidate: str,
    manifest_path: Path,
    snapshot_root: Path,
    source_commit: str,
    repository_commit: str,
    image_tag: str,
    output_dir: Path,
    repository_root: Path,
) -> Mapping[str, Any]:
    if candidate not in _CANDIDATES:
        raise QualificationImageError("candidate is not in the public-alpha image set")
    _require_source_commit(source_commit, repository_commit)
    _require_image_tag(image_tag, source_commit)
    manifest, manifest_bytes = _load_manifest(manifest_path)
    source_payloads = _load_committed_source(
        repository_root,
        source_commit,
        repository_commit,
        candidate,
        manifest_bytes,
    )
    source_inventory = _source_inventory(source_payloads)
    source_tree_digest = _source_tree_digest(source_inventory)
    dockerfile_digest = "sha256:" + source_inventory["Dockerfile.qualification"]["sha256"]
    snapshot = verify_snapshot(manifest, snapshot_root)

    output_dir = Path(os.path.abspath(os.fspath(output_dir.expanduser())))
    if output_dir.exists():
        raise QualificationImageError("image contract output directory must not already exist")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    metadata_path = output_dir.parent / f"{candidate}-image-metadata.json"
    source_context = output_dir / "source"
    contract: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "scope": "qualification-image-input",
        "candidate": candidate,
        "source_commit": source_commit,
        "source_tree_digest": source_tree_digest,
        "source_files": source_inventory,
        "dockerfile_digest": dockerfile_digest,
        "model_repository": manifest.source.repository,
        "model_revision": manifest.source.revision,
        "manifest_digest": manifest.digest_id,
        "manifest_filename": "model-manifest.json",
        **snapshot,
        "platform": PLATFORM,
        "python_image": PYTHON_IMAGE,
        "uv_image": UV_IMAGE,
        "image_tag": image_tag,
        "remote_manifest": "/workspace/qualification/model-manifest.json",
        "remote_cache_dir": "/cache/model",
        "image_built": False,
        "image_published": False,
    }
    contract["contract_digest"] = _contract_digest(contract)

    build_command = [
        "docker",
        "buildx",
        "build",
        "--platform",
        PLATFORM,
        "--file",
        os.fspath(source_context / "Dockerfile.qualification"),
        "--build-context",
        f"snapshot={snapshot_root.resolve()}",
        "--build-context",
        f"contract={output_dir}",
        "--build-arg",
        f"SOURCE_COMMIT={source_commit}",
        "--build-arg",
        f"CANDIDATE={candidate}",
        "--build-arg",
        f"MANIFEST_DIGEST={manifest.digest_id}",
        "--build-arg",
        f"DECLARED_ARTIFACT_BYTES={snapshot['declared_artifact_bytes']}",
        "--build-arg",
        f"SOURCE_TREE_DIGEST={source_tree_digest}",
        "--build-arg",
        f"DOCKERFILE_DIGEST={dockerfile_digest}",
        "--provenance=mode=max",
        "--sbom=true",
        "--push",
        "--tag",
        image_tag,
        "--metadata-file",
        os.fspath(metadata_path),
        os.fspath(source_context),
    ]
    plan = {
        "schema_version": SCHEMA_VERSION,
        "scope": "qualification-image-build-plan",
        "result": "passed",
        "candidate": candidate,
        "source_commit": source_commit,
        "source_tree_digest": source_tree_digest,
        "dockerfile_digest": dockerfile_digest,
        "manifest_digest": manifest.digest_id,
        "contract_digest": contract["contract_digest"],
        "image_tag": image_tag,
        "build_command": build_command,
        "metadata_output": os.fspath(metadata_path),
        "image_built": False,
        "image_published": False,
        "qualification_evidence": False,
        "complete_release_qualification": False,
    }

    temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent))
    try:
        temporary_source = temporary / "source"
        for name, payload in source_payloads.items():
            destination = temporary_source.joinpath(*PurePosixPath(name).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
        materialized_source = verify_source_tree(source_inventory, temporary_source)
        if (
            materialized_source["source_tree_digest"] != source_tree_digest
            or materialized_source["dockerfile_digest"] != dockerfile_digest
        ):
            raise QualificationImageError("materialized source context does not match the exact commit")
        (temporary / "model-manifest.json").write_bytes(manifest_bytes)
        _write_json(temporary / "image-contract.json", contract)
        _write_json(temporary / "build-plan.json", plan)
        os.replace(temporary, output_dir)
    except (OSError, UnicodeError):
        raise QualificationImageError("image contract directory could not be written atomically") from None
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)

    return {
        "schema_version": SCHEMA_VERSION,
        "scope": "qualification-image-preparation",
        "result": "passed",
        "candidate": candidate,
        "source_commit": source_commit,
        "source_tree_digest": source_tree_digest,
        "dockerfile_digest": dockerfile_digest,
        "manifest_digest": manifest.digest_id,
        "contract_digest": contract["contract_digest"],
        "declared_artifact_bytes": snapshot["declared_artifact_bytes"],
        "artifact_count": snapshot["artifact_count"],
        "image_tag": image_tag,
        "image_built": False,
        "image_published": False,
        "qualification_evidence": False,
        "complete_release_qualification": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare or verify an exact-snapshot qualification image contract",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--candidate", required=True, choices=tuple(_CANDIDATES))
    prepare.add_argument("--snapshot-root", type=Path, required=True)
    prepare.add_argument("--source-commit", required=True)
    prepare.add_argument("--image-tag", required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)
    prepare.add_argument("--repository-root", type=Path, default=Path(__file__).resolve().parents[1])

    verify = subparsers.add_parser("verify")
    verify.add_argument("--contract", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--snapshot-root", type=Path, required=True)
    verify.add_argument("--source-tree-root", type=Path, required=True)
    verify.add_argument("--source-commit", required=True)
    verify.add_argument("--candidate", required=True, choices=tuple(_CANDIDATES))
    verify.add_argument("--manifest-digest", required=True)
    verify.add_argument("--declared-artifact-bytes", type=int, required=True)
    verify.add_argument("--source-tree-digest", required=True)
    verify.add_argument("--dockerfile-digest", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            repository_root = Path(os.path.abspath(os.fspath(args.repository_root.expanduser())))
            snapshot_root = Path(os.path.abspath(os.fspath(args.snapshot_root.expanduser())))
            report = prepare_contract(
                candidate=args.candidate,
                manifest_path=repository_root / _CANDIDATES[args.candidate],
                snapshot_root=snapshot_root,
                source_commit=args.source_commit,
                repository_commit=_infer_repository_commit(repository_root),
                image_tag=args.image_tag,
                output_dir=args.output_dir,
                repository_root=repository_root,
            )
        else:
            report = verify_contract(
                contract_path=args.contract,
                manifest_path=args.manifest,
                snapshot_root=args.snapshot_root,
                source_tree_root=args.source_tree_root,
                source_commit=args.source_commit,
                candidate=args.candidate,
                expected_manifest_digest=args.manifest_digest,
                expected_declared_artifact_bytes=args.declared_artifact_bytes,
                expected_source_tree_digest=args.source_tree_digest,
                expected_dockerfile_digest=args.dockerfile_digest,
            )
    except QualificationImageError as exc:
        print(f"qualification image contract failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
