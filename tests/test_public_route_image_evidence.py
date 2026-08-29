import json
from pathlib import Path

import pytest

from drift.model_manifest import ModelManifest
from scripts import (
    public_route_image_contract as image_contract,
    public_route_image_evidence as evidence,
    qualification_image_evidence as qualification_evidence,
)

SOURCE_COMMIT = "b" * 40
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPOSITORY_ROOT / "manifests" / "candidates" / "qwen3.5-2b-bfloat16-eager.json"


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class EvidenceFixture:
    def __init__(self, tmp_path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        candidate = "qwen3.5-2b"
        manifest = ModelManifest.load(MANIFEST_PATH)
        carrier = image_contract._CARRIERS[candidate]
        source_files = {
            image_contract.DOCKERFILE: {"sha256": "1" * 64, "size": 1},
            "pyproject.toml": {"sha256": "2" * 64, "size": 1},
            "uv.lock": {"sha256": "3" * 64, "size": 1},
            "README.md": {"sha256": "4" * 64, "size": 1},
            "scripts/public_route_node.py": {"sha256": "5" * 64, "size": 1},
            "scripts/public_route_image_contract.py": {
                "sha256": "6" * 64,
                "size": 1,
            },
            "scripts/qualification_image_contract.py": {
                "sha256": "7" * 64,
                "size": 1,
            },
            "src/drift/__init__.py": {"sha256": "8" * 64, "size": 1},
        }
        repository = image_contract._TARGET_REPOSITORIES[candidate]
        self.image_tag = f"{repository}:source-{SOURCE_COMMIT}"
        self.contract = {
            "schema_version": image_contract.SCHEMA_VERSION,
            "scope": "public-route-image-input",
            "candidate": candidate,
            "source_commit": SOURCE_COMMIT,
            "source_tree_digest": image_contract._source_tree_digest(source_files),
            "source_files": source_files,
            "dockerfile_digest": "sha256:" + source_files[image_contract.DOCKERFILE]["sha256"],
            "uv_lock_digest": "sha256:" + source_files["uv.lock"]["sha256"],
            "model_repository": manifest.source.repository,
            "model_revision": manifest.source.revision,
            "manifest_digest": manifest.digest_id,
            "manifest_filename": "model-manifest.json",
            "artifact_count": len(manifest.artifacts),
            "declared_artifact_bytes": sum(artifact.size for artifact in manifest.artifacts),
            "artifact_paths": sorted(artifact.path for artifact in manifest.artifacts),
            "artifact_hashes_verified": True,
            "full_block_span": f"0:{manifest.model.num_blocks}",
            "device": "cuda",
            "platform": image_contract.PLATFORM,
            "python_image": image_contract.qualification.PYTHON_IMAGE,
            "uv_image": image_contract.qualification.UV_IMAGE,
            "torch_version": image_contract.TORCH_VERSION,
            "cuda_version": image_contract.CUDA_VERSION,
            "nonroot_uid": image_contract.NONROOT_UID,
            "health_state_path": "/run/communityai/health.json",
            "training_rpcs": "disabled",
            "carrier_evidence_digest": carrier["evidence_digest"],
            "carrier_index_reference": (f"{carrier['repository']}@{carrier['index_digest']}"),
            "carrier_runtime_image": (f"{carrier['repository']}@{carrier['runtime_digest']}"),
            "carrier_source_commit": carrier["source_commit"],
            "carrier_contract_digest": "sha256:" + "8" * 64,
            "image_tag": self.image_tag,
            "remote_manifest": "/workspace/public-route/model-manifest.json",
            "remote_cache_dir": "/cache/model",
            "source_hashes_verified": True,
            "carrier_evidence_verified": True,
            "image_built": False,
            "image_published": False,
        }
        self.contract["contract_digest"] = image_contract._contract_digest(self.contract)
        self.contract_path = tmp_path / "contract.json"
        self.contract_path.write_text(json.dumps(self.contract), encoding="utf-8")

        self.layers = [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "digest": "sha256:" + "9" * 64,
                "size": 1_000_000,
            }
        ]
        self.manifest = {
            "schemaVersion": 2,
            "mediaType": qualification_evidence.OCI_MANIFEST_MEDIA_TYPE,
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": "sha256:" + "a" * 64,
                "size": 100,
            },
            "layers": self.layers,
        }
        self.raw_manifest = _json_bytes(self.manifest)
        self.runtime_digest = qualification_evidence._digest(self.raw_manifest)
        self.attestation_digest = "sha256:" + "c" * 64
        self.index = {
            "schemaVersion": 2,
            "mediaType": qualification_evidence.OCI_INDEX_MEDIA_TYPE,
            "manifests": [
                {
                    "mediaType": qualification_evidence.OCI_MANIFEST_MEDIA_TYPE,
                    "digest": self.runtime_digest,
                    "size": len(self.raw_manifest),
                    "platform": {"architecture": "amd64", "os": "linux"},
                },
                {
                    "mediaType": qualification_evidence.OCI_MANIFEST_MEDIA_TYPE,
                    "digest": self.attestation_digest,
                    "size": 1112,
                    "platform": {"architecture": "unknown", "os": "unknown"},
                    "annotations": {
                        "vnd.docker.reference.type": (qualification_evidence.ATTESTATION_REFERENCE_TYPE),
                        "vnd.docker.reference.digest": self.runtime_digest,
                    },
                },
            ],
        }
        self.raw_index = _json_bytes(self.index)
        self.index_digest = qualification_evidence._digest(self.raw_index)
        self.repository = self.image_tag.rsplit(":", 1)[0]
        self.immutable_index = f"{self.repository}@{self.index_digest}"
        self.immutable_runtime = f"{self.repository}@{self.runtime_digest}"
        metadata = {
            "containerimage.digest": self.index_digest,
            "containerimage.descriptor": {
                "mediaType": qualification_evidence.OCI_INDEX_MEDIA_TYPE,
                "digest": self.index_digest,
                "size": len(self.raw_index),
            },
        }
        self.metadata_path = tmp_path / "metadata.json"
        self.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        build_args = {
            "build-arg:SOURCE_COMMIT": self.contract["source_commit"],
            "build-arg:CANDIDATE": self.contract["candidate"],
            "build-arg:MANIFEST_DIGEST": self.contract["manifest_digest"],
            "build-arg:DECLARED_ARTIFACT_BYTES": str(self.contract["declared_artifact_bytes"]),
            "build-arg:SOURCE_TREE_DIGEST": self.contract["source_tree_digest"],
            "build-arg:DOCKERFILE_DIGEST": self.contract["dockerfile_digest"],
            "build-arg:UV_LOCK_DIGEST": self.contract["uv_lock_digest"],
            "build-arg:CARRIER_RUNTIME_IMAGE": self.contract["carrier_runtime_image"],
            "build-arg:CARRIER_EVIDENCE_DIGEST": self.contract["carrier_evidence_digest"],
            "build-arg:CARRIER_INDEX_DIGEST": carrier["index_digest"],
            "build-arg:CARRIER_RUNTIME_DIGEST": carrier["runtime_digest"],
            "cmdline": "docker/dockerfile:1.7",
            "context:contract": "local:contract",
            "frontend.caps": "moby.buildkit.frontend.contexts+forward",
            "source": "docker/dockerfile:1.7",
        }
        self.provenance = {
            "buildDefinition": {
                "buildType": ("https://github.com/moby/buildkit/blob/master/docs/" "attestations/slsa-definitions.md"),
                "externalParameters": {
                    "configSource": {"path": image_contract.DOCKERFILE},
                    "request": {"args": build_args},
                },
                "resolvedDependencies": [
                    {
                        "uri": "pkg:docker/docker/buildkit-syft-scanner@stable-1",
                        "digest": {"sha256": "ae4f3b554449e7e25548e7d8ccc029d17357348e30c6e3df01b92bc93654d6a9"},
                    },
                    {
                        "uri": "pkg:docker/docker/dockerfile@1.7",
                        "digest": {"sha256": "a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e"},
                    },
                    *[
                        {"uri": uri, "digest": {"sha256": digest}}
                        for uri, digest in (
                            evidence._provenance_material(self.contract["carrier_runtime_image"]),
                            evidence._provenance_material(self.contract["python_image"]),
                            evidence._provenance_material(self.contract["uv_image"]),
                        )
                    ],
                ],
            }
        }
        self.sbom = {
            "spdxVersion": "SPDX-2.3",
            "packages": [
                {"name": "drift", "versionInfo": "2.3.0.dev2"},
                {"name": "torch", "versionInfo": "2.6.0"},
                {
                    "name": "nvidia-cuda-runtime-cu12",
                    "versionInfo": "12.4.127",
                },
            ],
        }
        labels = {
            "org.opencontainers.image.source": qualification_evidence.OCI_SOURCE,
            "org.opencontainers.image.revision": SOURCE_COMMIT,
            "org.opencontainers.image.base.name": self.contract["python_image"],
            "communityai.public-route.candidate": candidate,
            "communityai.public-route.device": "cuda",
            "communityai.public-route.manifest": manifest.digest_id,
            "communityai.public-route.artifact-bytes": str(self.contract["declared_artifact_bytes"]),
            "communityai.public-route.source-tree": self.contract["source_tree_digest"],
            "communityai.public-route.dockerfile": self.contract["dockerfile_digest"],
            "communityai.public-route.uv-lock": self.contract["uv_lock_digest"],
            "communityai.public-route.carrier-evidence": carrier["evidence_digest"],
            "communityai.public-route.carrier-index": carrier["index_digest"],
            "communityai.public-route.carrier-runtime": carrier["runtime_digest"],
            "communityai.public-route.torch": image_contract.TORCH_VERSION,
            "communityai.public-route.cuda": image_contract.CUDA_VERSION,
            "communityai.public-route.training": "disabled",
            "communityai.public-route.health": "/run/communityai/health.json",
        }
        self.image = {
            "architecture": "amd64",
            "os": "linux",
            "config": {
                "Labels": labels,
                "User": "65532:65532",
                "Entrypoint": [
                    "python",
                    "-u",
                    "/workspace/scripts/public_route_node.py",
                ],
                "WorkingDir": "/workspace",
                "Env": [
                    "HF_HUB_OFFLINE=1",
                    "TRANSFORMERS_OFFLINE=1",
                    "COMMUNITYAI_PUBLIC_ROUTE_CANDIDATE=qwen3.5-2b",
                    "COMMUNITYAI_PUBLIC_ROUTE_MANIFEST=/workspace/public-route/model-manifest.json",
                    "COMMUNITYAI_PUBLIC_ROUTE_CACHE_DIR=/cache/model",
                ],
            },
        }
        self.local = {
            "Architecture": "amd64",
            "Os": "linux",
            "Size": 20 * evidence.GIB,
            "RootFS": {"Type": "layers", "Layers": ["sha256:" + "d" * 64]},
        }
        self.calls = []

    def __call__(self, command, timeout, maximum_output):
        self.calls.append((list(command), timeout, maximum_output))
        command = list(command)
        if command[1:4] == ["buildx", "imagetools", "inspect"]:
            reference = command[4]
            if command[-1] == "--raw":
                payload = self.raw_index if reference == self.image_tag else self.raw_manifest
            elif command[-1] == "{{json .Provenance.SLSA}}":
                payload = _json_bytes(self.provenance)
            elif command[-1] == "{{json .SBOM.SPDX}}":
                payload = _json_bytes(self.sbom)
            elif command[-1] == "{{json .Image}}":
                payload = _json_bytes(self.image)
            else:
                raise AssertionError(command)
            return evidence.CommandResult(0, payload)
        if command[1:3] == ["pull", "--quiet"]:
            return evidence.CommandResult(0, b"pulled")
        if command[1:3] == ["image", "inspect"]:
            return evidence.CommandResult(0, _json_bytes(self.local))
        raise AssertionError(command)


def _collect(tmp_path, fixture):
    output = tmp_path / "evidence.json"
    report = evidence.collect_evidence(
        contract_path=fixture.contract_path,
        build_metadata_path=fixture.metadata_path,
        output_path=output,
        runner=fixture,
    )
    return report, output


def test_collects_bounded_cuda_route_publication_evidence(tmp_path):
    fixture = EvidenceFixture(tmp_path)

    report, output = _collect(tmp_path, fixture)

    assert report["result"] == "passed"
    assert report["image_reference"] == fixture.immutable_index
    assert report["runtime_image_reference"] == fixture.immutable_runtime
    assert report["candidate"] == "qwen3.5-2b"
    assert report["device"] == "cuda"
    assert report["torch_version"] == "2.6.0+cu124"
    assert report["cuda_version"] == "12.4"
    assert report["nonroot_uid"] == 65532
    assert report["training_rpcs"] == "disabled"
    assert report["compressed_layer_bytes"] == 1_000_000
    assert report["uncompressed_image_bytes"] == 20 * evidence.GIB
    assert report["provenance"].startswith("slsa-")
    assert report["sbom"].startswith("spdx-")
    assert report["image_built"] is True
    assert report["image_published"] is True
    assert report["complete_release_qualification"] is False
    assert json.loads(output.read_text(encoding="utf-8")) == report
    assert not any(forbidden in json.dumps(report).lower() for forbidden in ("password", "credential", "private_key"))


def test_accepts_buildkit_digest_only_purl_without_version(tmp_path):
    fixture = EvidenceFixture(tmp_path)
    canonical, digest = evidence._provenance_material(fixture.contract["carrier_runtime_image"])
    alias = canonical.replace(f"@sha256:{digest}?digest=", "?digest=", 1)
    dependency = next(
        item for item in fixture.provenance["buildDefinition"]["resolvedDependencies"] if item["uri"] == canonical
    )
    dependency["uri"] = alias

    report, _output = _collect(tmp_path, fixture)

    assert report["provenance"] == "slsa-build-arguments-and-materials-verified"


def test_rejects_digest_only_purl_for_different_repository(tmp_path):
    fixture = EvidenceFixture(tmp_path)
    canonical, digest = evidence._provenance_material(fixture.contract["carrier_runtime_image"])
    alias = canonical.replace(f"@sha256:{digest}?digest=", "?digest=", 1)
    dependency = next(
        item for item in fixture.provenance["buildDefinition"]["resolvedDependencies"] if item["uri"] == canonical
    )
    dependency["uri"] = alias.replace("communityai-qualification-qwen3.5-2b", "unreviewed-carrier")

    with pytest.raises(evidence.PublicRouteImageEvidenceError, match="exact public-route build"):
        _collect(tmp_path, fixture)


def test_rejects_provenance_build_argument_mutation(tmp_path):
    fixture = EvidenceFixture(tmp_path)
    fixture.provenance["buildDefinition"]["externalParameters"]["request"]["args"]["build-arg:CANDIDATE"] = "other"

    with pytest.raises(evidence.PublicRouteImageEvidenceError, match="exact public-route build"):
        _collect(tmp_path, fixture)


def test_rejects_provenance_without_structured_immutable_materials(tmp_path):
    fixture = EvidenceFixture(tmp_path)
    fixture.provenance["buildDefinition"]["resolvedDependencies"] = []
    fixture.provenance["untrustedNote"] = [
        fixture.contract["carrier_runtime_image"],
        fixture.contract["python_image"],
        fixture.contract["uv_image"],
    ]

    with pytest.raises(evidence.PublicRouteImageEvidenceError, match="exact public-route build"):
        _collect(tmp_path, fixture)


def test_rejects_extra_secret_bearing_provenance_argument(tmp_path):
    fixture = EvidenceFixture(tmp_path)
    fixture.provenance["buildDefinition"]["externalParameters"]["request"]["args"]["build-arg:SECRET"] = "private-value"

    with pytest.raises(evidence.PublicRouteImageEvidenceError, match="exact public-route build"):
        _collect(tmp_path, fixture)


def test_rejects_spdx_without_cuda_runtime_package(tmp_path):
    fixture = EvidenceFixture(tmp_path)
    fixture.sbom["packages"] = [
        package for package in fixture.sbom["packages"] if package["name"] != "nvidia-cuda-runtime-cu12"
    ]

    with pytest.raises(evidence.PublicRouteImageEvidenceError, match="required CUDA runtime"):
        _collect(tmp_path, fixture)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda image: image["config"].update({"User": "0"}),
        lambda image: image["config"].update({"Entrypoint": ["/bin/sh"]}),
        lambda image: image["config"]["Labels"].update({"communityai.public-route.device": "cpu"}),
        lambda image: image["config"]["Env"].append("API_TOKEN=private-value"),
    ],
)
def test_rejects_root_mutable_or_secret_bearing_runtime_config(tmp_path, mutation):
    fixture = EvidenceFixture(tmp_path)
    mutation(fixture.image)

    with pytest.raises(evidence.PublicRouteImageEvidenceError, match="private non-root CUDA contract"):
        _collect(tmp_path, fixture)


def test_rejects_compressed_and_uncompressed_size_escape(tmp_path):
    fixture = EvidenceFixture(tmp_path)
    fixture.layers = [
        {
            "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
            "digest": "sha256:" + digit * 64,
            "size": 8_000_000_001,
        }
        for digit in ("1", "2", "3")
    ]
    fixture.manifest["layers"] = fixture.layers
    fixture.raw_manifest = _json_bytes(fixture.manifest)
    fixture.runtime_digest = qualification_evidence._digest(fixture.raw_manifest)
    fixture.index["manifests"][0]["digest"] = fixture.runtime_digest
    fixture.index["manifests"][0]["size"] = len(fixture.raw_manifest)
    fixture.index["manifests"][1]["annotations"]["vnd.docker.reference.digest"] = fixture.runtime_digest
    fixture.raw_index = _json_bytes(fixture.index)
    fixture.index_digest = qualification_evidence._digest(fixture.raw_index)
    metadata = {
        "containerimage.digest": fixture.index_digest,
        "containerimage.descriptor": {
            "mediaType": qualification_evidence.OCI_INDEX_MEDIA_TYPE,
            "digest": fixture.index_digest,
            "size": len(fixture.raw_index),
        },
    }
    fixture.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(evidence.PublicRouteImageEvidenceError, match="compressed size"):
        _collect(tmp_path, fixture)

    fixture = EvidenceFixture(tmp_path / "uncompressed")
    fixture.local["Size"] = evidence._LIMITS["qwen3.5-2b"]["maximum_uncompressed_bytes"] + 1
    with pytest.raises(evidence.PublicRouteImageEvidenceError, match="uncompressed size"):
        _collect(tmp_path / "uncompressed", fixture)


def test_rejects_tag_drift_and_existing_output(tmp_path):
    fixture = EvidenceFixture(tmp_path)
    fixture.raw_index += b" "

    with pytest.raises(evidence.PublicRouteImageEvidenceError, match="does not match"):
        _collect(tmp_path, fixture)

    fixture = EvidenceFixture(tmp_path / "existing")
    output = tmp_path / "existing" / "evidence.json"
    output.write_text("owner evidence", encoding="utf-8")
    with pytest.raises(evidence.PublicRouteImageEvidenceError, match="must not already exist"):
        evidence.collect_evidence(
            contract_path=fixture.contract_path,
            build_metadata_path=fixture.metadata_path,
            output_path=output,
            runner=fixture,
        )
    assert fixture.calls == []


def test_contract_rejects_cpu_qualification_repository_before_docker(tmp_path):
    fixture = EvidenceFixture(tmp_path)
    fixture.contract["image_tag"] = "ghcr.io/flujo-app/communityai-qualification-qwen3.5-2b:source-" + SOURCE_COMMIT
    fixture.contract["contract_digest"] = image_contract._contract_digest(fixture.contract)
    fixture.contract_path.write_text(json.dumps(fixture.contract), encoding="utf-8")

    with pytest.raises(evidence.PublicRouteImageEvidenceError, match="reviewed CUDA repository"):
        _collect(tmp_path, fixture)

    assert fixture.calls == []
