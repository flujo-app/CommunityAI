from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from scripts import qualification_cost_guard as guard

SOURCE_COMMIT = "a" * 40
FLY_RUN_ID = "seed-20260826-a"
FLY_APP = f"communityai-{FLY_RUN_ID}"
FLY_IMAGE = guard.FLY_DISCOVERY_IMAGE_REPOSITORY + "@sha256:" + "b" * 64
FLY_IMAGE_EVIDENCE_DIGEST = "sha256:" + "c" * 64
WINDOWS_IMAGE = "windows-server-2022-dc-v20260814"
LINUX_IMAGE = "ubuntu-2404-noble-amd64-v20260826"


def _ledger(*rows: str) -> str:
    return (
        "# Readiness\n\n"
        "## Cloud authorization and spend ledger\n\n"
        "| Run | Provider | Purpose | Maximum estimate | Observed cost | Cleanup proof | State |\n"
        "| --- | --- | --- | ---: | ---: | --- | --- |\n"
        + "".join(f"{row}\n" for row in rows)
        + "\nRemaining authorized maximum: **USD 100**.\n"
    )


def _authorization(entries=(), **overrides):
    values = {
        "entries": entries,
        "run_id": "qual-20260826-a",
        "provider": "gcp",
        "workload": guard.GCP_QUALIFICATION_WORKLOAD,
        "purpose": "Four-host Windows/Linux qualification fleet",
        "source_commit": SOURCE_COMMIT,
        "maximum_hours": Decimal("14"),
        "project": "community-ai-506321",
        "zone": "us-central1-a",
        "windows_image": WINDOWS_IMAGE,
        "linux_image": LINUX_IMAGE,
        "cuda_fallback_zone": None,
        "cuda_shape": "n1-t4",
        "manual_maximum_usd": None,
        "fly_app": None,
        "fly_region": None,
        "fly_image": None,
        "fly_image_evidence_digest": None,
        "today": date(2026, 8, 26),
    }
    values.update(overrides)
    return guard.build_authorization(**values)


def _fly_discovery_authorization(entries=(), **overrides):
    values = {
        "entries": entries,
        "run_id": FLY_RUN_ID,
        "provider": "fly",
        "workload": guard.FLY_DISCOVERY_SEED_WORKLOAD,
        "purpose": "Gate 11 second-provider discovery seed",
        "source_commit": SOURCE_COMMIT,
        "maximum_hours": Decimal("168"),
        "project": None,
        "zone": None,
        "windows_image": None,
        "linux_image": None,
        "cuda_fallback_zone": None,
        "cuda_shape": "n1-t4",
        "manual_maximum_usd": Decimal("10"),
        "fly_app": FLY_APP,
        "fly_region": "iad",
        "fly_image": FLY_IMAGE,
        "fly_image_evidence_digest": FLY_IMAGE_EVIDENCE_DIGEST,
        "today": date(2026, 8, 26),
    }
    values.update(overrides)
    return guard.build_authorization(**values)


def test_empty_placeholder_ledger_has_no_commitment():
    entries = guard.parse_spend_ledger(_ledger("| No new paid run recorded | — | — | USD 0 | USD 0 | — | READY |"))

    assert entries == ()


def test_ledger_counts_unresolved_maximum_and_cleaned_observed_cost():
    entries = guard.parse_spend_ledger(
        _ledger(
            "| fly-open | Fly | recovery | USD 20 | — | Not provisioned | PLANNED |",
            "| gcp-clean | GCP | preflight | USD 10 | USD 3.25 | all deleted | CLEANED |",
        )
    )

    assert entries[0].committed_usd == Decimal("20")
    assert entries[1].committed_usd == Decimal("3.25")
    assert sum((entry.committed_usd for entry in entries), Decimal("0")) == Decimal("23.25")


@pytest.mark.parametrize(
    "row, message",
    [
        ("| broken | GCP | purpose | 10 | — | none | PLANNED |", "form USD"),
        ("| broken | GCP | purpose | USD -1 | — | none | PLANNED |", "form USD"),
        ("| too | many | ledger | columns | in | this | row | now |", "seven columns"),
    ],
)
def test_ledger_rejects_malformed_rows(row, message):
    with pytest.raises(guard.CostGuardError, match=message):
        guard.parse_spend_ledger(_ledger(row))


def test_ledger_rejects_missing_table_and_malformed_empty_placeholder():
    with pytest.raises(guard.CostGuardError, match="table is empty or missing"):
        guard.parse_spend_ledger("# Readiness\n\n## Cloud authorization and spend ledger\n")

    with pytest.raises(guard.CostGuardError, match="placeholder is malformed"):
        guard.parse_spend_ledger(_ledger("| No new paid run recorded | — | — | USD 99 | USD 0 | — | READY |"))


def test_ledger_rejects_duplicate_run_ids():
    content = _ledger(
        "| repeated | GCP | first | USD 1 | — | none | PLANNED |",
        "| repeated | Fly | second | USD 2 | — | none | PLANNED |",
    )

    with pytest.raises(guard.CostGuardError, match="repeats"):
        guard.parse_spend_ledger(content)


def test_ledger_requires_cleanup_proof_before_discounting_a_cleaned_run():
    with pytest.raises(guard.CostGuardError, match="requires cleanup proof"):
        guard.parse_spend_ledger(_ledger("| gcp-clean | GCP | preflight | USD 60 | USD 2 | — | CLEANED |"))

    entries = guard.parse_spend_ledger(_ledger("| gcp-active | GCP | preflight | USD 20 | USD 25 | pending | ACTIVE |"))
    assert entries[0].committed_usd == Decimal("25")


def test_gcp_plan_is_exact_bounded_and_does_not_authorize_unreserved_provisioning():
    report = _authorization()

    assert report["maximum_estimate_usd"] == "69.00"
    assert report["remaining_before_run_usd"] == "100.00"
    assert report["remaining_after_run_maximum_usd"] == "31.00"
    assert report["reservation_recorded"] is False
    assert report["provisioning_authorized"] is False
    assert "USD 69.00" in report["required_ledger_row"]
    assert f"[workload {guard.GCP_QUALIFICATION_WORKLOAD}]" in report["required_ledger_row"]
    assert f"[source {SOURCE_COMMIT}]" in report["required_ledger_row"]
    assert report["workload"] == guard.GCP_QUALIFICATION_WORKLOAD
    assert report["pricing_as_of"] == "2026-08-26"

    plan = report["provider_plan"]
    assert plan["project"] == "community-ai-506321"
    assert plan["zone"] == "us-central1-a"
    assert {resource["profile"] for resource in plan["resources"]} == {
        "windows-cpu",
        "windows-cuda",
        "linux-cpu",
        "linux-cuda",
    }
    assert sum(resource["gpu"] is not None for resource in plan["resources"]) == 2
    assert all(resource["external_address"] is False for resource in plan["resources"])
    assert all(resource["service_account"] is False for resource in plan["resources"])
    assert {resource["image"] for resource in plan["resources"]} == {
        WINDOWS_IMAGE,
        LINUX_IMAGE,
    }
    assert len(plan["verify_create_commands"]) == 4
    assert all("--image-family" not in command for command in plan["create_commands"])
    instance_creates = [
        command for command in plan["create_commands"] if command[:4] == ["gcloud", "compute", "instances", "create"]
    ]
    assert len(instance_creates) == 4
    assert [command[4] for command in instance_creates] == [
        "caiq-qual-20260826-a-win-cuda",
        "caiq-qual-20260826-a-lin-cuda",
        "caiq-qual-20260826-a-win-cpu",
        "caiq-qual-20260826-a-lin-cpu",
    ]
    assert all("--accelerator" in command for command in instance_creates[:2])
    assert all("--accelerator" not in command for command in instance_creates[2:])
    assert all(
        command[command.index("--max-run-duration") + 1] == "50400s"
        and command[command.index("--instance-termination-action") + 1] == "DELETE"
        for command in instance_creates
    )

    encoded = guard.json.dumps(plan, sort_keys=True)
    assert "communityai-bootstrap-1" not in encoded
    assert "$" + "{" not in encoded
    assert "*" not in encoded
    assert all(isinstance(command, list) for command in plan["create_commands"])
    assert all(isinstance(argument, str) for command in plan["create_commands"] for argument in command)
    assert len(plan["verify_cleanup_commands"]) == 9


def test_split_region_plan_uses_exact_images_and_zone_scoped_cleanup():
    report = _authorization(cuda_fallback_zone="us-east1-c")

    assert report["maximum_estimate_usd"] == "69.00"
    assert report["cost_assumptions"]["region_count"] == "2"
    assert report["cost_assumptions"]["fallback_nat_ip_hourly_usd"] == "0.005"

    plan = report["provider_plan"]
    assert plan["cuda_fallback_zone"] == "us-east1-c"
    assert {regional["region"] for regional in plan["regional_networks"]} == {
        "us-central1",
        "us-east1",
    }
    assert {regional["subnet_range"] for regional in plan["regional_networks"]} == {
        guard.GCP_PRIMARY_SUBNET_RANGE,
        guard.GCP_FALLBACK_SUBNET_RANGE,
    }
    resources = {resource["profile"]: resource for resource in plan["resources"]}
    assert resources["windows-cuda"]["zone"] == "us-central1-a"
    assert resources["linux-cuda"]["zone"] == "us-east1-c"
    assert resources["windows-cpu"]["image"] == WINDOWS_IMAGE
    assert resources["linux-cpu"]["image"] == LINUX_IMAGE
    instance_creates = [
        command for command in plan["create_commands"] if command[:4] == ["gcloud", "compute", "instances", "create"]
    ]
    assert [command[4] for command in instance_creates[:2]] == [
        "caiq-qual-20260826-a-win-cuda",
        "caiq-qual-20260826-a-lin-cuda",
    ]
    assert instance_creates[1][instance_creates[1].index("--zone") + 1] == "us-east1-c"
    assert len(plan["verify_cleanup_commands"]) == 11

    instance_deletes = [
        command for command in plan["cleanup_commands"] if command[:4] == ["gcloud", "compute", "instances", "delete"]
    ]
    assert {command[command.index("--zone") + 1] for command in instance_deletes} == {
        "us-central1-a",
        "us-east1-c",
    }
    encoded = guard.json.dumps(plan, sort_keys=True)
    assert "--image-family" not in encoded
    assert WINDOWS_IMAGE in encoded
    assert LINUX_IMAGE in encoded


def test_g2_l4_plan_uses_included_gpus_balanced_disks_and_shorter_deadline():
    report = _authorization(
        cuda_fallback_zone="us-east1-b",
        cuda_shape="g2-l4",
        maximum_hours=Decimal("13.5"),
    )

    assert report["maximum_estimate_usd"] == "69.00"
    assert report["cost_assumptions"]["calculated_hourly_usd"] == "3.45"
    assert report["cost_assumptions"]["cuda_machine_type"] == "g2-standard-8"
    assert report["cost_assumptions"]["cuda_accelerator"] == "nvidia-l4"
    assert report["cost_assumptions"]["l4_count"] == "2"
    assert report["cost_assumptions"]["t4_count"] == "0"
    assert report["cost_assumptions"]["l4_price_included_in_cuda_machine"] == "true"
    assert report["cost_assumptions"]["cuda_disk_type"] == "pd-balanced"

    plan = report["provider_plan"]
    assert plan["cuda_shape"] == "g2-l4"
    resources = {resource["profile"]: resource for resource in plan["resources"]}
    assert resources["windows-cuda"]["machine_type"] == "g2-standard-8"
    assert resources["linux-cuda"]["machine_type"] == "g2-standard-8"
    assert resources["windows-cuda"]["gpu"] == "nvidia-l4"
    assert resources["linux-cuda"]["gpu"] == "nvidia-l4"
    assert resources["windows-cuda"]["boot_disk_type"] == "pd-balanced"
    assert resources["linux-cuda"]["boot_disk_type"] == "pd-balanced"
    assert resources["windows-cpu"]["machine_type"] == "n1-highmem-8"
    assert resources["linux-cpu"]["boot_disk_type"] == "pd-standard"

    instance_creates = [
        command for command in plan["create_commands"] if command[:4] == ["gcloud", "compute", "instances", "create"]
    ]
    assert all(command[command.index("--machine-type") + 1] == "g2-standard-8" for command in instance_creates[:2])
    assert all(command[command.index("--boot-disk-type") + 1] == "pd-balanced" for command in instance_creates[:2])
    assert all("--accelerator" not in command for command in instance_creates)
    assert all("--maintenance-policy" in command for command in instance_creates[:2])
    assert all("--maintenance-policy" not in command for command in instance_creates[2:])
    assert all(command[command.index("--max-run-duration") + 1] == "48600s" for command in instance_creates)


def test_g2_l4_full_fourteen_hours_exceeds_the_existing_sixty_nine_dollar_envelope():
    report = _authorization(
        cuda_fallback_zone="us-east1-b",
        cuda_shape="g2-l4",
        maximum_hours=Decimal("14"),
    )

    assert report["maximum_estimate_usd"] == "71.00"


def test_split_region_plan_rejects_a_fallback_in_the_primary_region():
    with pytest.raises(guard.CostGuardError, match="different region"):
        _authorization(cuda_fallback_zone="us-central1-b")


def test_plan_rejects_unknown_cuda_shape():
    with pytest.raises(guard.CostGuardError, match="CUDA shape"):
        _authorization(cuda_shape="unknown")


def test_matching_planned_ledger_reservation_authorizes_exact_plan_without_double_counting():
    planned = _authorization()
    reservation = guard.LedgerEntry(
        run_id="qual-20260826-a",
        provider="GCP",
        purpose=planned["ledger_purpose"],
        maximum_usd=Decimal("69"),
        observed_usd=None,
        cleanup_proof="Not provisioned",
        state="PLANNED",
    )

    report = _authorization(entries=(reservation,))

    assert report["reservation_recorded"] is True
    assert report["provisioning_authorized"] is True
    assert report["remaining_before_run_usd"] == "100.00"
    assert report["remaining_after_run_maximum_usd"] == "31.00"


def test_gcp_reservation_cannot_authorize_a_mutated_provider_plan():
    planned = _authorization()
    reservation = guard.LedgerEntry(
        run_id="qual-20260826-a",
        provider="GCP",
        purpose=planned["ledger_purpose"],
        maximum_usd=Decimal("69"),
        observed_usd=None,
        cleanup_proof="Not provisioned",
        state="PLANNED",
    )

    for mutation in ({"zone": "us-central1-b"}, {"project": "community-ai-506322"}):
        with pytest.raises(guard.CostGuardError, match="purpose/source/plan"):
            _authorization(entries=(reservation,), **mutation)


def test_reservation_must_match_the_exact_purpose_and_source_commit():
    reservation = guard.LedgerEntry(
        run_id="qual-20260826-a",
        provider="GCP",
        purpose="Four-host Windows/Linux qualification fleet",
        maximum_usd=Decimal("69"),
        observed_usd=None,
        cleanup_proof="Not provisioned",
        state="PLANNED",
    )

    with pytest.raises(guard.CostGuardError, match="purpose/source"):
        _authorization(entries=(reservation,))


def test_other_provider_reservations_share_the_same_ceiling():
    fly = guard.LedgerEntry(
        run_id="fly-recovery-a",
        provider="FLY",
        purpose="recovery",
        maximum_usd=Decimal("32"),
        observed_usd=None,
        cleanup_proof="Not provisioned",
        state="PLANNED",
    )

    with pytest.raises(guard.CostGuardError, match="USD 100"):
        _authorization(entries=(fly,))


def test_cleaned_observed_cost_leaves_room_for_later_plan():
    cleaned = guard.LedgerEntry(
        run_id="prior-gcp",
        provider="GCP",
        purpose="preflight",
        maximum_usd=Decimal("40"),
        observed_usd=Decimal("3"),
        cleanup_proof="all deleted",
        state="CLEANED",
    )

    report = _authorization(entries=(cleaned,))

    assert report["ledger_committed_before_run_usd"] == "3.00"
    assert report["remaining_before_run_usd"] == "97.00"
    assert report["remaining_after_run_maximum_usd"] == "28.00"


def test_gcp_plan_rejects_stale_prices_and_overlong_lifetime():
    with pytest.raises(guard.CostGuardError, match="stale"):
        _authorization(today=date(2026, 9, 26))
    with pytest.raises(guard.CostGuardError, match="no more than 14"):
        _authorization(maximum_hours=Decimal("14.01"))


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"run_id": "QUAL-UPPER"}, "run ID"),
        ({"source_commit": "abc"}, "source commit"),
        ({"project": "INVALID_PROJECT"}, "project"),
        ({"zone": "central"}, "zone"),
        ({"windows_image": "family/windows-2022"}, "image names"),
        ({"purpose": "unsafe|purpose"}, "purpose"),
    ],
)
def test_plan_rejects_unsafe_identity_or_target(overrides, message):
    with pytest.raises(guard.CostGuardError, match=message):
        _authorization(**overrides)


def test_fly_plan_requires_manual_current_maximum_and_uses_combined_ledger():
    with pytest.raises(guard.CostGuardError, match="manual maximum"):
        _authorization(
            provider="fly",
            workload=guard.FLY_RECOVERY_WORKLOAD,
            project=None,
            zone=None,
            windows_image=None,
            linux_image=None,
            cuda_fallback_zone=None,
        )

    report = _authorization(
        provider="fly",
        workload=guard.FLY_RECOVERY_WORKLOAD,
        project=None,
        zone=None,
        windows_image=None,
        linux_image=None,
        cuda_fallback_zone=None,
        manual_maximum_usd=Decimal("12.345"),
    )

    assert report["provider"] == "FLY"
    assert report["maximum_estimate_usd"] == "12.35"
    assert report["remaining_after_run_maximum_usd"] == "87.65"
    assert report["provider_plan"]["resource_count"] == 5
    assert report["provider_plan"]["adapter"] == "scripts/fly_qualification_adapter.py"


def test_fly_discovery_seed_plan_is_exact_and_cannot_reuse_recovery_authorization():
    report = _fly_discovery_authorization()

    assert report["schema_version"] == 2
    assert report["workload"] == guard.FLY_DISCOVERY_SEED_WORKLOAD
    assert report["provisioning_authorized"] is False
    assert report["cleanup_required_for_pass"] is False
    assert report["failure_cleanup_required"] is True
    assert report["persistent_resources_after_pass"] is True
    assert report["provider_plan"]["app"] == FLY_APP
    assert report["provider_plan"]["image"] == FLY_IMAGE
    evidence = report["provider_plan"]["image_publication_evidence"]
    assert evidence["expected_digest"] == FLY_IMAGE_EVIDENCE_DIGEST
    assert evidence["required_repository"] == guard.FLY_DISCOVERY_IMAGE_REPOSITORY
    assert evidence["source_commit"] == SOURCE_COMMIT
    assert evidence["validated_by_cost_guard"] is False
    assert "before provider authentication or calls" in evidence["adapter_validation_contract"]
    assert report["cost_authorization_only"] is True
    assert report["provider_calls_authorized_without_preflight"] is False
    assert report["provider_plan"]["maximum_runtime_hours"] == "168"
    assert report["provider_plan"]["renewal_or_cleanup_deadline"] == ("provisioned_at + maximum_runtime_hours")
    assert report["provider_plan_digest"] in report["ledger_purpose"]
    assert report["provider_plan"]["resource_count"] == 5
    assert {resource["type"] for resource in report["provider_plan"]["resources"]} == {
        "app",
        "machine",
        "volume",
        "shared_ipv4",
        "anycast_ipv6",
    }

    recovery_reservation = guard.LedgerEntry(
        run_id=FLY_RUN_ID,
        provider="FLY",
        purpose="Gate 11 recovery authorization",
        maximum_usd=Decimal("10"),
        observed_usd=None,
        cleanup_proof="Not provisioned",
        state="PLANNED",
    )
    with pytest.raises(guard.CostGuardError, match="purpose/source/plan"):
        _fly_discovery_authorization(entries=(recovery_reservation,))


def test_fly_discovery_exact_reservation_binds_every_mutable_plan_input():
    planned = _fly_discovery_authorization()
    reservation = guard.LedgerEntry(
        run_id=FLY_RUN_ID,
        provider="FLY",
        purpose=planned["ledger_purpose"],
        maximum_usd=Decimal("10"),
        observed_usd=None,
        cleanup_proof="Not provisioned",
        state="PLANNED",
    )

    authorized = _fly_discovery_authorization(entries=(reservation,))
    assert authorized["provisioning_authorized"] is True
    assert authorized["provider_preflight_required"] is True
    assert authorized["provider_calls_authorized_without_preflight"] is False

    mutations = (
        {"fly_region": "ord"},
        {"fly_image": guard.FLY_DISCOVERY_IMAGE_REPOSITORY + "@sha256:" + "d" * 64},
        {"fly_image_evidence_digest": "sha256:" + "e" * 64},
        {"maximum_hours": Decimal("169")},
    )
    for mutation in mutations:
        with pytest.raises(guard.CostGuardError, match="purpose/source/plan"):
            _fly_discovery_authorization(entries=(reservation,), **mutation)


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"fly_app": "other-app"}, "run-derived name"),
        ({"fly_region": "us-east"}, "three-letter region"),
        ({"fly_image": guard.FLY_DISCOVERY_IMAGE_REPOSITORY + ":latest"}, "reviewed GHCR"),
        ({"fly_image": "ghcr.io/other/image@sha256:" + "b" * 64}, "reviewed GHCR"),
        (
            {"fly_image": "ghcr.io/flujo-app//communityai-discovery-seed@sha256:" + "b" * 64},
            "reviewed GHCR",
        ),
        ({"fly_image_evidence_digest": None}, "publication-evidence digest"),
        ({"fly_image_evidence_digest": "SHA256:" + "c" * 64}, "publication-evidence digest"),
        ({"maximum_hours": Decimal("0")}, "greater than zero"),
        ({"maximum_hours": Decimal("744.01")}, "no more than 744"),
    ],
)
def test_fly_discovery_seed_plan_rejects_unsafe_targets(overrides, message):
    with pytest.raises(guard.CostGuardError, match=message):
        _fly_discovery_authorization(**overrides)


def test_provider_and_workload_must_match():
    with pytest.raises(guard.CostGuardError, match="not valid for provider"):
        _authorization(workload=guard.FLY_DISCOVERY_SEED_WORKLOAD)


def test_cli_writes_bounded_plan_without_provider_calls(tmp_path, capsys):
    ledger = tmp_path / "readiness.md"
    ledger.write_text(
        _ledger("| No new paid run recorded | — | — | USD 0 | USD 0 | — | READY |"),
        encoding="utf-8",
    )
    output = tmp_path / "authorization.json"

    assert (
        guard.main(
            [
                "--run-id",
                "qual-20260826-a",
                "--provider",
                "gcp",
                "--workload",
                "gcp-qualification-fleet",
                "--purpose",
                "Four-host Windows/Linux qualification fleet",
                "--source-commit",
                SOURCE_COMMIT,
                "--project",
                "community-ai-506321",
                "--zone",
                "us-central1-a",
                "--windows-image",
                WINDOWS_IMAGE,
                "--linux-image",
                LINUX_IMAGE,
                "--ledger",
                str(ledger),
                "--output",
                str(output),
            ]
        )
        == 0
    )

    report = guard.json.loads(output.read_text(encoding="utf-8"))
    stdout = guard.json.loads(capsys.readouterr().out)
    assert stdout == {
        "provisioning_authorized": False,
        "result": "passed",
        "run_id": "qual-20260826-a",
    }
    assert report["maximum_estimate_usd"] == "69.00"
    assert output.stat().st_size < guard.MAX_OUTPUT_BYTES


def test_repository_readiness_ledger_remains_machine_readable():
    repository = Path(__file__).resolve().parents[1]

    entries = guard.load_spend_ledger(repository / "docs" / "RELEASE_READINESS.md")

    assert all(entry.maximum_usd <= guard.CLOUD_CEILING_USD for entry in entries)
