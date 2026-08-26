"""Validate that a declared external qualification runner fleet is dispatchable.

The GitHub API inventory contains private runner names and identifiers. This
validator consumes that inventory locally and emits only profile-level coverage,
so operators can fail a manual matrix before jobs wait on missing or ambiguous
self-hosted runner labels. The default remains the strict six-profile release fleet.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

RUNNER_FLEET_SCHEMA_VERSION = 1
MAX_INVENTORY_BYTES = 1_000_000
BASE_LABEL = "model-qualification"
PROFILE_SYSTEMS: Mapping[str, str] = {
    "windows-cpu": "windows",
    "windows-cuda": "windows",
    "linux-cpu": "linux",
    "linux-cuda": "linux",
    "macos-cpu": "macos",
    "macos-mps": "macos",
}


class RunnerFleetError(ValueError):
    """The API inventory cannot establish an unambiguous qualification fleet."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate an API snapshot of the six self-hosted model qualification runners",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("inventory", type=Path, help="JSON from GET /repos/{owner}/{repo}/actions/runners")
    parser.add_argument(
        "--require-profile",
        action="append",
        choices=tuple(PROFILE_SYSTEMS),
        help="Profile that must have exactly one online runner; repeatable (default: all six release profiles)",
    )
    parser.add_argument("--output", type=Path, required=True, help="Write a bounded, identity-free readiness report")
    return parser


def _load_inventory(path: Path) -> Any:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise RunnerFleetError("runner inventory is unavailable") from exc
    if size > MAX_INVENTORY_BYTES:
        raise RunnerFleetError("runner inventory exceeds the size limit")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RunnerFleetError("runner inventory is not valid bounded UTF-8 JSON") from exc


def _inventory_runners(value: Any) -> list[Mapping[str, Any]]:
    pages = value if isinstance(value, list) else [value]
    if not pages or any(not isinstance(page, dict) for page in pages):
        raise RunnerFleetError("runner inventory must contain one or more API response objects")

    runners: list[Mapping[str, Any]] = []
    for page in pages:
        page_runners = page.get("runners")
        if not isinstance(page_runners, list):
            raise RunnerFleetError("each runner inventory page must contain a runners array")
        if any(not isinstance(runner, dict) for runner in page_runners):
            raise RunnerFleetError("runner inventory entries must be JSON objects")
        runners.extend(page_runners)
    return runners


def _runner_labels(runner: Mapping[str, Any]) -> set[str]:
    labels = runner.get("labels")
    if not isinstance(labels, list):
        raise RunnerFleetError("runner labels must be a JSON array")

    names: set[str] = set()
    for label in labels:
        if isinstance(label, str):
            name = label
        elif isinstance(label, dict):
            name = label.get("name")
        else:
            raise RunnerFleetError("runner labels must be strings or label objects")
        if not isinstance(name, str) or not name.strip():
            raise RunnerFleetError("runner labels must have non-empty names")
        names.add(name.strip().lower())
    return names


def _empty_report(error: str, required_profiles: Sequence[str] = tuple(PROFILE_SYSTEMS)) -> dict[str, Any]:
    return {
        "schema_version": RUNNER_FLEET_SCHEMA_VERSION,
        "scope": "qualification-runner-fleet-readiness",
        "result": "failed",
        "required_profiles": list(required_profiles),
        "coverage": {},
        "errors": [error],
        "qualification_evidence": False,
        "complete_release_qualification": False,
    }


def validate_inventory(
    value: Any,
    required_profiles: Sequence[str] = tuple(PROFILE_SYSTEMS),
) -> dict[str, Any]:
    required_profiles = tuple(required_profiles)
    if (
        not required_profiles
        or len(required_profiles) != len(set(required_profiles))
        or any(profile not in PROFILE_SYSTEMS for profile in required_profiles)
    ):
        raise RunnerFleetError("required runner profiles must be a non-empty unique supported set")
    runners = _inventory_runners(value)
    errors: list[str] = []
    by_profile: dict[str, list[tuple[str, str]]] = {profile: [] for profile in required_profiles}
    seen_ids: set[int] = set()

    for index, runner in enumerate(runners, start=1):
        try:
            labels = _runner_labels(runner)
        except RunnerFleetError as exc:
            errors.append(f"runner entry {index} is malformed: {exc}")
            continue

        profile_labels = [profile for profile in PROFILE_SYSTEMS if profile in labels]
        selected_profile_labels = [profile for profile in required_profiles if profile in labels]
        if not selected_profile_labels and profile_labels:
            continue
        relevant = BASE_LABEL in labels or bool(selected_profile_labels)
        if not relevant:
            continue

        runner_id = runner.get("id")
        if not isinstance(runner_id, int):
            errors.append(f"qualification runner entry {index} has no integer API id")
        elif runner_id in seen_ids:
            errors.append("runner inventory repeats a qualification runner id")
        else:
            seen_ids.add(runner_id)

        if "self-hosted" not in labels:
            errors.append(f"qualification runner entry {index} is missing the self-hosted label")
        if BASE_LABEL not in labels:
            errors.append(f"qualification runner entry {index} is missing the {BASE_LABEL} label")
        if len(profile_labels) != 1:
            errors.append(f"qualification runner entry {index} must have exactly one qualification profile label")
            continue

        profile = profile_labels[0]
        observed_system = runner.get("os")
        normalized_system = observed_system.strip().lower() if isinstance(observed_system, str) else ""
        status = runner.get("status")
        normalized_status = status.strip().lower() if isinstance(status, str) else ""

        by_profile[profile].append((normalized_system, normalized_status))
        if normalized_system != PROFILE_SYSTEMS[profile]:
            errors.append(f"{profile} runner operating system does not match its profile")
        if normalized_status != "online":
            errors.append(f"{profile} runner is not online")

    coverage: dict[str, Any] = {}
    for profile in required_profiles:
        expected_system = PROFILE_SYSTEMS[profile]
        matches = by_profile[profile]
        coverage[profile] = {
            "registered": len(matches),
            "online": sum(status == "online" for _, status in matches),
            "expected_system": expected_system,
        }
        if len(matches) != 1:
            errors.append(f"{profile} must have exactly one registered qualification runner")

    report = {
        "schema_version": RUNNER_FLEET_SCHEMA_VERSION,
        "scope": "qualification-runner-fleet-readiness",
        "result": "passed" if not errors else "failed",
        "required_profiles": list(required_profiles),
        "coverage": coverage,
        "errors": errors,
        "qualification_evidence": False,
        "complete_release_qualification": False,
    }
    return report


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    path = Path(os.path.abspath(os.fspath(path.expanduser())))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    required_profiles = args.require_profile or list(PROFILE_SYSTEMS)
    try:
        report = validate_inventory(_load_inventory(args.inventory), required_profiles)
    except RunnerFleetError as exc:
        report = _empty_report(str(exc), required_profiles)
    _write_report(args.output, report)
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
