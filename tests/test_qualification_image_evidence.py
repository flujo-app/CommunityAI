import json
import subprocess
from pathlib import Path

import pytest

from scripts import qualification_image_contract as image_contract, qualification_image_evidence as evidence

SOURCE_COMMIT = "b" * 40
IMAGE_TAG = evidence._EXPECTED_REPOSITORIES["qwen3.5-2b"] + ":source-" + SOURCE_COMMIT


def _json_bytes(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class EvidenceFixture:
    def __init__(self, tmp_path: Path, *, candidate: str = "qwen3.5-2b"):
        self.candidate = candidate
        self.image_tag = evidence._EXPECTED_REPOSITORIES[candidate] + ":source-" + SOURCE_COMMIT
        self.repository = self.image_tag.rsplit(":", 1)[0]
        self.contract = {
            "schema_version": 1,
            "scope": "qualification-image-input",
            "candidate": candidate,
            "source_commit": SOURCE_COMMIT,
            "source_tree_digest": "sha256:" + "1" * 64,
            "source_files": {},
            "dockerfile_digest": "sha256:" + "2" * 64,
            "model_repository": "communityai/exact-model",
            "model_revision": "a" * 40,
            "manifest_digest": "sha256:" + "3" * 64,
            "manifest_filename": "model-manifest.json",
            "artifact_count": 1,
            "declared_artifact_bytes": 4_000_000_000,
            "artifact_paths": ["model.safetensors"],
            "platform": "linux/amd64",
            "python_image": image_contract.PYTHON_IMAGE,
            "uv_image": image_contract.UV_IMAGE,
            "image_tag": self.image_tag,
            "remote_manifest": "/workspace/qualification/model-manifest.json",
            "remote_cache_dir": "/cache/model",
            "artifact_hashes_verified": True,
            "image_built": False,
            "image_published": False,
        }
        self.contract["contract_digest"] = image_contract._contract_digest(self.contract)
        self.contract_path = tmp_path / "image-contract.json"
        self.contract_path.write_text(json.dumps(self.contract), encoding="utf-8")

        self.layers = [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "digest": "sha256:" + "4" * 64,
                "size": 1_000_000_000,
            },
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "digest": "sha256:" + "5" * 64,
                "size": 3_500_000_000,
            },
        ]
        self.manifest = {
            "schemaVersion": 2,
            "mediaType": evidence.OCI_MANIFEST_MEDIA_TYPE,
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": "sha256:" + "6" * 64,
                "size": 4096,
            },
            "layers": self.layers,
        }
        self.raw_manifest = _json_bytes(self.manifest)
        self.runtime_digest = evidence._digest(self.raw_manifest)
        self.attestation_digest = "sha256:" + "7" * 64
        self.index = {
            "schemaVersion": 2,
            "mediaType": evidence.OCI_INDEX_MEDIA_TYPE,
            "manifests": [
                {
                    "mediaType": evidence.OCI_MANIFEST_MEDIA_TYPE,
                    "digest": self.runtime_digest,
                    "size": len(self.raw_manifest),
                    "platform": {"architecture": "amd64", "os": "linux"},
                },
                {
                    "mediaType": evidence.OCI_MANIFEST_MEDIA_TYPE,
                    "digest": self.attestation_digest,
                    "size": 512,
                    "platform": {"architecture": "unknown", "os": "unknown"},
                    "annotations": {
                        "vnd.docker.reference.type": "attestation-manifest",
                        "vnd.docker.reference.digest": self.runtime_digest,
                    },
                },
            ],
        }
        self.raw_index = _json_bytes(self.index)
        self.metadata_path = tmp_path / "build-metadata.json"
        self._write_metadata()

        self.image = {
            "architecture": "amd64",
            "os": "linux",
            "config": {
                "Labels": {
                    "org.opencontainers.image.source": evidence.OCI_SOURCE,
                    "org.opencontainers.image.revision": SOURCE_COMMIT,
                    "communityai.qualification.candidate": candidate,
                    "communityai.qualification.manifest": self.contract["manifest_digest"],
                    "communityai.qualification.artifact-bytes": str(self.contract["declared_artifact_bytes"]),
                    "communityai.qualification.source-tree": self.contract["source_tree_digest"],
                    "communityai.qualification.dockerfile": self.contract["dockerfile_digest"],
                }
            },
        }
        self.local = {
            "Architecture": "amd64",
            "Os": "linux",
            "Size": 8_000_000_000,
            "RootFS": {
                "Type": "layers",
                "Layers": ["sha256:" + "8" * 64, "sha256:" + "9" * 64],
            },
        }
        self.provenance = b"slsa"
        self.sbom = b"spdx"
        self.calls = []

    @property
    def index_digest(self):
        return evidence._digest(self.raw_index)

    @property
    def immutable_index(self):
        return f"{self.repository}@{self.index_digest}"

    @property
    def immutable_runtime(self):
        return f"{self.repository}@{self.runtime_digest}"

    def _write_metadata(self):
        digest = evidence._digest(self.raw_index)
        metadata = {
            "containerimage.digest": digest,
            "containerimage.descriptor": {
                "mediaType": evidence.OCI_INDEX_MEDIA_TYPE,
                "digest": digest,
                "size": len(self.raw_index),
            },
            "buildx.build.ref": "private-builder-reference",
        }
        self.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    def replace_index(self, index, *, update_metadata=True):
        self.index = index
        self.raw_index = _json_bytes(index)
        if update_metadata:
            self._write_metadata()

    def __call__(self, command, timeout, maximum_output):
        self.calls.append((list(command), timeout, maximum_output))
        command = list(command)
        if command[1:4] == ["buildx", "imagetools", "inspect"]:
            reference = command[4]
            if command[-1] == "--raw":
                if reference == self.image_tag:
                    payload = self.raw_index
                elif reference == self.immutable_runtime:
                    payload = self.raw_manifest
                else:
                    raise AssertionError(f"unexpected raw reference: {reference}")
            else:
                template = command[-1]
                if reference == self.immutable_index and ".Provenance" in template:
                    assert template == "{{if .Provenance.SLSA}}slsa{{end}}"
                    payload = self.provenance
                elif reference == self.immutable_index and ".SBOM" in template:
                    assert template == "{{if .SBOM.SPDX}}spdx{{end}}"
                    payload = self.sbom
                elif reference == self.immutable_runtime and template == "{{json .Image}}":
                    payload = _json_bytes(self.image)
                else:
                    raise AssertionError(f"unexpected inspect command: {command}")
            return evidence.CommandResult(0, payload)
        if command[1:3] == ["pull", "--quiet"]:
            assert command[3:] == ["--platform", "linux/amd64", self.immutable_runtime]
            return evidence.CommandResult(0, b"pulled")
        if command[1:3] == ["image", "inspect"]:
            assert command[3:] == [self.immutable_runtime, "--format", "{{json .}}"]
            return evidence.CommandResult(0, _json_bytes(self.local))
        raise AssertionError(f"unexpected Docker command: {command}")


def _collect(tmp_path, fixture):
    output = tmp_path / "evidence.json"
    report = evidence.collect_evidence(
        contract_path=fixture.contract_path,
        build_metadata_path=fixture.metadata_path,
        output_path=output,
        runner=fixture,
    )
    return report, output


def test_collects_immutable_attested_bounded_publication_evidence(tmp_path):
    fixture = EvidenceFixture(tmp_path)

    report, output = _collect(tmp_path, fixture)

    assert report["result"] == "passed"
    assert report["candidate"] == "qwen3.5-2b"
    assert report["image_reference"] == fixture.immutable_index
    assert report["runtime_manifest_digest"] == fixture.runtime_digest
    assert report["attestation_manifest_digest"] == fixture.attestation_digest
    assert report["compressed_layer_bytes"] == 4_500_000_000
    assert report["uncompressed_image_bytes"] == 8_000_000_000
    assert report["required_fly_rootfs_gb"] == 10
    assert report["provenance"] == "slsa"
    assert report["sbom"] == "spdx"
    assert report["image_built"] is True
    assert report["image_published"] is True
    assert report["qualification_evidence"] is True
    assert report["complete_release_qualification"] is False
    assert json.loads(output.read_text(encoding="utf-8")) == report
    assert all(fixture.image_tag not in call[0] for call in fixture.calls[1:])
    assert any(fixture.immutable_runtime in call[0] for call in fixture.calls)


def test_rejects_tag_that_drifted_from_build_metadata(tmp_path):
    fixture = EvidenceFixture(tmp_path)
    fixture.raw_index += b" "

    with pytest.raises(evidence.QualificationImageEvidenceError, match="does not resolve"):
        _collect(tmp_path, fixture)


def test_rejects_build_metadata_without_bound_oci_descriptor(tmp_path):
    fixture = EvidenceFixture(tmp_path)
    metadata = json.loads(fixture.metadata_path.read_text(encoding="utf-8"))
    metadata["containerimage.descriptor"]["digest"] = "sha256:" + "0" * 64
    fixture.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(evidence.QualificationImageEvidenceError, match="does not bind"):
        _collect(tmp_path, fixture)


@pytest.mark.parametrize(
    "mutation, error",
    [
        (
            lambda index: index["manifests"].append(
                {
                    "mediaType": evidence.OCI_MANIFEST_MEDIA_TYPE,
                    "digest": "sha256:" + "c" * 64,
                    "size": 100,
                    "platform": {"architecture": "arm64", "os": "linux"},
                }
            ),
            "one runtime and one bound",
        ),
        (
            lambda index: index["manifests"][1]["annotations"].update(
                {"vnd.docker.reference.digest": "sha256:" + "d" * 64}
            ),
            "not bound",
        ),
        (
            lambda index: index["manifests"][0].update(
                {"platform": {"architecture": "amd64", "os": "linux", "variant": "v3"}}
            ),
            "unexpected platform",
        ),
    ],
)
def test_rejects_unbounded_or_unbound_index_contents(tmp_path, mutation, error):
    fixture = EvidenceFixture(tmp_path)
    mutation(fixture.index)
    fixture.replace_index(fixture.index)

    with pytest.raises(evidence.QualificationImageEvidenceError, match=error):
        _collect(tmp_path, fixture)


def test_rejects_runtime_manifest_bytes_that_do_not_match_index(tmp_path):
    fixture = EvidenceFixture(tmp_path)
    fixture.raw_manifest += b" "

    with pytest.raises(evidence.QualificationImageEvidenceError, match="manifest bytes"):
        _collect(tmp_path, fixture)


@pytest.mark.parametrize(
    "sizes, error",
    [
        ([evidence.GHCR_MAX_LAYER_BYTES + 1], "GHCR layer limit"),
        ([4_100_000_000, 4_100_000_000], "compressed size"),
    ],
)
def test_rejects_registry_layer_or_candidate_compressed_ceiling(tmp_path, sizes, error):
    fixture = EvidenceFixture(tmp_path)
    fixture.layers = [
        {
            "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
            "digest": "sha256:" + str(index + 1) * 64,
            "size": size,
        }
        for index, size in enumerate(sizes)
    ]
    fixture.manifest["layers"] = fixture.layers
    fixture.raw_manifest = _json_bytes(fixture.manifest)
    fixture.runtime_digest = evidence._digest(fixture.raw_manifest)
    fixture.index["manifests"][0]["digest"] = fixture.runtime_digest
    fixture.index["manifests"][0]["size"] = len(fixture.raw_manifest)
    fixture.index["manifests"][1]["annotations"]["vnd.docker.reference.digest"] = fixture.runtime_digest
    fixture.replace_index(fixture.index)
    fixture.local["RootFS"]["Layers"] = ["sha256:" + "e" * 64] * len(sizes)

    with pytest.raises(evidence.QualificationImageEvidenceError, match=error):
        _collect(tmp_path, fixture)


@pytest.mark.parametrize(
    "attribute, value",
    [
        ("provenance", b""),
        ("provenance", b"slsaslsa"),
        ("sbom", b""),
        ("sbom", b"spdxspdx"),
    ],
)
def test_requires_exactly_one_slsa_and_spdx_attestation(tmp_path, attribute, value):
    fixture = EvidenceFixture(tmp_path)
    setattr(fixture, attribute, value)

    with pytest.raises(evidence.QualificationImageEvidenceError, match="exactly one"):
        _collect(tmp_path, fixture)


def test_rejects_image_config_that_is_not_bound_to_contract(tmp_path):
    fixture = EvidenceFixture(tmp_path)
    fixture.image["config"]["Labels"]["communityai.qualification.candidate"] = "other"

    with pytest.raises(evidence.QualificationImageEvidenceError, match="exact build contract"):
        _collect(tmp_path, fixture)


@pytest.mark.parametrize(
    "local_change, error",
    [
        ({"Size": 17 * evidence.GIB}, "uncompressed size"),
        ({"RootFS": {"Type": "layers", "Layers": ["sha256:" + "8" * 64]}}, "layer inventory"),
        ({"Architecture": "arm64"}, "not linux/amd64"),
    ],
)
def test_rejects_invalid_local_uncompressed_inspection(tmp_path, local_change, error):
    fixture = EvidenceFixture(tmp_path)
    fixture.local.update(local_change)

    with pytest.raises(evidence.QualificationImageEvidenceError, match=error):
        _collect(tmp_path, fixture)


def test_gemma_has_a_larger_but_still_bounded_rootfs_plan(tmp_path):
    fixture = EvidenceFixture(tmp_path, candidate="gemma-4-e2b")
    fixture.contract["declared_artifact_bytes"] = 10_278_818_149
    fixture.contract["contract_digest"] = image_contract._contract_digest(fixture.contract)
    fixture.contract_path.write_text(json.dumps(fixture.contract), encoding="utf-8")
    fixture.image["config"]["Labels"]["communityai.qualification.artifact-bytes"] = "10278818149"
    fixture.local["Size"] = 18 * evidence.GIB

    report, _ = _collect(tmp_path, fixture)

    assert report["required_fly_rootfs_gb"] == 20
    assert report["limits"]["maximum_fly_rootfs_gb"] == 28


def test_rejects_unreviewed_registry_before_docker_access(tmp_path):
    fixture = EvidenceFixture(tmp_path)
    fixture.contract["image_tag"] = "registry.example/communityai/qwen3.5-2b:source-" + SOURCE_COMMIT
    fixture.contract["contract_digest"] = image_contract._contract_digest(fixture.contract)
    fixture.contract_path.write_text(json.dumps(fixture.contract), encoding="utf-8")

    with pytest.raises(evidence.QualificationImageEvidenceError, match="reviewed GHCR"):
        _collect(tmp_path, fixture)

    assert fixture.calls == []


def test_rejects_existing_evidence_output_without_overwrite(tmp_path):
    output = tmp_path / "evidence.json"
    output.write_text("owner evidence", encoding="utf-8")

    with pytest.raises(evidence.QualificationImageEvidenceError, match="must not already exist"):
        evidence._atomic_write(output, {"result": "passed"})

    assert output.read_text(encoding="utf-8") == "owner evidence"


def test_default_runner_fails_closed_without_exposing_docker_output(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, stdout=b"private stdout", stderr=b"private stderr")

    monkeypatch.setattr(evidence.subprocess, "run", fake_run)
    result = evidence._run_command(["docker", "version"], 1, 1024)

    assert result.returncode == 1
    with pytest.raises(evidence.QualificationImageEvidenceError, match="exited nonzero") as captured:
        evidence._docker(
            lambda command, timeout, maximum_output: result,
            "docker",
            ["version"],
            timeout=1,
        )
    assert "private stdout" not in str(captured.value)
    assert "private stderr" not in str(captured.value)
