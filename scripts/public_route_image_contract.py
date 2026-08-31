"""Prepare and verify immutable CUDA public-route image inputs.

The prepare command reads only files committed at the requested Git commit, validates
one exact legacy Gate 4 snapshot-carrier report, and emits an argv-only Buildx plan.
The build-time verifier rehashes the copied source, carrier report, manifest, and every
model artifact before the fresh CUDA runtime is installed. It never calls Docker,
a registry, provider authentication, or a cloud API.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from drift.model_manifest import ModelManifest

try:
    from scripts import qualification_image_contract as qualification
except ModuleNotFoundError:  # Direct execution from the repository scripts directory.
    import qualification_image_contract as qualification  # type: ignore[no-redef]

SCHEMA_VERSION = 1
PLATFORM = "linux/amd64"
DOCKERFILE = "Dockerfile.public-route-cuda"
TORCH_VERSION = "2.6.0+cu124"
CUDA_VERSION = "12.4"
NONROOT_UID = 65532
MAX_EVIDENCE_BYTES = 256_000
MAX_CONTRACT_BYTES = 256_000
MAX_SOURCE_FILES = 1_000
MAX_SOURCE_BYTES = 100_000_000
MAX_RELATIVE_PATH_CHARS = 512
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_CANDIDATE_MANIFESTS = {
    "qwen3.5-2b": "manifests/candidates/qwen3.5-2b-bfloat16-eager.json",
    "gemma-4-e2b": "manifests/candidates/gemma-4-e2b-it-bfloat16-eager.json",
}
_TARGET_REPOSITORIES = {
    "qwen3.5-2b": "ghcr.io/flujo-app/communityai-public-route-qwen3.5-2b",
    "gemma-4-e2b": "ghcr.io/flujo-app/communityai-public-route-gemma-4-e2b",
}
_CARRIERS: Mapping[str, Mapping[str, Any]] = {
    "qwen3.5-2b": {
        "evidence_path": "docs/evidence/gate4-20260826-b-qwen3.5-2b-publication-evidence.json",
        "evidence_digest": "sha256:47c767121a6a01fb69a7b802809701e4ebbd330cf4afb4a469014d543a7e0714",
        "repository": "ghcr.io/flujo-app/communityai-qualification-qwen3.5-2b",
        "index_digest": "sha256:129b96fd848b996a5e3a0c918c39c705d328e6e5010b3222a5c25ea10ab142ed",
        "runtime_digest": "sha256:5ad01b9ea9fea6adb5e2c60cc804685ba3bfa2a4f09d5ff48b56a762f3df1770",
        "source_commit": "7660e33e03326e5b868f81cb95282460ba649d5f",
        "model_repository": "Qwen/Qwen3.5-2B",
        "model_revision": "15852e8c16360a2fea060d615a32b45270f8a8fc",
        "manifest_digest": "sha256:3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33",
        "declared_artifact_bytes": 4_571_197_320,
    },
    "gemma-4-e2b": {
        "evidence_path": "docs/evidence/gate4-20260826-b-gemma-4-e2b-publication-evidence.json",
        "evidence_digest": "sha256:3d6f2eb8ddf50c4d42af9604017ccfb2cdf4bb99dc605967028692e7a8e5abbf",
        "repository": "ghcr.io/flujo-app/communityai-qualification-gemma-4-e2b",
        "index_digest": "sha256:5f04eb8e923023ff05f64d13fde5b879e8990725518d4e81210b03b4b6047c6f",
        "runtime_digest": "sha256:406f94b7a53bcef847fb4ea04eae0036310a4b5f92e87beade6ec919629530f8",
        "source_commit": "7660e33e03326e5b868f81cb95282460ba649d5f",
        "model_repository": "google/gemma-4-E2B-it",
        "model_revision": "3e22461f65e89153144f8adb70e3b8c2cc9845a7",
        "manifest_digest": "sha256:2f8debbe0fcdf5af8d4c56c982210fa50aa584314968ae2617e2ccc2de9eafdd",
        "declared_artifact_bytes": 10_278_818_149,
    },
}
_SOURCE_ROOT_FILES = {DOCKERFILE, "pyproject.toml", "uv.lock", "README.md"}
_SOURCE_SCRIPT_FILES = {
    "scripts/public_route_node.py",
    "scripts/public_route_image_contract.py",
    "scripts/qualification_image_contract.py",
}
_SOURCE_PATHS = (
    *_SOURCE_ROOT_FILES,
    *_SOURCE_SCRIPT_FILES,
    "src",
    *_CANDIDATE_MANIFESTS.values(),
)
_LEGACY_EVIDENCE_KEYS = {
    "artifact_hashes_verified",
    "attestation_manifest_digest",
    "attestation_manifest_size",
    "candidate",
    "complete_release_qualification",
    "compressed_layer_bytes",
    "contract_digest",
    "image_built",
    "image_published",
    "image_reference",
    "image_tag",
    "index_digest",
    "index_size",
    "layers",
    "limits",
    "manifest_digest",
    "model_repository",
    "model_revision",
    "platform",
    "provenance",
    "qualification_evidence",
    "required_fly_rootfs_gb",
    "result",
    "runtime_manifest_digest",
    "runtime_manifest_size",
    "sbom",
    "schema_version",
    "scope",
    "source_commit",
    "uncompressed_image_bytes",
}
_CONTRACT_KEYS = {
    "schema_version",
    "scope",
    "candidate",
    "source_commit",
    "source_tree_digest",
    "source_files",
    "dockerfile_digest",
    "uv_lock_digest",
    "model_repository",
    "model_revision",
    "manifest_digest",
    "manifest_filename",
    "artifact_count",
    "declared_artifact_bytes",
    "artifact_paths",
    "artifact_hashes_verified",
    "full_block_span",
    "device",
    "platform",
    "python_image",
    "uv_image",
    "torch_version",
    "cuda_version",
    "nonroot_uid",
    "health_state_path",
    "training_rpcs",
    "carrier_evidence_digest",
    "carrier_index_reference",
    "carrier_runtime_image",
    "carrier_source_commit",
    "carrier_contract_digest",
    "image_tag",
    "remote_manifest",
    "remote_cache_dir",
    "source_hashes_verified",
    "carrier_evidence_verified",
    "image_built",
    "image_published",
    "contract_digest",
}


class PublicRouteImageError(ValueError):
    """The proposed CUDA route image input is incomplete, mutable, or unsafe."""


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _contract_digest(contract: Mapping[str, Any]) -> str:
    unsigned = {key: value for key, value in contract.items() if key != "contract_digest"}
    return _digest(_canonical_json(unsigned))


def _require_digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise PublicRouteImageError(f"{field} must be an exact sha256 digest")
    return value


def _load_bounded_file(path: Path, maximum_bytes: int, field: str) -> bytes:
    try:
        if (
            qualification._is_link_or_junction(path)
            or not path.is_file()
            or path.stat().st_size < 1
            or path.stat().st_size > maximum_bytes
        ):
            raise PublicRouteImageError(f"{field} is missing, linked, empty, or oversized")
        return path.read_bytes()
    except PublicRouteImageError:
        raise
    except OSError:
        raise PublicRouteImageError(f"{field} is unreadable") from None


def _strict_json(raw: bytes, field: str) -> Mapping[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError):
        raise PublicRouteImageError(f"{field} is not strict JSON") from None
    if not isinstance(value, dict):
        raise PublicRouteImageError(f"{field} must be a JSON object")
    return value


def _load_carrier_evidence(
    path: Path, candidate: str, *, expected_raw_digest: str | None = None
) -> tuple[Mapping[str, Any], bytes, Mapping[str, Any]]:
    if candidate not in _CARRIERS:
        raise PublicRouteImageError("candidate is not in the immutable public-route set")
    expected = _CARRIERS[candidate]
    raw = _load_bounded_file(path, MAX_EVIDENCE_BYTES, "snapshot-carrier evidence")
    raw_digest = _digest(raw)
    required_raw_digest = str(expected["evidence_digest"])
    if raw_digest != required_raw_digest or (expected_raw_digest is not None and raw_digest != expected_raw_digest):
        raise PublicRouteImageError("snapshot-carrier evidence bytes do not match the reviewed report")
    evidence = _strict_json(raw, "snapshot-carrier evidence")
    repository = str(expected["repository"])
    index_digest = str(expected["index_digest"])
    runtime_digest = str(expected["runtime_digest"])
    layers = evidence.get("layers")
    if (
        set(evidence) != _LEGACY_EVIDENCE_KEYS
        or evidence.get("schema_version") != 1
        or evidence.get("scope") != "qualification-image-publication-evidence"
        or evidence.get("result") != "passed"
        or evidence.get("candidate") != candidate
        or evidence.get("source_commit") != expected["source_commit"]
        or evidence.get("model_repository") != expected["model_repository"]
        or evidence.get("model_revision") != expected["model_revision"]
        or evidence.get("manifest_digest") != expected["manifest_digest"]
        or evidence.get("image_reference") != f"{repository}@{index_digest}"
        or evidence.get("index_digest") != index_digest
        or evidence.get("runtime_manifest_digest") != runtime_digest
        or evidence.get("platform") != PLATFORM
        or evidence.get("provenance") != "slsa"
        or evidence.get("sbom") != "spdx"
        or evidence.get("artifact_hashes_verified") is not True
        or evidence.get("image_built") is not True
        or evidence.get("image_published") is not True
        or evidence.get("qualification_evidence") is not True
        or evidence.get("complete_release_qualification") is not False
        or not isinstance(layers, list)
        or not layers
    ):
        raise PublicRouteImageError("snapshot-carrier evidence identity is invalid")
    compressed = 0
    for layer in layers:
        if (
            not isinstance(layer, dict)
            or set(layer) != {"compressed_size", "digest", "media_type"}
            or isinstance(layer["compressed_size"], bool)
            or not isinstance(layer["compressed_size"], int)
            or layer["compressed_size"] <= 0
        ):
            raise PublicRouteImageError("snapshot-carrier layer inventory is invalid")
        _require_digest(layer["digest"], "snapshot-carrier layer digest")
        compressed += layer["compressed_size"]
    if compressed != evidence.get("compressed_layer_bytes"):
        raise PublicRouteImageError("snapshot-carrier layer total does not match its report")
    binding = {
        "evidence_digest": raw_digest,
        "index_reference": f"{repository}@{index_digest}",
        "runtime_image": f"{repository}@{runtime_digest}",
        "source_commit": expected["source_commit"],
        "contract_digest": evidence["contract_digest"],
    }
    return evidence, raw, binding


def _allowed_source_name(name: str) -> bool:
    return name in _SOURCE_ROOT_FILES or name in _SOURCE_SCRIPT_FILES or name.startswith("src/")


def _archive_repository_source(
    repository_root: Path,
    source_commit: str,
    repository_commit: str,
    candidate: str,
) -> tuple[dict[str, bytes], bytes]:
    qualification._require_source_commit(source_commit, repository_commit)
    if (
        not repository_root.is_absolute()
        or qualification._is_link_or_junction(repository_root)
        or not repository_root.is_dir()
    ):
        raise PublicRouteImageError("repository root must be an absolute unlinked Git directory")
    top_level = qualification._run_git(repository_root, ["rev-parse", "--show-toplevel"]).stdout
    try:
        reported_root = Path(top_level.decode("utf-8", errors="strict").strip()).resolve()
    except (OSError, UnicodeError):
        raise PublicRouteImageError("Git repository root is invalid") from None
    if reported_root != repository_root.resolve():
        raise PublicRouteImageError("repository root must be the exact Git top-level directory")

    tree = qualification._run_git(
        repository_root,
        ["ls-tree", "-r", "-z", source_commit, "--", *_SOURCE_PATHS],
    ).stdout
    payloads: dict[str, bytes] = {}
    manifest_bytes: bytes | None = None
    total_bytes = 0
    selected_manifest = _CANDIDATE_MANIFESTS[candidate]
    for raw_entry in tree.split(b"\x00"):
        if not raw_entry:
            continue
        try:
            metadata, raw_name = raw_entry.split(b"\t", 1)
            mode, object_type, object_id = metadata.decode("ascii", errors="strict").split(" ")
            name = raw_name.decode("utf-8", errors="strict")
        except (UnicodeError, ValueError):
            raise PublicRouteImageError("committed source inventory is invalid") from None
        if (
            mode not in {"100644", "100755"}
            or object_type != "blob"
            or not re.fullmatch(r"[0-9a-f]{40,64}", object_id)
            or len(name) > MAX_RELATIVE_PATH_CHARS
            or any(character in name for character in "\x00\r\n")
            or PurePosixPath(name).is_absolute()
            or ".." in PurePosixPath(name).parts
            or not (_allowed_source_name(name) or name in _CANDIDATE_MANIFESTS.values())
        ):
            raise PublicRouteImageError("committed source tree contains a linked, special, or unsafe file")
        payload = qualification._run_git(repository_root, ["cat-file", "blob", object_id]).stdout
        total_bytes += len(payload)
        if name == selected_manifest:
            manifest_bytes = payload
        elif name not in _CANDIDATE_MANIFESTS.values():
            payloads[name] = payload
        if len(payloads) > MAX_SOURCE_FILES or total_bytes > MAX_SOURCE_BYTES:
            raise PublicRouteImageError("committed public-route source exceeds its bounded inventory")

    if (
        manifest_bytes is None
        or not _SOURCE_ROOT_FILES.issubset(payloads)
        or not _SOURCE_SCRIPT_FILES.issubset(payloads)
        or not any(name.startswith("src/") for name in payloads)
    ):
        raise PublicRouteImageError("exact repository source is missing required public-route inputs")
    return payloads, manifest_bytes


def _source_inventory(payloads: Mapping[str, bytes]) -> dict[str, dict[str, Any]]:
    return {
        name: {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}
        for name, payload in sorted(payloads.items())
    }


def _source_tree_digest(inventory: Mapping[str, Any]) -> str:
    return _digest(_canonical_json(inventory))


def _validate_source_inventory(inventory: Any) -> Mapping[str, Mapping[str, Any]]:
    if not isinstance(inventory, dict) or not inventory or len(inventory) > MAX_SOURCE_FILES:
        raise PublicRouteImageError("source inventory is missing or oversized")
    total_bytes = 0
    for name, metadata in inventory.items():
        if (
            not isinstance(name, str)
            or not _allowed_source_name(name)
            or len(name) > MAX_RELATIVE_PATH_CHARS
            or any(character in name for character in "\x00\r\n")
            or PurePosixPath(name).is_absolute()
            or ".." in PurePosixPath(name).parts
            or not isinstance(metadata, dict)
            or set(metadata) != {"sha256", "size"}
            or not isinstance(metadata["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", metadata["sha256"])
            or isinstance(metadata["size"], bool)
            or not isinstance(metadata["size"], int)
            or metadata["size"] < 0
        ):
            raise PublicRouteImageError("source inventory contains an unsafe path or invalid metadata")
        total_bytes += metadata["size"]
    if (
        total_bytes > MAX_SOURCE_BYTES
        or not _SOURCE_ROOT_FILES.issubset(inventory)
        or not _SOURCE_SCRIPT_FILES.issubset(inventory)
        or not any(name.startswith("src/") for name in inventory)
    ):
        raise PublicRouteImageError("source inventory is incomplete or exceeds its bounded size")
    return inventory


def _walk_source(root: Path) -> dict[str, int]:
    if not root.is_absolute() or qualification._is_link_or_junction(root) or not root.is_dir():
        raise PublicRouteImageError("source root must be an absolute unlinked directory")
    files: dict[str, int] = {}
    stack: list[tuple[Path, PurePosixPath]] = [(root, PurePosixPath())]
    while stack:
        current, prefix = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda entry: entry.name)
        except OSError:
            raise PublicRouteImageError("copied source tree is unreadable") from None
        for entry in entries:
            relative = prefix / entry.name
            name = relative.as_posix()
            entry_path = Path(entry.path)
            if (
                len(name) > MAX_RELATIVE_PATH_CHARS
                or any(character in name for character in "\x00\r\n")
                or entry.is_symlink()
                or qualification._is_link_or_junction(entry_path)
            ):
                raise PublicRouteImageError("copied source tree contains an unsafe path or link")
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append((entry_path, relative))
                elif entry.is_file(follow_symlinks=False):
                    if not _allowed_source_name(name):
                        raise PublicRouteImageError("copied source tree contains an unexpected file")
                    files[name] = entry.stat(follow_symlinks=False).st_size
                else:
                    raise PublicRouteImageError("copied source tree contains a special file")
            except OSError:
                raise PublicRouteImageError("copied source tree changed while inspected") from None
            if len(files) > MAX_SOURCE_FILES:
                raise PublicRouteImageError("copied source tree contains too many files")
    return files


def verify_source_tree(inventory: Any, source_root: Path) -> Mapping[str, Any]:
    inventory = _validate_source_inventory(inventory)
    actual = _walk_source(source_root)
    expected_sizes = {name: metadata["size"] for name, metadata in inventory.items()}
    if actual != expected_sizes:
        raise PublicRouteImageError("copied source tree has missing, extra, or incorrectly sized files")
    for name, metadata in inventory.items():
        path = source_root.joinpath(*PurePosixPath(name).parts)
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            raise PublicRouteImageError("copied source file is unreadable") from None
        if digest != metadata["sha256"]:
            raise PublicRouteImageError("copied source file digest does not match the exact commit")
    return {
        "source_file_count": len(inventory),
        "source_tree_digest": _source_tree_digest(inventory),
        "dockerfile_digest": "sha256:" + inventory[DOCKERFILE]["sha256"],
        "uv_lock_digest": "sha256:" + inventory["uv.lock"]["sha256"],
        "source_hashes_verified": True,
    }


def _manifest_inventory(manifest: ModelManifest) -> Mapping[str, Any]:
    return {
        "artifact_count": len(manifest.artifacts),
        "declared_artifact_bytes": sum(artifact.size for artifact in manifest.artifacts),
        "artifact_paths": sorted(artifact.path for artifact in manifest.artifacts),
        "artifact_hashes_verified": True,
    }


def _validate_contract(
    contract: Mapping[str, Any],
    *,
    source_commit: str,
    candidate: str,
    manifest: ModelManifest,
    carrier: Mapping[str, Any],
) -> None:
    if (
        set(contract) != _CONTRACT_KEYS
        or contract.get("schema_version") != SCHEMA_VERSION
        or contract.get("scope") != "public-route-image-input"
        or contract.get("candidate") != candidate
        or contract.get("source_commit") != source_commit
        or contract.get("manifest_digest") != manifest.digest_id
        or contract.get("model_repository") != manifest.source.repository
        or contract.get("model_revision") != manifest.source.revision
        or contract.get("manifest_filename") != "model-manifest.json"
        or contract.get("platform") != PLATFORM
        or contract.get("python_image") != qualification.PYTHON_IMAGE
        or contract.get("uv_image") != qualification.UV_IMAGE
        or contract.get("torch_version") != TORCH_VERSION
        or contract.get("cuda_version") != CUDA_VERSION
        or contract.get("nonroot_uid") != NONROOT_UID
        or contract.get("health_state_path") != "/run/communityai/health.json"
        or contract.get("training_rpcs") != "disabled"
        or contract.get("device") != "cuda"
        or contract.get("full_block_span") != f"0:{manifest.model.num_blocks}"
        or contract.get("remote_manifest") != "/workspace/public-route/model-manifest.json"
        or contract.get("remote_cache_dir") != "/cache/model"
        or contract.get("source_hashes_verified") is not True
        or contract.get("carrier_evidence_verified") is not True
        or contract.get("artifact_hashes_verified") is not True
        or contract.get("image_built") is not False
        or contract.get("image_published") is not False
    ):
        raise PublicRouteImageError("public-route image contract identity is invalid")
    source_inventory = _validate_source_inventory(contract.get("source_files"))
    expected_dockerfile = "sha256:" + source_inventory[DOCKERFILE]["sha256"]
    expected_uv_lock = "sha256:" + source_inventory["uv.lock"]["sha256"]
    manifest_inventory = _manifest_inventory(manifest)
    if (
        contract.get("source_tree_digest") != _source_tree_digest(source_inventory)
        or contract.get("dockerfile_digest") != expected_dockerfile
        or contract.get("uv_lock_digest") != expected_uv_lock
        or contract.get("artifact_count") != manifest_inventory["artifact_count"]
        or contract.get("declared_artifact_bytes") != manifest_inventory["declared_artifact_bytes"]
        or contract.get("artifact_paths") != manifest_inventory["artifact_paths"]
        or contract.get("carrier_evidence_digest") != carrier["evidence_digest"]
        or contract.get("carrier_index_reference") != carrier["index_reference"]
        or contract.get("carrier_runtime_image") != carrier["runtime_image"]
        or contract.get("carrier_source_commit") != carrier["source_commit"]
        or contract.get("carrier_contract_digest") != carrier["contract_digest"]
        or contract.get("contract_digest") != _contract_digest(contract)
    ):
        raise PublicRouteImageError("public-route image contract digests or inventories do not match")
    image_tag = contract.get("image_tag")
    try:
        qualification._require_image_tag(str(image_tag), source_commit)
    except qualification.QualificationImageError:
        raise PublicRouteImageError("public-route image tag is invalid") from None
    if str(image_tag).rsplit(":", 1)[0] != _TARGET_REPOSITORIES[candidate]:
        raise PublicRouteImageError("public-route image tag does not use the reviewed CUDA repository")


def verify_contract(
    *,
    contract_path: Path,
    manifest_path: Path,
    carrier_evidence_path: Path,
    snapshot_root: Path,
    source_tree_root: Path,
    source_commit: str,
    candidate: str,
    expected_manifest_digest: str,
    expected_declared_artifact_bytes: int,
    expected_source_tree_digest: str,
    expected_dockerfile_digest: str,
    expected_uv_lock_digest: str,
    expected_carrier_runtime_image: str,
    expected_carrier_evidence_digest: str,
    expected_carrier_index_digest: str,
    expected_carrier_runtime_digest: str,
) -> Mapping[str, Any]:
    qualification._require_source_commit(source_commit, source_commit)
    if candidate not in _CARRIERS:
        raise PublicRouteImageError("candidate is not in the immutable public-route set")
    raw_contract = _load_bounded_file(contract_path, MAX_CONTRACT_BYTES, "public-route image contract")
    contract = _strict_json(raw_contract, "public-route image contract")
    manifest, _manifest_bytes = qualification._load_manifest(manifest_path)
    _evidence, _raw_evidence, carrier = _load_carrier_evidence(
        carrier_evidence_path,
        candidate,
        expected_raw_digest=expected_carrier_evidence_digest,
    )
    _validate_contract(
        contract,
        source_commit=source_commit,
        candidate=candidate,
        manifest=manifest,
        carrier=carrier,
    )
    carrier_expected = _CARRIERS[candidate]
    if (
        expected_manifest_digest != manifest.digest_id
        or expected_declared_artifact_bytes != sum(artifact.size for artifact in manifest.artifacts)
        or expected_source_tree_digest != contract["source_tree_digest"]
        or expected_dockerfile_digest != contract["dockerfile_digest"]
        or expected_uv_lock_digest != contract["uv_lock_digest"]
        or expected_carrier_runtime_image != carrier["runtime_image"]
        or expected_carrier_index_digest != carrier_expected["index_digest"]
        or expected_carrier_runtime_digest != carrier_expected["runtime_digest"]
    ):
        raise PublicRouteImageError("public-route build arguments do not match the exact contract")
    source = verify_source_tree(contract["source_files"], source_tree_root)
    if (
        source["source_tree_digest"] != expected_source_tree_digest
        or source["dockerfile_digest"] != expected_dockerfile_digest
        or source["uv_lock_digest"] != expected_uv_lock_digest
    ):
        raise PublicRouteImageError("copied source tree does not match the exact build arguments")
    try:
        snapshot = qualification.verify_snapshot(manifest, snapshot_root)
    except qualification.QualificationImageError:
        raise PublicRouteImageError("snapshot carrier does not match the exact current manifest") from None
    if (
        snapshot["artifact_count"] != contract["artifact_count"]
        or snapshot["declared_artifact_bytes"] != contract["declared_artifact_bytes"]
        or snapshot["artifact_paths"] != contract["artifact_paths"]
    ):
        raise PublicRouteImageError("snapshot carrier inventory does not match the image contract")
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": "public-route-image-verification",
        "result": "passed",
        "candidate": candidate,
        "source_commit": source_commit,
        "manifest_digest": manifest.digest_id,
        "carrier_evidence_digest": carrier["evidence_digest"],
        "carrier_runtime_image": carrier["runtime_image"],
        **source,
        **snapshot,
        "image_built": False,
        "image_published": False,
        "complete_release_qualification": False,
    }


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if len(payload.encode("utf-8")) > MAX_CONTRACT_BYTES:
        raise PublicRouteImageError("generated public-route image document exceeds its bound")
    path.write_text(payload, encoding="utf-8", newline="\n")


def prepare_contract(
    *,
    candidate: str,
    source_commit: str,
    repository_commit: str,
    image_tag: str,
    output_dir: Path,
    repository_root: Path,
    carrier_evidence_path: Path,
) -> Mapping[str, Any]:
    if candidate not in _CARRIERS:
        raise PublicRouteImageError("candidate is not in the immutable public-route set")
    qualification._require_source_commit(source_commit, repository_commit)
    try:
        qualification._require_image_tag(image_tag, source_commit)
    except qualification.QualificationImageError:
        raise PublicRouteImageError("public-route image tag is invalid") from None
    if image_tag.rsplit(":", 1)[0] != _TARGET_REPOSITORIES[candidate]:
        raise PublicRouteImageError("public-route image tag does not use the reviewed CUDA repository")

    payloads, manifest_bytes = _archive_repository_source(repository_root, source_commit, repository_commit, candidate)
    manifest_path = repository_root / _CANDIDATE_MANIFESTS[candidate]
    manifest, working_manifest_bytes = qualification._load_manifest(manifest_path)
    if working_manifest_bytes != manifest_bytes:
        raise PublicRouteImageError("candidate manifest does not match the exact source commit")
    expected = _CARRIERS[candidate]
    if (
        manifest.digest_id != expected["manifest_digest"]
        or manifest.source.repository != expected["model_repository"]
        or manifest.source.revision != expected["model_revision"]
    ):
        raise PublicRouteImageError("candidate manifest does not match the reviewed snapshot carrier")
    _evidence, carrier_raw, carrier = _load_carrier_evidence(carrier_evidence_path, candidate)
    source_inventory = _source_inventory(payloads)
    source_tree_digest = _source_tree_digest(source_inventory)
    dockerfile_digest = "sha256:" + source_inventory[DOCKERFILE]["sha256"]
    uv_lock_digest = "sha256:" + source_inventory["uv.lock"]["sha256"]
    artifacts = _manifest_inventory(manifest)
    if artifacts["declared_artifact_bytes"] != expected["declared_artifact_bytes"]:
        raise PublicRouteImageError("manifest artifact total does not match the reviewed snapshot carrier")

    output_dir = Path(os.path.abspath(os.fspath(output_dir.expanduser())))
    if output_dir.exists():
        raise PublicRouteImageError("public-route image output directory must not already exist")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    source_context = output_dir / "source"
    metadata_path = output_dir.parent / f"{candidate}-public-route-image-metadata.json"
    contract: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "scope": "public-route-image-input",
        "candidate": candidate,
        "source_commit": source_commit,
        "source_tree_digest": source_tree_digest,
        "source_files": source_inventory,
        "dockerfile_digest": dockerfile_digest,
        "uv_lock_digest": uv_lock_digest,
        "model_repository": manifest.source.repository,
        "model_revision": manifest.source.revision,
        "manifest_digest": manifest.digest_id,
        "manifest_filename": "model-manifest.json",
        **artifacts,
        "full_block_span": f"0:{manifest.model.num_blocks}",
        "device": "cuda",
        "platform": PLATFORM,
        "python_image": qualification.PYTHON_IMAGE,
        "uv_image": qualification.UV_IMAGE,
        "torch_version": TORCH_VERSION,
        "cuda_version": CUDA_VERSION,
        "nonroot_uid": NONROOT_UID,
        "health_state_path": "/run/communityai/health.json",
        "training_rpcs": "disabled",
        "carrier_evidence_digest": carrier["evidence_digest"],
        "carrier_index_reference": carrier["index_reference"],
        "carrier_runtime_image": carrier["runtime_image"],
        "carrier_source_commit": carrier["source_commit"],
        "carrier_contract_digest": carrier["contract_digest"],
        "image_tag": image_tag,
        "remote_manifest": "/workspace/public-route/model-manifest.json",
        "remote_cache_dir": "/cache/model",
        "source_hashes_verified": True,
        "carrier_evidence_verified": True,
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
        os.fspath(source_context / DOCKERFILE),
        "--build-context",
        f"contract={output_dir}",
        "--build-arg",
        f"SOURCE_COMMIT={source_commit}",
        "--build-arg",
        f"CANDIDATE={candidate}",
        "--build-arg",
        f"MANIFEST_DIGEST={manifest.digest_id}",
        "--build-arg",
        f"DECLARED_ARTIFACT_BYTES={artifacts['declared_artifact_bytes']}",
        "--build-arg",
        f"SOURCE_TREE_DIGEST={source_tree_digest}",
        "--build-arg",
        f"DOCKERFILE_DIGEST={dockerfile_digest}",
        "--build-arg",
        f"UV_LOCK_DIGEST={uv_lock_digest}",
        "--build-arg",
        f"CARRIER_RUNTIME_IMAGE={carrier['runtime_image']}",
        "--build-arg",
        f"CARRIER_EVIDENCE_DIGEST={carrier['evidence_digest']}",
        "--build-arg",
        f"CARRIER_INDEX_DIGEST={expected['index_digest']}",
        "--build-arg",
        f"CARRIER_RUNTIME_DIGEST={expected['runtime_digest']}",
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
        "scope": "public-route-image-build-plan",
        "result": "passed",
        "candidate": candidate,
        "source_commit": source_commit,
        "source_tree_digest": source_tree_digest,
        "dockerfile_digest": dockerfile_digest,
        "uv_lock_digest": uv_lock_digest,
        "manifest_digest": manifest.digest_id,
        "carrier_evidence_digest": carrier["evidence_digest"],
        "carrier_runtime_image": carrier["runtime_image"],
        "contract_digest": contract["contract_digest"],
        "image_tag": image_tag,
        "build_command": build_command,
        "metadata_output": os.fspath(metadata_path),
        "image_built": False,
        "image_published": False,
        "complete_release_qualification": False,
    }

    temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent))
    try:
        temporary_source = temporary / "source"
        for name, payload in payloads.items():
            destination = temporary_source.joinpath(*PurePosixPath(name).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
        materialized = verify_source_tree(source_inventory, temporary_source)
        if (
            materialized["source_tree_digest"] != source_tree_digest
            or materialized["dockerfile_digest"] != dockerfile_digest
            or materialized["uv_lock_digest"] != uv_lock_digest
        ):
            raise PublicRouteImageError("materialized source does not match the exact commit")
        (temporary / "model-manifest.json").write_bytes(manifest_bytes)
        (temporary / "carrier-evidence.json").write_bytes(carrier_raw)
        _write_json(temporary / "image-contract.json", contract)
        _write_json(temporary / "build-plan.json", plan)
        os.replace(temporary, output_dir)
    except (OSError, UnicodeError):
        raise PublicRouteImageError("public-route image directory could not be written atomically") from None
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)

    return {
        "schema_version": SCHEMA_VERSION,
        "scope": "public-route-image-preparation",
        "result": "passed",
        "candidate": candidate,
        "source_commit": source_commit,
        "manifest_digest": manifest.digest_id,
        "carrier_evidence_digest": carrier["evidence_digest"],
        "carrier_runtime_image": carrier["runtime_image"],
        "source_tree_digest": source_tree_digest,
        "dockerfile_digest": dockerfile_digest,
        "uv_lock_digest": uv_lock_digest,
        "contract_digest": contract["contract_digest"],
        "image_tag": image_tag,
        "image_built": False,
        "image_published": False,
        "complete_release_qualification": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare or verify an immutable CUDA public-route image contract",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--candidate", required=True, choices=tuple(_CARRIERS))
    prepare.add_argument("--source-commit", required=True)
    prepare.add_argument("--image-tag", required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)
    prepare.add_argument("--carrier-evidence", type=Path)
    prepare.add_argument("--repository-root", type=Path, default=Path(__file__).resolve().parents[1])

    verify = subparsers.add_parser("verify")
    verify.add_argument("--contract", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--carrier-evidence", type=Path, required=True)
    verify.add_argument("--snapshot-root", type=Path, required=True)
    verify.add_argument("--source-tree-root", type=Path, required=True)
    verify.add_argument("--source-commit", required=True)
    verify.add_argument("--candidate", required=True, choices=tuple(_CARRIERS))
    verify.add_argument("--manifest-digest", required=True)
    verify.add_argument("--declared-artifact-bytes", type=int, required=True)
    verify.add_argument("--source-tree-digest", required=True)
    verify.add_argument("--dockerfile-digest", required=True)
    verify.add_argument("--uv-lock-digest", required=True)
    verify.add_argument("--carrier-runtime-image", required=True)
    verify.add_argument("--carrier-evidence-digest", required=True)
    verify.add_argument("--carrier-index-digest", required=True)
    verify.add_argument("--carrier-runtime-digest", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            repository_root = Path(os.path.abspath(os.fspath(args.repository_root.expanduser())))
            carrier_path = (
                args.carrier_evidence
                if args.carrier_evidence is not None
                else repository_root / str(_CARRIERS[args.candidate]["evidence_path"])
            )
            report = prepare_contract(
                candidate=args.candidate,
                source_commit=args.source_commit,
                repository_commit=qualification._infer_repository_commit(repository_root),
                image_tag=args.image_tag,
                output_dir=args.output_dir,
                repository_root=repository_root,
                carrier_evidence_path=carrier_path,
            )
        else:
            report = verify_contract(
                contract_path=args.contract,
                manifest_path=args.manifest,
                carrier_evidence_path=args.carrier_evidence,
                snapshot_root=args.snapshot_root,
                source_tree_root=args.source_tree_root,
                source_commit=args.source_commit,
                candidate=args.candidate,
                expected_manifest_digest=args.manifest_digest,
                expected_declared_artifact_bytes=args.declared_artifact_bytes,
                expected_source_tree_digest=args.source_tree_digest,
                expected_dockerfile_digest=args.dockerfile_digest,
                expected_uv_lock_digest=args.uv_lock_digest,
                expected_carrier_runtime_image=args.carrier_runtime_image,
                expected_carrier_evidence_digest=args.carrier_evidence_digest,
                expected_carrier_index_digest=args.carrier_index_digest,
                expected_carrier_runtime_digest=args.carrier_runtime_digest,
            )
    except (PublicRouteImageError, qualification.QualificationImageError) as exc:
        print(f"public-route image contract failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
