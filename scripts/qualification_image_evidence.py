"""Collect fail-closed publication evidence for one qualification image.

The collector resolves a source-bound tag to an immutable OCI index, verifies its
single linux/amd64 runtime plus BuildKit provenance and SPDX attestations, inventories
every compressed layer, pulls the exact runtime manifest, and records Docker's
uncompressed image size. Registry credentials and provider output are never emitted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

try:
    from scripts import qualification_image_contract as image_contract
except ModuleNotFoundError:  # Direct execution from the repository's scripts directory.
    import qualification_image_contract as image_contract  # type: ignore[no-redef]

SCHEMA_VERSION = 1
PLATFORM = "linux/amd64"
OCI_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
ATTESTATION_REFERENCE_TYPE = "attestation-manifest"
MAX_OCI_JSON_BYTES = 1_000_000
MAX_BUILD_METADATA_BYTES = 1_000_000
MAX_DOCKER_OUTPUT_BYTES = 1_000_000
MAX_LAYERS = 128
GHCR_MAX_LAYER_BYTES = 10_000_000_000
GIB = 1024**3
MIN_FLY_ROOTFS_GB = 8
FLY_ROOTFS_HEADROOM_GB = 2
LIMITS_REVIEWED_ON = "2026-08-26"
GHCR_LIMITS_SOURCE = (
    "https://docs.github.com/en/packages/working-with-a-github-packages-registry/"
    "working-with-the-container-registry#troubleshooting"
)
FLY_ROOTFS_SOURCE = "https://fly.io/docs/machines/flyctl/fly-machine-create/"
OCI_SOURCE = "https://github.com/flujo-app/CommunityAI"
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_LAYER_MEDIA_TYPES = {
    "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.oci.image.layer.v1.tar+zstd",
    "application/vnd.docker.image.rootfs.diff.tar.gzip",
}
_CANDIDATE_LIMITS = {
    "qwen3.5-2b": {
        "maximum_compressed_bytes": 8_000_000_000,
        "maximum_uncompressed_bytes": 16 * GIB,
        "maximum_rootfs_gb": 20,
    },
    "gemma-4-e2b": {
        "maximum_compressed_bytes": 16_000_000_000,
        "maximum_uncompressed_bytes": 24 * GIB,
        "maximum_rootfs_gb": 28,
    },
}
_EXPECTED_REPOSITORIES = {
    "qwen3.5-2b": "ghcr.io/flujo-app/communityai-qualification-qwen3.5-2b",
    "gemma-4-e2b": "ghcr.io/flujo-app/communityai-qualification-gemma-4-e2b",
}


class QualificationImageEvidenceError(ValueError):
    """The published image cannot satisfy the qualification-image gate."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes = b""


Runner = Callable[[Sequence[str], int, int], CommandResult]


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _require_digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise QualificationImageEvidenceError(f"{field} must be an exact sha256 digest")
    return value


def _require_positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise QualificationImageEvidenceError(f"{field} must be a positive integer")
    return value


def _strict_json(payload: bytes, field: str, maximum_bytes: int) -> Mapping[str, Any]:
    if not payload or len(payload) > maximum_bytes:
        raise QualificationImageEvidenceError(f"{field} is empty or exceeds its bounded size")
    try:
        value = json.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError):
        raise QualificationImageEvidenceError(f"{field} is not strict JSON") from None
    if not isinstance(value, dict):
        raise QualificationImageEvidenceError(f"{field} must be a JSON object")
    return value


def _run_command(command: Sequence[str], timeout: int, maximum_output: int) -> CommandResult:
    try:
        completed = subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise QualificationImageEvidenceError("Docker evidence command could not be executed") from None
    if len(completed.stdout) + len(completed.stderr) > maximum_output:
        raise QualificationImageEvidenceError("Docker evidence command exceeded its bounded output")
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _docker(
    runner: Runner,
    executable: str,
    arguments: Sequence[str],
    *,
    timeout: int,
    maximum_output: int = MAX_DOCKER_OUTPUT_BYTES,
) -> bytes:
    if not executable or len(executable) > 4096 or any(character in executable for character in "\x00\r\n"):
        raise QualificationImageEvidenceError("Docker executable is invalid")
    result = runner([executable, *arguments], timeout, maximum_output)
    if result.returncode != 0:
        raise QualificationImageEvidenceError("Docker evidence command exited nonzero")
    if len(result.stdout) + len(result.stderr) > maximum_output:
        raise QualificationImageEvidenceError("Docker evidence command exceeded its bounded output")
    return result.stdout


def _load_contract(path: Path) -> Mapping[str, Any]:
    try:
        contract = image_contract._load_bounded_json(
            path, image_contract.MAX_CONTRACT_BYTES, "qualification image contract"
        )
    except image_contract.QualificationImageError:
        raise QualificationImageEvidenceError("qualification image contract is not readable bounded JSON") from None
    if (
        set(contract) != image_contract._CONTRACT_KEYS
        or contract.get("schema_version") != image_contract.SCHEMA_VERSION
        or contract.get("scope") != "qualification-image-input"
        or contract.get("candidate") not in _CANDIDATE_LIMITS
        or contract.get("platform") != PLATFORM
        or contract.get("image_built") is not False
        or contract.get("image_published") is not False
        or contract.get("artifact_hashes_verified") is not True
        or contract.get("contract_digest") != image_contract._contract_digest(contract)
    ):
        raise QualificationImageEvidenceError("qualification image contract identity is invalid")
    candidate = str(contract["candidate"])
    source_commit = str(contract["source_commit"])
    image_tag = str(contract["image_tag"])
    try:
        image_contract._require_source_commit(source_commit, source_commit)
        image_contract._require_image_tag(image_tag, source_commit)
    except image_contract.QualificationImageError:
        raise QualificationImageEvidenceError("qualification image contract identity is invalid") from None
    if image_tag.rsplit(":", 1)[0] != _EXPECTED_REPOSITORIES[candidate]:
        raise QualificationImageEvidenceError("qualification image must use the reviewed GHCR repository")
    _require_digest(contract.get("manifest_digest"), "contract manifest digest")
    _require_digest(contract.get("source_tree_digest"), "contract source-tree digest")
    _require_digest(contract.get("dockerfile_digest"), "contract Dockerfile digest")
    _require_positive_integer(contract.get("declared_artifact_bytes"), "declared artifact bytes")
    return contract


def _load_build_metadata(path: Path) -> Mapping[str, Any]:
    try:
        metadata = image_contract._load_bounded_json(path, MAX_BUILD_METADATA_BYTES, "Buildx metadata")
    except image_contract.QualificationImageError:
        raise QualificationImageEvidenceError("Buildx metadata is not readable bounded JSON") from None
    digest = _require_digest(metadata.get("containerimage.digest"), "Buildx image digest")
    descriptor = metadata.get("containerimage.descriptor")
    if (
        not isinstance(descriptor, dict)
        or descriptor.get("digest") != digest
        or descriptor.get("mediaType") != OCI_INDEX_MEDIA_TYPE
        or isinstance(descriptor.get("size"), bool)
        or not isinstance(descriptor.get("size"), int)
        or descriptor["size"] <= 0
    ):
        raise QualificationImageEvidenceError("Buildx metadata does not bind the pushed OCI index")
    return metadata


def _require_descriptor(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise QualificationImageEvidenceError(f"{field} must be an OCI descriptor")
    _require_digest(value.get("digest"), f"{field} digest")
    _require_positive_integer(value.get("size"), f"{field} size")
    media_type = value.get("mediaType")
    if not isinstance(media_type, str) or not media_type:
        raise QualificationImageEvidenceError(f"{field} media type is invalid")
    return value


def _index_descriptors(index: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    manifests = index.get("manifests")
    if (
        index.get("schemaVersion") != 2
        or index.get("mediaType") != OCI_INDEX_MEDIA_TYPE
        or not isinstance(manifests, list)
        or len(manifests) != 2
    ):
        raise QualificationImageEvidenceError(
            "published index must contain one runtime and one bound attestation manifest"
        )

    runtime: Mapping[str, Any] | None = None
    attestation: Mapping[str, Any] | None = None
    for index_number, raw_descriptor in enumerate(manifests):
        descriptor = _require_descriptor(raw_descriptor, f"index manifest {index_number}")
        platform = descriptor.get("platform")
        if not isinstance(platform, dict):
            raise QualificationImageEvidenceError("index manifest platform is missing")
        if platform == {"architecture": "amd64", "os": "linux"}:
            if runtime is not None or descriptor.get("mediaType") != OCI_MANIFEST_MEDIA_TYPE:
                raise QualificationImageEvidenceError("published index repeats or changes the runtime platform")
            runtime = descriptor
        elif platform == {"architecture": "unknown", "os": "unknown"}:
            annotations = descriptor.get("annotations")
            if (
                attestation is not None
                or descriptor.get("mediaType") != OCI_MANIFEST_MEDIA_TYPE
                or not isinstance(annotations, dict)
                or annotations.get("vnd.docker.reference.type") != ATTESTATION_REFERENCE_TYPE
            ):
                raise QualificationImageEvidenceError("published index attestation descriptor is invalid")
            attestation = descriptor
        else:
            raise QualificationImageEvidenceError("published index contains an unexpected platform")

    if runtime is None or attestation is None:
        raise QualificationImageEvidenceError("published index is missing runtime or attestation content")
    annotations = attestation["annotations"]
    if annotations.get("vnd.docker.reference.digest") != runtime["digest"]:
        raise QualificationImageEvidenceError("published attestations are not bound to the runtime manifest")
    return runtime, attestation


def _layer_inventory(manifest: Mapping[str, Any], *, candidate: str) -> tuple[list[dict[str, Any]], int]:
    if manifest.get("schemaVersion") != 2 or manifest.get("mediaType") != OCI_MANIFEST_MEDIA_TYPE:
        raise QualificationImageEvidenceError("runtime manifest is not an OCI image manifest")
    config = _require_descriptor(manifest.get("config"), "runtime config")
    if config.get("mediaType") not in {
        "application/vnd.oci.image.config.v1+json",
        "application/vnd.docker.container.image.v1+json",
    }:
        raise QualificationImageEvidenceError("runtime config media type is invalid")

    layers = manifest.get("layers")
    if not isinstance(layers, list) or not layers or len(layers) > MAX_LAYERS:
        raise QualificationImageEvidenceError("runtime layer inventory is empty or unbounded")
    inventory: list[dict[str, Any]] = []
    compressed_total = 0
    for index, raw_layer in enumerate(layers):
        layer = _require_descriptor(raw_layer, f"runtime layer {index}")
        if layer["mediaType"] not in _LAYER_MEDIA_TYPES:
            raise QualificationImageEvidenceError(f"runtime layer {index} media type is unsupported")
        size = int(layer["size"])
        if size > GHCR_MAX_LAYER_BYTES:
            raise QualificationImageEvidenceError(f"runtime layer {index} exceeds the GHCR layer limit")
        compressed_total += size
        inventory.append(
            {
                "digest": layer["digest"],
                "media_type": layer["mediaType"],
                "compressed_size": size,
            }
        )

    if compressed_total > _CANDIDATE_LIMITS[candidate]["maximum_compressed_bytes"]:
        raise QualificationImageEvidenceError("runtime compressed size exceeds the candidate ceiling")
    return inventory, compressed_total


def _require_image_labels(image: Mapping[str, Any], contract: Mapping[str, Any]) -> None:
    config = image.get("config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    expected = {
        "org.opencontainers.image.source": OCI_SOURCE,
        "org.opencontainers.image.revision": contract["source_commit"],
        "communityai.qualification.candidate": contract["candidate"],
        "communityai.qualification.manifest": contract["manifest_digest"],
        "communityai.qualification.artifact-bytes": str(contract["declared_artifact_bytes"]),
        "communityai.qualification.source-tree": contract["source_tree_digest"],
        "communityai.qualification.dockerfile": contract["dockerfile_digest"],
    }
    if (
        image.get("architecture") != "amd64"
        or image.get("os") != "linux"
        or not isinstance(labels, dict)
        or any(labels.get(key) != value for key, value in expected.items())
    ):
        raise QualificationImageEvidenceError("runtime image config does not match the exact build contract")


def _local_image_size(
    local: Mapping[str, Any], *, layer_count: int, maximum_uncompressed: int, maximum_rootfs_gb: int
) -> tuple[int, int]:
    if local.get("Architecture") != "amd64" or local.get("Os") != "linux":
        raise QualificationImageEvidenceError("locally pulled image platform is not linux/amd64")
    rootfs = local.get("RootFS")
    rootfs_layers = rootfs.get("Layers") if isinstance(rootfs, dict) else None
    if (
        not isinstance(rootfs_layers, list)
        or len(rootfs_layers) != layer_count
        or any(not isinstance(item, str) or not _DIGEST_RE.fullmatch(item) for item in rootfs_layers)
    ):
        raise QualificationImageEvidenceError("local rootfs layer inventory does not match the runtime manifest")
    uncompressed = _require_positive_integer(local.get("Size"), "local uncompressed image size")
    if uncompressed > maximum_uncompressed:
        raise QualificationImageEvidenceError("runtime uncompressed size exceeds the candidate ceiling")
    rootfs_gb = max(MIN_FLY_ROOTFS_GB, math.ceil(uncompressed / GIB) + FLY_ROOTFS_HEADROOM_GB)
    if rootfs_gb > maximum_rootfs_gb:
        raise QualificationImageEvidenceError("runtime image cannot fit the bounded Fly rootfs plan")
    return uncompressed, rootfs_gb


def collect_evidence(
    *,
    contract_path: Path,
    build_metadata_path: Path,
    output_path: Path,
    docker_executable: str = "docker",
    runner: Runner = _run_command,
) -> Mapping[str, Any]:
    resolved_output = Path(os.path.abspath(os.fspath(output_path.expanduser())))
    if resolved_output.exists():
        raise QualificationImageEvidenceError("evidence output must not already exist")
    contract = _load_contract(contract_path)
    metadata = _load_build_metadata(build_metadata_path)
    candidate = str(contract["candidate"])
    limits = _CANDIDATE_LIMITS[candidate]
    image_tag = str(contract["image_tag"])
    repository = image_tag.rsplit(":", 1)[0]
    index_digest = str(metadata["containerimage.digest"])

    raw_index = _docker(
        runner,
        docker_executable,
        ["buildx", "imagetools", "inspect", image_tag, "--raw"],
        timeout=120,
        maximum_output=MAX_OCI_JSON_BYTES,
    )
    metadata_descriptor = metadata["containerimage.descriptor"]
    if _digest(raw_index) != index_digest or len(raw_index) != metadata_descriptor["size"]:
        raise QualificationImageEvidenceError("published tag does not resolve to the Buildx metadata digest")
    index = _strict_json(raw_index, "published OCI index", MAX_OCI_JSON_BYTES)
    runtime_descriptor, attestation_descriptor = _index_descriptors(index)
    runtime_digest = str(runtime_descriptor["digest"])
    immutable_index = f"{repository}@{index_digest}"
    immutable_runtime = f"{repository}@{runtime_digest}"

    raw_manifest = _docker(
        runner,
        docker_executable,
        ["buildx", "imagetools", "inspect", immutable_runtime, "--raw"],
        timeout=120,
        maximum_output=MAX_OCI_JSON_BYTES,
    )
    if _digest(raw_manifest) != runtime_digest or len(raw_manifest) != runtime_descriptor["size"]:
        raise QualificationImageEvidenceError("runtime manifest bytes do not match the index descriptor")
    manifest = _strict_json(raw_manifest, "runtime OCI manifest", MAX_OCI_JSON_BYTES)
    layers, compressed_total = _layer_inventory(manifest, candidate=candidate)

    provenance = _docker(
        runner,
        docker_executable,
        [
            "buildx",
            "imagetools",
            "inspect",
            immutable_index,
            "--format",
            "{{if .Provenance.SLSA}}slsa{{end}}",
        ],
        timeout=120,
        maximum_output=64,
    ).strip()
    sbom = _docker(
        runner,
        docker_executable,
        [
            "buildx",
            "imagetools",
            "inspect",
            immutable_index,
            "--format",
            "{{if .SBOM.SPDX}}spdx{{end}}",
        ],
        timeout=120,
        maximum_output=64,
    ).strip()
    if provenance != b"slsa" or sbom != b"spdx":
        raise QualificationImageEvidenceError("runtime is missing exactly one SLSA provenance or SPDX SBOM")

    raw_image = _docker(
        runner,
        docker_executable,
        [
            "buildx",
            "imagetools",
            "inspect",
            immutable_runtime,
            "--format",
            "{{json .Image}}",
        ],
        timeout=120,
        maximum_output=MAX_OCI_JSON_BYTES,
    )
    image = _strict_json(raw_image, "runtime image config", MAX_OCI_JSON_BYTES)
    _require_image_labels(image, contract)

    _docker(
        runner,
        docker_executable,
        ["pull", "--quiet", "--platform", PLATFORM, immutable_runtime],
        timeout=1800,
        maximum_output=MAX_DOCKER_OUTPUT_BYTES,
    )
    raw_local = _docker(
        runner,
        docker_executable,
        ["image", "inspect", immutable_runtime, "--format", "{{json .}}"],
        timeout=120,
        maximum_output=MAX_OCI_JSON_BYTES,
    )
    local = _strict_json(raw_local, "local image inspection", MAX_OCI_JSON_BYTES)
    uncompressed_total, rootfs_gb = _local_image_size(
        local,
        layer_count=len(layers),
        maximum_uncompressed=int(limits["maximum_uncompressed_bytes"]),
        maximum_rootfs_gb=int(limits["maximum_rootfs_gb"]),
    )

    report = {
        "schema_version": SCHEMA_VERSION,
        "scope": "qualification-image-publication-evidence",
        "result": "passed",
        "candidate": candidate,
        "source_commit": contract["source_commit"],
        "model_repository": contract["model_repository"],
        "model_revision": contract["model_revision"],
        "manifest_digest": contract["manifest_digest"],
        "contract_digest": contract["contract_digest"],
        "image_tag": image_tag,
        "image_reference": immutable_index,
        "runtime_image_reference": immutable_runtime,
        "index_digest": index_digest,
        "index_size": len(raw_index),
        "runtime_manifest_digest": runtime_digest,
        "runtime_manifest_size": len(raw_manifest),
        "attestation_manifest_digest": attestation_descriptor["digest"],
        "attestation_manifest_size": attestation_descriptor["size"],
        "platform": PLATFORM,
        "provenance": "slsa",
        "sbom": "spdx",
        "layers": layers,
        "compressed_layer_bytes": compressed_total,
        "uncompressed_image_bytes": uncompressed_total,
        "required_fly_rootfs_gb": rootfs_gb,
        "limits": {
            "reviewed_on": LIMITS_REVIEWED_ON,
            "ghcr_max_layer_bytes": GHCR_MAX_LAYER_BYTES,
            "maximum_compressed_bytes": limits["maximum_compressed_bytes"],
            "maximum_uncompressed_bytes": limits["maximum_uncompressed_bytes"],
            "maximum_fly_rootfs_gb": limits["maximum_rootfs_gb"],
            "ghcr_source": GHCR_LIMITS_SOURCE,
            "fly_rootfs_source": FLY_ROOTFS_SOURCE,
        },
        "artifact_hashes_verified": True,
        "image_built": True,
        "image_published": True,
        "qualification_evidence": True,
        "complete_release_qualification": False,
    }
    _atomic_write(resolved_output, report)
    return report


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path = Path(os.path.abspath(os.fspath(path.expanduser())))
    if path.exists():
        raise QualificationImageEvidenceError("evidence output must not already exist")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    if len(payload) > MAX_OCI_JSON_BYTES:
        raise QualificationImageEvidenceError("generated evidence exceeds its bounded size")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise QualificationImageEvidenceError("evidence could not be written atomically") from None
    finally:
        if temporary.exists():
            temporary.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect immutable qualification-image publication evidence",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--build-metadata", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--docker", default="docker")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = collect_evidence(
            contract_path=args.contract,
            build_metadata_path=args.build_metadata,
            output_path=args.output,
            docker_executable=args.docker,
        )
    except QualificationImageEvidenceError as exc:
        print(f"qualification image evidence failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
