"""Validate a cross-platform matrix of local qualification reports.

The combiner is deliberately narrower than release approval. It proves that exact
manifest/runtime reports exist for explicitly requested operating-system/device
profiles. Multi-machine routing, resource envelopes, public-worker soak, and catalog
publication remain independent gates.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from drift.model_manifest import ManifestError, ModelManifest

MATRIX_QUALIFICATION_SCHEMA_VERSION = 1
LOCAL_QUALIFICATION_SCHEMA_VERSION = 1
_MACHINE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SOURCE_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SYSTEM_ALIASES = {
    "darwin": "macos",
    "linux": "linux",
    "macos": "macos",
    "windows": "windows",
}
_DEVICE_PROFILES = {"cpu", "cuda", "mps"}


class EvidenceError(ValueError):
    """A qualification report cannot support the requested matrix gate."""


@dataclass(frozen=True)
class ValidatedReport:
    report_id: str
    generated_at: str
    machine_id: str
    system: str
    device: str
    source_commit: str | None
    drift_version: str

    @property
    def profile(self) -> str:
        return f"{self.system}:{self.device}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "report": self.report_id,
            "generated_at": self.generated_at,
            "machine_id": self.machine_id,
            "system": self.system,
            "device": self.device,
            "profile": self.profile,
            "source_commit": self.source_commit,
            "drift": self.drift_version,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Combine strict local reports into an explicit cross-platform qualification matrix",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("manifest", type=Path, help="Exact ModelManifest v1 candidate")
    parser.add_argument(
        "reports",
        type=Path,
        nargs="*",
        help="Local qualification report JSON files; an empty set emits a failed matrix with all profiles missing",
    )
    parser.add_argument(
        "--require-profile",
        action="append",
        required=True,
        metavar="SYSTEM:DEVICE",
        help="Required profile; system is windows/linux/macos and device is cpu/cuda/mps (repeatable)",
    )
    parser.add_argument(
        "--require-source-commit",
        help="Require every report to come from this exact 40-character lowercase source commit",
    )
    parser.add_argument(
        "--require-drift-version",
        help="Require every report to record this exact non-empty DRIFT version",
    )
    parser.add_argument("--output", type=Path, help="Write the matrix report atomically; otherwise print JSON")
    return parser


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def normalize_device(value: str) -> str:
    candidate = value.strip().lower()
    if candidate.startswith("cuda"):
        return "cuda"
    if candidate.startswith("mps"):
        return "mps"
    if candidate.startswith("cpu"):
        return "cpu"
    return candidate.split(":", 1)[0]


def parse_profile(value: str) -> str:
    system, separator, device = value.strip().lower().partition(":")
    if not separator or ":" in device:
        raise EvidenceError(f"profile {value!r} must have the form system:device")
    system = _SYSTEM_ALIASES.get(system, system)
    device = normalize_device(device)
    if system not in {"windows", "linux", "macos"}:
        raise EvidenceError(f"profile {value!r} uses unsupported system {system!r}")
    if device not in _DEVICE_PROFILES:
        raise EvidenceError(f"profile {value!r} uses unsupported device {device!r}")
    if device == "mps" and system != "macos":
        raise EvidenceError("MPS qualification is valid only for macos")
    return f"{system}:{device}"


def _require_object(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceError(f"{field} must be a JSON object")
    return value


def _require_passed_stage(report: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    stages = report.get("stages")
    if not isinstance(stages, list):
        raise EvidenceError("stages must be a JSON array")
    matches = [stage for stage in stages if isinstance(stage, dict) and stage.get("name") == name]
    if len(matches) != 1:
        raise EvidenceError(f"report must contain exactly one {name!r} stage")
    stage = matches[0]
    if stage.get("status") != "passed":
        raise EvidenceError(f"stage {name!r} did not pass")
    return stage


def _require_true(value: Any, field: str) -> None:
    if value is not True:
        raise EvidenceError(f"{field} must be true")


def validate_local_report(
    path: Path,
    report: Any,
    manifest: ModelManifest,
    *,
    report_id: str | None = None,
) -> ValidatedReport:
    report = _require_object(report, "report")
    if report.get("schema_version") != LOCAL_QUALIFICATION_SCHEMA_VERSION:
        raise EvidenceError(f"schema_version must be {LOCAL_QUALIFICATION_SCHEMA_VERSION}")
    if report.get("scope") != "single-machine-local":
        raise EvidenceError("scope must be 'single-machine-local'")
    if report.get("result") != "passed":
        raise EvidenceError("result must be 'passed'")
    if report.get("complete_release_qualification") is not False:
        raise EvidenceError("local evidence must retain complete_release_qualification=false")

    model = _require_object(report.get("model"), "model")
    if model.get("manifest_digest") != manifest.digest_id:
        raise EvidenceError("model.manifest_digest does not match the candidate manifest")
    if model.get("repository") != manifest.source.repository or model.get("revision") != manifest.source.revision:
        raise EvidenceError("model source identity does not match the candidate manifest")
    if model.get("runtime") != manifest.runtime.to_dict():
        raise EvidenceError("model.runtime does not exactly match the manifested execution profile")

    requested = _require_object(report.get("requested"), "requested")
    _require_true(requested.get("artifact_verification"), "requested.artifact_verification")
    _require_true(requested.get("local_parity"), "requested.local_parity")
    _require_true(requested.get("local_failover"), "requested.local_failover")

    environment = _require_object(report.get("environment"), "environment")
    machine_id = environment.get("machine_id")
    if not isinstance(machine_id, str) or not _MACHINE_ID_RE.fullmatch(machine_id):
        raise EvidenceError("environment.machine_id must be an explicit privacy-safe opaque label")
    system = environment.get("system")
    if not isinstance(system, str) or system not in {"windows", "linux", "macos"}:
        raise EvidenceError("environment.system must be windows, linux, or macos")
    requested_device = requested.get("device")
    if not isinstance(requested_device, str):
        raise EvidenceError("requested.device must be a string")
    device = normalize_device(requested_device)
    if device not in _DEVICE_PROFILES:
        raise EvidenceError(f"requested.device has unsupported profile {device!r}")
    if environment.get("device_profile") != device:
        raise EvidenceError("environment.device_profile does not match requested.device")
    if device == "mps" and system != "macos":
        raise EvidenceError("MPS evidence is valid only on macos")

    source_commit = environment.get("source_commit")
    if source_commit is not None and (
        not isinstance(source_commit, str) or not _SOURCE_COMMIT_RE.fullmatch(source_commit)
    ):
        raise EvidenceError("environment.source_commit must be null or a 40-character lowercase commit")
    drift_version = environment.get("drift")
    if not isinstance(drift_version, str) or not drift_version:
        raise EvidenceError("environment.drift must be a non-empty string")
    generated_at = report.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        raise EvidenceError("generated_at must be a non-empty string")

    artifact_stage = _require_passed_stage(report, "manifest_and_artifacts")
    artifact_evidence = _require_object(artifact_stage.get("evidence"), "manifest_and_artifacts.evidence")
    _require_true(artifact_evidence.get("artifacts_verified"), "manifest_and_artifacts.evidence.artifacts_verified")
    if artifact_evidence.get("manifest_digest") != manifest.digest_id:
        raise EvidenceError("artifact stage manifest digest does not match")

    parity_stage = _require_passed_stage(report, "local_distributed_stock_parity")
    parity_evidence = _require_object(parity_stage.get("evidence"), "local_distributed_stock_parity.evidence")
    _require_true(parity_evidence.get("stock_token_parity"), "parity stock_token_parity")
    _require_true(parity_evidence.get("manifested_route_completed"), "parity manifested_route_completed")
    if normalize_device(str(parity_evidence.get("worker_device", ""))) != device:
        raise EvidenceError("observed worker device does not match the requested device profile")
    if parity_evidence.get("worker_torch_dtype") != manifest.runtime.dtype:
        raise EvidenceError("observed worker dtype does not match the manifested dtype")
    if parity_evidence.get("attention_implementation") != manifest.runtime.attention_implementation:
        raise EvidenceError("observed attention implementation does not match the manifest")

    failover_stage = _require_passed_stage(report, "local_in_generation_failover")
    failover_evidence = _require_object(failover_stage.get("evidence"), "local_in_generation_failover.evidence")
    _require_true(failover_evidence.get("stock_token_parity"), "failover stock_token_parity")
    _require_true(failover_evidence.get("manifested_route_completed"), "failover manifested_route_completed")
    _require_true(failover_evidence.get("selected_worker_interrupted"), "failover selected_worker_interrupted")
    _require_true(failover_evidence.get("recovery_observed"), "failover recovery_observed")
    if normalize_device(str(failover_evidence.get("worker_device", ""))) != device:
        raise EvidenceError("failover worker device does not match the requested device profile")
    if failover_evidence.get("worker_torch_dtype") != manifest.runtime.dtype:
        raise EvidenceError("failover worker dtype does not match the manifested dtype")
    if failover_evidence.get("attention_implementation") != manifest.runtime.attention_implementation:
        raise EvidenceError("failover attention implementation does not match the manifest")

    return ValidatedReport(
        report_id=report_id or path.name,
        generated_at=generated_at,
        machine_id=machine_id,
        system=system,
        device=device,
        source_commit=source_commit,
        drift_version=drift_version,
    )


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def _describe_report_error(exc: BaseException) -> str:
    if isinstance(exc, OSError):
        return f"{type(exc).__name__}: {exc.strerror or 'unable to read report'}"
    return str(exc)


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    path = _absolute(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        required_profiles = list(dict.fromkeys(parse_profile(value) for value in args.require_profile))
    except EvidenceError as exc:
        parser.error(str(exc))
    if args.require_source_commit is not None and not _SOURCE_COMMIT_RE.fullmatch(args.require_source_commit):
        parser.error("--require-source-commit must be a 40-character lowercase commit")
    if args.require_drift_version is not None and not args.require_drift_version.strip():
        parser.error("--require-drift-version must be non-empty")

    manifest_path = _absolute(args.manifest)
    try:
        manifest = ModelManifest.load(manifest_path)
    except (ManifestError, OSError) as exc:
        parser.error(str(exc))

    valid_reports: list[ValidatedReport] = []
    report_errors: list[dict[str, str]] = []
    machine_profiles: dict[str, str] = {}
    for report_index, supplied_path in enumerate(args.reports, start=1):
        report_path = _absolute(supplied_path)
        report_id = f"input-{report_index}"
        try:
            validated = validate_local_report(
                report_path,
                _load_json(report_path),
                manifest,
                report_id=report_id,
            )
            normalized_machine_id = validated.machine_id.casefold()
            previous_profile = machine_profiles.get(normalized_machine_id)
            if previous_profile is not None:
                raise EvidenceError(
                    f"machine_id {validated.machine_id!r} is reused across profiles "
                    f"{previous_profile!r} and {validated.profile!r}"
                )
            machine_profiles[normalized_machine_id] = validated.profile
            valid_reports.append(validated)
        except (EvidenceError, OSError, UnicodeError, json.JSONDecodeError) as exc:
            report_errors.append({"report": report_id, "error": _describe_report_error(exc)})

    coverage: dict[str, list[dict[str, Any]]] = {}
    for validated in valid_reports:
        coverage.setdefault(validated.profile, []).append(validated.to_dict())
    for entries in coverage.values():
        entries.sort(key=lambda entry: (entry["machine_id"], entry["generated_at"], entry["report"]))

    matrix_errors: list[str] = []
    source_commit: str | None = None
    drift_version: str | None = None
    if valid_reports:
        if any(report.source_commit is None for report in valid_reports):
            matrix_errors.append("every matrix report must record a source commit")
        else:
            source_commits = sorted({str(report.source_commit) for report in valid_reports})
            if len(source_commits) != 1:
                matrix_errors.append("matrix reports use different source commits")
            else:
                source_commit = source_commits[0]
                if args.require_source_commit is not None and source_commit != args.require_source_commit:
                    matrix_errors.append("matrix source commit does not match --require-source-commit")

        drift_versions = sorted({report.drift_version for report in valid_reports})
        if len(drift_versions) != 1:
            matrix_errors.append("matrix reports use different DRIFT versions")
        else:
            drift_version = drift_versions[0]
            if args.require_drift_version is not None and drift_version != args.require_drift_version:
                matrix_errors.append("matrix DRIFT version does not match --require-drift-version")

    missing_profiles = [profile for profile in required_profiles if profile not in coverage]
    unexpected_profiles = sorted(set(coverage) - set(required_profiles))
    if unexpected_profiles:
        matrix_errors.append(
            "matrix contains profiles that were not explicitly required: " + ", ".join(unexpected_profiles)
        )
    passed = not report_errors and not matrix_errors and not missing_profiles
    result = "passed" if passed else "failed"
    report: dict[str, Any] = {
        "schema_version": MATRIX_QUALIFICATION_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "cross-platform-local-matrix",
        "model": {
            "name": manifest.name,
            "repository": manifest.source.repository,
            "revision": manifest.source.revision,
            "manifest_digest": manifest.digest_id,
            "runtime": manifest.runtime.to_dict(),
        },
        "requirements": {
            "profiles": required_profiles,
            "source_commit": args.require_source_commit,
            "drift_version": args.require_drift_version,
        },
        "source_identity": {
            "source_commit": source_commit,
            "drift": drift_version,
        },
        "coverage": coverage,
        "missing_profiles": missing_profiles,
        "report_errors": report_errors,
        "matrix_errors": matrix_errors,
        "result": result,
        "complete_release_qualification": False,
        "not_covered": [
            "multi-machine routing and interruption recovery",
            "cold-client resource envelope",
            "public-worker route redundancy and soak",
            "signed catalog publication and release bootstrap",
        ],
    }
    if args.output is not None:
        _write_report(args.output, report)
    else:
        json.dump(report, sys.stdout, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    return 0 if result == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
