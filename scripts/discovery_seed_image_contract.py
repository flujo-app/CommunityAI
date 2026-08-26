"""Prepare an exact-source, credential-free discovery-seed image build plan.

This command reads the reviewed runtime inputs from one exact Git commit, inventories
and materializes them into an isolated context, and emits a shell-free Buildx push
plan with maximum provenance and an SPDX SBOM. It never calls Docker or a provider.
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

SCHEMA_VERSION = 1
PLATFORM = "linux/amd64"
IMAGE_REPOSITORY = "ghcr.io/flujo-app/communityai-discovery-seed"
PYTHON_IMAGE = "python:3.12.13-slim-bookworm@sha256:6e13e65c55e33adf203d77ee371cf8bf5d81bd4902ef07565721f46bf44917af"
UV_IMAGE = "ghcr.io/astral-sh/uv:0.11.21@sha256:6f1fa8fc4040ad7197d7e652057219871e5f6640abfe2b790f1419fdb2319e6b"
MAX_SOURCE_FILE_BYTES = 2_000_000
MAX_SOURCE_BYTES = 5_000_000
MAX_CONTRACT_BYTES = 256_000
SOURCE_PATHS = (
    "Dockerfile.discovery-seed",
    "deploy/gcp/bootstrap_node.py",
    "deploy/discovery/entrypoint.py",
    "deploy/discovery/requirements.in",
    "deploy/discovery/requirements.lock",
)
_SOURCE_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_TAG_RE = re.compile(rf"^{re.escape(IMAGE_REPOSITORY)}:source-(?P<commit>[0-9a-f]{{40}})$")
_CONTRACT_KEYS = {
    "schema_version",
    "scope",
    "source_commit",
    "source_tree_digest",
    "source_files",
    "dockerfile_digest",
    "lockfile_digest",
    "platform",
    "python_image",
    "uv_image",
    "image_repository",
    "image_tag",
    "runtime",
    "maximum_rootfs_gb",
    "image_built",
    "image_published",
    "contract_digest",
}


class DiscoveryImageContractError(ValueError):
    """The discovery image inputs are mutable, incomplete, or unsafe."""


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _contract_digest(contract: Mapping[str, Any]) -> str:
    unsigned = {key: value for key, value in contract.items() if key != "contract_digest"}
    return "sha256:" + hashlib.sha256(_canonical_json(unsigned)).hexdigest()


def _require_source_commit(source_commit: str, repository_commit: str) -> None:
    if _SOURCE_COMMIT_RE.fullmatch(source_commit) is None or source_commit != repository_commit:
        raise DiscoveryImageContractError("source commit must exactly match the checked-out repository")


def _require_image_tag(image_tag: str, source_commit: str) -> None:
    match = _IMAGE_TAG_RE.fullmatch(image_tag)
    if match is None or match.group("commit") != source_commit:
        raise DiscoveryImageContractError("image tag must use the reviewed repository and exact source commit")


def _run_git(repository_root: Path, arguments: Sequence[str], maximum_output: int) -> bytes:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repository_root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        raise DiscoveryImageContractError("exact repository source could not be read with Git") from None
    if result.returncode != 0:
        raise DiscoveryImageContractError("exact repository source is missing from the selected Git commit")
    if len(result.stdout) > maximum_output or len(result.stderr) > 64_000:
        raise DiscoveryImageContractError("exact repository source exceeds its bounded Git output")
    return result.stdout


def _repository_commit(repository_root: Path) -> str:
    try:
        commit = _run_git(repository_root, ["rev-parse", "HEAD"], 128).decode("ascii", errors="strict").strip()
    except UnicodeError:
        raise DiscoveryImageContractError("checked-out repository commit is invalid") from None
    if _SOURCE_COMMIT_RE.fullmatch(commit) is None:
        raise DiscoveryImageContractError("checked-out repository commit is invalid")
    return commit


def _load_committed_source(repository_root: Path, source_commit: str) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    total = 0
    for relative_path in SOURCE_PATHS:
        payload = _run_git(
            repository_root,
            ["cat-file", "blob", f"{source_commit}:{relative_path}"],
            MAX_SOURCE_FILE_BYTES,
        )
        if not payload:
            raise DiscoveryImageContractError(f"committed source file is empty: {relative_path}")
        total += len(payload)
        if total > MAX_SOURCE_BYTES:
            raise DiscoveryImageContractError("committed discovery source exceeds its bounded size")
        files[relative_path] = payload
    return files


def _source_inventory(files: Mapping[str, bytes]) -> dict[str, dict[str, Any]]:
    return {
        path: {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}
        for path, payload in sorted(files.items())
    }


def _source_tree_digest(inventory: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(inventory)).hexdigest()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    if len(payload) > MAX_CONTRACT_BYTES:
        raise DiscoveryImageContractError("generated image contract exceeds its bounded size")
    path.write_bytes(payload)


def _verify_materialized_source(root: Path, inventory: Mapping[str, Mapping[str, Any]]) -> None:
    expected = set(inventory)
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    if actual != expected:
        raise DiscoveryImageContractError("materialized source context contains missing or extra files")
    for relative_path, expected_file in inventory.items():
        path = root.joinpath(*PurePosixPath(relative_path).parts)
        if path.is_symlink() or not path.is_file():
            raise DiscoveryImageContractError("materialized source context contains a linked or missing file")
        payload = path.read_bytes()
        if len(payload) != expected_file["size"] or hashlib.sha256(payload).hexdigest() != expected_file["sha256"]:
            raise DiscoveryImageContractError("materialized source context does not match the exact commit")


def prepare_contract(
    *,
    source_commit: str,
    repository_commit: str,
    image_tag: str,
    output_dir: Path,
    repository_root: Path,
) -> Mapping[str, Any]:
    _require_source_commit(source_commit, repository_commit)
    _require_image_tag(image_tag, source_commit)
    source_payloads = _load_committed_source(repository_root, source_commit)
    inventory = _source_inventory(source_payloads)
    source_tree_digest = _source_tree_digest(inventory)
    dockerfile_digest = "sha256:" + inventory["Dockerfile.discovery-seed"]["sha256"]
    lockfile_digest = "sha256:" + inventory["deploy/discovery/requirements.lock"]["sha256"]

    output_dir = Path(os.path.abspath(os.fspath(output_dir.expanduser())))
    if output_dir.exists():
        raise DiscoveryImageContractError("image contract output directory must not already exist")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    source_context = output_dir / "source"
    metadata_path = output_dir.parent / "discovery-seed-image-metadata.json"

    contract: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "scope": "discovery-seed-image-input",
        "source_commit": source_commit,
        "source_tree_digest": source_tree_digest,
        "source_files": inventory,
        "dockerfile_digest": dockerfile_digest,
        "lockfile_digest": lockfile_digest,
        "platform": PLATFORM,
        "python_image": PYTHON_IMAGE,
        "uv_image": UV_IMAGE,
        "image_repository": IMAGE_REPOSITORY,
        "image_tag": image_tag,
        "runtime": "hivemind-dht-only",
        "maximum_rootfs_gb": 8,
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
        os.fspath(source_context / "Dockerfile.discovery-seed"),
        "--build-arg",
        f"SOURCE_COMMIT={source_commit}",
        "--build-arg",
        f"SOURCE_TREE_DIGEST={source_tree_digest}",
        "--build-arg",
        f"DOCKERFILE_DIGEST={dockerfile_digest}",
        "--build-arg",
        f"LOCKFILE_DIGEST={lockfile_digest}",
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
        "scope": "discovery-seed-image-build-plan",
        "result": "passed",
        "source_commit": source_commit,
        "source_tree_digest": source_tree_digest,
        "dockerfile_digest": dockerfile_digest,
        "lockfile_digest": lockfile_digest,
        "contract_digest": contract["contract_digest"],
        "image_tag": image_tag,
        "build_command": build_command,
        "metadata_output": os.fspath(metadata_path),
        "image_built": False,
        "image_published": False,
        "provider_calls_made": False,
        "complete_release_qualification": False,
    }

    temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent))
    try:
        temporary_source = temporary / "source"
        for relative_path, payload in source_payloads.items():
            destination = temporary_source.joinpath(*PurePosixPath(relative_path).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
        _verify_materialized_source(temporary_source, inventory)
        _write_json(temporary / "image-contract.json", contract)
        _write_json(temporary / "build-plan.json", plan)
        os.replace(temporary, output_dir)
    except DiscoveryImageContractError:
        raise
    except (OSError, UnicodeError):
        raise DiscoveryImageContractError("image contract directory could not be written atomically") from None
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)

    return {
        "schema_version": SCHEMA_VERSION,
        "scope": "discovery-seed-image-preparation",
        "result": "passed",
        "source_commit": source_commit,
        "source_tree_digest": source_tree_digest,
        "dockerfile_digest": dockerfile_digest,
        "lockfile_digest": lockfile_digest,
        "contract_digest": contract["contract_digest"],
        "image_tag": image_tag,
        "build_plan": os.fspath(output_dir / "build-plan.json"),
        "image_built": False,
        "image_published": False,
        "provider_calls_made": False,
        "complete_release_qualification": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare an exact-source discovery-seed image build contract",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("command", choices=("prepare",))
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--image-tag", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, default=Path(__file__).resolve().parents[1])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        repository_root = Path(os.path.abspath(os.fspath(args.repository_root.expanduser())))
        report = prepare_contract(
            source_commit=args.source_commit,
            repository_commit=_repository_commit(repository_root),
            image_tag=args.image_tag,
            output_dir=args.output_dir,
            repository_root=repository_root,
        )
    except DiscoveryImageContractError as exc:
        print(f"discovery image contract failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
