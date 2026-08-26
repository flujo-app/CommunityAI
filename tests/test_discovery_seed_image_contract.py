import json
import subprocess
from pathlib import Path

import pytest

from scripts import discovery_seed_image_contract as contract

REPOSITORY = Path(__file__).resolve().parents[1]


def _git(repository, *arguments):
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def source_repository(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "tests@example.invalid")
    _git(repository, "config", "user.name", "CommunityAI tests")
    for relative_path in contract.SOURCE_PATHS:
        source = REPOSITORY.joinpath(*relative_path.split("/"))
        destination = repository.joinpath(*relative_path.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    _git(repository, "add", "--force", "--", *contract.SOURCE_PATHS)
    _git(repository, "commit", "-m", "source")
    return repository, _git(repository, "rev-parse", "HEAD")


def test_prepare_materializes_exact_commit_and_shell_free_build_plan(source_repository, tmp_path):
    repository, source_commit = source_repository
    output = tmp_path / "contract"
    image_tag = f"{contract.IMAGE_REPOSITORY}:source-{source_commit}"

    report = contract.prepare_contract(
        source_commit=source_commit,
        repository_commit=source_commit,
        image_tag=image_tag,
        output_dir=output,
        repository_root=repository,
    )

    image_contract = json.loads((output / "image-contract.json").read_text(encoding="utf-8"))
    plan = json.loads((output / "build-plan.json").read_text(encoding="utf-8"))
    assert set(image_contract) == contract._CONTRACT_KEYS
    assert image_contract["contract_digest"] == contract._contract_digest(image_contract)
    assert image_contract["source_commit"] == source_commit
    assert image_contract["source_tree_digest"] == report["source_tree_digest"]
    assert image_contract["maximum_rootfs_gb"] == 8
    assert image_contract["runtime"] == "hivemind-dht-only"
    assert set(image_contract["source_files"]) == set(contract.SOURCE_PATHS)
    assert image_contract["dockerfile_digest"] == (
        "sha256:" + image_contract["source_files"]["Dockerfile.discovery-seed"]["sha256"]
    )
    assert image_contract["lockfile_digest"] == (
        "sha256:" + image_contract["source_files"]["deploy/discovery/requirements.lock"]["sha256"]
    )

    command = plan["build_command"]
    assert isinstance(command, list)
    assert command[0:3] == ["docker", "buildx", "build"]
    assert all(isinstance(argument, str) for argument in command)
    assert "--platform" in command and command[command.index("--platform") + 1] == "linux/amd64"
    assert "--provenance=mode=max" in command
    assert "--sbom=true" in command
    assert "--push" in command
    assert command[command.index("--tag") + 1] == image_tag
    assert plan["provider_calls_made"] is False
    assert plan["image_built"] is False
    assert plan["image_published"] is False

    for relative_path in contract.SOURCE_PATHS:
        expected = contract._run_git(
            repository,
            ["cat-file", "blob", f"{source_commit}:{relative_path}"],
            contract.MAX_SOURCE_FILE_BYTES,
        )
        assert output.joinpath("source", *relative_path.split("/")).read_bytes() == expected


def test_prepare_ignores_dirty_worktree_and_refuses_output_reuse(source_repository, tmp_path):
    repository, source_commit = source_repository
    dirty = repository / "deploy" / "gcp" / "bootstrap_node.py"
    dirty.write_text("uncommitted and unsafe\n", encoding="utf-8")
    output = tmp_path / "contract"
    image_tag = f"{contract.IMAGE_REPOSITORY}:source-{source_commit}"

    contract.prepare_contract(
        source_commit=source_commit,
        repository_commit=source_commit,
        image_tag=image_tag,
        output_dir=output,
        repository_root=repository,
    )

    assert (output / "source" / "deploy" / "gcp" / "bootstrap_node.py").read_text(
        encoding="utf-8"
    ) != "uncommitted and unsafe\n"
    with pytest.raises(contract.DiscoveryImageContractError, match="must not already exist"):
        contract.prepare_contract(
            source_commit=source_commit,
            repository_commit=source_commit,
            image_tag=image_tag,
            output_dir=output,
            repository_root=repository,
        )


@pytest.mark.parametrize(
    "source_mutation, tag_builder, message",
    [
        ("b" * 40, lambda commit: f"{contract.IMAGE_REPOSITORY}:source-{commit}", "source commit"),
        (None, lambda commit: f"{contract.IMAGE_REPOSITORY}:latest", "reviewed repository"),
        (None, lambda commit: f"ghcr.io/other/image:source-{commit}", "reviewed repository"),
        (None, lambda commit: f"{contract.IMAGE_REPOSITORY}:source-{'b' * 40}", "exact source"),
    ],
)
def test_prepare_rejects_unbound_source_or_tag(source_repository, tmp_path, source_mutation, tag_builder, message):
    repository, repository_commit = source_repository
    source_commit = source_mutation or repository_commit
    with pytest.raises(contract.DiscoveryImageContractError, match=message):
        contract.prepare_contract(
            source_commit=source_commit,
            repository_commit=repository_commit,
            image_tag=tag_builder(repository_commit),
            output_dir=tmp_path / "contract",
            repository_root=repository,
        )


def test_materialized_verifier_rejects_extra_and_changed_files(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    source = root / "runtime.py"
    source.write_bytes(b"original")
    inventory = contract._source_inventory({"runtime.py": b"original"})

    contract._verify_materialized_source(root, inventory)

    (root / "extra.py").write_text("extra", encoding="utf-8")
    with pytest.raises(contract.DiscoveryImageContractError, match="missing or extra"):
        contract._verify_materialized_source(root, inventory)
    (root / "extra.py").unlink()
    source.write_bytes(b"changed")
    with pytest.raises(contract.DiscoveryImageContractError, match="exact commit"):
        contract._verify_materialized_source(root, inventory)


def test_cli_prepares_contract_without_docker_or_provider_calls(source_repository, tmp_path, capsys):
    repository, source_commit = source_repository
    output = tmp_path / "contract"

    assert (
        contract.main(
            [
                "prepare",
                "--source-commit",
                source_commit,
                "--image-tag",
                f"{contract.IMAGE_REPOSITORY}:source-{source_commit}",
                "--output-dir",
                str(output),
                "--repository-root",
                str(repository),
            ]
        )
        == 0
    )

    report = json.loads(capsys.readouterr().out)
    assert report["provider_calls_made"] is False
    assert report["image_built"] is False
    assert report["image_published"] is False
    assert (output / "build-plan.json").is_file()
