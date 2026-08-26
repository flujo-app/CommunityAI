import json
import platform
from pathlib import Path

import pytest

from scripts import run_external_model_qualification as external_runner

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "qualify-model-matrix.yaml"


def _option(arguments: list[str], name: str) -> str:
    return arguments[arguments.index(name) + 1]


def test_external_runner_binds_host_profile_identity_and_exact_paths(tmp_path, monkeypatch):
    artifact_root = tmp_path / "snapshot"
    cache_dir = tmp_path / "hub"
    output = tmp_path / "reports" / "linux-cpu.json"
    artifact_root.mkdir()
    cache_dir.mkdir()
    captured: list[str] = []

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setenv("COMMUNITYAI_QUALIFICATION_MACHINE_ID", "linux-edge-a")
    monkeypatch.setenv("COMMUNITYAI_QWEN35_2B_ARTIFACT_ROOT", str(artifact_root))
    monkeypatch.setenv("COMMUNITYAI_QWEN35_2B_CACHE_DIR", str(cache_dir))
    monkeypatch.setattr(external_runner, "infer_source_commit", lambda: "a" * 40)
    monkeypatch.setattr(external_runner, "require_device", lambda profile: None)
    monkeypatch.setattr(external_runner, "qualify_main", lambda arguments: captured.extend(arguments) or 0)

    assert (
        external_runner.main(
            [
                "--candidate",
                "qwen3.5-2b",
                "--profile",
                "linux-cpu",
                "--source-commit",
                "a" * 40,
                "--output",
                str(output),
            ]
        )
        == 0
    )

    assert Path(captured[0]).name == "qwen3.5-2b-bfloat16-eager.json"
    assert _option(captured, "--artifact-root") == str(artifact_root)
    assert _option(captured, "--cache-dir") == str(cache_dir)
    assert _option(captured, "--device") == "cpu"
    assert _option(captured, "--machine-id") == "linux-edge-a"
    assert _option(captured, "--source-commit") == "a" * 40
    assert _option(captured, "--output") == str(output)
    assert "--with-failover" in captured


def test_external_runner_rejects_a_mislabelled_operating_system(tmp_path, monkeypatch):
    artifact_root = tmp_path / "snapshot"
    artifact_root.mkdir()
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setenv("COMMUNITYAI_QUALIFICATION_MACHINE_ID", "host-a")
    monkeypatch.setenv("COMMUNITYAI_QWEN35_2B_ARTIFACT_ROOT", str(artifact_root))

    with pytest.raises(external_runner.ExternalQualificationError, match="not the claimed 'linux'"):
        external_runner.main(
            [
                "--candidate",
                "qwen3.5-2b",
                "--profile",
                "linux-cpu",
                "--source-commit",
                "a" * 40,
                "--output",
                str(tmp_path / "report.json"),
            ]
        )


def test_external_runner_requires_private_runner_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.delenv("COMMUNITYAI_QUALIFICATION_MACHINE_ID", raising=False)
    monkeypatch.delenv("COMMUNITYAI_GEMMA4_E2B_ARTIFACT_ROOT", raising=False)

    with pytest.raises(external_runner.ExternalQualificationError, match="MACHINE_ID"):
        external_runner.main(
            [
                "--candidate",
                "gemma-4-e2b",
                "--profile",
                "linux-cpu",
                "--source-commit",
                "a" * 40,
                "--output",
                str(tmp_path / "report.json"),
            ]
        )


def test_external_runner_rejects_a_source_commit_not_matching_the_checkout(tmp_path, monkeypatch):
    artifact_root = tmp_path / "snapshot"
    artifact_root.mkdir()
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setenv("COMMUNITYAI_QUALIFICATION_MACHINE_ID", "linux-edge-a")
    monkeypatch.setenv("COMMUNITYAI_QWEN35_2B_ARTIFACT_ROOT", str(artifact_root))
    monkeypatch.setattr(external_runner, "infer_source_commit", lambda: "b" * 40)

    with pytest.raises(external_runner.ExternalQualificationError, match="checkout does not match"):
        external_runner.main(
            [
                "--candidate",
                "qwen3.5-2b",
                "--profile",
                "linux-cpu",
                "--source-commit",
                "a" * 40,
                "--output",
                str(tmp_path / "report.json"),
            ]
        )


def test_manual_workflow_covers_strict_and_incomplete_declared_profile_sets():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    for profile in external_runner.HOST_PROFILES:
        assert f'"{profile}"' in workflow
    assert "default: strict-six-profile" in workflow
    assert "incomplete-windows-linux" in workflow
    assert workflow.count("profile: ${{ fromJSON(needs.scope.outputs.profiles) }}") == 2
    assert "runs-on:\n      - self-hosted\n      - model-qualification" in workflow
    assert 'HF_HUB_OFFLINE: "1"' in workflow
    assert 'TRANSFORMERS_OFFLINE: "1"' in workflow
    assert "actions/upload-artifact@v7" in workflow
    assert "actions/download-artifact@v8" in workflow
    assert "QUALIFICATION_RUNNER_READ_TOKEN" not in workflow
    assert "scripts/validate_qualification_runner_fleet.py" not in workflow
    assert "--preflight-only" in workflow
    assert "needs: scope" in workflow
    assert "needs:\n      - scope\n      - preflight" in workflow
    assert "incomplete_args+=(--allow-incomplete)" in workflow
    assert '--require-source-commit "$GITHUB_SHA"' in workflow
    assert "continue-on-error: true" in workflow


def test_manual_workflow_bootstraps_patched_hivemind_before_windows_preflight_and_qualification():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    preflight_job = workflow.index("\n  preflight:\n")
    preflight_sync = workflow.index("- name: Install locked dependencies", preflight_job)
    preflight_setup_go = workflow.index("- name: Install Go for patched Windows Hivemind runtime", preflight_job)
    preflight_build = workflow.index("- name: Build patched Windows Hivemind runtime", preflight_job)
    validate = workflow.index("- name: Validate host configuration and device", preflight_job)

    qualify_job = workflow.index("\n  qualify:\n")
    qualify_sync = workflow.index("- name: Install locked dependencies", qualify_job)
    qualify_setup_go = workflow.index("- name: Install Go for patched Windows Hivemind runtime", qualify_job)
    qualify_build = workflow.index("- name: Build patched Windows Hivemind runtime", qualify_job)
    qualify = workflow.index("- name: Run full-artifact parity and selected-worker recovery", qualify_job)

    assert workflow.count("actions/setup-go@v6") == 2
    assert workflow.count('uv pip install --no-deps "patch==1.16"') == 2
    assert workflow.count("scripts/build_hivemind_windows.py") == 2
    assert workflow.count("uv pip install --no-deps $wheel.FullName") == 2
    assert preflight_sync < preflight_setup_go < preflight_build < validate < qualify_job
    assert qualify_sync < qualify_setup_go < qualify_build < qualify
    assert "uv run --no-sync python scripts/run_external_model_qualification.py" in workflow


def test_manual_workflow_aggregates_in_the_locked_project_environment():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    aggregate_job = workflow.index("\n  aggregate:\n")
    aggregate = workflow[aggregate_job:]

    assert "uv sync --extra dev --frozen --python 3.12" in aggregate
    assert "uv run --no-sync python -c 'import drift; print(drift.__version__)'" in aggregate
    assert "uv run --no-sync python scripts/aggregate_model_qualification.py" in aggregate
    assert "--no-project" not in aggregate
    assert "--with packaging" not in aggregate


def test_external_runner_preflight_checks_host_without_starting_qualification(tmp_path, monkeypatch, capsys):
    artifact_root = tmp_path / "snapshot"
    artifact_root.mkdir()
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setenv("COMMUNITYAI_QUALIFICATION_MACHINE_ID", "linux-edge-a")
    monkeypatch.setenv("COMMUNITYAI_QWEN35_2B_ARTIFACT_ROOT", str(artifact_root))
    monkeypatch.setattr(external_runner, "infer_source_commit", lambda: "a" * 40)
    monkeypatch.setattr(external_runner, "require_device", lambda profile: None)
    monkeypatch.setattr(
        external_runner,
        "preflight_candidate_snapshot",
        lambda candidate, root: {
            "manifest_digest": "sha256:" + "c" * 64,
            "artifact_layout_verified": True,
            "artifact_count": 12,
            "declared_artifact_bytes": 34,
        },
    )
    monkeypatch.setattr(
        external_runner,
        "qualify_main",
        lambda arguments: pytest.fail("preflight must not start the qualification harness"),
    )

    assert (
        external_runner.main(
            [
                "--candidate",
                "qwen3.5-2b",
                "--profile",
                "linux-cuda",
                "--source-commit",
                "a" * 40,
                "--preflight-only",
            ]
        )
        == 0
    )

    readiness = json.loads(capsys.readouterr().out)
    assert readiness["scope"] == "qualification-host-readiness"
    assert readiness["profile"] == "linux-cuda"
    assert readiness["machine_id"] == "linux-edge-a"
    assert readiness["artifact_layout_verified"] is True
    assert readiness["artifact_count"] == 12
    assert readiness["declared_artifact_bytes"] == 34
    assert readiness["qualification_evidence"] is False
    assert readiness["complete_release_qualification"] is False
    assert str(artifact_root) not in str(readiness)


def test_external_runner_preflight_rejects_an_incomplete_snapshot_without_leaking_its_path(tmp_path, monkeypatch):
    artifact_root = tmp_path / "private-snapshot"
    artifact_root.mkdir()
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setenv("COMMUNITYAI_QUALIFICATION_MACHINE_ID", "linux-edge-a")
    monkeypatch.setenv("COMMUNITYAI_QWEN35_2B_ARTIFACT_ROOT", str(artifact_root))
    monkeypatch.setattr(external_runner, "infer_source_commit", lambda: "a" * 40)

    with pytest.raises(external_runner.ExternalQualificationError) as raised:
        external_runner.main(
            [
                "--candidate",
                "qwen3.5-2b",
                "--profile",
                "linux-cpu",
                "--source-commit",
                "a" * 40,
                "--preflight-only",
            ]
        )

    assert str(raised.value) == "candidate snapshot layout does not match the exact manifest"
    assert str(artifact_root) not in str(raised.value)


def test_external_runner_requires_output_outside_preflight():
    with pytest.raises(external_runner.ExternalQualificationError, match="--output is required"):
        external_runner.main(
            [
                "--candidate",
                "qwen3.5-2b",
                "--profile",
                "linux-cpu",
                "--source-commit",
                "a" * 40,
            ]
        )
