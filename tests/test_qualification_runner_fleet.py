import json
from pathlib import Path

from scripts import validate_qualification_runner_fleet as fleet


def _runner(runner_id: int, profile: str, *, status: str = "online", system: str | None = None) -> dict:
    expected_system = fleet.PROFILE_SYSTEMS[profile]
    return {
        "id": runner_id,
        "name": f"private-{profile}-{runner_id}",
        "os": system or expected_system,
        "status": status,
        "busy": False,
        "labels": [
            {"name": "self-hosted"},
            {"name": fleet.BASE_LABEL},
            {"name": profile},
        ],
    }


def _inventory() -> dict:
    return {
        "total_count": len(fleet.PROFILE_SYSTEMS),
        "runners": [_runner(index, profile) for index, profile in enumerate(fleet.PROFILE_SYSTEMS, start=1)],
    }


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_runner_fleet_defaults_to_exact_online_public_alpha_inventory_without_private_identity(tmp_path):
    inventory = tmp_path / "private-runner-inventory.json"
    output = tmp_path / "readiness.json"
    _write(inventory, _inventory())

    assert fleet.main([str(inventory), "--output", str(output)]) == 0

    serialized = output.read_text(encoding="utf-8")
    report = json.loads(serialized)
    assert report["result"] == "passed"
    assert report["required_profiles"] == list(fleet.PUBLIC_ALPHA_PROFILES)
    assert report["errors"] == []
    assert all(value["registered"] == 1 for value in report["coverage"].values())
    assert report["qualification_evidence"] is False
    assert report["complete_release_qualification"] is False
    assert "private-" not in serialized
    assert '"id"' not in serialized
    assert str(tmp_path) not in serialized


def test_runner_fleet_accepts_explicit_deferred_macos_profiles_separately(tmp_path):
    required_profiles = ["macos-cpu", "macos-mps"]
    value = _inventory()
    value["runners"] = [
        runner for runner in value["runners"] if any(label["name"] in required_profiles for label in runner["labels"])
    ]
    inventory = tmp_path / "private-runner-inventory.json"
    output = tmp_path / "readiness.json"
    _write(inventory, value)

    args = [str(inventory)]
    for profile in required_profiles:
        args.extend(("--require-profile", profile))
    args.extend(("--output", str(output)))

    assert fleet.main(args) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["result"] == "passed"
    assert report["required_profiles"] == required_profiles
    assert set(report["coverage"]) == set(required_profiles)
    assert report["complete_release_qualification"] is False


def test_runner_fleet_never_serializes_api_supplied_operating_system(tmp_path):
    sentinel = "c:/private-path-sentinel"
    value = _inventory()
    value["runners"][0]["os"] = sentinel
    inventory = tmp_path / "inventory.json"
    output = tmp_path / "readiness.json"
    _write(inventory, value)

    assert fleet.main([str(inventory), "--output", str(output)]) == 1

    serialized = output.read_text(encoding="utf-8")
    report = json.loads(serialized)
    assert "windows-cpu runner operating system does not match its profile" in report["errors"]
    assert sentinel not in serialized
    assert "observed_systems" not in serialized


def test_runner_fleet_rejects_missing_offline_and_wrong_system_profiles(tmp_path):
    value = _inventory()
    value["runners"] = [
        runner for runner in value["runners"] if not any(label["name"] == "linux-cuda" for label in runner["labels"])
    ]
    value["runners"][0]["status"] = "offline"
    value["runners"][1]["os"] = "linux"
    inventory = tmp_path / "inventory.json"
    output = tmp_path / "readiness.json"
    _write(inventory, value)

    assert fleet.main([str(inventory), "--output", str(output)]) == 1

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["result"] == "failed"
    assert "windows-cpu runner is not online" in report["errors"]
    assert "windows-cuda runner operating system does not match its profile" in report["errors"]
    assert "linux-cuda must have exactly one registered qualification runner" in report["errors"]


def test_runner_fleet_rejects_duplicate_and_multi_profile_routing(tmp_path):
    value = _inventory()
    duplicate = _runner(99, "linux-cpu")
    value["runners"].append(duplicate)
    value["runners"][0]["labels"].append({"name": "linux-cuda"})
    inventory = tmp_path / "inventory.json"
    output = tmp_path / "readiness.json"
    _write(inventory, value)

    assert fleet.main([str(inventory), "--output", str(output)]) == 1

    report = json.loads(output.read_text(encoding="utf-8"))
    assert any("exactly one qualification profile label" in error for error in report["errors"])
    assert "linux-cpu must have exactly one registered qualification runner" in report["errors"]
    assert "windows-cpu must have exactly one registered qualification runner" in report["errors"]


def test_runner_fleet_accepts_paginated_github_cli_slurp_shape(tmp_path):
    runners = _inventory()["runners"]
    inventory = tmp_path / "inventory.json"
    output = tmp_path / "readiness.json"
    _write(
        inventory,
        [
            {"total_count": len(runners), "runners": runners[:3]},
            {"total_count": len(runners), "runners": runners[3:]},
        ],
    )

    assert fleet.main([str(inventory), "--output", str(output)]) == 0


def test_runner_fleet_emits_bounded_failure_without_input_path(tmp_path, monkeypatch):
    inventory = tmp_path / "private" / "inventory.json"
    inventory.parent.mkdir()
    inventory.write_text("{}", encoding="utf-8")
    output = tmp_path / "readiness.json"
    monkeypatch.setattr(fleet, "MAX_INVENTORY_BYTES", 1)

    assert fleet.main([str(inventory), "--output", str(output)]) == 1

    serialized = output.read_text(encoding="utf-8")
    report = json.loads(serialized)
    assert report["coverage"] == {}
    assert report["errors"] == ["runner inventory exceeds the size limit"]
    assert str(tmp_path) not in serialized
