import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from drift.model_manifest import ModelManifest
from scripts import qualification_image_contract as image_contract

SOURCE_COMMIT = "b" * 40
IMAGE_TAG = "registry.example/communityai/qwen3.5-2b:source-" + SOURCE_COMMIT
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ORIGINAL_LOAD_COMMITTED_SOURCE = image_contract._load_committed_source


@pytest.fixture(autouse=True)
def _stub_committed_source(monkeypatch: pytest.MonkeyPatch):
    def load_source(repository_root, source_commit, repository_commit, candidate, manifest_bytes):
        image_contract._require_source_commit(source_commit, repository_commit)
        return {
            "Dockerfile.qualification": b"FROM scratch\n",
            "pyproject.toml": b"[build-system]\n",
            "uv.lock": b"version = 1\n",
            "README.md": b"# exact source fixture\n",
            "src/drift/__init__.py": b'__version__ = "2.3.0.dev0"\n',
            "scripts/fly_qualification_node.py": b"raise SystemExit(0)\n",
            "scripts/qualification_image_contract.py": b"raise SystemExit(0)\n",
        }

    monkeypatch.setattr(image_contract, "_load_committed_source", load_source)


def _write_fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, bytes]]:
    snapshot = tmp_path / "snapshot"
    (snapshot / "tokenizer").mkdir(parents=True)
    payloads = {
        "config.json": b'{"architectures":["TinyForCausalLM"]}',
        "model.safetensors": b"exact weights",
        "tokenizer/tokenizer.json": b'{"version":"1.0"}',
    }
    roles = {
        "config.json": "config",
        "model.safetensors": "weight",
        "tokenizer/tokenizer.json": "tokenizer",
    }
    for relative_path, payload in payloads.items():
        path = snapshot.joinpath(*relative_path.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    manifest = {
        "schema_version": 1,
        "name": "Tiny qualification fixture",
        "aliases": ["tiny-qualification"],
        "source": {
            "repository": "communityai/tiny-qualification",
            "revision": "a" * 40,
        },
        "model": {
            "architecture": "TinyForCausalLM",
            "num_blocks": 4,
            "context_length": 1024,
            "license": "apache-2.0",
            "gated": False,
        },
        "runtime": {
            "implementation": "drift",
            "minimum_version": "2.3.0.dev0",
            "maximum_version_exclusive": "2.4.0",
            "protocol_version": 1,
            "tensor_schema": "hidden-states-v1",
            "attention_implementation": "eager",
            "dtype": "bfloat16",
            "quantization": "none",
            "adapter_profile": "none",
        },
        "artifacts": [
            {
                "role": roles[relative_path],
                "path": relative_path,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
            for relative_path, payload in payloads.items()
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path, snapshot, payloads


def _prepare(tmp_path: Path, **overrides):
    manifest_path, snapshot, payloads = _write_fixture(tmp_path)
    arguments = {
        "candidate": "qwen3.5-2b",
        "manifest_path": manifest_path,
        "snapshot_root": snapshot.absolute(),
        "source_commit": SOURCE_COMMIT,
        "repository_commit": SOURCE_COMMIT,
        "image_tag": IMAGE_TAG,
        "output_dir": tmp_path / "contract",
        "repository_root": REPOSITORY_ROOT,
    }
    arguments.update(overrides)
    report = image_contract.prepare_contract(**arguments)
    return report, arguments, payloads


def _verify_prepared(arguments, **overrides):
    output = arguments["output_dir"]
    contract = json.loads((output / "image-contract.json").read_text(encoding="utf-8"))
    verification = {
        "contract_path": output / "image-contract.json",
        "manifest_path": output / "model-manifest.json",
        "snapshot_root": arguments["snapshot_root"],
        "source_tree_root": output / "source",
        "source_commit": SOURCE_COMMIT,
        "candidate": "qwen3.5-2b",
        "expected_manifest_digest": contract["manifest_digest"],
        "expected_declared_artifact_bytes": contract["declared_artifact_bytes"],
        "expected_source_tree_digest": contract["source_tree_digest"],
        "expected_dockerfile_digest": contract["dockerfile_digest"],
    }
    verification.update(overrides)
    return image_contract.verify_contract(**verification)


def test_prepare_emits_exact_credential_free_build_contract(tmp_path: Path):
    report, arguments, payloads = _prepare(tmp_path)
    output = arguments["output_dir"]
    contract = json.loads((output / "image-contract.json").read_text(encoding="utf-8"))
    plan = json.loads((output / "build-plan.json").read_text(encoding="utf-8"))
    copied_manifest = ModelManifest.load(output / "model-manifest.json")

    assert report["result"] == "passed"
    assert report["image_built"] is False
    assert report["image_published"] is False
    assert set(path.name for path in output.iterdir()) == {
        "build-plan.json",
        "image-contract.json",
        "model-manifest.json",
        "source",
    }
    assert contract["source_commit"] == SOURCE_COMMIT
    assert contract["source_tree_digest"].startswith("sha256:")
    assert contract["dockerfile_digest"].startswith("sha256:")
    assert set(contract["source_files"]) == {
        "Dockerfile.qualification",
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "src/drift/__init__.py",
        "scripts/fly_qualification_node.py",
        "scripts/qualification_image_contract.py",
    }
    assert (output / "source" / "Dockerfile.qualification").read_bytes() == b"FROM scratch\n"
    assert contract["model_revision"] == "a" * 40
    assert contract["manifest_digest"] == copied_manifest.digest_id
    assert contract["artifact_count"] == len(payloads)
    assert contract["declared_artifact_bytes"] == sum(map(len, payloads.values()))
    assert contract["artifact_hashes_verified"] is True
    assert contract["platform"] == "linux/amd64"
    assert contract["python_image"] == image_contract.PYTHON_IMAGE
    assert contract["uv_image"] == image_contract.UV_IMAGE
    assert "@sha256:" in contract["python_image"]
    assert "@sha256:" in contract["uv_image"]
    assert contract["remote_manifest"] == "/workspace/qualification/model-manifest.json"
    assert contract["remote_cache_dir"] == "/cache/model"
    assert contract["contract_digest"].startswith("sha256:")

    command = plan["build_command"]
    assert command[:3] == ["docker", "buildx", "build"]
    assert "--push" in command
    assert "--provenance=mode=max" in command
    assert "--sbom=true" in command
    assert "snapshot=" + os.fspath(arguments["snapshot_root"].resolve()) in command
    assert "contract=" + os.fspath(output) in command
    assert os.fspath(output / "source" / "Dockerfile.qualification") in command
    assert command[-1] == os.fspath(output / "source")
    assert "SOURCE_TREE_DIGEST=" + contract["source_tree_digest"] in command
    assert "DOCKERFILE_DIGEST=" + contract["dockerfile_digest"] in command
    assert "MANIFEST_DIGEST=" + contract["manifest_digest"] in command
    assert "DECLARED_ARTIFACT_BYTES=" + str(contract["declared_artifact_bytes"]) in command
    assert not any("token" in argument.lower() or "secret" in argument.lower() for argument in command)
    assert plan["image_built"] is False
    assert plan["image_published"] is False
    assert plan["qualification_evidence"] is False


def test_verify_rehashes_copied_snapshot(tmp_path: Path):
    _, arguments, payloads = _prepare(tmp_path)
    report = _verify_prepared(arguments)

    assert report["result"] == "passed"
    assert report["artifact_count"] == len(payloads)
    assert report["artifact_hashes_verified"] is True
    assert report["qualification_evidence"] is False
    assert report["complete_release_qualification"] is False


def test_standalone_image_verifier_needs_no_prepare_only_helper(tmp_path: Path):
    _, arguments, _ = _prepare(tmp_path)
    output = arguments["output_dir"]
    contract = json.loads((output / "image-contract.json").read_text(encoding="utf-8"))
    isolated_script = tmp_path / "image-runtime" / "qualification_image_contract.py"
    isolated_script.parent.mkdir()
    isolated_script.write_bytes((REPOSITORY_ROOT / "scripts" / isolated_script.name).read_bytes())

    result = subprocess.run(
        [
            sys.executable,
            os.fspath(isolated_script),
            "verify",
            "--contract",
            os.fspath(output / "image-contract.json"),
            "--manifest",
            os.fspath(output / "model-manifest.json"),
            "--snapshot-root",
            os.fspath(arguments["snapshot_root"]),
            "--source-tree-root",
            os.fspath(output / "source"),
            "--source-commit",
            SOURCE_COMMIT,
            "--candidate",
            "qwen3.5-2b",
            "--manifest-digest",
            contract["manifest_digest"],
            "--declared-artifact-bytes",
            str(contract["declared_artifact_bytes"]),
            "--source-tree-digest",
            contract["source_tree_digest"],
            "--dockerfile-digest",
            contract["dockerfile_digest"],
        ],
        cwd=isolated_script.parent,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["result"] == "passed"


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"source_commit": "B" * 40}, "exact lowercase"),
        ({"repository_commit": "c" * 40}, "does not match"),
        ({"repository_commit": None}, "does not match"),
        ({"candidate": "unlisted"}, "public-alpha image set"),
        ({"image_tag": "registry.example/communityai/image"}, "credential-free tagged"),
        ({"image_tag": "registry.example/communityai/image:latest"}, "exact source commit"),
        ({"image_tag": "https://user:password@example/image:tag"}, "credential-free tagged"),
        ({"image_tag": "registry.example/image:tag@sha256:" + "1" * 64}, "credential-free tagged"),
        ({"image_tag": "r/" + "a" * 254 + ":source-" + SOURCE_COMMIT}, "bounded credential-free"),
    ],
)
def test_prepare_fails_closed_on_unbound_identity(tmp_path: Path, override, message):
    manifest_path, snapshot, _ = _write_fixture(tmp_path)
    arguments = {
        "candidate": "qwen3.5-2b",
        "manifest_path": manifest_path,
        "snapshot_root": snapshot.absolute(),
        "source_commit": SOURCE_COMMIT,
        "repository_commit": SOURCE_COMMIT,
        "image_tag": IMAGE_TAG,
        "output_dir": tmp_path / "contract",
        "repository_root": REPOSITORY_ROOT,
        **override,
    }

    with pytest.raises(image_contract.QualificationImageError, match=message):
        image_contract.prepare_contract(**arguments)


def test_prepare_rejects_changed_artifact_digest(tmp_path: Path):
    manifest_path, snapshot, _ = _write_fixture(tmp_path)
    (snapshot / "model.safetensors").write_bytes(b"other weights")

    with pytest.raises(image_contract.QualificationImageError, match="digest"):
        image_contract.prepare_contract(
            candidate="qwen3.5-2b",
            manifest_path=manifest_path,
            snapshot_root=snapshot.absolute(),
            source_commit=SOURCE_COMMIT,
            repository_commit=SOURCE_COMMIT,
            image_tag=IMAGE_TAG,
            output_dir=tmp_path / "contract",
            repository_root=REPOSITORY_ROOT,
        )


@pytest.mark.parametrize("extra", ["extra.bin", "empty-directory"])
def test_prepare_rejects_extra_snapshot_entries(tmp_path: Path, extra: str):
    manifest_path, snapshot, _ = _write_fixture(tmp_path)
    if extra == "extra.bin":
        (snapshot / extra).write_bytes(b"not declared")
    else:
        (snapshot / extra).mkdir()

    with pytest.raises(image_contract.QualificationImageError, match="missing, extra"):
        image_contract.prepare_contract(
            candidate="qwen3.5-2b",
            manifest_path=manifest_path,
            snapshot_root=snapshot.absolute(),
            source_commit=SOURCE_COMMIT,
            repository_commit=SOURCE_COMMIT,
            image_tag=IMAGE_TAG,
            output_dir=tmp_path / "contract",
            repository_root=REPOSITORY_ROOT,
        )


def test_prepare_rejects_existing_output_directory(tmp_path: Path):
    manifest_path, snapshot, _ = _write_fixture(tmp_path)
    output = tmp_path / "contract"
    output.mkdir()

    with pytest.raises(image_contract.QualificationImageError, match="must not already exist"):
        image_contract.prepare_contract(
            candidate="qwen3.5-2b",
            manifest_path=manifest_path,
            snapshot_root=snapshot.absolute(),
            source_commit=SOURCE_COMMIT,
            repository_commit=SOURCE_COMMIT,
            image_tag=IMAGE_TAG,
            output_dir=output,
            repository_root=REPOSITORY_ROOT,
        )


def test_verify_rejects_tampered_contract_and_invalid_source(tmp_path: Path):
    _, arguments, _ = _prepare(tmp_path)
    output = arguments["output_dir"]
    contract_path = output / "image-contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["image_built"] = True
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    with pytest.raises(image_contract.QualificationImageError, match="cannot claim"):
        _verify_prepared(arguments)
    with pytest.raises(image_contract.QualificationImageError, match="exact lowercase"):
        _verify_prepared(arguments, source_commit="not-a-commit")


def test_image_repository_name_boundary_is_enforced():
    image_contract._require_image_tag("r/" + "a" * 253 + ":source-" + SOURCE_COMMIT, SOURCE_COMMIT)
    with pytest.raises(image_contract.QualificationImageError, match="bounded credential-free"):
        image_contract._require_image_tag("r/" + "a" * 254 + ":source-" + SOURCE_COMMIT, SOURCE_COMMIT)


def test_verify_rejects_mismatched_build_metadata_and_source_tampering(tmp_path: Path):
    _, arguments, _ = _prepare(tmp_path)
    output = arguments["output_dir"]
    contract = json.loads((output / "image-contract.json").read_text(encoding="utf-8"))

    with pytest.raises(image_contract.QualificationImageError, match="build manifest digest"):
        _verify_prepared(arguments, expected_manifest_digest="sha256:" + "0" * 64)
    with pytest.raises(image_contract.QualificationImageError, match="exact build arguments"):
        _verify_prepared(
            arguments,
            expected_declared_artifact_bytes=contract["declared_artifact_bytes"] + 1,
        )
    with pytest.raises(image_contract.QualificationImageError, match="non-negative integer"):
        _verify_prepared(arguments, expected_declared_artifact_bytes=-1)
    with pytest.raises(image_contract.QualificationImageError, match="exact build arguments"):
        _verify_prepared(arguments, expected_source_tree_digest="sha256:" + "1" * 64)
    with pytest.raises(image_contract.QualificationImageError, match="exact build arguments"):
        _verify_prepared(arguments, expected_dockerfile_digest="sha256:" + "2" * 64)

    source_file = output / "source" / "src" / "drift" / "__init__.py"
    source_file.write_bytes(source_file.read_bytes().replace(b"2.3.0", b"9.9.9"))
    with pytest.raises(image_contract.QualificationImageError, match="source file digest"):
        _verify_prepared(arguments)


def _create_source_repository(root: Path, manifest_bytes: bytes) -> tuple[str, bytes]:
    dockerfile_bytes = b"FROM scratch\n"
    files = {
        ".gitignore": b"*.so\n*.egg-info/\n",
        "Dockerfile.qualification": dockerfile_bytes,
        "pyproject.toml": b"[build-system]\n",
        "uv.lock": b"version = 1\n",
        "README.md": b"# committed source\n",
        "src/drift/__init__.py": b'__version__ = "2.3.0.dev0"\n',
        "scripts/fly_qualification_node.py": b"raise SystemExit(0)\n",
        "scripts/qualification_image_contract.py": b"raise SystemExit(0)\n",
        image_contract._CANDIDATES["qwen3.5-2b"]: manifest_bytes,
        image_contract._CANDIDATES["gemma-4-e2b"]: b'{"standby":true}\n',
    }
    for name, payload in files.items():
        destination = root.joinpath(*name.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
    commands = [
        ["git", "init"],
        ["git", "config", "user.email", "qualification-test@example.invalid"],
        ["git", "config", "user.name", "Qualification Test"],
        ["git", "add", "."],
        ["git", "commit", "-m", "exact source fixture"],
    ]
    for command in commands:
        subprocess.run(command, cwd=root, check=True, capture_output=True, timeout=10)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    return commit, dockerfile_bytes


def test_git_archive_context_uses_only_exact_committed_candidate_source(tmp_path: Path):
    manifest_path, _, _ = _write_fixture(tmp_path / "fixture")
    repository_root = (tmp_path / "repository").absolute()
    commit, _ = _create_source_repository(repository_root, manifest_path.read_bytes())

    tracked_source = repository_root / "src" / "drift" / "__init__.py"
    tracked_source.write_bytes(b'__version__ = "dirty-staged"\n')
    subprocess.run(
        ["git", "add", "src/drift/__init__.py"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        timeout=10,
    )
    (repository_root / "src" / "rogue.so").write_bytes(b"ignored native payload")
    ignored_metadata = repository_root / "src" / "drift.egg-info" / "PKG-INFO"
    ignored_metadata.parent.mkdir()
    ignored_metadata.write_bytes(b"ignored build metadata")
    (repository_root / "src" / "untracked.py").write_bytes(b"untracked source")

    source = ORIGINAL_LOAD_COMMITTED_SOURCE(
        repository_root,
        commit,
        commit,
        "qwen3.5-2b",
        manifest_path.read_bytes(),
    )

    committed_dockerfile = subprocess.run(
        ["git", "show", commit + ":Dockerfile.qualification"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        timeout=10,
    ).stdout
    committed_package = subprocess.run(
        ["git", "show", commit + ":src/drift/__init__.py"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        timeout=10,
    ).stdout
    assert image_contract._infer_repository_commit(repository_root) == commit
    assert source["Dockerfile.qualification"] == committed_dockerfile
    assert source["src/drift/__init__.py"] == committed_package
    assert source["src/drift/__init__.py"] != tracked_source.read_bytes()
    assert "src/rogue.so" not in source
    assert "src/drift.egg-info/PKG-INFO" not in source
    assert "src/untracked.py" not in source
    with pytest.raises(image_contract.QualificationImageError, match="candidate manifest"):
        ORIGINAL_LOAD_COMMITTED_SOURCE(repository_root, commit, commit, "qwen3.5-2b", b"tampered")

    subprocess.run(
        ["git", "rm", "--cached", "Dockerfile.qualification"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        timeout=10,
    )
    subprocess.run(
        ["git", "commit", "-m", "remove tracked Dockerfile"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        timeout=10,
    )
    unbound_commit = image_contract._infer_repository_commit(repository_root)
    with pytest.raises(image_contract.QualificationImageError, match="exact repository source"):
        ORIGINAL_LOAD_COMMITTED_SOURCE(
            repository_root,
            unbound_commit,
            unbound_commit,
            "qwen3.5-2b",
            manifest_path.read_bytes(),
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows junction behavior")
def test_prepare_cli_rejects_repository_junction_before_materializing_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    manifest_path, snapshot, _ = _write_fixture(tmp_path / "fixture")
    repository_root = (tmp_path / "repository").absolute()
    commit, _ = _create_source_repository(repository_root, manifest_path.read_bytes())
    junction = tmp_path / "repository-junction"
    output = tmp_path / "contract"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", os.fspath(junction), os.fspath(repository_root)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        pytest.skip("directory junctions are not available to this test account")
    monkeypatch.setattr(image_contract, "_load_committed_source", ORIGINAL_LOAD_COMMITTED_SOURCE)
    try:
        exit_code = image_contract.main(
            [
                "prepare",
                "--candidate",
                "qwen3.5-2b",
                "--snapshot-root",
                os.fspath(snapshot),
                "--source-commit",
                commit,
                "--image-tag",
                "registry.example/communityai/qwen3.5-2b:source-" + commit,
                "--output-dir",
                os.fspath(output),
                "--repository-root",
                os.fspath(junction),
            ]
        )
    finally:
        os.rmdir(junction)

    assert exit_code == 1
    assert "absolute unlinked Git directory" in capsys.readouterr().err
    assert not output.exists()


def test_snapshot_root_junction_predicate_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manifest_path, snapshot, _ = _write_fixture(tmp_path)
    original_isjunction = getattr(os.path, "isjunction", lambda path: False)
    snapshot_path = os.path.normcase(os.path.abspath(snapshot))

    def isjunction(path):
        return os.path.normcase(os.path.abspath(path)) == snapshot_path or original_isjunction(path)

    monkeypatch.setattr(os.path, "isjunction", isjunction)
    with pytest.raises(image_contract.QualificationImageError, match="absolute unlinked"):
        image_contract.prepare_contract(
            candidate="qwen3.5-2b",
            manifest_path=manifest_path,
            snapshot_root=snapshot.absolute(),
            source_commit=SOURCE_COMMIT,
            repository_commit=SOURCE_COMMIT,
            image_tag=IMAGE_TAG,
            output_dir=tmp_path / "contract",
            repository_root=REPOSITORY_ROOT,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows junction behavior")
def test_snapshot_root_real_windows_junction_is_rejected(tmp_path: Path):
    manifest_path, snapshot, _ = _write_fixture(tmp_path)
    junction = tmp_path / "snapshot-junction"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", os.fspath(junction), os.fspath(snapshot)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        pytest.skip("directory junctions are not available to this test account")
    try:
        with pytest.raises(image_contract.QualificationImageError, match="absolute unlinked"):
            image_contract.prepare_contract(
                candidate="qwen3.5-2b",
                manifest_path=manifest_path,
                snapshot_root=junction.absolute(),
                source_commit=SOURCE_COMMIT,
                repository_commit=SOURCE_COMMIT,
                image_tag=IMAGE_TAG,
                output_dir=tmp_path / "contract",
                repository_root=REPOSITORY_ROOT,
            )
    finally:
        os.rmdir(junction)


def test_snapshot_root_symlink_is_rejected_when_supported(tmp_path: Path):
    manifest_path, snapshot, _ = _write_fixture(tmp_path)
    linked_root = tmp_path / "linked-snapshot"
    try:
        linked_root.symlink_to(snapshot, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are not available to this test account")

    with pytest.raises(image_contract.QualificationImageError, match="absolute unlinked"):
        image_contract.prepare_contract(
            candidate="qwen3.5-2b",
            manifest_path=manifest_path,
            snapshot_root=linked_root.absolute(),
            source_commit=SOURCE_COMMIT,
            repository_commit=SOURCE_COMMIT,
            image_tag=IMAGE_TAG,
            output_dir=tmp_path / "contract",
            repository_root=REPOSITORY_ROOT,
        )


def test_public_alpha_candidates_and_dockerfile_are_immutable_and_offline():
    qwen = ModelManifest.load(REPOSITORY_ROOT / image_contract._CANDIDATES["qwen3.5-2b"])
    gemma = ModelManifest.load(REPOSITORY_ROOT / image_contract._CANDIDATES["gemma-4-e2b"])
    dockerfile = (REPOSITORY_ROOT / "Dockerfile.qualification").read_text(encoding="utf-8")

    assert qwen.source.revision == "15852e8c16360a2fea060d615a32b45270f8a8fc"
    assert qwen.digest_id == "sha256:3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33"
    assert sum(artifact.size for artifact in qwen.artifacts) == 4_571_197_320
    assert gemma.source.revision == "3e22461f65e89153144f8adb70e3b8c2cc9845a7"
    assert gemma.digest_id == "sha256:2f8debbe0fcdf5af8d4c56c982210fa50aa584314968ae2617e2ccc2de9eafdd"
    assert sum(artifact.size for artifact in gemma.artifacts) == 10_278_818_149

    assert dockerfile.count("@sha256:") == 2
    assert "COPY --from=contract image-contract.json" in dockerfile
    assert "COPY --from=snapshot --chown=65532:65532 . /cache/model" in dockerfile
    assert "qualification_image_contract.py verify" in dockerfile
    assert '--manifest-digest "${MANIFEST_DIGEST}"' in dockerfile
    assert '--declared-artifact-bytes "${DECLARED_ARTIFACT_BYTES}"' in dockerfile
    assert '--source-tree-digest "${SOURCE_TREE_DIGEST}"' in dockerfile
    assert '--dockerfile-digest "${DOCKERFILE_DIGEST}"' in dockerfile
    assert "COPY Dockerfile.qualification pyproject.toml uv.lock README.md ./" in dockerfile
    assert "COPY . /qualification-source" in dockerfile
    assert "--source-tree-root /qualification-source" in dockerfile
    assert "--source-tree-root /workspace" not in dockerfile
    assert "rm -rf /qualification-source" in dockerfile
    build_toolchain_install = "apt-get install --no-install-recommends -y build-essential"
    build_toolchain_purge = "apt-get purge --auto-remove -y build-essential"
    assert build_toolchain_install in dockerfile
    assert build_toolchain_purge in dockerfile
    assert dockerfile.index(build_toolchain_install) < dockerfile.index("uv sync --frozen")
    assert dockerfile.index("uv sync --frozen") < dockerfile.index(build_toolchain_purge)
    assert "--no-install-package nvidia-cublas-cu12" in dockerfile
    assert "--no-install-package triton" in dockerfile
    assert (
        "https://download-r2.pytorch.org/whl/cpu/torch-2.6.0%2Bcpu-cp312-cp312-linux_x86_64.whl#sha256=59e78aa0c690f70734e42670036d6b541930b8eabbaa18d94e090abf14cc4d91"
        in dockerfile
    )
    assert 'torch.__version__ == "2.6.0+cpu"' in dockerfile
    assert "torch.version.cuda is None" in dockerfile
    assert "uv pip check --python /workspace/.venv/bin/python" in dockerfile
    assert 'communityai.qualification.device="cpu"' in dockerfile
    assert "from importlib.metadata import version" in dockerfile
    assert 'raise SystemExit(0 if __version__ == version("drift") else 1)' in dockerfile
    assert "assert __version__" not in dockerfile
    assert 'test "$(python -c' not in dockerfile
    assert "rm -rf /var/lib/apt/lists/*" in dockerfile
    assert "HF_HUB_OFFLINE=1" in dockerfile
    assert "TRANSFORMERS_OFFLINE=1" in dockerfile
    assert "USER 65532:65532" in dockerfile
    assert 'ENTRYPOINT ["python", "-u", "/workspace/scripts/fly_qualification_node.py"]' in dockerfile
    assert "--mount=type=secret" not in dockerfile
    assert "curl " not in dockerfile
    assert "wget " not in dockerfile
