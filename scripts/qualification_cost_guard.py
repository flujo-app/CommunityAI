"""Build a fail-closed qualification cost authorization and exact provider plan.

The command never calls a provider. It parses the release-readiness spend ledger,
accounts for unresolved GCP and Fly reservations against one USD 100 ceiling, and
writes a bounded JSON plan. A provider run remains unauthorized until its exact
run ID and maximum estimate are recorded in the ledger.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = 2
CLOUD_CEILING_USD = Decimal("100.00")
PRICING_AS_OF = date(2026, 8, 26)
PRICING_REVALIDATE_BY = date(2026, 9, 25)
MAX_GCP_FLEET_HOURS = Decimal("14")
MAX_FLY_DISCOVERY_HOURS = Decimal("744")
GCP_MACHINE_HOURLY_USD = Decimal("0.473212")
GCP_T4_HOURLY_USD = Decimal("0.35")
GCP_WINDOWS_VCPU_HOURLY_USD = Decimal("0.046")
GCP_DISK_GIB_HOURLY_USD = Decimal("0.000054795")
GCP_HEADROOM_MULTIPLIER = Decimal("1.25")
GCP_FIXED_CONTINGENCY_USD = Decimal("10.00")
GCP_MACHINE_VCPUS = 8
GCP_DISK_GIB = 150
GCP_SUBNET_RANGE = "10.210.0.0/24"
GCP_IAP_SOURCE_RANGE = "35.235.240.0/20"
MAX_LEDGER_BYTES = 200_000
MAX_OUTPUT_BYTES = 1_000_000

_RUN_ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,19}$")
_SOURCE_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_ZONE_RE = re.compile(r"^[a-z]+(?:-[a-z0-9]+)+-[a-z]$")
_USD_RE = re.compile(r"^USD ([0-9]+(?:\.[0-9]{1,2})?)$")
_FLY_APP_RE = re.compile(r"^[a-z][a-z0-9-]{2,62}$")
_FLY_REGION_RE = re.compile(r"^[a-z]{3}$")
_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
FLY_DISCOVERY_IMAGE_REPOSITORY = "ghcr.io/flujo-app/communityai-discovery-seed"
_FLY_DISCOVERY_IMAGE_RE = re.compile(rf"^{re.escape(FLY_DISCOVERY_IMAGE_REPOSITORY)}@sha256:[0-9a-f]{{64}}$")

GCP_QUALIFICATION_WORKLOAD = "gcp-qualification-fleet"
FLY_RECOVERY_WORKLOAD = "fly-recovery"
FLY_DISCOVERY_SEED_WORKLOAD = "fly-discovery-seed"
_WORKLOADS_BY_PROVIDER = {
    "GCP": {GCP_QUALIFICATION_WORKLOAD},
    "FLY": {FLY_RECOVERY_WORKLOAD, FLY_DISCOVERY_SEED_WORKLOAD},
}

_GCP_PROFILES = (
    ("windows-cpu", "windows-2022", "windows-cloud", False),
    ("windows-cuda", "windows-2022", "windows-cloud", True),
    ("linux-cpu", "ubuntu-2404-lts-amd64", "ubuntu-os-cloud", False),
    ("linux-cuda", "ubuntu-2404-lts-amd64", "ubuntu-os-cloud", True),
)


class CostGuardError(ValueError):
    """The proposed paid run cannot be authorized safely."""


@dataclass(frozen=True)
class LedgerEntry:
    run_id: str
    provider: str
    purpose: str
    maximum_usd: Decimal
    observed_usd: Decimal | None
    cleanup_proof: str
    state: str

    @property
    def committed_usd(self) -> Decimal:
        if self.state == "CLEANED" and self.observed_usd is not None:
            return self.observed_usd
        if self.observed_usd is not None:
            return max(self.maximum_usd, self.observed_usd)
        return self.maximum_usd


def _usd(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):.2f}"


def _parse_usd(value: str, field: str, *, optional: bool) -> Decimal | None:
    if optional and value in {"—", "-", ""}:
        return None
    match = _USD_RE.fullmatch(value)
    if match is None:
        raise CostGuardError(f"{field} must use the form USD 0 or USD 0.00")
    try:
        amount = Decimal(match.group(1))
    except InvalidOperation:
        raise CostGuardError(f"{field} is not a valid USD amount") from None
    if amount < 0 or not amount.is_finite():
        raise CostGuardError(f"{field} must be a finite non-negative USD amount")
    return amount


def parse_spend_ledger(content: str) -> tuple[LedgerEntry, ...]:
    if len(content.encode("utf-8")) > MAX_LEDGER_BYTES:
        raise CostGuardError("release-readiness ledger exceeds the bounded size")
    marker = "## Cloud authorization and spend ledger"
    section = content.find(marker)
    if section < 0:
        raise CostGuardError("release-readiness ledger section is missing")

    entries: list[LedgerEntry] = []
    in_table = False
    table_found = False
    ledger_row_seen = False
    for raw_line in content[section:].splitlines():
        line = raw_line.strip()
        if line.startswith("| Run | Provider | Purpose |"):
            in_table = True
            table_found = True
            continue
        if not in_table:
            continue
        if not line.startswith("|"):
            if entries or line:
                break
            continue
        columns = [column.strip() for column in line.strip("|").split("|")]
        if len(columns) != 7:
            raise CostGuardError("spend ledger rows must contain exactly seven columns")
        if set(columns[0]) <= {"-", ":", " "}:
            continue
        if columns[0] == "No new paid run recorded":
            ledger_row_seen = True
            if columns != ["No new paid run recorded", "—", "—", "USD 0", "USD 0", "—", "READY"]:
                raise CostGuardError("empty spend ledger placeholder is malformed")
            continue

        ledger_row_seen = True
        run_id = columns[0]
        provider = columns[1].upper()
        purpose = columns[2]
        cleanup_proof = columns[5]
        state = columns[6].upper()
        maximum = _parse_usd(columns[3], f"{run_id} maximum estimate", optional=False)
        observed = _parse_usd(columns[4], f"{run_id} observed cost", optional=True)
        assert maximum is not None
        if not _RUN_ID_RE.fullmatch(run_id):
            raise CostGuardError("spend ledger contains an invalid run ID")
        if provider not in {"GCP", "FLY"}:
            raise CostGuardError(f"{run_id} provider must be GCP or Fly")
        if not purpose or len(purpose) > 384:
            raise CostGuardError(f"{run_id} purpose must be a bounded non-empty value")
        if maximum <= 0:
            raise CostGuardError(f"{run_id} maximum estimate must be positive")
        if not state or len(state) > 32:
            raise CostGuardError(f"{run_id} state must be a bounded non-empty value")
        if state == "CLEANED" and cleanup_proof in {"", "—", "-", "Not provisioned"}:
            raise CostGuardError(f"{run_id} CLEANED state requires cleanup proof")
        entries.append(
            LedgerEntry(
                run_id=run_id,
                provider=provider,
                purpose=purpose,
                maximum_usd=maximum,
                observed_usd=observed,
                cleanup_proof=cleanup_proof,
                state=state,
            )
        )

    if not table_found or not ledger_row_seen:
        raise CostGuardError("release-readiness spend ledger table is empty or missing")
    duplicates = sorted({entry.run_id for entry in entries if sum(item.run_id == entry.run_id for item in entries) > 1})
    if duplicates:
        raise CostGuardError("spend ledger repeats a run ID")
    return tuple(entries)


def load_spend_ledger(path: Path) -> tuple[LedgerEntry, ...]:
    try:
        if path.stat().st_size > MAX_LEDGER_BYTES:
            raise CostGuardError("release-readiness ledger exceeds the bounded size")
        content = path.read_text(encoding="utf-8")
    except CostGuardError:
        raise
    except (OSError, UnicodeError):
        raise CostGuardError("release-readiness ledger is not readable UTF-8") from None
    return parse_spend_ledger(content)


def _require_run_identity(run_id: str, source_commit: str) -> None:
    if not _RUN_ID_RE.fullmatch(run_id):
        raise CostGuardError("run ID must be 3-20 lowercase letters, digits, or hyphens")
    if not _SOURCE_COMMIT_RE.fullmatch(source_commit):
        raise CostGuardError("source commit must be one exact lowercase 40-character SHA-1")


def _require_gcp_target(project: str, zone: str) -> str:
    if not _PROJECT_RE.fullmatch(project):
        raise CostGuardError("GCP project ID is invalid")
    if not _ZONE_RE.fullmatch(zone):
        raise CostGuardError("GCP zone is invalid")
    return zone.rsplit("-", 1)[0]


def _gcp_cost(maximum_hours: Decimal) -> tuple[Decimal, Mapping[str, str]]:
    if not maximum_hours.is_finite() or maximum_hours <= 0 or maximum_hours > MAX_GCP_FLEET_HOURS:
        raise CostGuardError("GCP maximum hours must be greater than zero and no more than 14")
    machine_hourly = GCP_MACHINE_HOURLY_USD * len(_GCP_PROFILES)
    gpu_hourly = GCP_T4_HOURLY_USD * sum(profile[3] for profile in _GCP_PROFILES)
    windows_hourly = (
        GCP_WINDOWS_VCPU_HOURLY_USD
        * GCP_MACHINE_VCPUS
        * sum(profile[0].startswith("windows-") for profile in _GCP_PROFILES)
    )
    disk_hourly = GCP_DISK_GIB_HOURLY_USD * GCP_DISK_GIB * len(_GCP_PROFILES)
    hourly = machine_hourly + gpu_hourly + windows_hourly + disk_hourly
    raw_maximum = hourly * maximum_hours * GCP_HEADROOM_MULTIPLIER + GCP_FIXED_CONTINGENCY_USD
    maximum = raw_maximum.quantize(Decimal("1"), rounding=ROUND_CEILING)
    assumptions = {
        "machine_type": "n1-highmem-8",
        "machine_count": str(len(_GCP_PROFILES)),
        "machine_hourly_usd_each": _usd(GCP_MACHINE_HOURLY_USD),
        "t4_count": str(sum(profile[3] for profile in _GCP_PROFILES)),
        "t4_hourly_usd_each": _usd(GCP_T4_HOURLY_USD),
        "windows_vcpu_count": str(
            GCP_MACHINE_VCPUS * sum(profile[0].startswith("windows-") for profile in _GCP_PROFILES)
        ),
        "windows_vcpu_hourly_usd_each": _usd(GCP_WINDOWS_VCPU_HOURLY_USD),
        "disk_gib_each": str(GCP_DISK_GIB),
        "disk_gib_hourly_usd": str(GCP_DISK_GIB_HOURLY_USD),
        "maximum_hours": str(maximum_hours),
        "headroom_multiplier": str(GCP_HEADROOM_MULTIPLIER),
        "fixed_network_and_setup_contingency_usd": _usd(GCP_FIXED_CONTINGENCY_USD),
        "calculated_hourly_usd": _usd(hourly),
    }
    return maximum, assumptions


def _provider_plan_digest(provider_plan: Mapping[str, Any]) -> str:
    payload = json.dumps(
        provider_plan,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _command(*parts: str) -> list[str]:
    if any(not part or "\x00" in part or "\r" in part or "\n" in part for part in parts):
        raise CostGuardError("provider command contains an unsafe argument")
    return list(parts)


def _gcp_plan(run_id: str, project: str, zone: str, source_commit: str) -> Mapping[str, Any]:
    region = _require_gcp_target(project, zone)
    prefix = f"caiq-{run_id}"
    network = f"{prefix}-net"
    subnet = f"{prefix}-subnet"
    router = f"{prefix}-router"
    nat = f"{prefix}-nat"
    firewall = f"{prefix}-iap"
    target_tag = prefix
    labels = f"communityai_run={run_id},communityai_purpose=qualification,communityai_source={source_commit}"
    common = ("--project", project, "--quiet")

    create_commands = [
        _command("gcloud", "compute", "networks", "create", network, "--subnet-mode", "custom", *common),
        _command(
            "gcloud",
            "compute",
            "networks",
            "subnets",
            "create",
            subnet,
            "--network",
            network,
            "--region",
            region,
            "--range",
            GCP_SUBNET_RANGE,
            "--enable-private-ip-google-access",
            *common,
        ),
        _command("gcloud", "compute", "routers", "create", router, "--network", network, "--region", region, *common),
        _command(
            "gcloud",
            "compute",
            "routers",
            "nats",
            "create",
            nat,
            "--router",
            router,
            "--region",
            region,
            "--nat-all-subnet-ip-ranges",
            "--auto-allocate-nat-external-ips",
            *common,
        ),
        _command(
            "gcloud",
            "compute",
            "firewall-rules",
            "create",
            firewall,
            "--network",
            network,
            "--direction",
            "INGRESS",
            "--action",
            "ALLOW",
            "--rules",
            "tcp:22,tcp:3389",
            "--source-ranges",
            GCP_IAP_SOURCE_RANGE,
            "--target-tags",
            target_tag,
            *common,
        ),
    ]

    resources = []
    instance_names = []
    for profile, image_family, image_project, cuda in _GCP_PROFILES:
        name = f"{prefix}-{profile.replace('windows', 'win').replace('linux', 'lin')}"
        if name == "communityai-bootstrap-1" or len(name) > 63:
            raise CostGuardError("resolved GCP instance name is unsafe")
        instance_names.append(name)
        profile_labels = f"{labels},communityai_profile={profile}"
        command = [
            "gcloud",
            "compute",
            "instances",
            "create",
            name,
            "--project",
            project,
            "--zone",
            zone,
            "--machine-type",
            "n1-highmem-8",
            "--network-interface",
            f"network={network},subnet={subnet},no-address",
            "--image-family",
            image_family,
            "--image-project",
            image_project,
            "--boot-disk-size",
            f"{GCP_DISK_GIB}GB",
            "--boot-disk-type",
            "pd-standard",
            "--boot-disk-auto-delete",
            "--no-service-account",
            "--no-scopes",
            "--provisioning-model",
            "STANDARD",
            "--tags",
            target_tag,
            "--labels",
            profile_labels,
        ]
        if cuda:
            command.extend(
                [
                    "--accelerator",
                    "type=nvidia-tesla-t4,count=1",
                    "--maintenance-policy",
                    "TERMINATE",
                    "--metadata",
                    "install-nvidia-driver=True",
                ]
            )
        command.append("--quiet")
        create_commands.append(_command(*command))
        resources.append(
            {
                "profile": profile,
                "instance": name,
                "zone": zone,
                "machine_type": "n1-highmem-8",
                "gpu": "nvidia-tesla-t4" if cuda else None,
                "image_family": image_family,
                "image_project": image_project,
                "boot_disk_gib": GCP_DISK_GIB,
                "external_address": False,
                "service_account": False,
            }
        )

    cleanup_commands = [
        _command(
            "gcloud",
            "compute",
            "instances",
            "delete",
            *instance_names,
            "--zone",
            zone,
            "--delete-disks",
            "all",
            *common,
        ),
        _command("gcloud", "compute", "firewall-rules", "delete", firewall, *common),
        _command(
            "gcloud",
            "compute",
            "routers",
            "nats",
            "delete",
            nat,
            "--router",
            router,
            "--region",
            region,
            *common,
        ),
        _command("gcloud", "compute", "routers", "delete", router, "--region", region, *common),
        _command("gcloud", "compute", "networks", "subnets", "delete", subnet, "--region", region, *common),
        _command("gcloud", "compute", "networks", "delete", network, *common),
    ]
    verify_cleanup_commands = [
        _command(
            "gcloud",
            "compute",
            "instances",
            "list",
            "--project",
            project,
            "--filter",
            f"labels.communityai_run={run_id}",
            "--format",
            "value(name)",
        ),
        _command(
            "gcloud",
            "compute",
            "firewall-rules",
            "list",
            "--project",
            project,
            "--filter",
            f"name={firewall}",
            "--format",
            "value(name)",
        ),
        _command(
            "gcloud",
            "compute",
            "routers",
            "list",
            "--project",
            project,
            "--filter",
            f"name={router} AND region:{region}",
            "--format",
            "value(name)",
        ),
        _command(
            "gcloud",
            "compute",
            "networks",
            "subnets",
            "list",
            "--project",
            project,
            "--filter",
            f"name={subnet} AND region:{region}",
            "--format",
            "value(name)",
        ),
        _command(
            "gcloud",
            "compute",
            "networks",
            "list",
            "--project",
            project,
            "--filter",
            f"name={network}",
            "--format",
            "value(name)",
        ),
    ]
    serialized = json.dumps(
        {
            "create": create_commands,
            "cleanup": cleanup_commands,
            "verify": verify_cleanup_commands,
        },
        sort_keys=True,
    )
    if "communityai-bootstrap-1" in serialized:
        raise CostGuardError("provider plan must never target the existing bootstrap peer")
    return {
        "project": project,
        "zone": zone,
        "region": region,
        "network": network,
        "subnet": subnet,
        "router": router,
        "nat": nat,
        "iap_firewall_rule": firewall,
        "resources": resources,
        "create_commands": create_commands,
        "cleanup_commands": cleanup_commands,
        "verify_cleanup_commands": verify_cleanup_commands,
        "cleanup_success_condition": "every verification command returns empty stdout",
    }


def build_authorization(
    *,
    entries: Sequence[LedgerEntry],
    run_id: str,
    provider: str,
    workload: str,
    purpose: str,
    source_commit: str,
    maximum_hours: Decimal,
    project: str | None,
    zone: str | None,
    manual_maximum_usd: Decimal | None,
    fly_app: str | None = None,
    fly_region: str | None = None,
    fly_image: str | None = None,
    fly_image_evidence_digest: str | None = None,
    today: date | None = None,
) -> Mapping[str, Any]:
    _require_run_identity(run_id, source_commit)
    normalized_provider = provider.upper()
    if normalized_provider not in {"GCP", "FLY"}:
        raise CostGuardError("provider must be GCP or Fly")
    if workload not in _WORKLOADS_BY_PROVIDER[normalized_provider]:
        raise CostGuardError(f"workload {workload!r} is not valid for provider {normalized_provider}")
    if not purpose.strip() or len(purpose) > 120 or any(character in purpose for character in "\x00\r\n|"):
        raise CostGuardError("purpose must be a bounded single-line ledger value")

    provider_plan: Mapping[str, Any]
    if normalized_provider == "GCP":
        if manual_maximum_usd is not None:
            raise CostGuardError("GCP maximum is calculated from the pinned rate snapshot")
        if any(value is not None for value in (fly_app, fly_region, fly_image, fly_image_evidence_digest)):
            raise CostGuardError("GCP planning must not contain Fly target fields")
        effective_today = today or date.today()
        if effective_today > PRICING_REVALIDATE_BY:
            raise CostGuardError("GCP pricing snapshot is stale and must be revalidated before planning")
        if project is None or zone is None:
            raise CostGuardError("GCP planning requires an exact project and zone")
        maximum_usd, assumptions = _gcp_cost(maximum_hours)
        provider_plan = _gcp_plan(run_id, project, zone, source_commit)
    else:
        if project is not None or zone is not None:
            raise CostGuardError("Fly planning must not contain GCP target fields")
        if manual_maximum_usd is None or not manual_maximum_usd.is_finite() or manual_maximum_usd <= 0:
            raise CostGuardError("Fly planning requires a finite positive manual maximum estimate")
        maximum_usd = manual_maximum_usd.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
        if workload == FLY_RECOVERY_WORKLOAD:
            if any(value is not None for value in (fly_app, fly_region, fly_image, fly_image_evidence_digest)):
                raise CostGuardError("Fly recovery planning does not accept discovery-seed target fields")
            assumptions = {
                "resource_count": "5",
                "topology": "one isolated bootstrap plus four exact-manifest workers",
                "estimate_source": "operator-supplied current Fly pricing maximum",
            }
            provider_plan = {
                "adapter": "scripts/fly_qualification_adapter.py",
                "resource_count": 5,
                "cleanup_contract": "adapter cleanup acknowledgement must list all five resources and no survivors",
            }
        else:
            expected_app = f"communityai-{run_id}"
            if fly_app is None or _FLY_APP_RE.fullmatch(fly_app) is None or fly_app != expected_app:
                raise CostGuardError(f"Fly discovery-seed app must be the dedicated run-derived name {expected_app!r}")
            if fly_region is None or _FLY_REGION_RE.fullmatch(fly_region) is None:
                raise CostGuardError("Fly discovery-seed planning requires an exact three-letter region")
            if fly_image is None or _FLY_DISCOVERY_IMAGE_RE.fullmatch(fly_image) is None:
                raise CostGuardError(
                    "Fly discovery-seed image must be an immutable digest in the reviewed GHCR repository"
                )
            if fly_image_evidence_digest is None or _SHA256_DIGEST_RE.fullmatch(fly_image_evidence_digest) is None:
                raise CostGuardError(
                    "Fly discovery-seed planning requires a canonical image publication-evidence digest"
                )
            if not maximum_hours.is_finite() or maximum_hours <= 0 or maximum_hours > MAX_FLY_DISCOVERY_HOURS:
                raise CostGuardError("Fly discovery-seed maximum hours must be greater than zero and no more than 744")
            machine = f"{run_id}-seed"
            volume = f"{run_id}-identity"
            assumptions = {
                "resource_count": "5",
                "topology": "one public discovery-only Machine with persistent identity and dual-stack app service",
                "estimate_source": "operator-supplied current Fly pricing maximum",
                "maximum_runtime_hours": str(maximum_hours),
            }
            provider_plan = {
                "adapter": "scripts/fly_discovery_seed.py",
                "app": fly_app,
                "region": fly_region,
                "image": fly_image,
                "image_publication_evidence": {
                    "expected_digest": fly_image_evidence_digest,
                    "required_repository": FLY_DISCOVERY_IMAGE_REPOSITORY,
                    "source_commit": source_commit,
                    "validated_by_cost_guard": False,
                    "adapter_validation_contract": (
                        "before provider authentication or calls, load the bounded regular evidence file, "
                        "recompute expected_digest, and validate its schema, source commit, repository, "
                        "and immutable image digest"
                    ),
                },
                "maximum_runtime_hours": str(maximum_hours),
                "renewal_or_cleanup_deadline": "provisioned_at + maximum_runtime_hours",
                "resource_count": 5,
                "resources": [
                    {"type": "app", "name": fly_app},
                    {"type": "machine", "name": machine, "count": 1},
                    {"type": "volume", "name": volume, "size_gb": 1},
                    {"type": "shared_ipv4", "count": 1},
                    {"type": "anycast_ipv6", "count": 1},
                ],
                "machine": {
                    "guest": {"cpu_kind": "shared", "cpus": 1, "memory_mb": 1024},
                    "rootfs_size_gb": 8,
                    "internal_port": 31337,
                    "public_tcp_port": 31337,
                    "auto_stop": False,
                    "restart_policy": "always",
                    "identity_mount": "/data",
                },
                "failure_cleanup_contract": (
                    "remove only the exact run-bound Machine, volume, IP allocations, and dedicated app; "
                    "verify every resource absent"
                ),
                "success_retention_contract": (
                    "retain the exact five resources only through maximum_runtime_hours; before the deadline, "
                    "clean them up, renew with an exact ledger reservation, or transition through a separately "
                    "authorized baseline"
                ),
                "protected_resources": ["communityai-bootstrap-1", "unrelated Fly applications"],
            }

    provider_plan_digest = _provider_plan_digest(provider_plan)
    ledger_purpose = (
        f"{purpose.strip()} [workload {workload}] [source {source_commit}] " f"[plan {provider_plan_digest}]"
    )

    existing = [entry for entry in entries if entry.run_id == run_id]
    if len(existing) > 1:
        raise CostGuardError("spend ledger repeats the proposed run ID")
    committed = sum((entry.committed_usd for entry in entries), Decimal("0"))
    reservation_recorded = False
    if existing:
        reservation = existing[0]
        if reservation.state != "PLANNED":
            raise CostGuardError("proposed run ID already exists in a non-PLANNED ledger state")
        if (
            reservation.provider != normalized_provider
            or reservation.purpose != ledger_purpose
            or reservation.maximum_usd != maximum_usd
        ):
            raise CostGuardError(
                "existing ledger reservation does not match the proposed provider, purpose/source/plan, and maximum"
            )
        unreserved_committed = committed - reservation.committed_usd
        remaining_before = CLOUD_CEILING_USD - unreserved_committed
        remaining_after = CLOUD_CEILING_USD - committed
        reservation_recorded = True
    else:
        remaining_before = CLOUD_CEILING_USD - committed
        remaining_after = remaining_before - maximum_usd

    if remaining_before < 0 or remaining_after < 0:
        raise CostGuardError("proposed run could exceed the combined USD 100 cloud ceiling")

    ledger_row = (
        f"| {run_id} | {normalized_provider} | {ledger_purpose} | USD {_usd(maximum_usd)} | "
        "— | Not provisioned | PLANNED |"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": "communityai-cloud-cost-authorization",
        "result": "passed",
        "run_id": run_id,
        "provider": normalized_provider,
        "workload": workload,
        "source_commit": source_commit,
        "cloud_ceiling_usd": _usd(CLOUD_CEILING_USD),
        "ledger_committed_before_run_usd": _usd(CLOUD_CEILING_USD - remaining_before),
        "remaining_before_run_usd": _usd(remaining_before),
        "maximum_estimate_usd": _usd(maximum_usd),
        "remaining_after_run_maximum_usd": _usd(remaining_after),
        "reservation_recorded": reservation_recorded,
        "provisioning_authorized": reservation_recorded,
        "cost_authorization_only": True,
        "provider_preflight_required": True,
        "provider_calls_authorized_without_preflight": False,
        "required_ledger_row": ledger_row,
        "ledger_purpose": ledger_purpose,
        "provider_plan_digest": provider_plan_digest,
        "pricing_as_of": PRICING_AS_OF.isoformat() if normalized_provider == "GCP" else None,
        "pricing_revalidate_by": PRICING_REVALIDATE_BY.isoformat() if normalized_provider == "GCP" else None,
        "cost_assumptions": assumptions,
        "provider_plan": provider_plan,
        "cleanup_required_for_pass": workload != FLY_DISCOVERY_SEED_WORKLOAD,
        "failure_cleanup_required": True,
        "persistent_resources_after_pass": workload == FLY_DISCOVERY_SEED_WORKLOAD,
        "qualification_evidence": False,
        "complete_release_qualification": False,
    }


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(payload) > MAX_OUTPUT_BYTES:
        raise CostGuardError("cost authorization exceeds the bounded output size")
    path = Path(os.path.abspath(os.fspath(path.expanduser())))
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _decimal_argument(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("must be a decimal number") from None
    if not parsed.is_finite():
        raise argparse.ArgumentTypeError("must be finite")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Authorize a bounded CommunityAI GCP or Fly workload against the shared cloud ledger",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--provider", required=True, choices=("gcp", "fly"))
    parser.add_argument(
        "--workload",
        required=True,
        choices=(GCP_QUALIFICATION_WORKLOAD, FLY_RECOVERY_WORKLOAD, FLY_DISCOVERY_SEED_WORKLOAD),
    )
    parser.add_argument("--purpose", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--ledger", type=Path, default=Path("docs/RELEASE_READINESS.md"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--maximum-hours", type=_decimal_argument, default=MAX_GCP_FLEET_HOURS)
    parser.add_argument("--project")
    parser.add_argument("--zone")
    parser.add_argument("--manual-maximum-usd", type=_decimal_argument)
    parser.add_argument("--fly-app")
    parser.add_argument("--fly-region")
    parser.add_argument("--fly-image")
    parser.add_argument("--fly-image-evidence-digest")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = build_authorization(
            entries=load_spend_ledger(args.ledger),
            run_id=args.run_id,
            provider=args.provider,
            workload=args.workload,
            purpose=args.purpose,
            source_commit=args.source_commit,
            maximum_hours=args.maximum_hours,
            project=args.project,
            zone=args.zone,
            manual_maximum_usd=args.manual_maximum_usd,
            fly_app=args.fly_app,
            fly_region=args.fly_region,
            fly_image=args.fly_image,
            fly_image_evidence_digest=args.fly_image_evidence_digest,
        )
        _atomic_json(args.output, report)
    except CostGuardError as exc:
        print(f"qualification cost guard failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "result": "passed",
                "run_id": report["run_id"],
                "provisioning_authorized": report["provisioning_authorized"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
