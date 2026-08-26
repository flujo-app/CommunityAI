import json
import os
from pathlib import Path

import pytest

from scripts import prepare_qualification_runner as prepare, run_external_model_qualification as external


def _directory(path: Path) -> Path:
    path.mkdir()
    return path


def _arguments(tmp_path: Path, profile: str = "windows-cpu") -> tuple[list[str], dict[str, Path]]:
    runner_root = _directory(tmp_path / "runner")
    launcher = "config.cmd" if profile.startswith("windows-") else "config.sh"
    (runner_root / launcher).write_text("launcher", encoding="utf-8")
    paths = {
        "runner": runner_root,
        "qwen": _directory(tmp_path / "qwen-snapshot"),
        "qwen_cache": _directory(tmp_path / "qwen-cache"),
        "gemma": _directory(tmp_path / "gemma-snapshot"),
        "gemma_cache": _directory(tmp_path / "gemma-cache"),
    }
    args = [
        "--profile",
        profile,
        "--machine-id",
        "qualification-host-a",
        "--runner-root",
        str(paths["runner"]),
        "--qwen-artifact-root",
        str(paths["qwen"]),
        "--qwen-cache-dir",
        str(paths["qwen_cache"]),
        "--gemma-artifact-root",
        str(paths["gemma"]),
        "--gemma-cache-dir",
        str(paths["gemma_cache"]),
    ]
    return args, paths


def _mock_host_validation(monkeypatch, system: str = "windows") -> list[str]:
    device_checks: list[str] = []
    monkeypatch.setattr(external, "normalize_system", lambda: system)
    monkeypatch.setattr(external, "require_device", lambda profile: device_checks.append(profile.device))

    def snapshot(candidate, artifact_root):
        name = next(name for name, value in external.CANDIDATES.items() if value == candidate)
        return {
            "manifest_digest": f"sha256:{name}",
            "artifact_layout_verified": True,
            "artifact_count": 8,
            "declared_artifact_bytes": 123,
        }

    monkeypatch.setattr(external, "preflight_candidate_snapshot", snapshot)
    return device_checks


def test_prepare_runner_validates_both_candidates_and_writes_only_managed_environment(tmp_path, monkeypatch, capsys):
    args, paths = _arguments(tmp_path)
    (paths["runner"] / ".env").write_text("LANG=en_US.UTF-8\n", encoding="utf-8")
    device_checks = _mock_host_validation(monkeypatch)
    monkeypatch.setenv("QUALIFICATION_REGISTRATION_TOKEN", "private-token-sentinel")

    assert prepare.main(args) == 0

    report_text = capsys.readouterr().out
    report = json.loads(report_text)
    environment = (paths["runner"] / ".env").read_text(encoding="utf-8")
    assert report["result"] == "passed"
    assert report["profile"] == "windows-cpu"
    assert report["registration_labels"] == ["model-qualification", "windows-cpu"]
    assert set(report["candidate_snapshots"]) == {"qwen3.5-2b", "gemma-4-e2b"}
    assert report["environment_file_status"] == "updated"
    assert report["qualification_evidence"] is False
    assert report["complete_release_qualification"] is False
    assert device_checks == ["cpu"]
    assert environment.splitlines() == [
        "LANG=en_US.UTF-8",
        "COMMUNITYAI_QUALIFICATION_MACHINE_ID=qualification-host-a",
        f"COMMUNITYAI_QWEN35_2B_ARTIFACT_ROOT={paths['qwen'].resolve()}",
        f"COMMUNITYAI_QWEN35_2B_CACHE_DIR={paths['qwen_cache'].resolve()}",
        f"COMMUNITYAI_GEMMA4_E2B_ARTIFACT_ROOT={paths['gemma'].resolve()}",
        f"COMMUNITYAI_GEMMA4_E2B_CACHE_DIR={paths['gemma_cache'].resolve()}",
    ]
    for private_value in (
        "qualification-host-a",
        os.fspath(paths["runner"]),
        os.fspath(paths["qwen"]),
        os.fspath(paths["gemma"]),
        "private-token-sentinel",
    ):
        assert private_value not in report_text


def test_prepare_runner_is_idempotent_and_replaces_stale_managed_values(tmp_path, monkeypatch, capsys):
    args, paths = _arguments(tmp_path)
    _mock_host_validation(monkeypatch)
    environment_path = paths["runner"] / ".env"
    environment_path.write_text(
        "COMMUNITYAI_QUALIFICATION_MACHINE_ID=stale-host\n"
        "LANG=C.UTF-8\n"
        "COMMUNITYAI_QWEN35_2B_ARTIFACT_ROOT=C:\\stale\n",
        encoding="utf-8",
    )

    assert prepare.main(args) == 0
    first = environment_path.read_text(encoding="utf-8")
    assert json.loads(capsys.readouterr().out)["environment_file_status"] == "updated"

    assert prepare.main(args) == 0
    second = environment_path.read_text(encoding="utf-8")
    assert json.loads(capsys.readouterr().out)["environment_file_status"] == "unchanged"
    assert first == second
    assert second.count("COMMUNITYAI_QUALIFICATION_MACHINE_ID=") == 1
    assert second.startswith("LANG=C.UTF-8\n")


def test_prepare_runner_rejects_wrong_operating_system_without_writing(tmp_path, monkeypatch):
    args, paths = _arguments(tmp_path)
    _mock_host_validation(monkeypatch, system="linux")

    with pytest.raises(prepare.RunnerPreparationError, match="operating system"):
        prepare.main(args)

    assert not (paths["runner"] / ".env").exists()


def test_prepare_runner_rejects_missing_or_symlinked_runner_launcher(tmp_path, monkeypatch):
    args, paths = _arguments(tmp_path)
    _mock_host_validation(monkeypatch)
    (paths["runner"] / "config.cmd").unlink()

    with pytest.raises(prepare.RunnerPreparationError, match="configuration launcher"):
        prepare.main(args)

    if hasattr(os, "symlink"):
        target = tmp_path / "launcher-target"
        target.write_text("launcher", encoding="utf-8")
        try:
            os.symlink(target, paths["runner"] / "config.cmd")
        except OSError:
            pytest.skip("symlinks unavailable")
        with pytest.raises(prepare.RunnerPreparationError, match="configuration launcher"):
            prepare.main(args)


def test_prepare_runner_rejects_invalid_machine_id_and_relative_paths(tmp_path, monkeypatch):
    args, _ = _arguments(tmp_path)
    _mock_host_validation(monkeypatch)
    invalid_machine = list(args)
    invalid_machine[invalid_machine.index("qualification-host-a")] = "private host path"

    with pytest.raises(prepare.RunnerPreparationError, match="machine id"):
        prepare.main(invalid_machine)

    relative_root = list(args)
    relative_root[relative_root.index("--qwen-artifact-root") + 1] = "relative-snapshot"
    with pytest.raises(prepare.RunnerPreparationError, match="Qwen snapshot"):
        prepare.main(relative_root)


def test_prepare_runner_rejects_malformed_existing_environment_without_changing_it(tmp_path, monkeypatch):
    args, paths = _arguments(tmp_path)
    _mock_host_validation(monkeypatch)
    environment_path = paths["runner"] / ".env"
    original = "LANG=C\nnot-an-environment-entry\n"
    environment_path.write_text(original, encoding="utf-8")

    with pytest.raises(prepare.RunnerPreparationError, match="NAME=value"):
        prepare.main(args)

    assert environment_path.read_text(encoding="utf-8") == original


def test_prepare_runner_rejects_duplicate_existing_environment_names(tmp_path, monkeypatch):
    args, paths = _arguments(tmp_path)
    _mock_host_validation(monkeypatch)
    (paths["runner"] / ".env").write_text("LANG=C\nLANG=en_US.UTF-8\n", encoding="utf-8")

    with pytest.raises(prepare.RunnerPreparationError, match="repeats LANG"):
        prepare.main(args)


def test_prepare_runner_rejects_oversized_existing_environment_before_reading(tmp_path, monkeypatch):
    args, paths = _arguments(tmp_path)
    _mock_host_validation(monkeypatch)
    environment_path = paths["runner"] / ".env"
    environment_path.write_bytes(b"A" * (prepare.MAX_ENVIRONMENT_BYTES + 1))

    with pytest.raises(prepare.RunnerPreparationError, match="size limit"):
        prepare.main(args)

    assert environment_path.stat().st_size == prepare.MAX_ENVIRONMENT_BYTES + 1


def test_prepare_runner_rejects_symlinked_environment_file(tmp_path, monkeypatch):
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    args, paths = _arguments(tmp_path)
    _mock_host_validation(monkeypatch)
    target = tmp_path / "private-environment"
    target.write_text("LANG=C\n", encoding="utf-8")
    try:
        os.symlink(target, paths["runner"] / ".env")
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(prepare.RunnerPreparationError, match="symbolic link"):
        prepare.main(args)

    assert target.read_text(encoding="utf-8") == "LANG=C\n"


def test_prepare_runner_does_not_write_when_candidate_snapshot_fails(tmp_path, monkeypatch):
    args, paths = _arguments(tmp_path)
    monkeypatch.setattr(external, "normalize_system", lambda: "windows")
    monkeypatch.setattr(external, "require_device", lambda profile: None)

    def fail_snapshot(candidate, artifact_root):
        raise external.ExternalQualificationError("candidate snapshot layout does not match the exact manifest")

    monkeypatch.setattr(external, "preflight_candidate_snapshot", fail_snapshot)

    with pytest.raises(prepare.RunnerPreparationError, match="snapshot layout"):
        prepare.main(args)

    assert not (paths["runner"] / ".env").exists()


def test_prepare_cuda_runner_checks_cuda_device(tmp_path, monkeypatch):
    args, paths = _arguments(tmp_path, profile="linux-cuda")
    device_checks = _mock_host_validation(monkeypatch, system="linux")

    assert prepare.main(args) == 0

    assert device_checks == ["cuda"]
    assert (paths["runner"] / ".env").is_file()
