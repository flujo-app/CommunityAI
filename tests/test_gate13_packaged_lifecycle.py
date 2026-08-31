import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gate13_packaged_lifecycle as lifecycle  # noqa: E402

PACKAGE_DIGEST = "a" * 64
REPLACEMENT_DIGEST = "b" * 64
MANIFEST_DIGEST = "3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33"
CATALOG_DIGEST = "d" * 64
BOOTSTRAP_DIGEST = "e" * 64
SOURCE_COMMIT = "f" * 40
REVISION_COMMIT = "15852e8c16360a2fea060d615a32b45270f8a8fc"
SELECTED_BYTES = 4_571_197_320


def _phase(name, **facts):
    return {"phase": name, "passed": True, "duration_seconds": 1.25, **facts}


def valid_document(platform="windows"):
    return {
        "schema_version": 1,
        "run_id": "gate13-20260830-a",
        "platform": platform,
        "source_commit": SOURCE_COMMIT,
        "package_version": "0.4.0-alpha.1",
        "package_sha256": PACKAGE_DIGEST,
        "package_bytes": 123_456_789,
        "model_id": "Qwen3.5 2B",
        "manifest_digest": MANIFEST_DIGEST,
        "phases": [
            _phase(
                "package_verification",
                package_sha256=PACKAGE_DIGEST,
                package_bytes=123_456_789,
                checksum_inventory_verified=True,
                provenance_verified=True,
                release_metadata_verified=True,
                unsigned_alpha_acknowledged=True,
                publisher_signature_present=False,
                authenticated_update_present=False,
                bundled_weight_file_count=0,
                bundled_weight_bytes=0,
            ),
            _phase(
                "clean_install",
                clean_host=True,
                preexisting_product_file_count=0,
                preexisting_persistent_file_count=0,
                preexisting_secret_material_count=0,
                installed_product_file_count=2500,
                source_checkout_present=False,
                source_imports_used=False,
            ),
            _phase(
                "packaged_self_tests",
                desktop_self_test_passed=True,
                node_self_test_passed=True,
                worker_self_test_passed=True,
                bootstrap_payload_present=True,
                source_imports_used=False,
            ),
            _phase(
                "signed_bootstrap",
                catalog_id="communityai-public-alpha-v1",
                catalog_sequence=1,
                catalog_digest=CATALOG_DIGEST,
                catalog_signature_verified=True,
                bootstrap_digest=BOOTSTRAP_DIGEST,
                bootstrap_verified=True,
                manifest_digest=MANIFEST_DIGEST,
                model_id="Qwen3.5 2B",
                source_imports_used=False,
            ),
            _phase(
                "selected_bytes",
                manifest_digest=MANIFEST_DIGEST,
                model_id="Qwen3.5 2B",
                selected_artifact_count=8,
                selected_artifact_bytes=SELECTED_BYTES,
                cache_verified_artifact_bytes_before=0,
                transfer_started=False,
            ),
            _phase(
                "verified_acquisition",
                manifest_digest=MANIFEST_DIGEST,
                model_id="Qwen3.5 2B",
                revision_commit=REVISION_COMMIT,
                selected_artifact_count=8,
                selected_artifact_bytes=SELECTED_BYTES,
                acquired_artifact_count=8,
                acquired_artifact_bytes=SELECTED_BYTES,
                artifact_digest_verification_count=8,
                resume_count=0,
                direct_upstream_transfer=True,
                mirror_used=False,
                cache_verified_artifact_bytes_after=SELECTED_BYTES,
                source_imports_used=False,
            ),
            _phase(
                "localhost_inference",
                loopback_only=True,
                manifest_digest=MANIFEST_DIGEST,
                model_id="Qwen3.5 2B",
                completion_count=1,
                generated_token_count=8,
                response_content_retained=False,
                token_identifier_count=0,
                source_imports_used=False,
            ),
            _phase(
                "bounded_contribution",
                opt_in=True,
                automatic_placement=True,
                manifest_digest=MANIFEST_DIGEST,
                model_id="Qwen3.5 2B",
                worker_count=1,
                block_start=0,
                block_end=4,
                block_count=4,
                resource_limit_count=5,
                limits_enforced=True,
                accepted_request_count=1,
                source_imports_used=False,
            ),
            _phase(
                "contribution_pause",
                pause_requested=True,
                pause_completed=True,
                pause_seconds=2.0,
                worker_count_after=0,
                process_count_after=0,
            ),
            _phase(
                "restart_cache_reuse",
                restart_completed=True,
                manifest_digest=MANIFEST_DIGEST,
                verified_artifact_bytes_before=SELECTED_BYTES,
                verified_artifact_bytes_after=SELECTED_BYTES,
                transferred_artifact_bytes=0,
                cache_reused=True,
                localhost_inference_passed=True,
                source_imports_used=False,
            ),
            _phase(
                "manual_replacement",
                replacement_kind="reinstall",
                previous_package_sha256=PACKAGE_DIGEST,
                replacement_package_sha256=PACKAGE_DIGEST,
                replacement_package_bytes=123_456_789,
                checksum_inventory_verified=True,
                provenance_verified=True,
                manual_operation=True,
                automatic_update_used=False,
                publisher_signature_claimed=False,
                verified_artifact_bytes_before=SELECTED_BYTES,
                verified_artifact_bytes_after=SELECTED_BYTES,
                secret_material_count_before=2,
                secret_material_count_after=2,
                localhost_inference_passed=True,
                source_imports_used=False,
            ),
            _phase(
                "recovery",
                recovery_action_count=2,
                fault_observed=True,
                recovery_completed=True,
                verified_artifact_bytes_after=SELECTED_BYTES,
                localhost_inference_passed=True,
                source_imports_used=False,
            ),
            _phase(
                "uninstall_retain",
                uninstall_completed=True,
                retain_choice_explicit=True,
                installed_product_file_count_after=0,
                process_count_after=0,
                persistent_file_count_after=20,
                verified_artifact_bytes_after=SELECTED_BYTES,
                secret_material_count_before=2,
                secret_material_count_after=2,
            ),
            _phase(
                "retained_data_reinstall",
                install_completed=True,
                verified_artifact_bytes_before=SELECTED_BYTES,
                verified_artifact_bytes_after=SELECTED_BYTES,
                transferred_artifact_bytes=0,
                secret_material_count_before=2,
                secret_material_count_after=2,
                cache_reused=True,
                secret_material_reused=True,
                localhost_inference_passed=True,
                source_imports_used=False,
            ),
            _phase(
                "uninstall_delete",
                uninstall_completed=True,
                delete_choice_explicit=True,
                installed_product_file_count_after=0,
                process_count_after=0,
                persistent_file_count_after=0,
                persistent_data_bytes_after=0,
                secret_material_count_after=0,
            ),
            _phase(
                "process_cleanup",
                cleanup_complete=True,
                product_file_count=0,
                persistent_file_count=0,
                persistent_data_bytes=0,
                secret_material_count=0,
                process_count=0,
                temporary_file_count=0,
            ),
        ],
    }


@pytest.mark.parametrize("platform", ["windows", "linux"])
def test_complete_windows_linux_lifecycle_emits_bounded_evidence(platform):
    evidence = lifecycle.validate_lifecycle_document(valid_document(platform))

    assert evidence["result"] == "passed"
    assert evidence["platform"] == platform
    assert evidence["package"]["unsigned_alpha"] is True
    assert evidence["package"]["publisher_signature_present"] is False
    assert evidence["package"]["authenticated_update_present"] is False
    assert evidence["package"]["bundled_weight_bytes"] == 0
    assert evidence["model"]["selected_artifact_bytes"] == SELECTED_BYTES
    assert evidence["lifecycle"]["phase_count"] == len(lifecycle.PHASES)
    assert evidence["lifecycle"]["source_imports_used"] is False
    assert evidence["cleanup"]["complete"] is True


def test_controller_requires_exact_sequence_and_completion():
    document = valid_document()
    controller = lifecycle.LifecycleController({key: value for key, value in document.items() if key not in {"phases"}})
    with pytest.raises(lifecycle.LifecycleEvidenceError):
        controller.accept(document["phases"][1])
    controller.accept(document["phases"][0])
    with pytest.raises(lifecycle.LifecycleEvidenceError):
        controller.finalize()


def test_unknown_or_sensitive_fields_fail_closed():
    document = valid_document()
    document["phases"][6]["prompt"] = "must-never-appear"
    with pytest.raises(lifecycle.LifecycleEvidenceError):
        lifecycle.validate_lifecycle_document(document)

    document = valid_document()
    document["private_path"] = "must-never-appear"
    with pytest.raises(lifecycle.LifecycleEvidenceError):
        lifecycle.validate_lifecycle_document(document)


def test_source_runtime_import_claim_fails_closed():
    document = valid_document()
    document["phases"][5]["source_imports_used"] = True
    with pytest.raises(lifecycle.LifecycleEvidenceError):
        lifecycle.validate_lifecycle_document(document)


@pytest.mark.parametrize(
    ("phase_index", "field", "value"),
    [
        (0, "bundled_weight_file_count", 1),
        (4, "transfer_started", True),
        (5, "direct_upstream_transfer", False),
        (5, "mirror_used", True),
        (6, "loopback_only", False),
        (8, "process_count_after", 1),
        (9, "transferred_artifact_bytes", 1),
        (10, "automatic_update_used", True),
        (10, "publisher_signature_claimed", True),
        (14, "secret_material_count_after", 1),
        (15, "process_count", 1),
    ],
)
def test_required_safety_claims_fail_closed(phase_index, field, value):
    document = valid_document()
    document["phases"][phase_index][field] = value
    with pytest.raises(lifecycle.LifecycleEvidenceError):
        lifecycle.validate_lifecycle_document(document)


def test_acquisition_must_match_selected_counts_and_bytes():
    document = valid_document()
    document["phases"][5]["acquired_artifact_bytes"] -= 1
    with pytest.raises(lifecycle.LifecycleEvidenceError):
        lifecycle.validate_lifecycle_document(document)

    document = valid_document()
    document["phases"][5]["artifact_digest_verification_count"] -= 1
    with pytest.raises(lifecycle.LifecycleEvidenceError):
        lifecycle.validate_lifecycle_document(document)

    document = valid_document()
    document["phases"][4]["selected_artifact_bytes"] -= 1
    with pytest.raises(lifecycle.LifecycleEvidenceError):
        lifecycle.validate_lifecycle_document(document)


def test_model_identity_must_be_an_exact_public_alpha_profile():
    document = valid_document()
    document["model_id"] = "Prompt disguised as model"
    for index in (3, 4, 5, 6, 7):
        document["phases"][index]["model_id"] = document["model_id"]
    with pytest.raises(lifecycle.LifecycleEvidenceError):
        lifecycle.validate_lifecycle_document(document)


def test_gemma_public_alpha_profile_is_supported():
    document = valid_document("linux")
    profile = lifecycle.MODEL_PROFILES["Gemma 4 E2B IT"]
    document["model_id"] = "Gemma 4 E2B IT"
    document["manifest_digest"] = profile["manifest_digest"]
    for phase in document["phases"]:
        if "model_id" in phase:
            phase["model_id"] = document["model_id"]
        if "manifest_digest" in phase:
            phase["manifest_digest"] = profile["manifest_digest"]
        if "revision_commit" in phase:
            phase["revision_commit"] = profile["revision_commit"]
        for field in ("selected_artifact_count", "acquired_artifact_count", "artifact_digest_verification_count"):
            if field in phase:
                phase[field] = profile["selected_artifact_count"]
        for field in (
            "selected_artifact_bytes",
            "acquired_artifact_bytes",
            "cache_verified_artifact_bytes_after",
            "verified_artifact_bytes_before",
            "verified_artifact_bytes_after",
        ):
            if field in phase:
                phase[field] = profile["selected_artifact_bytes"]

    evidence = lifecycle.validate_lifecycle_document(document)
    assert evidence["model"]["id"] == "Gemma 4 E2B IT"
    assert evidence["model"]["selected_artifact_bytes"] == 10_278_818_149


def test_manual_upgrade_requires_a_different_verified_package():
    document = valid_document()
    replacement = document["phases"][10]
    replacement["replacement_kind"] = "upgrade"
    replacement["replacement_package_sha256"] = REPLACEMENT_DIGEST
    replacement["replacement_package_bytes"] = 234_567_890
    evidence = lifecycle.validate_lifecycle_document(document)
    assert evidence["package"]["manual_replacement_kind"] == "upgrade"
    assert evidence["package"]["manual_replacement_sha256"] == REPLACEMENT_DIGEST
    assert evidence["package"]["manual_replacement_bytes"] == 234_567_890

    replacement["replacement_package_sha256"] = PACKAGE_DIGEST
    with pytest.raises(lifecycle.LifecycleEvidenceError):
        lifecycle.validate_lifecycle_document(document)


def test_reinstall_requires_the_same_verified_package_identity():
    document = valid_document()
    document["phases"][10]["replacement_package_sha256"] = REPLACEMENT_DIGEST
    with pytest.raises(lifecycle.LifecycleEvidenceError):
        lifecycle.validate_lifecycle_document(document)

    document = valid_document()
    document["phases"][10]["replacement_package_bytes"] += 1
    with pytest.raises(lifecycle.LifecycleEvidenceError):
        lifecycle.validate_lifecycle_document(document)


def test_retained_secret_material_count_cannot_drift_between_phases():
    document = valid_document()
    document["phases"][12]["secret_material_count_before"] = 3
    document["phases"][12]["secret_material_count_after"] = 3
    document["phases"][13]["secret_material_count_before"] = 4
    document["phases"][13]["secret_material_count_after"] = 4
    with pytest.raises(lifecycle.LifecycleEvidenceError):
        lifecycle.validate_lifecycle_document(document)

    document = valid_document()
    document["phases"][13]["secret_material_count_before"] = 3
    document["phases"][13]["secret_material_count_after"] = 3
    with pytest.raises(lifecycle.LifecycleEvidenceError):
        lifecycle.validate_lifecycle_document(document)


def test_retained_material_must_survive_then_be_deleted():
    document = valid_document()
    document["phases"][12]["secret_material_count_after"] = 0
    with pytest.raises(lifecycle.LifecycleEvidenceError):
        lifecycle.validate_lifecycle_document(document)

    document = valid_document()
    document["phases"][14]["persistent_data_bytes_after"] = 1
    with pytest.raises(lifecycle.LifecycleEvidenceError):
        lifecycle.validate_lifecycle_document(document)


def test_schema_rejects_macos_nonfinite_numbers_and_bool_counts():
    document = valid_document("macos")
    with pytest.raises(lifecycle.LifecycleEvidenceError):
        lifecycle.validate_lifecycle_document(document)

    document = valid_document()
    document["phases"][0]["duration_seconds"] = float("nan")
    with pytest.raises(lifecycle.LifecycleEvidenceError):
        lifecycle.validate_lifecycle_document(document)

    document = valid_document()
    document["phases"][0]["bundled_weight_file_count"] = False
    with pytest.raises(lifecycle.LifecycleEvidenceError):
        lifecycle.validate_lifecycle_document(document)


def test_loader_rejects_duplicate_keys_and_nonfinite_json():
    with pytest.raises(lifecycle.LifecycleEvidenceError):
        lifecycle.load_lifecycle_json('{"schema_version":1,"schema_version":1}')
    with pytest.raises(lifecycle.LifecycleEvidenceError):
        lifecycle.load_lifecycle_json('{"duration_seconds":NaN}')


def test_cli_failure_is_generic_json_and_never_echoes_input(tmp_path, capsys):
    marker = "private-value-must-not-appear"
    source = tmp_path / "invalid.json"
    source.write_text(json.dumps({"prompt": marker, "private_path": marker}), encoding="utf-8")

    assert lifecycle.main(["--input", str(source)]) == 2
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        "failure_code": "invalid_evidence",
        "result": "failed",
        "schema_version": 1,
    }
    assert output.err == ""
    assert marker not in output.out


def test_cli_success_is_canonical_and_contains_no_phase_records(tmp_path, capsys):
    source = tmp_path / "valid.json"
    source.write_text(json.dumps(valid_document()), encoding="utf-8")

    assert lifecycle.main(["--input", str(source)]) == 0
    output = capsys.readouterr()
    evidence = json.loads(output.out)
    assert evidence["result"] == "passed"
    assert "phases" not in evidence
    assert output.err == ""
    assert output.out == json.dumps(evidence, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n"


def test_phase_list_must_be_exact_with_no_omission_or_reordering():
    document = valid_document()
    del document["phases"][7]
    with pytest.raises(lifecycle.LifecycleEvidenceError):
        lifecycle.validate_lifecycle_document(document)

    document = valid_document()
    document["phases"][6], document["phases"][7] = document["phases"][7], document["phases"][6]
    with pytest.raises(lifecycle.LifecycleEvidenceError):
        lifecycle.validate_lifecycle_document(document)


def test_public_summary_does_not_retain_raw_phase_only_fields():
    evidence = lifecycle.validate_lifecycle_document(valid_document())
    rendered = json.dumps(evidence, sort_keys=True)

    for forbidden_value in (
        REVISION_COMMIT,
        "block_start",
        "accepted_request_count",
        "secret_material_count_before",
        "recovery_action_count",
    ):
        assert forbidden_value not in rendered
