import hashlib
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
PRIMARY_IMAGE = guard.GCP_PRIMARY_IMAGE_REPOSITORY + "@sha256:" + "d" * 64
STANDBY_IMAGE = guard.GCP_STANDBY_IMAGE_REPOSITORY + "@sha256:" + "e" * 64
PRIMARY_IMAGE_EVIDENCE_DIGEST = "sha256:" + "f" * 64
STANDBY_IMAGE_EVIDENCE_DIGEST = "sha256:" + "1" * 64
RUNTIME_BOOTSTRAP_PATH = Path(__file__).resolve().parents[1] / "scripts" / "gcp_public_route_startup.sh"
RUNTIME_BOOTSTRAP_PAYLOAD = RUNTIME_BOOTSTRAP_PATH.read_bytes()
RUNTIME_BOOTSTRAP_DIGEST = "sha256:" + hashlib.sha256(RUNTIME_BOOTSTRAP_PAYLOAD).hexdigest()
RUNTIME_BOOTSTRAP_BYTES = len(RUNTIME_BOOTSTRAP_PAYLOAD)
HOST_CONTROLLER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "gcp_public_route_host.py"
HOST_CONTROLLER_PAYLOAD = HOST_CONTROLLER_PATH.read_bytes()
HOST_CONTROLLER_DIGEST = "sha256:" + hashlib.sha256(HOST_CONTROLLER_PAYLOAD).hexdigest()
HOST_CONTROLLER_BYTES = len(HOST_CONTROLLER_PAYLOAD)
ACCEPTANCE_PROBE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "public_route_acceptance.py"
ACCEPTANCE_PROBE_PAYLOAD = ACCEPTANCE_PROBE_PATH.read_bytes()
ACCEPTANCE_PROBE_DIGEST = "sha256:" + hashlib.sha256(ACCEPTANCE_PROBE_PAYLOAD).hexdigest()
ACCEPTANCE_PROBE_BYTES = len(ACCEPTANCE_PROBE_PAYLOAD)
INITIAL_PEER = "/ip4/34.42.181.232/tcp/31337/p2p/QmYwAPJzv5CZsnAzt8auVZRnGi2Cj8Xn4K6q5V9z8M2w7P"


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


def _gcp_public_route_authorization(entries=(), **overrides):
    values = {
        "entries": entries,
        "run_id": "route-20260829-a",
        "provider": "gcp",
        "workload": guard.GCP_PUBLIC_ROUTE_WORKLOAD,
        "purpose": "Gate 11 finite Qwen primary and Gemma standby routes",
        "source_commit": SOURCE_COMMIT,
        "maximum_hours": Decimal("14"),
        "project": "community-ai-506321",
        "zone": "us-central1-a",
        "windows_image": None,
        "linux_image": LINUX_IMAGE,
        "cuda_fallback_zone": None,
        "cuda_shape": "g2-l4",
        "manual_maximum_usd": None,
        "fly_app": None,
        "fly_region": None,
        "fly_image": None,
        "fly_image_evidence_digest": None,
        "primary_image": PRIMARY_IMAGE,
        "primary_image_evidence_digest": PRIMARY_IMAGE_EVIDENCE_DIGEST,
        "standby_image": STANDBY_IMAGE,
        "standby_image_evidence_digest": STANDBY_IMAGE_EVIDENCE_DIGEST,
        "runtime_bootstrap_digest": RUNTIME_BOOTSTRAP_DIGEST,
        "runtime_bootstrap_bytes": RUNTIME_BOOTSTRAP_BYTES,
        "initial_peer": INITIAL_PEER,
        "host_controller_digest": HOST_CONTROLLER_DIGEST,
        "host_controller_bytes": HOST_CONTROLLER_BYTES,
        "acceptance_probe_digest": ACCEPTANCE_PROBE_DIGEST,
        "acceptance_probe_bytes": ACCEPTANCE_PROBE_BYTES,
        "today": date(2026, 8, 29),
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


def test_canceled_unprovisioned_run_commits_observed_zero_not_stale_maximum():
    entries = guard.parse_spend_ledger(
        _ledger("| canceled | GCP | stopped before create | USD 10 | USD 0 | no resources created | CANCELED |")
    )

    assert entries[0].committed_usd == Decimal("0")

    with pytest.raises(guard.CostGuardError, match="requires cleanup proof"):
        guard.parse_spend_ledger(
            _ledger("| canceled | GCP | stopped before create | USD 10 | USD 0 | Not provisioned | CANCELED |")
        )
    with pytest.raises(guard.CostGuardError, match="observed cost"):
        guard.parse_spend_ledger(
            _ledger("| canceled | GCP | stopped before create | USD 10 | — | no resources created | CANCELED |")
        )


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

    assert report["maximum_estimate_usd"] == "70.00"
    assert report["remaining_before_run_usd"] == "100.00"
    assert report["remaining_after_run_maximum_usd"] == "30.00"
    assert report["reservation_recorded"] is False
    assert report["provisioning_authorized"] is False
    assert "USD 70.00" in report["required_ledger_row"]
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
    assert plan["one_host_at_a_time"] is True
    assert len(plan["verify_create_commands"]) == 1
    assert all("--image-family" not in command for command in plan["create_commands"])
    assert not any(command[:4] == ["gcloud", "compute", "instances", "create"] for command in plan["create_commands"])
    phases = plan["profile_phases"]
    assert [phase["profile"] for phase in phases] == [
        "windows-cpu",
        "linux-cpu",
        "windows-cuda",
        "linux-cuda",
    ]
    instance_creates = [phase["create_commands"][0] for phase in phases]
    assert [command[4] for command in instance_creates] == [
        "caiq-qual-20260826-a-win-cpu",
        "caiq-qual-20260826-a-lin-cpu",
        "caiq-qual-20260826-a-win-cuda",
        "caiq-qual-20260826-a-lin-cuda",
    ]
    assert all("--accelerator" not in command for command in instance_creates[:2])
    assert all("--accelerator" in command for command in instance_creates[2:])
    assert all(
        command[command.index("--max-run-duration") + 1] == "50400s"
        and command[command.index("--instance-termination-action") + 1] == "DELETE"
        for command in instance_creates
    )
    assert all(len(phase["cleanup_commands"]) == 1 for phase in phases)
    assert all(len(phase["verify_cleanup_commands"]) == 2 for phase in phases)
    assert phases[2]["machine_id"] != phases[3]["machine_id"]
    windows_metadata = instance_creates[0][instance_creates[0].index("--metadata") + 1]
    assert "google-compute-engine-ssh" in windows_metadata
    assert "enable-windows-ssh=TRUE" in windows_metadata

    encoded = guard.json.dumps(plan, sort_keys=True)
    assert "communityai-bootstrap-1" not in encoded
    assert "$" + "{" not in encoded
    assert "*" not in encoded
    assert "--auto-allocate-nat-external-ips" not in encoded
    assert "--nat-external-ip-pool" in encoded
    assert all(isinstance(command, list) for command in plan["create_commands"])
    assert all(isinstance(argument, str) for command in plan["create_commands"] for argument in command)
    assert len(plan["verify_cleanup_commands"]) == 10


def test_split_region_plan_uses_exact_images_and_zone_scoped_cleanup():
    report = _authorization(cuda_fallback_zone="us-east1-c")

    assert report["maximum_estimate_usd"] == "70.00"
    assert report["cost_assumptions"]["region_count"] == "2"
    assert report["cost_assumptions"]["nat_ip_count"] == "2"
    assert report["cost_assumptions"]["nat_ip_hourly_usd"] == "0.010"

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
    instance_creates = [phase["create_commands"][0] for phase in plan["profile_phases"]]
    assert [command[4] for command in instance_creates[-2:]] == [
        "caiq-qual-20260826-a-win-cuda",
        "caiq-qual-20260826-a-lin-cuda",
    ]
    assert instance_creates[-1][instance_creates[-1].index("--zone") + 1] == "us-east1-c"
    assert len(plan["verify_cleanup_commands"]) == 13

    instance_deletes = [phase["cleanup_commands"][0] for phase in plan["profile_phases"]]
    assert {command[command.index("--zone") + 1] for command in instance_deletes} == {
        "us-central1-a",
        "us-east1-c",
    }
    assert {regional["address"] for regional in plan["regional_networks"]} == {
        "caiq-qual-20260826-a-nat-ip",
        "caiq-qual-20260826-a-cuda-nat-ip",
    }
    nat_cleanup_phases = [phase for phase in plan["cleanup_phases"] if phase["role"] in {"primary", "cuda-fallback"}]
    assert all(phase["verify_cleanup_commands"] for phase in nat_cleanup_phases)
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
    assert report["cost_assumptions"]["calculated_compute_hourly_usd"] == "3.45"
    assert report["cost_assumptions"]["calculated_hourly_usd"] == "3.49"
    assert report["cost_assumptions"]["network_maximum_hours"] == "54.0"
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

    instance_creates = {phase["profile"]: phase["create_commands"][0] for phase in plan["profile_phases"]}
    cuda_creates = [instance_creates["windows-cuda"], instance_creates["linux-cuda"]]
    cpu_creates = [instance_creates["windows-cpu"], instance_creates["linux-cpu"]]
    assert all(command[command.index("--machine-type") + 1] == "g2-standard-8" for command in cuda_creates)
    assert all(command[command.index("--boot-disk-type") + 1] == "pd-balanced" for command in cuda_creates)
    assert all("--accelerator" not in command for command in instance_creates.values())
    assert all("--maintenance-policy" in command for command in cuda_creates)
    assert all("--maintenance-policy" not in command for command in cpu_creates)
    assert all(command[command.index("--max-run-duration") + 1] == "48600s" for command in instance_creates.values())
    assert "windows-startup-script-ps1=scripts/gcp_windows_cuda_startup.ps1" in cuda_creates[0]
    assert "startup-script=scripts/gcp_linux_cuda_startup.sh" in cuda_creates[1]
    assert resources["windows-cuda"]["bootstrap"]["windows_cuda_torch"] == "torch==2.6.0+cu124"


def test_g2_startup_scripts_use_pinned_checksum_verified_installers():
    repository_root = Path(__file__).resolve().parents[1]
    linux = (repository_root / "scripts" / "gcp_linux_cuda_startup.sh").read_text(encoding="utf-8")
    windows = (repository_root / "scripts" / "gcp_windows_cuda_startup.ps1").read_text(encoding="utf-8")

    assert "generation=1785935286399764" in linux
    assert "876d7d02e3e1166c105bb0a9148993c3ea9b789a041f78143c928e7ab317c14f" in linux
    assert "sha256sum --check --strict" in linux
    assert "/installer/latest/cuda_installer.pyz" in linux

    assert "e4d32d90993a17795b9f6bc411d2ae6d767052ca/windows/install_gpu_driver.ps1" in windows
    assert "9d3eb7064a19aaf8e043c6eb863a490054105f0c7f8f121cdab76b100a092897" in windows
    assert "Get-FileHash" in windows
    assert "/raw/main/" not in windows
    assert all(secret_word not in (linux + windows).lower() for secret_word in ("ghp_", "password=", "token="))


def test_g2_l4_full_fourteen_hours_exceeds_the_existing_sixty_nine_dollar_envelope():
    report = _authorization(
        cuda_fallback_zone="us-east1-b",
        cuda_shape="g2-l4",
        maximum_hours=Decimal("14"),
    )

    assert report["maximum_estimate_usd"] == "72.00"


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
        maximum_usd=Decimal("70"),
        observed_usd=None,
        cleanup_proof="Not provisioned",
        state="PLANNED",
    )

    report = _authorization(entries=(reservation,))

    assert report["reservation_recorded"] is True
    assert report["provisioning_authorized"] is True
    assert report["remaining_before_run_usd"] == "100.00"
    assert report["remaining_after_run_maximum_usd"] == "30.00"


def test_gcp_reservation_cannot_authorize_a_mutated_provider_plan():
    planned = _authorization()
    reservation = guard.LedgerEntry(
        run_id="qual-20260826-a",
        provider="GCP",
        purpose=planned["ledger_purpose"],
        maximum_usd=Decimal("70"),
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
        maximum_usd=Decimal("70"),
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
    assert report["remaining_after_run_maximum_usd"] == "27.00"


def test_owner_released_cleaned_maximum_does_not_consume_new_budget_epoch():
    released = guard.LedgerEntry(
        run_id="prior-gcp",
        provider="GCP",
        purpose="completed qualification",
        maximum_usd=Decimal("99"),
        observed_usd=None,
        cleanup_proof="all exact resources absent",
        state="CLEANED-RELEASED",
    )

    report = _authorization(entries=(released,))

    assert released.committed_usd == Decimal("0")
    assert report["ledger_committed_before_run_usd"] == "0.00"
    assert report["remaining_before_run_usd"] == "100.00"
    assert report["remaining_after_run_maximum_usd"] == "30.00"


def test_owner_released_state_requires_cleanup_proof():
    with pytest.raises(guard.CostGuardError, match="CLEANED-RELEASED state requires cleanup proof"):
        guard.parse_spend_ledger(
            _ledger("| prior-gcp | GCP | completed qualification | USD 99 | — | — | CLEANED-RELEASED |")
        )


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


def test_gcp_public_route_plan_binds_finite_routes_health_and_cleanup():
    report = _gcp_public_route_authorization()

    assert report["workload"] == guard.GCP_PUBLIC_ROUTE_WORKLOAD
    assert report["maximum_estimate_usd"] == "26.00"
    assert report["remaining_after_run_maximum_usd"] == "74.00"
    assert report["provisioning_authorized"] is False
    assert report["cleanup_required_for_pass"] is False
    assert report["failure_cleanup_required"] is True
    assert report["persistent_resources_after_pass"] is True

    plan = report["provider_plan"]
    assert plan["maximum_runtime_hours"] == "14"
    assert plan["machine"] == {
        "machine_type": "g2-standard-8",
        "accelerator": "NVIDIA L4",
        "boot_disk_gib": 200,
        "boot_disk_type": "pd-balanced",
        "os_image": LINUX_IMAGE,
        "service_account": False,
        "scopes": [],
        "public_ipv4": "ephemeral and bound to the instance lifetime",
        "public_tcp_ports": [31337, 31338],
        "max_run_seconds": 50400,
        "termination_action": "DELETE",
    }
    assert [(route["role"], route["candidate"], route["manifest_digest"]) for route in plan["routes"]] == [
        ("primary", "qwen3.5-2b", guard.GCP_PRIMARY_MANIFEST_DIGEST),
        ("standby", "gemma-4-e2b", guard.GCP_STANDBY_MANIFEST_DIGEST),
    ]
    assert plan["routes"][0]["image"] == PRIMARY_IMAGE
    assert plan["routes"][1]["image"] == STANDBY_IMAGE
    assert plan["runtime_bootstrap"] == {
        "relative_path": "scripts/gcp_public_route_startup.sh",
        "sha256": RUNTIME_BOOTSTRAP_DIGEST,
        "byte_size": RUNTIME_BOOTSTRAP_BYTES,
        "source_commit_bound": True,
        "validated_by_cost_guard": False,
    }
    assert plan["host_controller"] == {
        "relative_path": "scripts/gcp_public_route_host.py",
        "sha256": HOST_CONTROLLER_DIGEST,
        "byte_size": HOST_CONTROLLER_BYTES,
        "source_commit_bound": True,
        "validated_by_cost_guard": False,
    }
    assert plan["acceptance_probe"] == {
        "relative_path": "scripts/public_route_acceptance.py",
        "sha256": ACCEPTANCE_PROBE_DIGEST,
        "byte_size": ACCEPTANCE_PROBE_BYTES,
        "source_commit_bound": True,
        "validated_by_cost_guard": False,
    }
    assert plan["initial_peer"] == INITIAL_PEER
    assert plan["operating_contract"]["resource_ceilings"] == {
        "qwen_device_memory_gib": 7,
        "gemma_device_memory_gib": 15,
        "combined_device_memory_gib": 22,
        "host_memory_gib": 30,
        "route_storage_gib": 160,
        "combined_logs_gib": 1,
        "qualification_claim": False,
    }
    assert "not independent redundancy" in plan["topology"]
    assert plan["operating_contract"]["health_sample_period_seconds"] == 300
    assert any("auto selects Qwen" in item for item in plan["operating_contract"]["required_ready_evidence"])
    assert any("unavailable" in item for item in plan["operating_contract"]["stop_conditions"])
    assert "both routes" in plan["operating_contract"]["disable_contract"]
    assert "no redundancy claim" in plan["operating_contract"]["degraded_contract"]
    assert len(plan["create_commands"]) == 5
    assert len(plan["cleanup_commands"]) == 5
    assert len(plan["verify_cleanup_commands"]) == 6
    iap_firewall = plan["create_commands"][-2]
    assert "tcp:22" in iap_firewall
    assert guard.GCP_IAP_SOURCE_RANGE in iap_firewall
    create_instance = plan["create_commands"][-1]
    assert "startup-script=scripts/gcp_public_route_startup.sh" in create_instance
    verify_instance = plan["verify_create_commands"][0]
    assert verify_instance[verify_instance.index("--format") + 1] == (
        "json(status,machineType,scheduling.maxRunDuration," "networkInterfaces[0].accessConfigs[0].natIP)"
    )
    assert ["--max-run-duration", "50400s"] == create_instance[
        create_instance.index("--max-run-duration") : create_instance.index("--max-run-duration") + 2
    ]
    assert ["--instance-termination-action", "DELETE"] == create_instance[
        create_instance.index("--instance-termination-action") : create_instance.index("--instance-termination-action")
        + 2
    ]
    flattened_cleanup = {part for command in plan["cleanup_commands"] for part in command}
    assert "communityai-bootstrap-1" not in flattened_cleanup
    assert report["provider_plan_digest"] in report["ledger_purpose"]


def test_gcp_public_route_exact_reservation_binds_every_mutable_input():
    planned = _gcp_public_route_authorization()
    reservation = guard.LedgerEntry(
        run_id=planned["run_id"],
        provider="GCP",
        purpose=planned["ledger_purpose"],
        maximum_usd=Decimal("26"),
        observed_usd=None,
        cleanup_proof="Not provisioned",
        state="PLANNED",
    )

    authorized = _gcp_public_route_authorization(entries=(reservation,))
    assert authorized["provisioning_authorized"] is True
    assert authorized["remaining_after_run_maximum_usd"] == "74.00"

    mutations = (
        {"zone": "us-east1-b"},
        {"maximum_hours": Decimal("13")},
        {"primary_image": guard.GCP_PRIMARY_IMAGE_REPOSITORY + "@sha256:" + "2" * 64},
        {"primary_image_evidence_digest": "sha256:" + "3" * 64},
        {"standby_image": guard.GCP_STANDBY_IMAGE_REPOSITORY + "@sha256:" + "4" * 64},
        {"standby_image_evidence_digest": "sha256:" + "5" * 64},
        {"runtime_bootstrap_digest": "sha256:" + "6" * 64},
        {"runtime_bootstrap_bytes": RUNTIME_BOOTSTRAP_BYTES - 1},
        {"initial_peer": INITIAL_PEER.replace("34.42.181.232", "35.42.181.232")},
        {"host_controller_digest": "sha256:" + "7" * 64},
        {"host_controller_bytes": HOST_CONTROLLER_BYTES - 1},
        {"acceptance_probe_digest": "sha256:" + "8" * 64},
        {"acceptance_probe_bytes": ACCEPTANCE_PROBE_BYTES - 1},
    )
    for mutation in mutations:
        with pytest.raises(guard.CostGuardError, match="purpose/source/plan"):
            _gcp_public_route_authorization(entries=(reservation,), **mutation)


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"windows_image": WINDOWS_IMAGE}, "does not accept Windows"),
        ({"cuda_shape": "n1-t4"}, "requires g2-l4"),
        ({"maximum_hours": Decimal("0")}, "greater than zero"),
        ({"maximum_hours": Decimal("14.01")}, "no more than 14"),
        ({"primary_image": guard.GCP_PRIMARY_IMAGE_REPOSITORY + ":latest"}, "immutable Qwen"),
        ({"standby_image": guard.GCP_STANDBY_IMAGE_REPOSITORY + ":latest"}, "immutable Gemma"),
        (
            {"primary_image": "ghcr.io/flujo-app/communityai-qualification-qwen3.5-2b@sha256:" + "2" * 64},
            "immutable Qwen CUDA route",
        ),
        (
            {"standby_image": "ghcr.io/flujo-app/communityai-qualification-gemma-4-e2b@sha256:" + "3" * 64},
            "immutable Gemma CUDA route",
        ),
        ({"primary_image_evidence_digest": "SHA256:" + "f" * 64}, "publication-evidence digest"),
        ({"standby_image_evidence_digest": None}, "immutable images, evidence digests"),
        ({"runtime_bootstrap_digest": "SHA256:" + "4" * 64}, "canonical source digest"),
        ({"runtime_bootstrap_bytes": 0}, "between 1 and 16384 bytes"),
        ({"runtime_bootstrap_bytes": 16_385}, "between 1 and 16384 bytes"),
        ({"initial_peer": "/ip4/127.0.0.1/tcp/1"}, "authenticated multiaddr"),
        ({"initial_peer": INITIAL_PEER.replace("/tcp", " /tcp")}, "authenticated multiaddr"),
        ({"initial_peer": INITIAL_PEER.replace("/tcp", "\t/tcp")}, "authenticated multiaddr"),
        ({"initial_peer": INITIAL_PEER + "\n"}, "authenticated multiaddr"),
        ({"host_controller_digest": "SHA256:" + "5" * 64}, "canonical source digest"),
        ({"host_controller_bytes": 0}, "between 1 and 131072 bytes"),
        ({"acceptance_probe_digest": "SHA256:" + "6" * 64}, "canonical source digest"),
        ({"acceptance_probe_bytes": 65_537}, "between 1 and 65536 bytes"),
    ],
)
def test_gcp_public_route_plan_rejects_unsafe_or_incomplete_targets(overrides, message):
    with pytest.raises(guard.CostGuardError, match=message):
        _gcp_public_route_authorization(**overrides)


def test_gcp_public_route_peer_allows_dns_names_containing_s():
    peer = INITIAL_PEER.replace("/ip4/34.42.181.232/", "/dns4/seed.communityai.example/")

    authorization = _gcp_public_route_authorization(initial_peer=peer)

    assert authorization["provider_plan"]["initial_peer"] == peer


def test_gcp_public_route_plan_respects_existing_combined_commitments():
    entries = (
        guard.LedgerEntry(
            run_id="existing-run",
            provider="GCP",
            purpose="previous run",
            maximum_usd=Decimal("75"),
            observed_usd=None,
            cleanup_proof="cleanup pending",
            state="CLEANED",
        ),
    )

    with pytest.raises(guard.CostGuardError, match="USD 100"):
        _gcp_public_route_authorization(entries=entries)


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
    assert report["maximum_estimate_usd"] == "70.00"
    assert output.stat().st_size < guard.MAX_OUTPUT_BYTES


def test_repository_readiness_ledger_remains_machine_readable():
    repository = Path(__file__).resolve().parents[1]

    entries = guard.load_spend_ledger(repository / "docs" / "RELEASE_READINESS.md")

    assert all(entry.maximum_usd <= guard.CLOUD_CEILING_USD for entry in entries)
