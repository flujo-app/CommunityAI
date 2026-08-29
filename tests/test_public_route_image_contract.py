import hashlib
import json
import os
from pathlib import Path

import pytest

from scripts import public_route_image_contract as contract, qualification_image_contract as qualification

SOURCE_COMMIT = "b" * 40
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
QWEN_EVIDENCE = REPOSITORY_ROOT / "docs" / "evidence" / "gate4-20260826-b-qwen3.5-2b-publication-evidence.json"
GEMMA_EVIDENCE = REPOSITORY_ROOT / "docs" / "evidence" / "gate4-20260826-b-gemma-4-e2b-publication-evidence.json"


def test_cuda_arch_contract_reads_compile_time_flags_without_a_runtime_gpu():
    dockerfile = (REPOSITORY_ROOT / contract.DOCKERFILE).read_text(encoding="utf-8")

    assert "torch._C._cuda_getArchFlags()" in dockerfile
    assert "torch.cuda.get_arch_list()" not in dockerfile
    assert '"sm_86" in arches and "sm_90" in arches' in dockerfile


@pytest.fixture(autouse=True)
def _stub_committed_archive(monkeypatch):
    def archive(repository_root, source_commit, repository_commit, candidate):
        qualification._require_source_commit(source_commit, repository_commit)
        manifest_path = repository_root / contract._CANDIDATE_MANIFESTS[candidate]
        return (
            {
                contract.DOCKERFILE: b"FROM scratch\n",
                "pyproject.toml": b"[build-system]\n",
                "uv.lock": b"version = 1\n",
                "README.md": b"# exact route source\n",
                "src/drift/__init__.py": b'__version__ = "2.3.0.dev2"\n',
                "scripts/public_route_node.py": b"raise SystemExit(0)\n",
                "scripts/public_route_image_contract.py": b"raise SystemExit(0)\n",
                "scripts/qualification_image_contract.py": b"raise SystemExit(0)\n",
            },
            manifest_path.read_bytes(),
        )

    monkeypatch.setattr(contract, "_archive_repository_source", archive)


def _prepare(tmp_path, candidate="qwen3.5-2b", **overrides):
    repository = contract._TARGET_REPOSITORIES[candidate]
    arguments = {
        "candidate": candidate,
        "source_commit": SOURCE_COMMIT,
        "repository_commit": SOURCE_COMMIT,
        "image_tag": f"{repository}:source-{SOURCE_COMMIT}",
        "output_dir": tmp_path / f"{candidate}-route-input",
        "repository_root": REPOSITORY_ROOT,
        "carrier_evidence_path": QWEN_EVIDENCE if candidate == "qwen3.5-2b" else GEMMA_EVIDENCE,
    }
    arguments.update(overrides)
    report = contract.prepare_contract(**arguments)
    return report, arguments


def _verification(arguments):
    output = arguments["output_dir"]
    document = json.loads((output / "image-contract.json").read_text(encoding="utf-8"))
    carrier = contract._CARRIERS[arguments["candidate"]]
    return {
        "contract_path": output / "image-contract.json",
        "manifest_path": output / "model-manifest.json",
        "carrier_evidence_path": output / "carrier-evidence.json",
        "snapshot_root": output / "snapshot",
        "source_tree_root": output / "source",
        "source_commit": SOURCE_COMMIT,
        "candidate": arguments["candidate"],
        "expected_manifest_digest": document["manifest_digest"],
        "expected_declared_artifact_bytes": document["declared_artifact_bytes"],
        "expected_source_tree_digest": document["source_tree_digest"],
        "expected_dockerfile_digest": document["dockerfile_digest"],
        "expected_uv_lock_digest": document["uv_lock_digest"],
        "expected_carrier_runtime_image": document["carrier_runtime_image"],
        "expected_carrier_evidence_digest": document["carrier_evidence_digest"],
        "expected_carrier_index_digest": carrier["index_digest"],
        "expected_carrier_runtime_digest": carrier["runtime_digest"],
    }


@pytest.mark.parametrize(
    "candidate,evidence_path,runtime_digest",
    [
        (
            "qwen3.5-2b",
            QWEN_EVIDENCE,
            "sha256:5ad01b9ea9fea6adb5e2c60cc804685ba3bfa2a4f09d5ff48b56a762f3df1770",
        ),
        (
            "gemma-4-e2b",
            GEMMA_EVIDENCE,
            "sha256:406f94b7a53bcef847fb4ea04eae0036310a4b5f92e87beade6ec919629530f8",
        ),
    ],
)
def test_exact_legacy_carrier_is_strictly_reconstructed(candidate, evidence_path, runtime_digest):
    evidence, raw, binding = contract._load_carrier_evidence(evidence_path, candidate)

    assert contract._digest(raw) == contract._CARRIERS[candidate]["evidence_digest"]
    assert evidence["runtime_manifest_digest"] == runtime_digest
    assert binding["runtime_image"].endswith("@" + runtime_digest)
    assert binding["index_reference"] == evidence["image_reference"]
    assert binding["source_commit"] == "7660e33e03326e5b868f81cb95282460ba649d5f"


def test_legacy_carrier_rejects_any_byte_change_before_trusting_schema(tmp_path):
    raw = QWEN_EVIDENCE.read_bytes()
    changed = tmp_path / "changed.json"
    changed.write_bytes(raw + b" ")

    with pytest.raises(contract.PublicRouteImageError, match="bytes do not match"):
        contract._load_carrier_evidence(changed, "qwen3.5-2b")


def test_legacy_carrier_rejects_extra_field_even_with_matching_review_digest(tmp_path, monkeypatch):
    document = json.loads(QWEN_EVIDENCE.read_text(encoding="utf-8"))
    document["unexpected"] = True
    raw = (json.dumps(document, sort_keys=True) + "\n").encode()
    changed = tmp_path / "changed.json"
    changed.write_bytes(raw)
    monkeypatch.setitem(contract._CARRIERS["qwen3.5-2b"], "evidence_digest", contract._digest(raw))

    with pytest.raises(contract.PublicRouteImageError, match="identity is invalid"):
        contract._load_carrier_evidence(changed, "qwen3.5-2b")


@pytest.mark.parametrize("candidate", tuple(contract._CARRIERS))
def test_prepare_emits_exact_cuda_route_build_contract(tmp_path, candidate):
    report, arguments = _prepare(tmp_path, candidate)
    output = arguments["output_dir"]
    document = json.loads((output / "image-contract.json").read_text(encoding="utf-8"))
    plan = json.loads((output / "build-plan.json").read_text(encoding="utf-8"))

    assert report["result"] == "passed"
    assert set(path.name for path in output.iterdir()) == {
        "build-plan.json",
        "carrier-evidence.json",
        "image-contract.json",
        "model-manifest.json",
        "source",
    }
    assert document["candidate"] == candidate
    assert document["source_commit"] == SOURCE_COMMIT
    assert document["device"] == "cuda"
    assert document["torch_version"] == "2.6.0+cu124"
    assert document["cuda_version"] == "12.4"
    assert document["nonroot_uid"] == 65532
    assert document["training_rpcs"] == "disabled"
    assert document["health_state_path"] == "/run/communityai/health.json"
    assert document["full_block_span"].startswith("0:")
    assert document["carrier_runtime_image"].startswith(contract._CARRIERS[candidate]["repository"] + "@sha256:")
    assert document["carrier_evidence_digest"] == contract._CARRIERS[candidate]["evidence_digest"]
    assert document["image_tag"].startswith(contract._TARGET_REPOSITORIES[candidate] + ":source-")
    assert document["image_built"] is False
    assert document["image_published"] is False
    assert document["contract_digest"] == contract._contract_digest(document)

    command = plan["build_command"]
    assert command[:3] == ["docker", "buildx", "build"]
    assert "--platform" in command and "linux/amd64" in command
    assert "--provenance=mode=max" in command
    assert "--sbom=true" in command
    assert "--push" in command
    assert f"contract={output}" in command
    assert "CARRIER_RUNTIME_IMAGE=" + document["carrier_runtime_image"] in command
    assert "CARRIER_EVIDENCE_DIGEST=" + document["carrier_evidence_digest"] in command
    assert "SOURCE_TREE_DIGEST=" + document["source_tree_digest"] in command
    assert "DOCKERFILE_DIGEST=" + document["dockerfile_digest"] in command
    assert "UV_LOCK_DIGEST=" + document["uv_lock_digest"] in command
    assert command[-1] == os.fspath(output / "source")
    assert not any(
        forbidden in argument.lower()
        for argument in command
        for forbidden in ("token", "secret", "password", "credential")
    )


def test_prepare_excludes_dirty_worktree_by_using_archive_payload(monkeypatch, tmp_path):
    marker = b"committed-only\n"

    def archive(repository_root, source_commit, repository_commit, candidate):
        _, manifest = _stub_archive_payload(repository_root, candidate)
        payloads = _stub_archive_payload(repository_root, candidate)[0]
        payloads["scripts/public_route_node.py"] = marker
        return payloads, manifest

    monkeypatch.setattr(contract, "_archive_repository_source", archive)
    _report, arguments = _prepare(tmp_path)

    assert (arguments["output_dir"] / "source" / "scripts" / "public_route_node.py").read_bytes() == marker


def _stub_archive_payload(repository_root, candidate):
    manifest_path = repository_root / contract._CANDIDATE_MANIFESTS[candidate]
    return (
        {
            contract.DOCKERFILE: b"FROM scratch\n",
            "pyproject.toml": b"[build-system]\n",
            "uv.lock": b"version = 1\n",
            "README.md": b"# exact route source\n",
            "src/drift/__init__.py": b'__version__ = "2.3.0.dev2"\n',
            "scripts/public_route_node.py": b"raise SystemExit(0)\n",
            "scripts/public_route_image_contract.py": b"raise SystemExit(0)\n",
            "scripts/qualification_image_contract.py": b"raise SystemExit(0)\n",
        },
        manifest_path.read_bytes(),
    )


def test_verify_rehashes_source_carrier_and_snapshot(monkeypatch, tmp_path):
    _report, arguments = _prepare(tmp_path)
    verification = _verification(arguments)
    manifest = qualification._load_manifest(verification["manifest_path"])[0]

    def verify_snapshot(observed_manifest, snapshot_root):
        assert observed_manifest.digest_id == manifest.digest_id
        return contract._manifest_inventory(observed_manifest)

    monkeypatch.setattr(qualification, "verify_snapshot", verify_snapshot)

    report = contract.verify_contract(**verification)

    assert report["result"] == "passed"
    assert report["source_hashes_verified"] is True
    assert report["artifact_hashes_verified"] is True
    assert report["carrier_evidence_digest"] == contract._CARRIERS["qwen3.5-2b"]["evidence_digest"]
    assert report["image_built"] is False
    assert report["image_published"] is False


def test_verify_rejects_tampered_materialized_source(monkeypatch, tmp_path):
    _report, arguments = _prepare(tmp_path)
    verification = _verification(arguments)
    source = arguments["output_dir"] / "source" / "scripts" / "public_route_node.py"
    source.write_bytes(source.read_bytes() + b"# tampered\n")
    monkeypatch.setattr(
        qualification,
        "verify_snapshot",
        lambda manifest, snapshot_root: contract._manifest_inventory(manifest),
    )

    with pytest.raises(contract.PublicRouteImageError, match="missing, extra, or incorrectly sized"):
        contract.verify_contract(**verification)


def test_verify_rejects_mutated_contract_before_snapshot(monkeypatch, tmp_path):
    _report, arguments = _prepare(tmp_path)
    verification = _verification(arguments)
    path = verification["contract_path"]
    document = json.loads(path.read_text(encoding="utf-8"))
    document["training_rpcs"] = "enabled"
    document["contract_digest"] = contract._contract_digest(document)
    path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(
        qualification,
        "verify_snapshot",
        lambda *args: pytest.fail("snapshot must not be read"),
    )

    with pytest.raises(contract.PublicRouteImageError, match="identity is invalid"):
        contract.verify_contract(**verification)


@pytest.mark.parametrize(
    "image_tag",
    [
        "ghcr.io/flujo-app/communityai-qualification-qwen3.5-2b:source-" + SOURCE_COMMIT,
        "registry.example/public-route-qwen:source-" + SOURCE_COMMIT,
        contract._TARGET_REPOSITORIES["qwen3.5-2b"] + ":latest",
    ],
)
def test_prepare_rejects_cpu_unreviewed_or_unbound_image_repository(tmp_path, image_tag):
    with pytest.raises(contract.PublicRouteImageError, match="tag|repository"):
        _prepare(tmp_path, image_tag=image_tag)


def test_contract_rejects_unsafe_source_inventory_path(tmp_path):
    _report, arguments = _prepare(tmp_path)
    path = arguments["output_dir"] / "image-contract.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["source_files"]["../secret"] = {
        "sha256": hashlib.sha256(b"secret").hexdigest(),
        "size": 6,
    }
    document["contract_digest"] = contract._contract_digest(document)
    path.write_text(json.dumps(document), encoding="utf-8")
    verification = _verification(arguments)

    with pytest.raises(contract.PublicRouteImageError, match="unsafe path"):
        contract.verify_contract(**verification)


def test_dockerfile_is_fresh_pinned_nonroot_cuda_runtime():
    dockerfile = (REPOSITORY_ROOT / contract.DOCKERFILE).read_text(encoding="utf-8")

    assert "FROM ${CARRIER_RUNTIME_IMAGE} AS snapshot" in dockerfile
    assert qualification.PYTHON_IMAGE in dockerfile
    assert f'org.opencontainers.image.base.name="{qualification.PYTHON_IMAGE}"' in dockerfile
    assert qualification.UV_IMAGE in dockerfile
    assert "COPY --from=snapshot --chown=0:0 /cache/model /cache/model" in dockerfile
    assert "chmod -R a+rX,a-w /cache/model" in dockerfile
    assert "scripts/qualification_image_contract.py ./scripts/" in dockerfile
    assert "scripts/qualification_image_contract.py" in contract._SOURCE_SCRIPT_FILES
    assert "uv sync --frozen --no-dev --no-editable" in dockerfile
    assert dockerfile.index("uv sync --frozen") < dockerfile.index("public_route_image_contract.py verify")
    assert 'torch.__version__ == "2.6.0+cu124"' in dockerfile
    assert 'torch.version.cuda == "12.4"' in dockerfile
    assert "USER 65532:65532" in dockerfile
    assert 'ENTRYPOINT ["python", "-u", "/workspace/scripts/public_route_node.py"]' in dockerfile
    assert "curl" not in dockerfile
    assert "--allow_training_rpcs" not in dockerfile
