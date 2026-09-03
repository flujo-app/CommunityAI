from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from desktop import build_desktop
from scripts import gateq38_route_controller as controller, gateq38_stage_package as stage

SOURCE_COMMIT = "a" * 40
SOURCE_TREE = "b" * 40
SOURCE_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = SOURCE_ROOT / "manifests" / "candidates" / "qwen3.8-27b-fp8-dequant-eager.json"


def _runtime_metrics(
    root: Path,
    bundle: Path,
    summary: dict[str, object],
) -> dict[str, object]:
    provenance = json.loads((root / build_desktop.PROVENANCE_NAME).read_text(encoding="utf-8"))
    install_platform = summary["install_archive"]["platform"]
    executable_suffix = ".exe" if install_platform == "Windows" else ""
    bundle_bytes, file_count = build_desktop._directory_metrics(bundle)
    node_root = bundle / build_desktop.NODE_DIRECTORY
    node_bytes, node_files = build_desktop._directory_metrics(node_root)
    return {
        "schema_version": 1,
        "application": build_desktop.APP_NAME,
        "package": "communityai-desktop",
        "platform": provenance["build_platform"],
        "python": provenance["build_python"],
        "bundle_bytes": bundle_bytes,
        "file_count": file_count,
        "runtime": {
            "shell": "pyside",
            "framework": "PySide6",
            "version": "6.9.0",
        },
        "acceptance": {
            "api_version": 1,
            "model_count": 3,
            "worker_actions": 3,
            "key_lifecycle": "passed",
            "contribution_policy": "passed",
            "policy_update": "passed",
            "auto_selection": "passed",
        },
        "ui_smoke_passed": True,
        "onboarding_ui_smoke_passed": True,
        "node_sidecar": {
            "relative_executable": f"node/CommunityAI-Node{executable_suffix}",
            "bundle_bytes": node_bytes,
            "file_count": node_files,
            "runtime": {
                "schema_version": 1,
                "application": "CommunityAI-Node",
                "drift": "0.1.0",
                "torch": "2.6.0+cu124",
                "transformers": "4.55.4",
                "hivemind": "1.1.12",
                "fastapi": "0.116.1",
                "uvicorn": "0.35.0",
                "keyring": "25.6.0",
                "p2pd": f"p2pd{executable_suffix}",
                "catalog_bootstrap_schema": 1,
                "frozen": True,
            },
            "worker_runtime": {
                "schema_version": 1,
                "application": "CommunityAI-Worker",
                "entrypoint": "server",
                "server_class": "Server",
                "model_loading_performed": False,
                "network_join_performed": False,
                "throughput_mode": "dry_run",
                "training_rpcs_enabled": False,
                "process_lifetime_guard_armed": True,
                "frozen": True,
            },
            "self_test_passed": True,
            "worker_self_test_passed": True,
            "node_entrypoint_smoke_passed": True,
            "worker_entrypoint_smoke_passed": True,
        },
        "console_window": install_platform != "Windows",
        "signed": False,
        "catalog_bootstrap_bundled": False,
        "catalog_publication_bundle": None,
        "release_artifacts": summary,
    }


def _release_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    extra_node: dict[str, bytes] | None = None,
    install_platform: str = "Linux",
    outside_node_symlink: bool = False,
    node_mode: int = 0o755,
) -> Path:
    build_platform = "Windows-test" if install_platform == "Windows" else "Linux-test"
    monkeypatch.setattr(build_desktop.platform, "platform", lambda: build_platform)
    root = tmp_path / "release"
    bundle = root / build_desktop.APP_NAME
    executable_suffix = ".exe" if install_platform == "Windows" else ""
    files = {
        f"CommunityAI{executable_suffix}": b"desktop\n",
        f"node/CommunityAI-Node{executable_suffix}": b"node executable\n",
        "node/_internal/python-runtime.bin": b"sidecar\x00",
        "node/_internal/drift/runtime.pyc": b"bytecode\x00",
    }
    files.update(extra_node or {})
    for relative, payload in files.items():
        target = bundle / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    if outside_node_symlink:
        (bundle / "shared.so").write_bytes(b"shared")
        try:
            (bundle / "node" / "_internal" / "shared-link.so").symlink_to("../../shared.so")
        except OSError:
            pytest.skip("file symlink creation is unavailable")
    (bundle / f"CommunityAI{executable_suffix}").chmod(0o755)
    node_executable = bundle / "node" / f"CommunityAI-Node{executable_suffix}"
    node_executable.chmod(node_mode)
    if install_platform == "Linux":
        native_bundle_artifacts = build_desktop._bundle_artifacts

        def linux_bundle_artifacts(bundle_root: Path) -> list[dict[str, object]]:
            artifacts = native_bundle_artifacts(bundle_root)
            for artifact in artifacts:
                if artifact["path"] == "CommunityAI/CommunityAI":
                    artifact["mode"] = 0o755
                elif artifact["path"] == "CommunityAI/node/CommunityAI-Node":
                    artifact["mode"] = node_mode
            return artifacts

        monkeypatch.setattr(build_desktop, "_bundle_artifacts", linux_bundle_artifacts)
    summary = build_desktop._write_release_attestations(
        root,
        bundle,
        source_commit=SOURCE_COMMIT,
        source_tree=SOURCE_TREE,
        build_workflow="desktop.yaml@refs/heads/test",
        build_pyinstaller="6.11.1",
        publication_evidence=None,
        install_platform=install_platform,
    )
    build_desktop._write_desktop_metrics(
        root,
        _runtime_metrics(root, bundle, summary),
    )
    build_desktop._verify_release_attestations(
        root,
        expected_source_commit=SOURCE_COMMIT,
        require_metrics=True,
    )
    return root


def _validate(
    root: Path,
    manifest: Path = MANIFEST,
    *,
    source_commit: str = SOURCE_COMMIT,
    source_tree: str = SOURCE_TREE,
    protection_verifier: stage.ProtectionVerifier | None = None,
) -> dict[str, object]:
    bindings = []
    for relative in (
        controller.DESKTOP_RELEASE_VERIFIER_SOURCE_PATH,
        controller.STAGE_PACKAGE_SOURCE_PATH,
    ):
        payload = (SOURCE_ROOT / relative).read_bytes()
        bindings.append(
            {
                "relative_path": relative,
                "sha256": stage._sha256(payload),
                "byte_size": len(payload),
            }
        )
    return stage.validate_release_root(
        root,
        manifest,
        expected_source_commit=source_commit,
        expected_source_tree=source_tree,
        source_root=SOURCE_ROOT,
        source_bindings=bindings,
        protection_verifier=protection_verifier or (lambda _path, _directory: None),
    )


def test_binds_complete_linux_node_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _release_root(tmp_path, monkeypatch)

    record = _validate(root)

    assert record["platform"] == "linux"
    assert record["source_commit"] == SOURCE_COMMIT
    assert record["source_tree"] == SOURCE_TREE
    assert record["manifest_digest"] == controller.EXPECTED_MANIFEST_DIGEST
    assert record["manifest_sha256"].startswith("sha256:")
    assert record["node_executable"] == "CommunityAI/node/CommunityAI-Node"
    assert record["node_runtime_entry_count"] == 3
    assert record["node_runtime_bytes"] == sum(
        path.stat().st_size for path in (root / "CommunityAI" / "node").rglob("*") if path.is_file()
    )
    assert (
        stage.validate_record(
            record,
            expected_source_commit=SOURCE_COMMIT,
            expected_source_tree=SOURCE_TREE,
            expected_manifest_digest=controller.EXPECTED_MANIFEST_DIGEST,
        )
        == record
    )


def test_archive_hashing_uses_bounded_streaming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _release_root(tmp_path, monkeypatch)
    archive_name = "communityai-desktop-linux.tar.gz"
    native_read_bytes = Path.read_bytes
    native_os_read = os.read
    read_sizes: list[int] = []

    def reject_archive_read_bytes(path: Path) -> bytes:
        if path.name == archive_name:
            raise AssertionError("release archive must not use Path.read_bytes")
        return native_read_bytes(path)

    def bounded_read(descriptor: int, byte_count: int) -> bytes:
        read_sizes.append(byte_count)
        assert byte_count <= stage.HASH_CHUNK_BYTES
        return native_os_read(descriptor, byte_count)

    monkeypatch.setattr(Path, "read_bytes", reject_archive_read_bytes)
    monkeypatch.setattr(os, "read", bounded_read)

    record = _validate(root)

    assert record["release_archive_bytes"] == (root / archive_name).stat().st_size
    assert read_sizes
    assert set(read_sizes) == {stage.HASH_CHUNK_BYTES}


def test_archive_replacement_during_streaming_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _release_root(tmp_path, monkeypatch)
    archive = root / "communityai-desktop-linux.tar.gz"
    replacement = root / "replacement.tar.gz"
    replacement.write_bytes(archive.read_bytes())
    native_os_read = os.read
    replaced = False

    def replace_then_read(descriptor: int, byte_count: int) -> bytes:
        nonlocal replaced
        if not replaced:
            replaced = True
            os.replace(replacement, archive)
        return native_os_read(descriptor, byte_count)

    monkeypatch.setattr(os, "read", replace_then_read)

    with pytest.raises(stage.Q38StagePackageError, match="changed|safely"):
        _validate(root)
    assert replaced


@pytest.mark.parametrize(
    "node_mode",
    (0o100, 0o644, 0o700, 0o757, 0o775, 0o2755, 0o4755),
)
def test_rejects_unsafe_or_inaccessible_node_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    node_mode: int,
) -> None:
    root = _release_root(tmp_path, monkeypatch, node_mode=node_mode)

    with pytest.raises(stage.Q38StagePackageError, match="mode is not 0755"):
        _validate(root)


def test_requires_protection_for_the_complete_release_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _release_root(tmp_path, monkeypatch)
    calls: list[tuple[Path, bool]] = []

    def verify(path: Path, directory: bool) -> None:
        calls.append((path.resolve(), directory))

    _validate(root, protection_verifier=verify)

    assert (MANIFEST.resolve(), False) in calls
    assert (root.resolve(), True) in calls
    assert (
        (root / "CommunityAI" / "node" / "_internal" / "python-runtime.bin").resolve(),
        False,
    ) in calls

    def reject_sidecar(path: Path, _directory: bool) -> None:
        if path.name == "python-runtime.bin":
            raise stage.Q38StagePackageError("sidecar protection failed")

    with pytest.raises(stage.Q38StagePackageError, match="sidecar protection failed"):
        _validate(root, protection_verifier=reject_sidecar)


@pytest.mark.parametrize("mutation", ["missing", "extra", "changed"])
def test_release_inventory_mutation_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    root = _release_root(tmp_path, monkeypatch)
    sidecar = root / "CommunityAI" / "node" / "_internal" / "python-runtime.bin"
    if mutation == "missing":
        sidecar.unlink()
    elif mutation == "extra":
        (sidecar.parent / "extra.bin").write_bytes(b"extra")
    else:
        sidecar.write_bytes(b"changed")

    with pytest.raises(stage.Q38StagePackageError, match="attestations"):
        _validate(root)


def test_rejects_wrong_source_or_platform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _release_root(tmp_path, monkeypatch)

    with pytest.raises(stage.Q38StagePackageError, match="attestations"):
        _validate(root, source_commit="c" * 40)

    other = _release_root(
        tmp_path / "windows",
        monkeypatch,
        install_platform="Windows",
    )
    with pytest.raises(stage.Q38StagePackageError, match="Linux production"):
        _validate(other)


@pytest.mark.parametrize(
    "weight_name",
    (
        "layers-0.safetensors",
        "model-00001-of-00002.safetensors",
        "renamed-weight.gguf",
        "pytorch_model-00001-of-00002.bin",
    ),
)
def test_rejects_model_weights_inside_node_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    weight_name: str,
) -> None:
    root = _release_root(
        tmp_path,
        monkeypatch,
        extra_node={f"node/_internal/{weight_name}": b"weight"},
    )

    with pytest.raises(stage.Q38StagePackageError, match="model weights"):
        _validate(root)


def test_rejects_wrong_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _release_root(tmp_path, monkeypatch)
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")

    with pytest.raises(stage.Q38StagePackageError, match="manifest"):
        _validate(root, manifest)


def test_rejects_node_symlink_outside_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX release symlink semantics require native Linux")
    root = _release_root(
        tmp_path,
        monkeypatch,
        outside_node_symlink=True,
    )

    with pytest.raises(stage.Q38StagePackageError, match="symlink escapes"):
        _validate(root)


@pytest.mark.parametrize("target", ("provenance", "archive", "runtime"))
def test_rejects_mutation_after_release_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    root = _release_root(tmp_path, monkeypatch)
    verify = build_desktop._verify_release_attestations

    def verified_then_mutated(*args: object, **kwargs: object) -> dict[str, object]:
        result = verify(*args, **kwargs)
        if target == "provenance":
            path = root / build_desktop.PROVENANCE_NAME
            value = json.loads(path.read_text(encoding="utf-8"))
            value["artifacts"].append(
                {
                    "kind": "file",
                    "mode": 0o644,
                    "path": "CommunityAI/node/_internal/phantom.bin",
                    "sha256": "0" * 64,
                    "size_bytes": 1,
                }
            )
            path.write_bytes(build_desktop._canonical_json(value).encode("utf-8"))
        elif target == "archive":
            archive = result["install_archive"]["path"]
            with (root / archive).open("ab") as stream:
                stream.write(b"x")
        else:
            sidecar = root / "CommunityAI" / "node" / "_internal" / "python-runtime.bin"
            sidecar.write_bytes(b"mutated")
        return result

    monkeypatch.setattr(
        build_desktop,
        "_verify_release_attestations",
        verified_then_mutated,
    )

    with pytest.raises(
        stage.Q38StagePackageError,
        match="changed|binding|identity",
    ):
        _validate(root)


def test_rejects_unbound_verifier_source() -> None:
    bindings = []
    for relative in (
        controller.STAGE_PACKAGE_SOURCE_PATH,
        controller.DESKTOP_RELEASE_VERIFIER_SOURCE_PATH,
    ):
        payload = (SOURCE_ROOT / relative).read_bytes()
        bindings.append(
            {
                "relative_path": relative,
                "sha256": stage._sha256(payload),
                "byte_size": len(payload),
            }
        )

    with pytest.raises(stage.Q38StagePackageError, match="not plan-bound"):
        stage._assert_verifier_sources(SOURCE_ROOT, bindings[:1])

    bindings[1]["sha256"] = "sha256:" + "0" * 64
    with pytest.raises(stage.Q38StagePackageError, match="binding changed"):
        stage._assert_verifier_sources(SOURCE_ROOT, bindings)


def test_record_digest_rejects_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _release_root(tmp_path, monkeypatch)
    record = _validate(root)
    record["node_runtime_bytes"] += 1

    with pytest.raises(stage.Q38StagePackageError, match="record digest"):
        stage.validate_record(record)


def test_atomic_record_round_trip_and_unsafe_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _release_root(tmp_path, monkeypatch)
    record = _validate(root)
    output = tmp_path / "records" / "runtime.json"

    stage._atomic_record(output, record)

    assert json.loads(output.read_text(encoding="utf-8")) == record
    output.unlink()
    output.mkdir()
    with pytest.raises(stage.Q38StagePackageError, match="target is unsafe"):
        stage._atomic_record(output, record)
