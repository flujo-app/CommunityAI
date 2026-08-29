"""Collect strict immutable publication evidence for one CUDA public route image.

The collector resolves a source-bound tag, verifies one linux/amd64 runtime and one
bound attestation manifest, validates the SLSA build arguments and SPDX package
inventory, checks the final non-root/offline entrypoint contract, and measures bounded
layer and local image sizes. Raw attestations, Docker output, credentials, paths, and
host details are never copied into the report.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from scripts import (
        public_route_image_contract as image_contract,
        qualification_image_evidence as qualification_evidence,
    )
except ModuleNotFoundError:  # Direct execution from the repository scripts directory.
    import public_route_image_contract as image_contract  # type: ignore[no-redef]
    import qualification_image_evidence as qualification_evidence  # type: ignore[no-redef]

SCHEMA_VERSION = 1
MAX_ATTESTATION_JSON_BYTES = 32_000_000
MAX_LAYERS = 256
MAX_EVIDENCE_BYTES = 1_000_000
GIB = 1024**3
_LIMITS = {
    "qwen3.5-2b": {
        "maximum_compressed_bytes": 24_000_000_000,
        "maximum_uncompressed_bytes": 40 * GIB,
    },
    "gemma-4-e2b": {
        "maximum_compressed_bytes": 32_000_000_000,
        "maximum_uncompressed_bytes": 56 * GIB,
    },
}
_REQUIRED_SPDX_PACKAGES = {
    "drift": None,
    "torch": "2.6.0",
    "nvidia-cuda-runtime-cu12": "12.4.127",
}
_FORBIDDEN_ENV_RE = re.compile(r"(?i)^(?:[^=]*(?:TOKEN|PASSWORD|SECRET|CREDENTIAL|PRIVATE_KEY|API_KEY)[^=]*)=")


class PublicRouteImageEvidenceError(ValueError):
    """The published image cannot satisfy the immutable CUDA route contract."""


CommandResult = qualification_evidence.CommandResult
Runner = qualification_evidence.Runner


def _translate(error: Exception) -> PublicRouteImageEvidenceError:
    return PublicRouteImageEvidenceError(str(error))


def _load_contract(path: Path) -> Mapping[str, Any]:
    try:
        raw = image_contract._load_bounded_file(path, image_contract.MAX_CONTRACT_BYTES, "public-route image contract")
        contract = image_contract._strict_json(raw, "public-route image contract")
    except image_contract.PublicRouteImageError as exc:
        raise _translate(exc) from None
    candidate = contract.get("candidate")
    image_tag = contract.get("image_tag")
    source_commit = contract.get("source_commit")
    if (
        set(contract) != image_contract._CONTRACT_KEYS
        or contract.get("schema_version") != image_contract.SCHEMA_VERSION
        or contract.get("scope") != "public-route-image-input"
        or candidate not in _LIMITS
        or contract.get("platform") != image_contract.PLATFORM
        or contract.get("device") != "cuda"
        or contract.get("torch_version") != image_contract.TORCH_VERSION
        or contract.get("cuda_version") != image_contract.CUDA_VERSION
        or contract.get("nonroot_uid") != image_contract.NONROOT_UID
        or contract.get("training_rpcs") != "disabled"
        or contract.get("source_hashes_verified") is not True
        or contract.get("carrier_evidence_verified") is not True
        or contract.get("artifact_hashes_verified") is not True
        or contract.get("image_built") is not False
        or contract.get("image_published") is not False
        or contract.get("contract_digest") != image_contract._contract_digest(contract)
    ):
        raise PublicRouteImageEvidenceError("public-route image contract identity is invalid")
    try:
        image_contract.qualification._require_source_commit(str(source_commit), str(source_commit))
        image_contract.qualification._require_image_tag(str(image_tag), str(source_commit))
        image_contract._require_digest(contract.get("manifest_digest"), "manifest digest")
        image_contract._require_digest(contract.get("source_tree_digest"), "source tree digest")
        image_contract._require_digest(contract.get("dockerfile_digest"), "Dockerfile digest")
        image_contract._require_digest(contract.get("uv_lock_digest"), "uv.lock digest")
        image_contract._require_digest(contract.get("carrier_evidence_digest"), "carrier evidence digest")
    except (
        image_contract.PublicRouteImageError,
        image_contract.qualification.QualificationImageError,
    ) as exc:
        raise _translate(exc) from None
    if str(image_tag).rsplit(":", 1)[0] != image_contract._TARGET_REPOSITORIES[str(candidate)]:
        raise PublicRouteImageEvidenceError("public-route image tag does not use the reviewed CUDA repository")
    carrier = image_contract._CARRIERS[str(candidate)]
    if (
        contract.get("carrier_index_reference") != f"{carrier['repository']}@{carrier['index_digest']}"
        or contract.get("carrier_runtime_image") != f"{carrier['repository']}@{carrier['runtime_digest']}"
        or contract.get("carrier_evidence_digest") != carrier["evidence_digest"]
    ):
        raise PublicRouteImageEvidenceError("public-route image contract changes the reviewed snapshot carrier")
    return contract


def _docker(
    runner: Runner,
    executable: str,
    arguments: Sequence[str],
    *,
    timeout: int,
    maximum_output: int,
) -> bytes:
    try:
        return qualification_evidence._docker(
            runner,
            executable,
            arguments,
            timeout=timeout,
            maximum_output=maximum_output,
        )
    except qualification_evidence.QualificationImageEvidenceError as exc:
        raise _translate(exc) from None


def _strict_json(payload: bytes, field: str, maximum_bytes: int) -> Mapping[str, Any]:
    try:
        return qualification_evidence._strict_json(payload, field, maximum_bytes)
    except qualification_evidence.QualificationImageEvidenceError as exc:
        raise _translate(exc) from None


def _load_build_metadata(path: Path) -> Mapping[str, Any]:
    try:
        return qualification_evidence._load_build_metadata(path)
    except qualification_evidence.QualificationImageEvidenceError as exc:
        raise _translate(exc) from None


def _index_descriptors(index: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    try:
        return qualification_evidence._index_descriptors(index)
    except qualification_evidence.QualificationImageEvidenceError as exc:
        raise _translate(exc) from None


def _require_descriptor(value: Any, field: str) -> Mapping[str, Any]:
    try:
        return qualification_evidence._require_descriptor(value, field)
    except qualification_evidence.QualificationImageEvidenceError as exc:
        raise _translate(exc) from None


def _layer_inventory(manifest: Mapping[str, Any], *, candidate: str) -> tuple[list[dict[str, Any]], int]:
    if (
        manifest.get("schemaVersion") != 2
        or manifest.get("mediaType") != qualification_evidence.OCI_MANIFEST_MEDIA_TYPE
    ):
        raise PublicRouteImageEvidenceError("public-route runtime manifest is not an OCI image manifest")
    config = _require_descriptor(manifest.get("config"), "runtime config")
    if config.get("mediaType") not in {
        "application/vnd.oci.image.config.v1+json",
        "application/vnd.docker.container.image.v1+json",
    }:
        raise PublicRouteImageEvidenceError("public-route runtime config media type is invalid")
    raw_layers = manifest.get("layers")
    if not isinstance(raw_layers, list) or not raw_layers or len(raw_layers) > MAX_LAYERS:
        raise PublicRouteImageEvidenceError("public-route runtime layer inventory is empty or unbounded")
    layers: list[dict[str, Any]] = []
    compressed_total = 0
    for index, raw_layer in enumerate(raw_layers):
        layer = _require_descriptor(raw_layer, f"runtime layer {index}")
        if layer["mediaType"] not in qualification_evidence._LAYER_MEDIA_TYPES:
            raise PublicRouteImageEvidenceError(f"runtime layer {index} media type is unsupported")
        size = int(layer["size"])
        if size > qualification_evidence.GHCR_MAX_LAYER_BYTES:
            raise PublicRouteImageEvidenceError(f"runtime layer {index} exceeds the GHCR layer limit")
        compressed_total += size
        layers.append(
            {
                "digest": layer["digest"],
                "media_type": layer["mediaType"],
                "compressed_size": size,
            }
        )
    if compressed_total > _LIMITS[candidate]["maximum_compressed_bytes"]:
        raise PublicRouteImageEvidenceError("public-route compressed size exceeds its reviewed ceiling")
    return layers, compressed_total


def _provenance_material(reference: str) -> tuple[str, str]:
    image_name, digest = reference.rsplit("@sha256:", 1)
    last_slash = image_name.rfind("/")
    last_colon = image_name.rfind(":")
    if last_colon > last_slash:
        repository = image_name[:last_colon]
        version = image_name[last_colon + 1 :]
    else:
        repository = image_name
        version = f"sha256:{digest}"
    uri = f"pkg:docker/{repository}@{version}?digest=sha256:{digest}&platform=linux%2Famd64"
    return uri, digest


def _require_provenance(provenance: Mapping[str, Any], contract: Mapping[str, Any]) -> None:
    build_definition = provenance.get("buildDefinition")
    external = build_definition.get("externalParameters") if isinstance(build_definition, dict) else None
    config_source = external.get("configSource") if isinstance(external, dict) else None
    request = external.get("request") if isinstance(external, dict) else None
    arguments = request.get("args") if isinstance(request, dict) else None
    expected_arguments = {
        "build-arg:SOURCE_COMMIT": contract["source_commit"],
        "build-arg:CANDIDATE": contract["candidate"],
        "build-arg:MANIFEST_DIGEST": contract["manifest_digest"],
        "build-arg:DECLARED_ARTIFACT_BYTES": str(contract["declared_artifact_bytes"]),
        "build-arg:SOURCE_TREE_DIGEST": contract["source_tree_digest"],
        "build-arg:DOCKERFILE_DIGEST": contract["dockerfile_digest"],
        "build-arg:UV_LOCK_DIGEST": contract["uv_lock_digest"],
        "build-arg:CARRIER_RUNTIME_IMAGE": contract["carrier_runtime_image"],
        "build-arg:CARRIER_EVIDENCE_DIGEST": contract["carrier_evidence_digest"],
        "build-arg:CARRIER_INDEX_DIGEST": contract["carrier_index_reference"].rsplit("@", 1)[1],
        "build-arg:CARRIER_RUNTIME_DIGEST": contract["carrier_runtime_image"].rsplit("@", 1)[1],
        "cmdline": "docker/dockerfile:1.7",
        "context:contract": "local:contract",
        "frontend.caps": "moby.buildkit.frontend.contexts+forward",
        "source": "docker/dockerfile:1.7",
    }
    dependencies = build_definition.get("resolvedDependencies") if isinstance(build_definition, dict) else None
    expected_materials = {
        (
            "pkg:docker/docker/buildkit-syft-scanner@stable-1",
            "ae4f3b554449e7e25548e7d8ccc029d17357348e30c6e3df01b92bc93654d6a9",
        ),
        (
            "pkg:docker/docker/dockerfile@1.7",
            "a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e",
        ),
        _provenance_material(contract["carrier_runtime_image"]),
        _provenance_material(contract["python_image"]),
        _provenance_material(contract["uv_image"]),
    }
    observed_materials: set[tuple[str, str]] = set()
    if isinstance(dependencies, list) and len(dependencies) == len(expected_materials):
        for dependency in dependencies:
            if not isinstance(dependency, dict) or set(dependency) != {"uri", "digest"}:
                break
            digest = dependency.get("digest")
            uri = dependency.get("uri")
            if not isinstance(uri, str) or not isinstance(digest, dict) or set(digest) != {"sha256"}:
                break
            sha256 = digest.get("sha256")
            if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
                break
            observed_materials.add((uri, sha256))
    if (
        not isinstance(build_definition, dict)
        or build_definition.get("buildType")
        != "https://github.com/moby/buildkit/blob/master/docs/attestations/slsa-definitions.md"
        or not isinstance(config_source, dict)
        or set(config_source) != {"path"}
        or config_source.get("path") != image_contract.DOCKERFILE
        or arguments != expected_arguments
        or observed_materials != expected_materials
    ):
        raise PublicRouteImageEvidenceError("SLSA provenance does not bind the exact public-route build")


def _require_spdx(sbom: Mapping[str, Any]) -> None:
    packages = sbom.get("packages")
    if sbom.get("spdxVersion") != "SPDX-2.3" or not isinstance(packages, list):
        raise PublicRouteImageEvidenceError("SPDX attestation is missing its bounded package document")
    observed: dict[str, set[str]] = {}
    for package in packages:
        if not isinstance(package, dict):
            continue
        name = package.get("name")
        version = package.get("versionInfo")
        if isinstance(name, str) and isinstance(version, str) and name in _REQUIRED_SPDX_PACKAGES:
            observed.setdefault(name, set()).add(version)
    for name, version in _REQUIRED_SPDX_PACKAGES.items():
        if name not in observed or (version is not None and version not in observed[name]):
            raise PublicRouteImageEvidenceError("SPDX attestation is missing a required CUDA runtime package")


def _require_image_config(image: Mapping[str, Any], contract: Mapping[str, Any]) -> None:
    config = image.get("config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    environment = config.get("Env") if isinstance(config, dict) else None
    expected_labels = {
        "org.opencontainers.image.source": qualification_evidence.OCI_SOURCE,
        "org.opencontainers.image.revision": contract["source_commit"],
        "org.opencontainers.image.base.name": contract["python_image"],
        "communityai.public-route.candidate": contract["candidate"],
        "communityai.public-route.device": "cuda",
        "communityai.public-route.manifest": contract["manifest_digest"],
        "communityai.public-route.artifact-bytes": str(contract["declared_artifact_bytes"]),
        "communityai.public-route.source-tree": contract["source_tree_digest"],
        "communityai.public-route.dockerfile": contract["dockerfile_digest"],
        "communityai.public-route.uv-lock": contract["uv_lock_digest"],
        "communityai.public-route.carrier-evidence": contract["carrier_evidence_digest"],
        "communityai.public-route.carrier-index": contract["carrier_index_reference"].rsplit("@", 1)[1],
        "communityai.public-route.carrier-runtime": contract["carrier_runtime_image"].rsplit("@", 1)[1],
        "communityai.public-route.torch": image_contract.TORCH_VERSION,
        "communityai.public-route.cuda": image_contract.CUDA_VERSION,
        "communityai.public-route.training": "disabled",
        "communityai.public-route.health": "/run/communityai/health.json",
    }
    required_environment = {
        "HF_HUB_OFFLINE=1",
        "TRANSFORMERS_OFFLINE=1",
        "COMMUNITYAI_PUBLIC_ROUTE_CANDIDATE=" + str(contract["candidate"]),
        "COMMUNITYAI_PUBLIC_ROUTE_MANIFEST=/workspace/public-route/model-manifest.json",
        "COMMUNITYAI_PUBLIC_ROUTE_CACHE_DIR=/cache/model",
    }
    if (
        image.get("architecture") != "amd64"
        or image.get("os") != "linux"
        or not isinstance(config, dict)
        or config.get("User") != "65532:65532"
        or config.get("Entrypoint") != ["python", "-u", "/workspace/scripts/public_route_node.py"]
        or config.get("WorkingDir") != "/workspace"
        or not isinstance(labels, dict)
        or any(labels.get(key) != value for key, value in expected_labels.items())
        or not isinstance(environment, list)
        or not required_environment.issubset(environment)
        or any(not isinstance(value, str) or _FORBIDDEN_ENV_RE.search(value) for value in environment)
    ):
        raise PublicRouteImageEvidenceError("runtime image config does not match the private non-root CUDA contract")


def _local_image_size(local: Mapping[str, Any], *, layer_count: int, candidate: str) -> int:
    if local.get("Architecture") != "amd64" or local.get("Os") != "linux":
        raise PublicRouteImageEvidenceError("locally pulled public-route image is not linux/amd64")
    rootfs = local.get("RootFS")
    rootfs_layers = rootfs.get("Layers") if isinstance(rootfs, dict) else None
    if (
        not isinstance(rootfs_layers, list)
        or len(rootfs_layers) != layer_count
        or any(
            not isinstance(item, str) or not qualification_evidence._DIGEST_RE.fullmatch(item) for item in rootfs_layers
        )
    ):
        raise PublicRouteImageEvidenceError("local rootfs layer inventory does not match the runtime manifest")
    size = local.get("Size")
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or size <= 0
        or size > _LIMITS[candidate]["maximum_uncompressed_bytes"]
    ):
        raise PublicRouteImageEvidenceError("public-route uncompressed size exceeds its reviewed ceiling")
    return size


def _collect_evidence(
    *,
    contract_path: Path,
    build_metadata_path: Path,
    output_path: Path,
    docker_executable: str,
    runner: Runner,
) -> Mapping[str, Any]:
    resolved_output = Path(os.path.abspath(os.fspath(output_path.expanduser())))
    if resolved_output.exists():
        raise PublicRouteImageEvidenceError("public-route evidence output must not already exist")
    contract = _load_contract(contract_path)
    metadata = _load_build_metadata(build_metadata_path)
    candidate = str(contract["candidate"])
    image_tag = str(contract["image_tag"])
    repository = image_tag.rsplit(":", 1)[0]
    index_digest = str(metadata["containerimage.digest"])

    raw_index = _docker(
        runner,
        docker_executable,
        ["buildx", "imagetools", "inspect", image_tag, "--raw"],
        timeout=120,
        maximum_output=qualification_evidence.MAX_OCI_JSON_BYTES,
    )
    metadata_descriptor = metadata["containerimage.descriptor"]
    if qualification_evidence._digest(raw_index) != index_digest or len(raw_index) != metadata_descriptor["size"]:
        raise PublicRouteImageEvidenceError("published tag does not match its Buildx metadata")
    index = _strict_json(raw_index, "published OCI index", qualification_evidence.MAX_OCI_JSON_BYTES)
    runtime_descriptor, attestation_descriptor = _index_descriptors(index)
    runtime_digest = str(runtime_descriptor["digest"])
    immutable_index = f"{repository}@{index_digest}"
    immutable_runtime = f"{repository}@{runtime_digest}"

    raw_manifest = _docker(
        runner,
        docker_executable,
        ["buildx", "imagetools", "inspect", immutable_runtime, "--raw"],
        timeout=120,
        maximum_output=qualification_evidence.MAX_OCI_JSON_BYTES,
    )
    if (
        qualification_evidence._digest(raw_manifest) != runtime_digest
        or len(raw_manifest) != runtime_descriptor["size"]
    ):
        raise PublicRouteImageEvidenceError("runtime manifest bytes do not match the OCI index")
    manifest = _strict_json(raw_manifest, "runtime OCI manifest", qualification_evidence.MAX_OCI_JSON_BYTES)
    layers, compressed_total = _layer_inventory(manifest, candidate=candidate)

    raw_provenance = _docker(
        runner,
        docker_executable,
        [
            "buildx",
            "imagetools",
            "inspect",
            immutable_index,
            "--format",
            "{{json .Provenance.SLSA}}",
        ],
        timeout=180,
        maximum_output=MAX_ATTESTATION_JSON_BYTES,
    )
    provenance = _strict_json(raw_provenance, "SLSA provenance", MAX_ATTESTATION_JSON_BYTES)
    _require_provenance(provenance, contract)

    raw_sbom = _docker(
        runner,
        docker_executable,
        [
            "buildx",
            "imagetools",
            "inspect",
            immutable_index,
            "--format",
            "{{json .SBOM.SPDX}}",
        ],
        timeout=180,
        maximum_output=MAX_ATTESTATION_JSON_BYTES,
    )
    sbom = _strict_json(raw_sbom, "SPDX SBOM", MAX_ATTESTATION_JSON_BYTES)
    _require_spdx(sbom)

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
        maximum_output=qualification_evidence.MAX_OCI_JSON_BYTES,
    )
    image = _strict_json(raw_image, "runtime image config", qualification_evidence.MAX_OCI_JSON_BYTES)
    _require_image_config(image, contract)

    _docker(
        runner,
        docker_executable,
        ["pull", "--quiet", "--platform", image_contract.PLATFORM, immutable_runtime],
        timeout=3600,
        maximum_output=qualification_evidence.MAX_DOCKER_OUTPUT_BYTES,
    )
    raw_local = _docker(
        runner,
        docker_executable,
        ["image", "inspect", immutable_runtime, "--format", "{{json .}}"],
        timeout=120,
        maximum_output=qualification_evidence.MAX_OCI_JSON_BYTES,
    )
    local = _strict_json(raw_local, "local image inspection", qualification_evidence.MAX_OCI_JSON_BYTES)
    uncompressed_total = _local_image_size(local, layer_count=len(layers), candidate=candidate)

    report = {
        "schema_version": SCHEMA_VERSION,
        "scope": "public-route-image-publication-evidence",
        "result": "passed",
        "candidate": candidate,
        "source_commit": contract["source_commit"],
        "source_tree_digest": contract["source_tree_digest"],
        "dockerfile_digest": contract["dockerfile_digest"],
        "uv_lock_digest": contract["uv_lock_digest"],
        "manifest_digest": contract["manifest_digest"],
        "model_repository": contract["model_repository"],
        "model_revision": contract["model_revision"],
        "contract_digest": contract["contract_digest"],
        "carrier_evidence_digest": contract["carrier_evidence_digest"],
        "carrier_index_reference": contract["carrier_index_reference"],
        "carrier_runtime_image": contract["carrier_runtime_image"],
        "image_tag": image_tag,
        "image_reference": immutable_index,
        "runtime_image_reference": immutable_runtime,
        "index_digest": index_digest,
        "index_size": len(raw_index),
        "runtime_manifest_digest": runtime_digest,
        "runtime_manifest_size": len(raw_manifest),
        "attestation_manifest_digest": attestation_descriptor["digest"],
        "attestation_manifest_size": attestation_descriptor["size"],
        "platform": image_contract.PLATFORM,
        "device": "cuda",
        "torch_version": image_contract.TORCH_VERSION,
        "cuda_version": image_contract.CUDA_VERSION,
        "nonroot_uid": image_contract.NONROOT_UID,
        "training_rpcs": "disabled",
        "health_state_path": "/run/communityai/health.json",
        "full_block_span": contract["full_block_span"],
        "provenance": "slsa-build-arguments-and-materials-verified",
        "sbom": "spdx-2.3-required-cuda-packages-verified",
        "layers": layers,
        "compressed_layer_bytes": compressed_total,
        "uncompressed_image_bytes": uncompressed_total,
        "limits": {
            "ghcr_max_layer_bytes": qualification_evidence.GHCR_MAX_LAYER_BYTES,
            **_LIMITS[candidate],
            "combined_route_disk_ceiling_bytes": 160 * GIB,
            "planned_boot_disk_bytes": 200 * GIB,
        },
        "source_hashes_verified": True,
        "carrier_evidence_verified": True,
        "artifact_hashes_verified": True,
        "image_built": True,
        "image_published": True,
        "complete_release_qualification": False,
    }
    try:
        qualification_evidence._atomic_write(resolved_output, report)
    except qualification_evidence.QualificationImageEvidenceError as exc:
        raise _translate(exc) from None
    return report


def collect_evidence(
    *,
    contract_path: Path,
    build_metadata_path: Path,
    output_path: Path,
    docker_executable: str = "docker",
    runner: Runner = qualification_evidence._run_command,
) -> Mapping[str, Any]:
    try:
        return _collect_evidence(
            contract_path=contract_path,
            build_metadata_path=build_metadata_path,
            output_path=output_path,
            docker_executable=docker_executable,
            runner=runner,
        )
    except qualification_evidence.QualificationImageEvidenceError as exc:
        raise _translate(exc) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect immutable CUDA public-route image publication evidence",
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
    except PublicRouteImageEvidenceError as exc:
        print(f"public-route image evidence failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "result": report["result"],
                "candidate": report["candidate"],
                "image_reference": report["image_reference"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
