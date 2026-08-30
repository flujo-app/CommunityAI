"""Validate privacy-safe Gate 13 packaged lifecycle phase records.

This controller intentionally uses only the Python standard library.  Qualification
startup scripts run the packaged CommunityAI executables, record one bounded phase at
a time, and pass the resulting document here.  The product must never import from a
source checkout during the lifecycle.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = 1
SCOPE = "gate13-packaged-lifecycle"
MAX_INPUT_BYTES = 1_048_576
MAX_COUNT = 1_000_000
MAX_BYTES = 1 << 50
MAX_DURATION_SECONDS = 86_400.0

MODEL_PROFILES = {
    "Qwen3.5 2B": {
        "manifest_digest": "3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33",
        "revision_commit": "15852e8c16360a2fea060d615a32b45270f8a8fc",
        "selected_artifact_count": 8,
        "selected_artifact_bytes": 4_571_197_320,
    },
    "Gemma 4 E2B IT": {
        "manifest_digest": "2f8debbe0fcdf5af8d4c56c982210fa50aa584314968ae2617e2ccc2de9eafdd",
        "revision_commit": "3e22461f65e89153144f8adb70e3b8c2cc9845a7",
        "selected_artifact_count": 5,
        "selected_artifact_bytes": 10_278_818_149,
    },
}

PHASES = (
    "package_verification",
    "clean_install",
    "packaged_self_tests",
    "signed_bootstrap",
    "selected_bytes",
    "verified_acquisition",
    "localhost_inference",
    "bounded_contribution",
    "contribution_pause",
    "restart_cache_reuse",
    "manual_replacement",
    "recovery",
    "uninstall_retain",
    "retained_data_reinstall",
    "uninstall_delete",
    "process_cleanup",
)

_HEADER_FIELDS = {
    "schema_version",
    "run_id",
    "platform",
    "source_commit",
    "package_version",
    "package_sha256",
    "package_bytes",
    "model_id",
    "manifest_digest",
}
_DOCUMENT_FIELDS = _HEADER_FIELDS | {"phases"}
_BASE_PHASE_FIELDS = {"phase", "passed", "duration_seconds"}
_HEX40_RE = re.compile(r"[0-9a-f]{40}")
_DIGEST_RE = re.compile(r"(?:sha256:)?[0-9a-f]{64}")
_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
_DISPLAY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._+()-]{0,127}")


class LifecycleEvidenceError(ValueError):
    """One lifecycle fact was absent, malformed, unsafe, or inconsistent."""


def _fail() -> None:
    raise LifecycleEvidenceError("invalid lifecycle evidence")


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        _fail()
    return value


def _exact_fields(value: Mapping[str, Any], fields: set[str]) -> None:
    if set(value) != fields:
        _fail()


def _bool(value: Any) -> bool:
    if type(value) is not bool:
        _fail()
    return value


def _true(value: Any) -> None:
    if _bool(value) is not True:
        _fail()


def _false(value: Any) -> None:
    if _bool(value) is not False:
        _fail()


def _integer(value: Any, *, minimum: int = 0, maximum: int = MAX_COUNT) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail()
    return value


def _number(value: Any, *, maximum: float = MAX_DURATION_SECONDS) -> float:
    if type(value) not in (int, float):
        _fail()
    result = float(value)
    if not math.isfinite(result) or result < 0 or result > maximum:
        _fail()
    return result


def _bytes(value: Any) -> int:
    return _integer(value, maximum=MAX_BYTES)


def _digest(value: Any) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        _fail()
    return value


def _commit(value: Any) -> str:
    if not isinstance(value, str) or _HEX40_RE.fullmatch(value) is None:
        _fail()
    return value


def _label(value: Any) -> str:
    if not isinstance(value, str) or _LABEL_RE.fullmatch(value) is None:
        _fail()
    return value


def _display(value: Any) -> str:
    if not isinstance(value, str) or _DISPLAY_RE.fullmatch(value) is None:
        _fail()
    return value


def _same(value: Any, expected: Any) -> None:
    if value != expected or type(value) is not type(expected):
        _fail()


def _phase_fields(record: Mapping[str, Any], *fields: str) -> None:
    _exact_fields(record, _BASE_PHASE_FIELDS | set(fields))
    _true(record["passed"])
    _number(record["duration_seconds"])


class LifecycleController:
    """Accept the exact Gate 13 phase sequence and produce bounded evidence."""

    def __init__(self, header: Mapping[str, Any]):
        header = dict(_mapping(header))
        _exact_fields(header, _HEADER_FIELDS)
        _same(header["schema_version"], SCHEMA_VERSION)
        _label(header["run_id"])
        if not isinstance(header["platform"], str) or header["platform"] not in {"windows", "linux"}:
            _fail()
        _commit(header["source_commit"])
        _label(header["package_version"])
        _digest(header["package_sha256"])
        _integer(header["package_bytes"], minimum=1, maximum=MAX_BYTES)
        _display(header["model_id"])
        _digest(header["manifest_digest"])
        if header["model_id"] not in MODEL_PROFILES:
            _fail()
        if header["manifest_digest"] != MODEL_PROFILES[header["model_id"]]["manifest_digest"]:
            _fail()
        self.header = dict(header)
        self._phases: list[dict[str, Any]] = []

    @property
    def next_phase(self) -> str | None:
        if len(self._phases) == len(PHASES):
            return None
        return PHASES[len(self._phases)]

    def accept(self, raw_record: Mapping[str, Any]) -> None:
        record = dict(_mapping(raw_record))
        if self.next_phase is None or record.get("phase") != self.next_phase:
            _fail()
        validator = getattr(self, f"_validate_{self.next_phase}")
        validator(record)
        self._phases.append(record)

    def _validate_package_verification(self, record: Mapping[str, Any]) -> None:
        _phase_fields(
            record,
            "package_sha256",
            "package_bytes",
            "checksum_inventory_verified",
            "provenance_verified",
            "release_metadata_verified",
            "unsigned_alpha_acknowledged",
            "publisher_signature_present",
            "authenticated_update_present",
            "bundled_weight_file_count",
            "bundled_weight_bytes",
        )
        _same(record["package_sha256"], self.header["package_sha256"])
        _same(record["package_bytes"], self.header["package_bytes"])
        for field in (
            "checksum_inventory_verified",
            "provenance_verified",
            "release_metadata_verified",
            "unsigned_alpha_acknowledged",
        ):
            _true(record[field])
        _false(record["publisher_signature_present"])
        _false(record["authenticated_update_present"])
        _same(record["bundled_weight_file_count"], 0)
        _same(record["bundled_weight_bytes"], 0)

    def _validate_clean_install(self, record: Mapping[str, Any]) -> None:
        _phase_fields(
            record,
            "clean_host",
            "preexisting_product_file_count",
            "preexisting_persistent_file_count",
            "preexisting_secret_material_count",
            "installed_product_file_count",
            "source_checkout_present",
            "source_imports_used",
        )
        _true(record["clean_host"])
        for field in (
            "preexisting_product_file_count",
            "preexisting_persistent_file_count",
            "preexisting_secret_material_count",
        ):
            _same(record[field], 0)
        _integer(record["installed_product_file_count"], minimum=1)
        _false(record["source_checkout_present"])
        _false(record["source_imports_used"])

    def _validate_packaged_self_tests(self, record: Mapping[str, Any]) -> None:
        _phase_fields(
            record,
            "desktop_self_test_passed",
            "node_self_test_passed",
            "worker_self_test_passed",
            "bootstrap_payload_present",
            "source_imports_used",
        )
        for field in (
            "desktop_self_test_passed",
            "node_self_test_passed",
            "worker_self_test_passed",
            "bootstrap_payload_present",
        ):
            _true(record[field])
        _false(record["source_imports_used"])

    def _validate_signed_bootstrap(self, record: Mapping[str, Any]) -> None:
        _phase_fields(
            record,
            "catalog_id",
            "catalog_sequence",
            "catalog_digest",
            "catalog_signature_verified",
            "bootstrap_digest",
            "bootstrap_verified",
            "manifest_digest",
            "model_id",
            "source_imports_used",
        )
        _label(record["catalog_id"])
        _integer(record["catalog_sequence"], minimum=1)
        _digest(record["catalog_digest"])
        _digest(record["bootstrap_digest"])
        _true(record["catalog_signature_verified"])
        _true(record["bootstrap_verified"])
        _same(record["manifest_digest"], self.header["manifest_digest"])
        _same(record["model_id"], self.header["model_id"])
        _false(record["source_imports_used"])

    def _validate_selected_bytes(self, record: Mapping[str, Any]) -> None:
        _phase_fields(
            record,
            "manifest_digest",
            "model_id",
            "selected_artifact_count",
            "selected_artifact_bytes",
            "cache_verified_artifact_bytes_before",
            "transfer_started",
        )
        _same(record["manifest_digest"], self.header["manifest_digest"])
        _same(record["model_id"], self.header["model_id"])
        profile = MODEL_PROFILES[self.header["model_id"]]
        _same(record["selected_artifact_count"], profile["selected_artifact_count"])
        _same(record["selected_artifact_bytes"], profile["selected_artifact_bytes"])
        _same(record["cache_verified_artifact_bytes_before"], 0)
        _false(record["transfer_started"])

    def _validate_verified_acquisition(self, record: Mapping[str, Any]) -> None:
        _phase_fields(
            record,
            "manifest_digest",
            "model_id",
            "revision_commit",
            "selected_artifact_count",
            "selected_artifact_bytes",
            "acquired_artifact_count",
            "acquired_artifact_bytes",
            "artifact_digest_verification_count",
            "resume_count",
            "direct_upstream_transfer",
            "mirror_used",
            "cache_verified_artifact_bytes_after",
            "source_imports_used",
        )
        selected = self._phases[4]
        _same(record["manifest_digest"], self.header["manifest_digest"])
        _same(record["model_id"], self.header["model_id"])
        _same(record["revision_commit"], MODEL_PROFILES[self.header["model_id"]]["revision_commit"])
        for field in ("selected_artifact_count", "acquired_artifact_count", "artifact_digest_verification_count"):
            _same(record[field], selected["selected_artifact_count"])
        for field in ("selected_artifact_bytes", "acquired_artifact_bytes", "cache_verified_artifact_bytes_after"):
            _same(record[field], selected["selected_artifact_bytes"])
        _integer(record["resume_count"], maximum=3)
        _true(record["direct_upstream_transfer"])
        _false(record["mirror_used"])
        _false(record["source_imports_used"])

    def _validate_localhost_inference(self, record: Mapping[str, Any]) -> None:
        _phase_fields(
            record,
            "loopback_only",
            "manifest_digest",
            "model_id",
            "completion_count",
            "generated_token_count",
            "response_content_retained",
            "token_identifier_count",
            "source_imports_used",
        )
        _true(record["loopback_only"])
        _same(record["manifest_digest"], self.header["manifest_digest"])
        _same(record["model_id"], self.header["model_id"])
        _integer(record["completion_count"], minimum=1, maximum=16)
        _integer(record["generated_token_count"], minimum=1, maximum=4096)
        _false(record["response_content_retained"])
        _same(record["token_identifier_count"], 0)
        _false(record["source_imports_used"])

    def _validate_bounded_contribution(self, record: Mapping[str, Any]) -> None:
        _phase_fields(
            record,
            "opt_in",
            "automatic_placement",
            "manifest_digest",
            "model_id",
            "worker_count",
            "block_start",
            "block_end",
            "block_count",
            "resource_limit_count",
            "limits_enforced",
            "accepted_request_count",
            "source_imports_used",
        )
        _true(record["opt_in"])
        _true(record["automatic_placement"])
        _same(record["manifest_digest"], self.header["manifest_digest"])
        _same(record["model_id"], self.header["model_id"])
        _same(record["worker_count"], 1)
        start = _integer(record["block_start"], maximum=511)
        end = _integer(record["block_end"], minimum=1, maximum=512)
        if end <= start:
            _fail()
        _same(record["block_count"], end - start)
        _integer(record["resource_limit_count"], minimum=4, maximum=16)
        _true(record["limits_enforced"])
        _integer(record["accepted_request_count"], maximum=MAX_COUNT)
        _false(record["source_imports_used"])

    def _validate_contribution_pause(self, record: Mapping[str, Any]) -> None:
        _phase_fields(
            record,
            "pause_requested",
            "pause_completed",
            "pause_seconds",
            "worker_count_after",
            "process_count_after",
        )
        _true(record["pause_requested"])
        _true(record["pause_completed"])
        _number(record["pause_seconds"], maximum=300.0)
        _same(record["worker_count_after"], 0)
        _same(record["process_count_after"], 0)

    def _validate_restart_cache_reuse(self, record: Mapping[str, Any]) -> None:
        _phase_fields(
            record,
            "restart_completed",
            "manifest_digest",
            "verified_artifact_bytes_before",
            "verified_artifact_bytes_after",
            "transferred_artifact_bytes",
            "cache_reused",
            "localhost_inference_passed",
            "source_imports_used",
        )
        expected = self._phases[4]["selected_artifact_bytes"]
        _true(record["restart_completed"])
        _same(record["manifest_digest"], self.header["manifest_digest"])
        _same(record["verified_artifact_bytes_before"], expected)
        _same(record["verified_artifact_bytes_after"], expected)
        _same(record["transferred_artifact_bytes"], 0)
        _true(record["cache_reused"])
        _true(record["localhost_inference_passed"])
        _false(record["source_imports_used"])

    def _validate_manual_replacement(self, record: Mapping[str, Any]) -> None:
        _phase_fields(
            record,
            "replacement_kind",
            "previous_package_sha256",
            "replacement_package_sha256",
            "replacement_package_bytes",
            "checksum_inventory_verified",
            "provenance_verified",
            "manual_operation",
            "automatic_update_used",
            "publisher_signature_claimed",
            "verified_artifact_bytes_before",
            "verified_artifact_bytes_after",
            "secret_material_count_before",
            "secret_material_count_after",
            "localhost_inference_passed",
            "source_imports_used",
        )
        kind = record["replacement_kind"]
        if not isinstance(kind, str) or kind not in {"upgrade", "reinstall"}:
            _fail()
        _same(record["previous_package_sha256"], self.header["package_sha256"])
        replacement = _digest(record["replacement_package_sha256"])
        if (kind == "reinstall") != (replacement == self.header["package_sha256"]):
            _fail()
        replacement_bytes = _integer(record["replacement_package_bytes"], minimum=1, maximum=MAX_BYTES)
        if kind == "reinstall":
            _same(replacement_bytes, self.header["package_bytes"])
        _true(record["checksum_inventory_verified"])
        _true(record["provenance_verified"])
        _true(record["manual_operation"])
        _false(record["automatic_update_used"])
        _false(record["publisher_signature_claimed"])
        expected = self._phases[4]["selected_artifact_bytes"]
        _same(record["verified_artifact_bytes_before"], expected)
        _same(record["verified_artifact_bytes_after"], expected)
        before = _integer(record["secret_material_count_before"], minimum=1, maximum=64)
        _same(record["secret_material_count_after"], before)
        _true(record["localhost_inference_passed"])
        _false(record["source_imports_used"])

    def _validate_recovery(self, record: Mapping[str, Any]) -> None:
        _phase_fields(
            record,
            "recovery_action_count",
            "fault_observed",
            "recovery_completed",
            "verified_artifact_bytes_after",
            "localhost_inference_passed",
            "source_imports_used",
        )
        _integer(record["recovery_action_count"], minimum=1, maximum=16)
        _true(record["fault_observed"])
        _true(record["recovery_completed"])
        _same(record["verified_artifact_bytes_after"], self._phases[4]["selected_artifact_bytes"])
        _true(record["localhost_inference_passed"])
        _false(record["source_imports_used"])

    def _validate_uninstall_retain(self, record: Mapping[str, Any]) -> None:
        _phase_fields(
            record,
            "uninstall_completed",
            "retain_choice_explicit",
            "installed_product_file_count_after",
            "process_count_after",
            "persistent_file_count_after",
            "verified_artifact_bytes_after",
            "secret_material_count_before",
            "secret_material_count_after",
        )
        _true(record["uninstall_completed"])
        _true(record["retain_choice_explicit"])
        _same(record["installed_product_file_count_after"], 0)
        _same(record["process_count_after"], 0)
        _integer(record["persistent_file_count_after"], minimum=1)
        _same(record["verified_artifact_bytes_after"], self._phases[4]["selected_artifact_bytes"])
        expected_secret_count = self._phases[10]["secret_material_count_after"]
        _same(record["secret_material_count_before"], expected_secret_count)
        _same(record["secret_material_count_after"], expected_secret_count)

    def _validate_retained_data_reinstall(self, record: Mapping[str, Any]) -> None:
        _phase_fields(
            record,
            "install_completed",
            "verified_artifact_bytes_before",
            "verified_artifact_bytes_after",
            "transferred_artifact_bytes",
            "secret_material_count_before",
            "secret_material_count_after",
            "cache_reused",
            "secret_material_reused",
            "localhost_inference_passed",
            "source_imports_used",
        )
        _true(record["install_completed"])
        expected = self._phases[4]["selected_artifact_bytes"]
        _same(record["verified_artifact_bytes_before"], expected)
        _same(record["verified_artifact_bytes_after"], expected)
        _same(record["transferred_artifact_bytes"], 0)
        expected_secret_count = self._phases[12]["secret_material_count_after"]
        _same(record["secret_material_count_before"], expected_secret_count)
        _same(record["secret_material_count_after"], expected_secret_count)
        _true(record["cache_reused"])
        _true(record["secret_material_reused"])
        _true(record["localhost_inference_passed"])
        _false(record["source_imports_used"])

    def _validate_uninstall_delete(self, record: Mapping[str, Any]) -> None:
        _phase_fields(
            record,
            "uninstall_completed",
            "delete_choice_explicit",
            "installed_product_file_count_after",
            "process_count_after",
            "persistent_file_count_after",
            "persistent_data_bytes_after",
            "secret_material_count_after",
        )
        _true(record["uninstall_completed"])
        _true(record["delete_choice_explicit"])
        for field in (
            "installed_product_file_count_after",
            "process_count_after",
            "persistent_file_count_after",
            "persistent_data_bytes_after",
            "secret_material_count_after",
        ):
            _same(record[field], 0)

    def _validate_process_cleanup(self, record: Mapping[str, Any]) -> None:
        _phase_fields(
            record,
            "cleanup_complete",
            "product_file_count",
            "persistent_file_count",
            "persistent_data_bytes",
            "secret_material_count",
            "process_count",
            "temporary_file_count",
        )
        _true(record["cleanup_complete"])
        for field in (
            "product_file_count",
            "persistent_file_count",
            "persistent_data_bytes",
            "secret_material_count",
            "process_count",
            "temporary_file_count",
        ):
            _same(record[field], 0)

    def finalize(self) -> dict[str, Any]:
        if self.next_phase is not None:
            _fail()
        package = self._phases[0]
        bootstrap = self._phases[3]
        selection = self._phases[4]
        replacement = self._phases[10]
        total_duration = round(sum(float(item["duration_seconds"]) for item in self._phases), 6)
        return {
            "schema_version": SCHEMA_VERSION,
            "scope": SCOPE,
            "result": "passed",
            "run_id": self.header["run_id"],
            "platform": self.header["platform"],
            "source_commit": self.header["source_commit"],
            "package": {
                "version": self.header["package_version"],
                "sha256": self.header["package_sha256"],
                "bytes": self.header["package_bytes"],
                "checksum_inventory_verified": True,
                "provenance_verified": True,
                "unsigned_alpha": True,
                "publisher_signature_present": False,
                "authenticated_update_present": False,
                "bundled_weight_file_count": package["bundled_weight_file_count"],
                "bundled_weight_bytes": package["bundled_weight_bytes"],
                "manual_replacement_kind": replacement["replacement_kind"],
                "manual_replacement_sha256": replacement["replacement_package_sha256"],
                "manual_replacement_bytes": replacement["replacement_package_bytes"],
            },
            "catalog": {
                "id": bootstrap["catalog_id"],
                "sequence": bootstrap["catalog_sequence"],
                "digest": bootstrap["catalog_digest"],
                "bootstrap_digest": bootstrap["bootstrap_digest"],
                "signature_verified": True,
            },
            "model": {
                "id": self.header["model_id"],
                "manifest_digest": self.header["manifest_digest"],
                "selected_artifact_count": selection["selected_artifact_count"],
                "selected_artifact_bytes": selection["selected_artifact_bytes"],
            },
            "lifecycle": {
                "phase_count": len(self._phases),
                "total_duration_seconds": total_duration,
                "clean_install": True,
                "packaged_self_tests": True,
                "direct_verified_acquisition": True,
                "localhost_inference": True,
                "bounded_contribution_and_pause": True,
                "restart_cache_reuse": True,
                "manual_replacement": True,
                "recovery": True,
                "retained_data_reinstall": True,
                "source_imports_used": False,
                "response_content_retained": False,
                "token_identifier_count": 0,
            },
            "cleanup": {
                "retain_choice_proved": True,
                "delete_choice_proved": True,
                "product_files_remaining": 0,
                "persistent_files_remaining": 0,
                "persistent_data_bytes_remaining": 0,
                "secret_material_count_remaining": 0,
                "process_count_remaining": 0,
                "temporary_file_count_remaining": 0,
                "complete": True,
            },
        }


def validate_lifecycle_document(raw_document: Mapping[str, Any]) -> dict[str, Any]:
    document = dict(_mapping(raw_document))
    _exact_fields(document, _DOCUMENT_FIELDS)
    phases = document["phases"]
    if not isinstance(phases, list) or len(phases) != len(PHASES):
        _fail()
    controller = LifecycleController({field: document[field] for field in _HEADER_FIELDS})
    for phase in phases:
        controller.accept(_mapping(phase))
    return controller.finalize()


def _reject_constant(_value: str) -> None:
    _fail()


def load_lifecycle_json(payload: str) -> Mapping[str, Any]:
    if len(payload.encode("utf-8")) > MAX_INPUT_BYTES:
        _fail()

    def unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                _fail()
            result[key] = value
        return result

    try:
        parsed = json.loads(payload, object_pairs_hook=unique_object, parse_constant=_reject_constant)
    except (json.JSONDecodeError, UnicodeError):
        _fail()
    return _mapping(parsed)


def _read_input(argv: Sequence[str]) -> str:
    if len(argv) == 0:
        payload = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    elif len(argv) == 2 and argv[0] == "--input":
        candidate = Path(argv[1])
        if candidate.is_symlink() or not candidate.is_file():
            _fail()
        if candidate.stat().st_size > MAX_INPUT_BYTES:
            _fail()
        payload = candidate.read_bytes()
    else:
        _fail()
    if len(payload) > MAX_INPUT_BYTES:
        _fail()
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        _fail()


def _render(value: Mapping[str, Any]) -> str:
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        payload = _read_input(sys.argv[1:] if argv is None else argv)
        evidence = validate_lifecycle_document(load_lifecycle_json(payload))
    except Exception:
        # Never copy attacker-controlled input, filesystem locations, argv, provider
        # output, or exception text into a qualification record.
        print(_render({"failure_code": "invalid_evidence", "result": "failed", "schema_version": SCHEMA_VERSION}))
        return 2
    print(_render(evidence))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
