import copy
import json
from pathlib import Path

from drift.model_manifest import ModelManifest
from scripts.aggregate_model_qualification import main as aggregate_main
from scripts.qualify_model_manifest import extract_smoke_evidence, main as local_main

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VECTOR_MANIFEST = REPOSITORY_ROOT / "tests" / "data" / "model_manifest_v1_vector.json"


def _local_report(manifest: ModelManifest, *, system: str, device: str, machine_id: str) -> dict:
    runtime_evidence = {
        "stock_token_parity": True,
        "manifested_route_completed": True,
        "worker_device": device,
        "worker_torch_dtype": manifest.runtime.dtype,
        "attention_implementation": manifest.runtime.attention_implementation,
    }
    return {
        "schema_version": 1,
        "generated_at": "2026-08-25T00:00:00+00:00",
        "scope": "single-machine-local",
        "model": {
            "name": manifest.name,
            "repository": manifest.source.repository,
            "revision": manifest.source.revision,
            "manifest_digest": manifest.digest_id,
            "runtime": manifest.runtime.to_dict(),
        },
        "environment": {
            "platform": system,
            "system": system,
            "device_profile": device,
            "machine_id": machine_id,
            "source_commit": "a" * 40,
            "drift": "2.3.0.dev2",
        },
        "requested": {
            "artifact_verification": True,
            "local_parity": True,
            "local_failover": True,
            "device": device,
        },
        "stages": [
            {
                "name": "manifest_and_artifacts",
                "status": "passed",
                "evidence": {
                    "artifacts_verified": True,
                    "manifest_digest": manifest.digest_id,
                },
            },
            {
                "name": "local_distributed_stock_parity",
                "status": "passed",
                "evidence": dict(runtime_evidence),
            },
            {
                "name": "local_in_generation_failover",
                "status": "passed",
                "evidence": {
                    **runtime_evidence,
                    "selected_worker_interrupted": True,
                    "recovery_observed": True,
                },
            },
        ],
        "result": "passed",
        "complete_release_qualification": False,
    }


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_matrix_requires_exact_profiles_and_remains_partial(tmp_path):
    manifest = ModelManifest.load(VECTOR_MANIFEST)
    windows_report = tmp_path / "windows-cpu.json"
    linux_report = tmp_path / "linux-cuda.json"
    output = tmp_path / "matrix.json"
    _write_json(windows_report, _local_report(manifest, system="windows", device="cpu", machine_id="win-a"))
    _write_json(linux_report, _local_report(manifest, system="linux", device="cuda", machine_id="linux-a"))

    assert (
        aggregate_main(
            [
                str(VECTOR_MANIFEST),
                str(windows_report),
                str(linux_report),
                "--require-profile",
                "windows:cpu",
                "--require-profile",
                "linux:cuda",
                "--require-source-commit",
                "a" * 40,
                "--output",
                str(output),
            ]
        )
        == 0
    )

    serialized = output.read_text(encoding="utf-8")
    matrix = json.loads(serialized)
    assert matrix["result"] == "passed"
    assert matrix["missing_profiles"] == []
    assert matrix["report_errors"] == []
    assert matrix["matrix_errors"] == []
    assert matrix["source_identity"]["source_commit"] == "a" * 40
    assert matrix["source_identity"]["drift"] == "2.3.0.dev2"
    assert matrix["coverage"]["windows:cpu"][0]["machine_id"] == "win-a"
    assert matrix["coverage"]["windows:cpu"][0]["report"] == "input-1"
    assert "path" not in matrix["coverage"]["windows:cpu"][0]
    assert matrix["coverage"]["linux:cuda"][0]["machine_id"] == "linux-a"
    assert str(tmp_path) not in serialized
    assert matrix["complete_release_qualification"] is False
    assert "multi-machine routing and interruption recovery" in matrix["not_covered"]


def test_matrix_treats_exact_windows_linux_alpha_profiles_as_a_strict_pass(tmp_path):
    manifest = ModelManifest.load(VECTOR_MANIFEST)
    profiles = ("windows:cpu", "windows:cuda", "linux:cpu", "linux:cuda")
    reports = []
    for profile in profiles:
        system, device = profile.split(":")
        report_path = tmp_path / f"{system}-{device}.json"
        _write_json(
            report_path,
            _local_report(
                manifest,
                system=system,
                device=device,
                machine_id=f"{system}-{device}",
            ),
        )
        reports.append(report_path)

    def matrix_args(selected_reports):
        args = [str(VECTOR_MANIFEST), *(str(report) for report in selected_reports)]
        for profile in profiles:
            args.extend(("--require-profile", profile))
        args.extend(("--require-source-commit", "a" * 40))
        return args

    alpha_output = tmp_path / "public-alpha-matrix.json"
    assert aggregate_main([*matrix_args(reports), "--output", str(alpha_output)]) == 0
    alpha = json.loads(alpha_output.read_text(encoding="utf-8"))
    assert alpha["result"] == "passed"
    assert alpha["missing_profiles"] == []
    assert set(alpha["coverage"]) == set(profiles)
    assert alpha["complete_release_qualification"] is False

    missing_output = tmp_path / "missing-profile-matrix.json"
    assert aggregate_main([*matrix_args(reports[:-1]), "--output", str(missing_output)]) == 1
    missing = json.loads(missing_output.read_text(encoding="utf-8"))
    assert missing["result"] == "failed"
    assert missing["missing_profiles"] == ["linux:cuda"]

    macos_report = tmp_path / "macos-cpu.json"
    _write_json(
        macos_report,
        _local_report(manifest, system="macos", device="cpu", machine_id="deferred-macos-cpu"),
    )
    extra_output = tmp_path / "unexpected-profile-matrix.json"
    assert aggregate_main([*matrix_args([*reports, macos_report]), "--output", str(extra_output)]) == 1
    extra = json.loads(extra_output.read_text(encoding="utf-8"))
    assert extra["result"] == "failed"
    assert extra["missing_profiles"] == []
    assert extra["matrix_errors"] == ["matrix contains profiles that were not explicitly required: macos:cpu"]


def test_matrix_rejects_a_normalized_machine_id_reused_across_profiles(tmp_path):
    manifest = ModelManifest.load(VECTOR_MANIFEST)
    cpu_report = tmp_path / "windows-cpu.json"
    cuda_report = tmp_path / "windows-cuda.json"
    output = tmp_path / "matrix.json"
    _write_json(
        cpu_report,
        _local_report(manifest, system="windows", device="cpu", machine_id="shared-host"),
    )
    _write_json(
        cuda_report,
        _local_report(manifest, system="windows", device="cuda", machine_id="SHARED-HOST"),
    )

    assert (
        aggregate_main(
            [
                str(VECTOR_MANIFEST),
                str(cpu_report),
                str(cuda_report),
                "--require-profile",
                "windows:cpu",
                "--require-profile",
                "windows:cuda",
                "--output",
                str(output),
            ]
        )
        == 1
    )

    matrix = json.loads(output.read_text(encoding="utf-8"))
    assert matrix["result"] == "failed"
    assert matrix["missing_profiles"] == ["windows:cuda"]
    assert matrix["coverage"]["windows:cpu"][0]["machine_id"] == "shared-host"
    assert "windows:cuda" not in matrix["coverage"]
    assert matrix["report_errors"] == [
        {
            "report": "input-2",
            "error": ("machine_id 'SHARED-HOST' is reused across profiles " "'windows:cpu' and 'windows:cuda'"),
        }
    ]
    assert matrix["matrix_errors"] == []
    assert str(tmp_path) not in json.dumps(matrix)


def test_matrix_fails_closed_for_missing_or_runtime_mismatched_evidence(tmp_path):
    manifest = ModelManifest.load(VECTOR_MANIFEST)
    invalid_report = _local_report(manifest, system="windows", device="cpu", machine_id="win-a")
    invalid_report = copy.deepcopy(invalid_report)
    invalid_report["stages"][1]["evidence"]["attention_implementation"] = "sdpa"
    report_path = tmp_path / "invalid.json"
    output = tmp_path / "matrix.json"
    _write_json(report_path, invalid_report)

    assert (
        aggregate_main(
            [
                str(VECTOR_MANIFEST),
                str(report_path),
                "--require-profile",
                "windows:cpu",
                "--require-profile",
                "macos:mps",
                "--output",
                str(output),
            ]
        )
        == 1
    )

    serialized = output.read_text(encoding="utf-8")
    matrix = json.loads(serialized)
    assert matrix["result"] == "failed"
    assert matrix["missing_profiles"] == ["windows:cpu", "macos:mps"]
    assert matrix["report_errors"][0]["report"] == "input-1"
    assert "attention implementation" in matrix["report_errors"][0]["error"]
    assert str(tmp_path) not in serialized


def test_matrix_does_not_serialize_a_missing_report_path(tmp_path):
    missing_report = tmp_path / "private-host-directory" / "missing.json"
    output = tmp_path / "matrix.json"

    assert (
        aggregate_main(
            [
                str(VECTOR_MANIFEST),
                str(missing_report),
                "--require-profile",
                "windows:cpu",
                "--output",
                str(output),
            ]
        )
        == 1
    )

    serialized = output.read_text(encoding="utf-8")
    matrix = json.loads(serialized)
    assert matrix["report_errors"][0]["report"] == "input-1"
    assert "FileNotFoundError" in matrix["report_errors"][0]["error"]
    assert str(tmp_path) not in serialized


def test_matrix_emits_a_failed_report_when_no_host_artifacts_exist(tmp_path):
    output = tmp_path / "matrix.json"

    assert (
        aggregate_main(
            [
                str(VECTOR_MANIFEST),
                "--require-profile",
                "windows:cpu",
                "--require-profile",
                "macos:mps",
                "--require-source-commit",
                "a" * 40,
                "--output",
                str(output),
            ]
        )
        == 1
    )

    matrix = json.loads(output.read_text(encoding="utf-8"))
    assert matrix["result"] == "failed"
    assert matrix["coverage"] == {}
    assert matrix["missing_profiles"] == ["windows:cpu", "macos:mps"]
    assert matrix["report_errors"] == []
    assert matrix["matrix_errors"] == []


def test_matrix_rejects_mixed_source_and_runtime_builds(tmp_path):
    manifest = ModelManifest.load(VECTOR_MANIFEST)
    first = _local_report(manifest, system="windows", device="cpu", machine_id="win-a")
    second = _local_report(manifest, system="linux", device="cuda", machine_id="linux-a")
    second["environment"]["source_commit"] = "b" * 40
    second["environment"]["drift"] = "2.3.0.dev3"
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    output = tmp_path / "matrix.json"
    _write_json(first_path, first)
    _write_json(second_path, second)

    assert (
        aggregate_main(
            [
                str(VECTOR_MANIFEST),
                str(first_path),
                str(second_path),
                "--require-profile",
                "windows:cpu",
                "--require-profile",
                "linux:cuda",
                "--output",
                str(output),
            ]
        )
        == 1
    )

    matrix = json.loads(output.read_text(encoding="utf-8"))
    assert matrix["missing_profiles"] == []
    assert matrix["report_errors"] == []
    assert "matrix reports use different source commits" in matrix["matrix_errors"]
    assert "matrix reports use different DRIFT versions" in matrix["matrix_errors"]
    assert matrix["source_identity"] == {"source_commit": None, "drift": None}


def test_matrix_requires_the_dispatched_source_commit(tmp_path):
    manifest = ModelManifest.load(VECTOR_MANIFEST)
    report_path = tmp_path / "report.json"
    output = tmp_path / "matrix.json"
    _write_json(report_path, _local_report(manifest, system="windows", device="cpu", machine_id="win-a"))

    assert (
        aggregate_main(
            [
                str(VECTOR_MANIFEST),
                str(report_path),
                "--require-profile",
                "windows:cpu",
                "--require-source-commit",
                "b" * 40,
                "--output",
                str(output),
            ]
        )
        == 1
    )

    matrix = json.loads(output.read_text(encoding="utf-8"))
    assert matrix["source_identity"]["source_commit"] == "a" * 40
    assert matrix["matrix_errors"] == ["matrix source commit does not match --require-source-commit"]


def test_local_report_records_explicit_machine_and_source_identity(tmp_path):
    output = tmp_path / "local.json"

    assert (
        local_main(
            [
                str(VECTOR_MANIFEST),
                "--manifest-only",
                "--machine-id",
                "qualification-host-a",
                "--source-commit",
                "b" * 40,
                "--output",
                str(output),
            ]
        )
        == 0
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["environment"]["machine_id"] == "qualification-host-a"
    assert report["environment"]["source_commit"] == "b" * 40
    assert report["environment"]["system"] in {"windows", "linux", "macos"}
    assert report["environment"]["device_profile"] == "cpu"


def test_smoke_evidence_records_observed_device_dtype_and_attention():
    output = "\n".join(
        (
            "torch=2.6.0, device=cuda:0",
            "attention_implementation=eager",
            "torch_dtype=torch.bfloat16",
            "distributed output matches the stock model exactly",
            "manifested local swarm qualification ok model=org/model",
        )
    )

    evidence = extract_smoke_evidence(output, failover=False)

    assert evidence["torch_version"] == "2.6.0"
    assert evidence["worker_device"] == "cuda:0"
    assert evidence["worker_torch_dtype"] == "bfloat16"
    assert evidence["attention_implementation"] == "eager"
