"""Build a fail-closed qualification cost authorization and exact provider plan.

The command never calls a provider. It parses the release-readiness spend ledger,
accounts for unresolved GCP and Fly reservations against one USD 100 authorization
epoch, and writes a bounded JSON plan. A provider run remains unauthorized until its
exact run ID and maximum estimate are recorded in the ledger. Fully cleaned historical
runs may be marked ``CLEANED-RELEASED`` only after an explicit owner budget reset; their
maximum remains auditable but no longer consumes the new epoch.
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
MAX_GCP_PUBLIC_ROUTE_HOURS = Decimal("14")
MAX_FLY_DISCOVERY_HOURS = Decimal("744")
GCP_MACHINE_HOURLY_USD = Decimal("0.473212")
GCP_G2_MACHINE_HOURLY_USD = Decimal("0.853624312")
GCP_T4_HOURLY_USD = Decimal("0.35")
GCP_WINDOWS_VCPU_HOURLY_USD = Decimal("0.046")
GCP_DISK_GIB_HOURLY_USD = Decimal("0.000054795")
GCP_BALANCED_DISK_GIB_HOURLY_USD = Decimal("0.000136986")
GCP_HEADROOM_MULTIPLIER = Decimal("1.25")
GCP_FIXED_CONTINGENCY_USD = Decimal("10.00")
GCP_NAT_IP_HOURLY_USD = Decimal("0.005")
GCP_MACHINE_VCPUS = 8
GCP_DISK_GIB = 150
GCP_PUBLIC_ROUTE_DISK_GIB = 200
GCP_PRIMARY_SUBNET_RANGE = "10.210.0.0/24"
GCP_FALLBACK_SUBNET_RANGE = "10.210.1.0/24"
GCP_IAP_SOURCE_RANGE = "35.235.240.0/20"
MAX_LEDGER_BYTES = 200_000
MAX_OUTPUT_BYTES = 1_000_000

_RUN_ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,19}$")
_SOURCE_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_ZONE_RE = re.compile(r"^[a-z]+(?:-[a-z0-9]+)+-[a-z]$")
_IMAGE_RE = re.compile(r"^[a-z][a-z0-9-]{2,62}$")
_USD_RE = re.compile(r"^USD ([0-9]+(?:\.[0-9]{1,2})?)$")
_FLY_APP_RE = re.compile(r"^[a-z][a-z0-9-]{2,62}$")
_FLY_REGION_RE = re.compile(r"^[a-z]{3}$")
_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
FLY_DISCOVERY_IMAGE_REPOSITORY = "ghcr.io/flujo-app/communityai-discovery-seed"
_FLY_DISCOVERY_IMAGE_RE = re.compile(rf"^{re.escape(FLY_DISCOVERY_IMAGE_REPOSITORY)}@sha256:[0-9a-f]{{64}}$")
GCP_PRIMARY_IMAGE_REPOSITORY = "ghcr.io/flujo-app/communityai-public-route-qwen3.5-2b"
GCP_STANDBY_IMAGE_REPOSITORY = "ghcr.io/flujo-app/communityai-public-route-gemma-4-e2b"
_GCP_PRIMARY_IMAGE_RE = re.compile(rf"^{re.escape(GCP_PRIMARY_IMAGE_REPOSITORY)}@sha256:[0-9a-f]{{64}}$")
_GCP_STANDBY_IMAGE_RE = re.compile(rf"^{re.escape(GCP_STANDBY_IMAGE_REPOSITORY)}@sha256:[0-9a-f]{{64}}$")
_PUBLIC_ROUTE_PEER_RE = re.compile(r"^/(?:ip4|ip6|dns|dns4|dns6)/[^\s]{1,1900}/p2p/[1-9A-HJ-NP-Za-km-z]{32,128}$")
GCP_PRIMARY_MANIFEST_DIGEST = "sha256:3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33"
GCP_STANDBY_MANIFEST_DIGEST = "sha256:2f8debbe0fcdf5af8d4c56c982210fa50aa584314968ae2617e2ccc2de9eafdd"

GCP_QUALIFICATION_WORKLOAD = "gcp-qualification-fleet"
GCP_PUBLIC_ROUTE_WORKLOAD = "gcp-public-route"
FLY_RECOVERY_WORKLOAD = "fly-recovery"
FLY_DISCOVERY_SEED_WORKLOAD = "fly-discovery-seed"
_WORKLOADS_BY_PROVIDER = {
    "GCP": {GCP_QUALIFICATION_WORKLOAD, GCP_PUBLIC_ROUTE_WORKLOAD},
    "FLY": {FLY_RECOVERY_WORKLOAD, FLY_DISCOVERY_SEED_WORKLOAD},
}

_GCP_PROFILES = (
    ("windows-cpu", "windows-2022", "windows-cloud", False),
    ("linux-cpu", "ubuntu-2404-lts-amd64", "ubuntu-os-cloud", False),
    ("windows-cuda", "windows-2022", "windows-cloud", True),
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
        if self.state == "CLEANED-RELEASED":
            return Decimal("0")
        if self.state in {"CLEANED", "CANCELED"} and self.observed_usd is not None:
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
        if state in {"CANCELED", "CLEANED", "CLEANED-RELEASED"} and cleanup_proof in {
            "",
            "—",
            "-",
            "Not provisioned",
        }:
            raise CostGuardError(f"{run_id} {state} state requires cleanup proof")
        if state == "CANCELED" and observed is None:
            raise CostGuardError(f"{run_id} CANCELED state requires an observed cost")
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


def _gcp_cost(
    maximum_hours: Decimal,
    *,
    split_region: bool,
    cuda_shape: str,
) -> tuple[Decimal, Mapping[str, str]]:
    if not maximum_hours.is_finite() or maximum_hours <= 0 or maximum_hours > MAX_GCP_FLEET_HOURS:
        raise CostGuardError("GCP maximum hours must be greater than zero and no more than 14")
    if cuda_shape not in {"n1-t4", "g2-l4"}:
        raise CostGuardError("GCP CUDA shape must be n1-t4 or g2-l4")

    cuda_count = sum(profile[3] for profile in _GCP_PROFILES)
    cpu_count = len(_GCP_PROFILES) - cuda_count
    if cuda_shape == "n1-t4":
        cuda_machine_type = "n1-highmem-8"
        cuda_accelerator = "nvidia-tesla-t4"
        cuda_machine_hourly = GCP_MACHINE_HOURLY_USD
        machine_hourly = GCP_MACHINE_HOURLY_USD * len(_GCP_PROFILES)
        gpu_hourly = GCP_T4_HOURLY_USD * cuda_count
        standard_disk_count = len(_GCP_PROFILES)
        balanced_disk_count = 0
    else:
        cuda_machine_type = "g2-standard-8"
        cuda_accelerator = "nvidia-l4"
        cuda_machine_hourly = GCP_G2_MACHINE_HOURLY_USD
        machine_hourly = GCP_MACHINE_HOURLY_USD * cpu_count + GCP_G2_MACHINE_HOURLY_USD * cuda_count
        gpu_hourly = Decimal("0")
        standard_disk_count = cpu_count
        balanced_disk_count = cuda_count

    windows_hourly = (
        GCP_WINDOWS_VCPU_HOURLY_USD
        * GCP_MACHINE_VCPUS
        * sum(profile[0].startswith("windows-") for profile in _GCP_PROFILES)
    )
    disk_hourly = GCP_DISK_GIB * (
        GCP_DISK_GIB_HOURLY_USD * standard_disk_count + GCP_BALANCED_DISK_GIB_HOURLY_USD * balanced_disk_count
    )
    nat_ip_count = 2 if split_region else 1
    nat_ip_hourly = GCP_NAT_IP_HOURLY_USD * nat_ip_count
    compute_hourly = machine_hourly + gpu_hourly + windows_hourly + disk_hourly
    network_maximum_hours = maximum_hours * len(_GCP_PROFILES)
    variable_maximum = compute_hourly * maximum_hours + nat_ip_hourly * network_maximum_hours
    equivalent_hourly = variable_maximum / maximum_hours
    raw_maximum = variable_maximum * GCP_HEADROOM_MULTIPLIER + GCP_FIXED_CONTINGENCY_USD
    maximum = raw_maximum.quantize(Decimal("1"), rounding=ROUND_CEILING)
    assumptions = {
        "cpu_machine_type": "n1-highmem-8",
        "cuda_shape": cuda_shape,
        "cuda_machine_type": cuda_machine_type,
        "cuda_accelerator": cuda_accelerator,
        "machine_count": str(len(_GCP_PROFILES)),
        "cpu_machine_count": str(cpu_count),
        "cuda_machine_count": str(cuda_count),
        "cpu_machine_hourly_usd_each": str(GCP_MACHINE_HOURLY_USD),
        "cuda_machine_hourly_usd_each": str(cuda_machine_hourly),
        "t4_count": str(cuda_count if cuda_shape == "n1-t4" else 0),
        "t4_hourly_usd_each": _usd(GCP_T4_HOURLY_USD),
        "l4_count": str(cuda_count if cuda_shape == "g2-l4" else 0),
        "l4_price_included_in_cuda_machine": str(cuda_shape == "g2-l4").lower(),
        "windows_vcpu_count": str(
            GCP_MACHINE_VCPUS * sum(profile[0].startswith("windows-") for profile in _GCP_PROFILES)
        ),
        "windows_vcpu_hourly_usd_each": _usd(GCP_WINDOWS_VCPU_HOURLY_USD),
        "disk_gib_each": str(GCP_DISK_GIB),
        "disk_gib_hourly_usd": str(GCP_DISK_GIB_HOURLY_USD),
        "cuda_disk_type": "pd-balanced" if cuda_shape == "g2-l4" else "pd-standard",
        "cuda_disk_gib_hourly_usd": str(
            GCP_BALANCED_DISK_GIB_HOURLY_USD if cuda_shape == "g2-l4" else GCP_DISK_GIB_HOURLY_USD
        ),
        "region_count": "2" if split_region else "1",
        "nat_ip_count": str(nat_ip_count),
        "nat_ip_hourly_usd_each": str(GCP_NAT_IP_HOURLY_USD),
        "nat_ip_hourly_usd": str(nat_ip_hourly),
        "maximum_hours": str(maximum_hours),
        "network_maximum_hours": str(network_maximum_hours),
        "headroom_multiplier": str(GCP_HEADROOM_MULTIPLIER),
        "fixed_network_and_setup_contingency_usd": _usd(GCP_FIXED_CONTINGENCY_USD),
        "calculated_compute_hourly_usd": _usd(compute_hourly),
        "calculated_equivalent_hourly_usd": _usd(equivalent_hourly),
        "calculated_hourly_usd": _usd(equivalent_hourly),
    }
    return maximum, assumptions


def _gcp_public_route_cost(maximum_hours: Decimal) -> tuple[Decimal, Mapping[str, str]]:
    if not maximum_hours.is_finite() or maximum_hours <= 0 or maximum_hours > MAX_GCP_PUBLIC_ROUTE_HOURS:
        raise CostGuardError("GCP public-route maximum hours must be greater than zero and no more than 14")
    disk_hourly = GCP_PUBLIC_ROUTE_DISK_GIB * GCP_BALANCED_DISK_GIB_HOURLY_USD
    public_ip_hourly = GCP_NAT_IP_HOURLY_USD
    compute_hourly = GCP_G2_MACHINE_HOURLY_USD + disk_hourly + public_ip_hourly
    raw_maximum = compute_hourly * maximum_hours * GCP_HEADROOM_MULTIPLIER + GCP_FIXED_CONTINGENCY_USD
    maximum = raw_maximum.quantize(Decimal("1"), rounding=ROUND_CEILING)
    assumptions = {
        "machine_count": "1",
        "machine_type": "g2-standard-8",
        "l4_count": "1",
        "l4_price_included_in_machine": "true",
        "machine_hourly_usd": str(GCP_G2_MACHINE_HOURLY_USD),
        "disk_type": "pd-balanced",
        "disk_gib": str(GCP_PUBLIC_ROUTE_DISK_GIB),
        "disk_gib_hourly_usd": str(GCP_BALANCED_DISK_GIB_HOURLY_USD),
        "public_ipv4_count": "1",
        "public_ipv4_hourly_usd": str(GCP_NAT_IP_HOURLY_USD),
        "maximum_hours": str(maximum_hours),
        "headroom_multiplier": str(GCP_HEADROOM_MULTIPLIER),
        "fixed_network_and_setup_contingency_usd": _usd(GCP_FIXED_CONTINGENCY_USD),
        "calculated_hourly_usd": _usd(compute_hourly),
    }
    return maximum, assumptions


def _gcp_public_route_plan(
    run_id: str,
    project: str,
    zone: str,
    source_commit: str,
    maximum_hours: Decimal,
    *,
    linux_image: str,
    primary_image: str,
    primary_image_evidence_digest: str,
    standby_image: str,
    standby_image_evidence_digest: str,
    runtime_bootstrap_digest: str,
    runtime_bootstrap_bytes: int,
    initial_peer: str,
    host_controller_digest: str,
    host_controller_bytes: int,
    acceptance_probe_digest: str,
    acceptance_probe_bytes: int,
) -> Mapping[str, Any]:
    region = _require_gcp_target(project, zone)
    if _IMAGE_RE.fullmatch(linux_image) is None:
        raise CostGuardError("GCP public-route OS image must be an immutable image name")
    if _GCP_PRIMARY_IMAGE_RE.fullmatch(primary_image) is None:
        raise CostGuardError("GCP public-route primary image must be an immutable Qwen CUDA route digest")
    if _GCP_STANDBY_IMAGE_RE.fullmatch(standby_image) is None:
        raise CostGuardError("GCP public-route standby image must be an immutable Gemma CUDA route digest")
    if _SHA256_DIGEST_RE.fullmatch(primary_image_evidence_digest or "") is None:
        raise CostGuardError("GCP public-route primary image requires a canonical publication-evidence digest")
    if _SHA256_DIGEST_RE.fullmatch(standby_image_evidence_digest or "") is None:
        raise CostGuardError("GCP public-route standby image requires a canonical publication-evidence digest")
    if _SHA256_DIGEST_RE.fullmatch(runtime_bootstrap_digest or "") is None:
        raise CostGuardError("GCP public-route runtime bootstrap requires a canonical source digest")
    source_bindings = (
        (runtime_bootstrap_digest, runtime_bootstrap_bytes, "runtime bootstrap", 16_384),
        (host_controller_digest, host_controller_bytes, "host controller", 131_072),
        (acceptance_probe_digest, acceptance_probe_bytes, "acceptance probe", 65_536),
    )
    for digest, byte_size, field, maximum_bytes in source_bindings:
        if _SHA256_DIGEST_RE.fullmatch(digest or "") is None:
            raise CostGuardError(f"GCP public-route {field} requires a canonical source digest")
        if isinstance(byte_size, bool) or not isinstance(byte_size, int) or not 1 <= byte_size <= maximum_bytes:
            raise CostGuardError(f"GCP public-route {field} must be between 1 and {maximum_bytes} bytes")
    if (
        not isinstance(initial_peer, str)
        or len(initial_peer) > 2048
        or _PUBLIC_ROUTE_PEER_RE.fullmatch(initial_peer) is None
    ):
        raise CostGuardError("GCP public-route initial peer must be one bounded authenticated multiaddr")

    instance = f"{run_id}-route"
    network = f"{run_id}-net"
    subnet = f"{run_id}-subnet"
    firewall = f"{run_id}-dht"
    ssh_firewall = f"{run_id}-iap"
    target_tag = f"{run_id}-route"
    max_run_seconds = int(maximum_hours * Decimal(3600))
    common = ["--project", project, "--quiet"]

    create_commands = [
        _command(
            "gcloud",
            "compute",
            "networks",
            "create",
            network,
            "--subnet-mode",
            "custom",
            *common,
        ),
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
            GCP_PRIMARY_SUBNET_RANGE,
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
            "tcp:31337-31338",
            "--source-ranges",
            "0.0.0.0/0",
            "--target-tags",
            target_tag,
            *common,
        ),
        _command(
            "gcloud",
            "compute",
            "firewall-rules",
            "create",
            ssh_firewall,
            "--network",
            network,
            "--direction",
            "INGRESS",
            "--action",
            "ALLOW",
            "--rules",
            "tcp:22",
            "--source-ranges",
            GCP_IAP_SOURCE_RANGE,
            "--target-tags",
            target_tag,
            *common,
        ),
        _command(
            "gcloud",
            "compute",
            "instances",
            "create",
            instance,
            "--zone",
            zone,
            "--machine-type",
            "g2-standard-8",
            "--network",
            network,
            "--subnet",
            subnet,
            "--image",
            linux_image,
            "--image-project",
            "ubuntu-os-cloud",
            "--boot-disk-size",
            f"{GCP_PUBLIC_ROUTE_DISK_GIB}GB",
            "--boot-disk-type",
            "pd-balanced",
            "--boot-disk-auto-delete",
            "--no-service-account",
            "--no-scopes",
            "--provisioning-model",
            "STANDARD",
            "--max-run-duration",
            f"{max_run_seconds}s",
            "--instance-termination-action",
            "DELETE",
            "--maintenance-policy",
            "TERMINATE",
            "--metadata-from-file",
            "startup-script=scripts/gcp_public_route_startup.sh",
            "--tags",
            target_tag,
            "--labels",
            (
                f"communityai_run={run_id},communityai_gate=gate11,"
                f"communityai_purpose=public-route,communityai_source={source_commit}"
            ),
            *common,
        ),
    ]
    verify_create_commands = [
        _command(
            "gcloud",
            "compute",
            "instances",
            "describe",
            instance,
            "--zone",
            zone,
            "--format",
            "value(status,machineType,scheduling.maxRunDuration,networkInterfaces[0].accessConfigs[0].natIP)",
            "--project",
            project,
        ),
    ]
    cleanup_commands = [
        _command(
            "gcloud",
            "compute",
            "instances",
            "delete",
            instance,
            "--zone",
            zone,
            "--delete-disks",
            "all",
            *common,
        ),
        _command("gcloud", "compute", "firewall-rules", "delete", ssh_firewall, *common),
        _command("gcloud", "compute", "firewall-rules", "delete", firewall, *common),
        _command(
            "gcloud",
            "compute",
            "networks",
            "subnets",
            "delete",
            subnet,
            "--region",
            region,
            *common,
        ),
        _command("gcloud", "compute", "networks", "delete", network, *common),
    ]
    verify_cleanup_commands = [
        _command(
            "gcloud",
            "compute",
            "instances",
            "list",
            "--filter",
            f"name={instance}",
            "--format",
            "value(name)",
            "--project",
            project,
        ),
        _command(
            "gcloud",
            "compute",
            "disks",
            "list",
            "--filter",
            f"name={instance}",
            "--format",
            "value(name)",
            "--project",
            project,
        ),
        _command(
            "gcloud",
            "compute",
            "firewall-rules",
            "list",
            "--filter",
            f"name={firewall}",
            "--format",
            "value(name)",
            "--project",
            project,
        ),
        _command(
            "gcloud",
            "compute",
            "firewall-rules",
            "list",
            "--filter",
            f"name={ssh_firewall}",
            "--format",
            "value(name)",
            "--project",
            project,
        ),
        _command(
            "gcloud",
            "compute",
            "networks",
            "subnets",
            "list",
            "--filter",
            f"name={subnet}",
            "--format",
            "value(name)",
            "--project",
            project,
        ),
        _command(
            "gcloud",
            "compute",
            "networks",
            "list",
            "--filter",
            f"name={network}",
            "--format",
            "value(name)",
            "--project",
            project,
        ),
    ]
    return {
        "topology": (
            "one bounded G2/L4 host with co-located complete Qwen primary and complete Gemma standby routes; "
            "this is fallback coverage, not independent redundancy"
        ),
        "project": project,
        "zone": zone,
        "region": region,
        "maximum_runtime_hours": str(maximum_hours),
        "renewal_or_cleanup_deadline": "instance creation time + maximum_runtime_hours",
        "resource_count": 6,
        "resources": {
            "instance": instance,
            "boot_disk": instance,
            "network": network,
            "subnet": subnet,
            "public_ipv4": "ephemeral and released with instance deletion",
            "firewall": firewall,
            "iap_firewall": ssh_firewall,
        },
        "machine": {
            "machine_type": "g2-standard-8",
            "accelerator": "NVIDIA L4",
            "boot_disk_gib": GCP_PUBLIC_ROUTE_DISK_GIB,
            "boot_disk_type": "pd-balanced",
            "os_image": linux_image,
            "service_account": False,
            "scopes": [],
            "public_ipv4": "ephemeral and bound to the instance lifetime",
            "public_tcp_ports": [31337, 31338],
            "max_run_seconds": max_run_seconds,
            "termination_action": "DELETE",
        },
        "runtime_bootstrap": {
            "relative_path": "scripts/gcp_public_route_startup.sh",
            "sha256": runtime_bootstrap_digest,
            "byte_size": runtime_bootstrap_bytes,
            "source_commit_bound": True,
            "validated_by_cost_guard": False,
        },
        "host_controller": {
            "relative_path": "scripts/gcp_public_route_host.py",
            "sha256": host_controller_digest,
            "byte_size": host_controller_bytes,
            "source_commit_bound": True,
            "validated_by_cost_guard": False,
        },
        "acceptance_probe": {
            "relative_path": "scripts/public_route_acceptance.py",
            "sha256": acceptance_probe_digest,
            "byte_size": acceptance_probe_bytes,
            "source_commit_bound": True,
            "validated_by_cost_guard": False,
        },
        "initial_peer": initial_peer,
        "routes": [
            {
                "role": "primary",
                "candidate": "qwen3.5-2b",
                "image": primary_image,
                "manifest_digest": GCP_PRIMARY_MANIFEST_DIGEST,
                "public_tcp_port": 31337,
                "publication_evidence": {
                    "expected_digest": primary_image_evidence_digest,
                    "required_repository": GCP_PRIMARY_IMAGE_REPOSITORY,
                    "validated_by_cost_guard": False,
                },
            },
            {
                "role": "standby",
                "candidate": "gemma-4-e2b",
                "image": standby_image,
                "manifest_digest": GCP_STANDBY_MANIFEST_DIGEST,
                "public_tcp_port": 31338,
                "publication_evidence": {
                    "expected_digest": standby_image_evidence_digest,
                    "required_repository": GCP_STANDBY_IMAGE_REPOSITORY,
                    "validated_by_cost_guard": False,
                },
            },
        ],
        "preflight_contract": [
            "revalidate native gcloud and gh authentication before the first provider call",
            "verify one unused global and zonal L4 slot and exact g2-standard-8 availability",
            "verify IAP SSH authorization and the exact IAP source-range firewall before remote launch",
            (
                "rehash the committed fresh-VM runtime bootstrap and require its exact relative path, digest, "
                "byte size, source commit, pinned driver, Docker, containerd, and NVIDIA toolkit versions"
            ),
            (
                "rehash the committed fixed-action host controller and privacy-safe acceptance probe and require "
                "their exact path, digest, byte size, source commit, and planned authenticated bootstrap peer"
            ),
            (
                "load each bounded publication-evidence file, recompute its digest, and verify its candidate, "
                "manifest, immutable image, source, provenance, SBOM, and passed result"
            ),
            "verify the exact ledger reservation and provider-plan digest still match this plan",
        ],
        "operating_contract": {
            "startup_timeout_seconds": 3600,
            "health_sample_period_seconds": 300,
            "required_ready_evidence": [
                "both exact manifested workers announce complete routes",
                "aggregate admission health is present and healthy for both workers",
                "one exact primary inference and one exact standby inference succeed",
                "auto selects Qwen while healthy and Gemma while Qwen is deliberately disabled",
                "restored Qwen becomes primary without hiding route unavailability during the transition",
            ],
            "stop_conditions": [
                "startup timeout expires",
                "either route loses exact manifest identity or complete block coverage",
                "aggregate admission health is missing, stale, malformed, or unhealthy",
                "both routes become unavailable outside the deliberate fallback drill",
                "combined GPU allocation exceeds 22 GiB or cannot be observed",
                "host memory exceeds 30 GiB, route storage exceeds 160 GiB, or combined logs exceed 1 GiB",
                "worker restarts repeat, logs grow without bound, or a privacy or security incident is suspected",
            ],
            "resource_ceilings": {
                "qwen_device_memory_gib": 7,
                "gemma_device_memory_gib": 15,
                "combined_device_memory_gib": 22,
                "host_memory_gib": 30,
                "route_storage_gib": 160,
                "combined_logs_gib": 1,
                "qualification_claim": False,
            },
            "disable_contract": (
                "stop the affected worker first; if health or control is unavailable, delete the exact run-bound "
                "instance and report both routes unavailable until signed announcements expire"
            ),
            "degraded_contract": (
                "co-location means host loss removes both routes; expose standby-only, primary-only, or unavailable "
                "state exactly and make no redundancy claim"
            ),
        },
        "create_commands": create_commands,
        "verify_create_commands": verify_create_commands,
        "cleanup_commands": cleanup_commands,
        "verify_cleanup_commands": verify_cleanup_commands,
        "cleanup_success_condition": "every cleanup verification command returns empty stdout",
        "failure_cleanup_contract": "run cleanup immediately after any failed or partial create or stop condition",
        "success_retention_contract": (
            "retain only through maximum_runtime_hours; before the deadline, clean up, renew with an exact ledger "
            "reservation, or transition through a separately authorized baseline"
        ),
        "protected_resources": ["communityai-bootstrap-1", "all resources not named in this plan"],
    }


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


def _gcp_plan(
    run_id: str,
    project: str,
    zone: str,
    source_commit: str,
    maximum_hours: Decimal,
    *,
    windows_image: str,
    linux_image: str,
    cuda_fallback_zone: str | None,
    cuda_shape: str,
) -> Mapping[str, Any]:
    primary_region = _require_gcp_target(project, zone)
    max_run_seconds = int((maximum_hours * Decimal("3600")).to_integral_value(rounding=ROUND_CEILING))
    if not _IMAGE_RE.fullmatch(windows_image) or not _IMAGE_RE.fullmatch(linux_image):
        raise CostGuardError("GCP image names must be exact bounded image resources")
    if cuda_shape not in {"n1-t4", "g2-l4"}:
        raise CostGuardError("GCP CUDA shape must be n1-t4 or g2-l4")

    fallback_region = None
    if cuda_fallback_zone is not None:
        fallback_region = _require_gcp_target(project, cuda_fallback_zone)
        if fallback_region == primary_region:
            raise CostGuardError("CUDA fallback zone must use a different region")

    prefix = f"caiq-{run_id}"
    network = f"{prefix}-net"
    firewall = f"{prefix}-iap"
    target_tag = prefix
    labels = f"communityai_run={run_id},communityai_purpose=qualification,communityai_source={source_commit}"
    common = ("--project", project, "--quiet")

    regional_networks = [
        {
            "role": "primary",
            "zone": zone,
            "region": primary_region,
            "subnet": f"{prefix}-subnet",
            "subnet_range": GCP_PRIMARY_SUBNET_RANGE,
            "router": f"{prefix}-router",
            "nat": f"{prefix}-nat",
            "address": f"{prefix}-nat-ip",
        }
    ]
    if cuda_fallback_zone is not None:
        regional_networks.append(
            {
                "role": "cuda-fallback",
                "zone": cuda_fallback_zone,
                "region": fallback_region,
                "subnet": f"{prefix}-cuda-subnet",
                "subnet_range": GCP_FALLBACK_SUBNET_RANGE,
                "router": f"{prefix}-cuda-router",
                "nat": f"{prefix}-cuda-nat",
                "address": f"{prefix}-cuda-nat-ip",
            }
        )

    create_commands = [_command("gcloud", "compute", "networks", "create", network, "--subnet-mode", "custom", *common)]
    for regional in regional_networks:
        create_commands.extend(
            [
                _command(
                    "gcloud",
                    "compute",
                    "networks",
                    "subnets",
                    "create",
                    regional["subnet"],
                    "--network",
                    network,
                    "--region",
                    regional["region"],
                    "--range",
                    regional["subnet_range"],
                    "--enable-private-ip-google-access",
                    *common,
                ),
                _command(
                    "gcloud",
                    "compute",
                    "routers",
                    "create",
                    regional["router"],
                    "--network",
                    network,
                    "--region",
                    regional["region"],
                    *common,
                ),
                _command(
                    "gcloud",
                    "compute",
                    "addresses",
                    "create",
                    regional["address"],
                    "--region",
                    regional["region"],
                    "--network-tier",
                    "PREMIUM",
                    *common,
                ),
                _command(
                    "gcloud",
                    "compute",
                    "routers",
                    "nats",
                    "create",
                    regional["nat"],
                    "--router",
                    regional["router"],
                    "--region",
                    regional["region"],
                    "--nat-all-subnet-ip-ranges",
                    "--nat-external-ip-pool",
                    regional["address"],
                    *common,
                ),
            ]
        )
    create_commands.append(
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
        )
    )

    verify_create_commands = [
        _command(
            "gcloud",
            "compute",
            "addresses",
            "describe",
            regional["address"],
            "--region",
            regional["region"],
            "--project",
            project,
            "--format",
            "value(name)",
        )
        for regional in regional_networks
    ]

    resources = []
    profile_phases = []
    expected_source_images = {}
    for profile, _image_family, image_project, cuda in _GCP_PROFILES:
        name = f"{prefix}-{profile.replace('windows', 'win').replace('linux', 'lin')}"
        if name == "communityai-bootstrap-1" or len(name) > 63:
            raise CostGuardError("resolved GCP instance name is unsafe")
        use_fallback = profile == "linux-cuda" and cuda_fallback_zone is not None
        regional = regional_networks[1] if use_fallback else regional_networks[0]
        profile_zone = regional["zone"]
        image = windows_image if profile.startswith("windows-") else linux_image
        machine_type = "g2-standard-8" if cuda and cuda_shape == "g2-l4" else "n1-highmem-8"
        boot_disk_type = "pd-balanced" if cuda and cuda_shape == "g2-l4" else "pd-standard"
        gpu = "nvidia-l4" if cuda and cuda_shape == "g2-l4" else ("nvidia-tesla-t4" if cuda else None)
        profile_labels = f"{labels},communityai_profile={profile}"
        machine_id = f"{run_id}-{profile}"
        command = [
            "gcloud",
            "compute",
            "instances",
            "create",
            name,
            "--project",
            project,
            "--zone",
            profile_zone,
            "--machine-type",
            machine_type,
            "--network-interface",
            f"network={network},subnet={regional['subnet']},no-address",
            "--image",
            image,
            "--image-project",
            image_project,
            "--boot-disk-size",
            f"{GCP_DISK_GIB}GB",
            "--boot-disk-type",
            boot_disk_type,
            "--boot-disk-auto-delete",
            "--no-service-account",
            "--no-scopes",
            "--provisioning-model",
            "STANDARD",
            "--max-run-duration",
            f"{max_run_seconds}s",
            "--instance-termination-action",
            "DELETE",
            "--tags",
            target_tag,
            "--labels",
            profile_labels,
        ]
        if profile.startswith("windows-"):
            command.extend(
                [
                    "--metadata",
                    (
                        "sysprep-specialize-script-cmd=googet -noconfirm=true install "
                        "google-compute-engine-ssh,enable-windows-ssh=TRUE"
                    ),
                ]
            )
        if cuda:
            if cuda_shape == "n1-t4":
                command.extend(["--accelerator", "type=nvidia-tesla-t4,count=1"])
            command.extend(["--maintenance-policy", "TERMINATE"])
            if profile == "windows-cuda":
                command.extend(
                    [
                        "--metadata-from-file",
                        "windows-startup-script-ps1=scripts/gcp_windows_cuda_startup.ps1",
                    ]
                )
            elif cuda_shape == "g2-l4":
                command.extend(
                    [
                        "--metadata-from-file",
                        "startup-script=scripts/gcp_linux_cuda_startup.sh",
                    ]
                )
            else:
                command.extend(["--metadata", "install-nvidia-driver=True"])
        command.append("--quiet")

        create_command = _command(*command)
        verify_create_command = _command(
            "gcloud",
            "compute",
            "disks",
            "describe",
            name,
            "--zone",
            profile_zone,
            "--project",
            project,
            "--format",
            "value(sourceImage)",
        )
        cleanup_command = _command(
            "gcloud",
            "compute",
            "instances",
            "delete",
            name,
            "--zone",
            profile_zone,
            "--delete-disks",
            "all",
            *common,
        )
        phase_verify_cleanup_commands = [
            _command(
                "gcloud",
                "compute",
                "instances",
                "list",
                "--project",
                project,
                "--filter",
                f"name={name}",
                "--format",
                "value(name)",
            ),
            _command(
                "gcloud",
                "compute",
                "disks",
                "list",
                "--project",
                project,
                "--filter",
                f"name={name}",
                "--format",
                "value(name)",
            ),
        ]

        expected_source_images[name] = (
            f"https://www.googleapis.com/compute/v1/projects/{image_project}/" f"global/images/{image}"
        )
        bootstrap = {
            "windows_ssh": profile.startswith("windows-"),
            "cuda_driver": (
                "scripts/gcp_windows_cuda_startup.ps1"
                if profile == "windows-cuda"
                else ("scripts/gcp_linux_cuda_startup.sh" if cuda and cuda_shape == "g2-l4" else None)
            ),
            "required_post_boot_checks": (
                ["gcloud compute ssh", "nvidia-smi", "torch.cuda.is_available()"] if cuda else ["gcloud compute ssh"]
            ),
            "windows_cuda_torch": "torch==2.6.0+cu124" if profile == "windows-cuda" else None,
        }
        resource = {
            "profile": profile,
            "machine_id": machine_id,
            "instance": name,
            "zone": profile_zone,
            "region": regional["region"],
            "subnet": regional["subnet"],
            "machine_type": machine_type,
            "gpu": gpu,
            "image": image,
            "image_project": image_project,
            "boot_disk_gib": GCP_DISK_GIB,
            "boot_disk_type": boot_disk_type,
            "external_address": False,
            "service_account": False,
            "bootstrap": bootstrap,
        }
        resources.append(resource)
        profile_phases.append(
            {
                "order": len(profile_phases) + 1,
                "profile": profile,
                "machine_id": machine_id,
                "resource": resource,
                "create_commands": [create_command],
                "verify_create_commands": [verify_create_command],
                "cleanup_commands": [cleanup_command],
                "verify_cleanup_commands": phase_verify_cleanup_commands,
                "qualification_must_pass_before_cleanup": True,
                "cleanup_success_condition": "every profile cleanup verification command returns empty stdout",
            }
        )

    cleanup_commands = [_command("gcloud", "compute", "firewall-rules", "delete", firewall, *common)]
    cleanup_phases = [
        {
            "role": "firewall",
            "cleanup_commands": [cleanup_commands[0]],
            "verify_cleanup_commands": [
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
                )
            ],
        }
    ]
    for regional in reversed(regional_networks):
        nat_delete = _command(
            "gcloud",
            "compute",
            "routers",
            "nats",
            "delete",
            regional["nat"],
            "--router",
            regional["router"],
            "--region",
            regional["region"],
            *common,
        )
        nat_verify = _command(
            "gcloud",
            "compute",
            "routers",
            "nats",
            "list",
            "--router",
            regional["router"],
            "--region",
            regional["region"],
            "--project",
            project,
            "--filter",
            f"name={regional['nat']}",
            "--format",
            "value(name)",
        )
        cleanup_phases.append(
            {
                "role": regional["role"],
                "cleanup_commands": [nat_delete],
                "verify_cleanup_commands": [nat_verify],
            }
        )
        regional_deletes = [
            _command(
                "gcloud",
                "compute",
                "routers",
                "delete",
                regional["router"],
                "--region",
                regional["region"],
                *common,
            ),
            _command(
                "gcloud",
                "compute",
                "networks",
                "subnets",
                "delete",
                regional["subnet"],
                "--region",
                regional["region"],
                *common,
            ),
            _command(
                "gcloud",
                "compute",
                "addresses",
                "delete",
                regional["address"],
                "--region",
                regional["region"],
                *common,
            ),
        ]
        cleanup_phases.append(
            {
                "role": f"{regional['role']}-regional-resources",
                "cleanup_commands": regional_deletes,
                "verify_cleanup_commands": [],
            }
        )
        cleanup_commands.extend([nat_delete, *regional_deletes])
    network_delete = _command("gcloud", "compute", "networks", "delete", network, *common)
    cleanup_commands.append(network_delete)
    cleanup_phases.append(
        {
            "role": "network",
            "cleanup_commands": [network_delete],
            "verify_cleanup_commands": [],
        }
    )

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
        *[
            _command(
                "gcloud",
                "compute",
                "disks",
                "list",
                "--project",
                project,
                "--filter",
                f"name={resource['instance']}",
                "--format",
                "value(name)",
            )
            for resource in resources
        ],
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
    ]
    for regional in regional_networks:
        verify_cleanup_commands.extend(
            [
                _command(
                    "gcloud",
                    "compute",
                    "routers",
                    "list",
                    "--project",
                    project,
                    "--filter",
                    f"name={regional['router']} AND region:{regional['region']}",
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
                    f"name={regional['subnet']} AND region:{regional['region']}",
                    "--format",
                    "value(name)",
                ),
                _command(
                    "gcloud",
                    "compute",
                    "addresses",
                    "list",
                    "--project",
                    project,
                    "--filter",
                    f"name={regional['address']} AND region:{regional['region']}",
                    "--format",
                    "value(name)",
                ),
            ]
        )
    verify_cleanup_commands.append(
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
        )
    )

    serialized = json.dumps(
        {
            "create": create_commands,
            "verify_create": verify_create_commands,
            "profile_phases": profile_phases,
            "cleanup_phases": cleanup_phases,
            "cleanup": cleanup_commands,
            "verify_cleanup": verify_cleanup_commands,
        },
        sort_keys=True,
    )
    if "communityai-bootstrap-1" in serialized:
        raise CostGuardError("provider plan must never target the existing bootstrap peer")
    return {
        "project": project,
        "zone": zone,
        "region": primary_region,
        "cuda_fallback_zone": cuda_fallback_zone,
        "cuda_shape": cuda_shape,
        "network": network,
        "subnet": regional_networks[0]["subnet"],
        "router": regional_networks[0]["router"],
        "nat": regional_networks[0]["nat"],
        "nat_address": regional_networks[0]["address"],
        "regional_networks": regional_networks,
        "iap_firewall_rule": firewall,
        "resources": resources,
        "one_host_at_a_time": True,
        "execution_contract": (
            "execute infrastructure create/verify once, then each profile phase in order including "
            "cleanup and empty-output verification before the next host, then each cleanup phase in order"
        ),
        "create_commands": create_commands,
        "verify_create_commands": verify_create_commands,
        "profile_phases": profile_phases,
        "expected_source_images": expected_source_images,
        "cleanup_phases": cleanup_phases,
        "cleanup_commands": cleanup_commands,
        "verify_cleanup_commands": verify_cleanup_commands,
        "cleanup_success_condition": "every cleanup verification command returns empty stdout",
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
    windows_image: str | None,
    linux_image: str | None,
    cuda_fallback_zone: str | None,
    cuda_shape: str,
    manual_maximum_usd: Decimal | None,
    fly_app: str | None = None,
    fly_region: str | None = None,
    fly_image: str | None = None,
    fly_image_evidence_digest: str | None = None,
    primary_image: str | None = None,
    primary_image_evidence_digest: str | None = None,
    standby_image: str | None = None,
    standby_image_evidence_digest: str | None = None,
    runtime_bootstrap_digest: str | None = None,
    runtime_bootstrap_bytes: int | None = None,
    initial_peer: str | None = None,
    host_controller_digest: str | None = None,
    host_controller_bytes: int | None = None,
    acceptance_probe_digest: str | None = None,
    acceptance_probe_bytes: int | None = None,
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
        if project is None or zone is None or linux_image is None:
            raise CostGuardError("GCP planning requires an exact project, zone, and immutable Linux OS image")
        public_route_fields = (
            primary_image,
            primary_image_evidence_digest,
            standby_image,
            standby_image_evidence_digest,
            runtime_bootstrap_digest,
            runtime_bootstrap_bytes,
            initial_peer,
            host_controller_digest,
            host_controller_bytes,
            acceptance_probe_digest,
            acceptance_probe_bytes,
        )
        if workload == GCP_QUALIFICATION_WORKLOAD:
            if windows_image is None:
                raise CostGuardError("GCP qualification planning requires an immutable Windows OS image")
            if any(value is not None for value in public_route_fields):
                raise CostGuardError("GCP qualification planning must not contain public-route image fields")
            maximum_usd, assumptions = _gcp_cost(
                maximum_hours,
                split_region=cuda_fallback_zone is not None,
                cuda_shape=cuda_shape,
            )
            provider_plan = _gcp_plan(
                run_id,
                project,
                zone,
                source_commit,
                maximum_hours,
                windows_image=windows_image,
                linux_image=linux_image,
                cuda_fallback_zone=cuda_fallback_zone,
                cuda_shape=cuda_shape,
            )
        else:
            if windows_image is not None or cuda_fallback_zone is not None or cuda_shape != "g2-l4":
                raise CostGuardError(
                    "GCP public-route planning requires g2-l4 and does not accept Windows or fallback-zone fields"
                )
            if any(value is None for value in public_route_fields):
                raise CostGuardError(
                    "GCP public-route planning requires immutable images, evidence digests, and a bound runtime bootstrap"
                )
            maximum_usd, assumptions = _gcp_public_route_cost(maximum_hours)
            provider_plan = _gcp_public_route_plan(
                run_id,
                project,
                zone,
                source_commit,
                maximum_hours,
                linux_image=linux_image,
                primary_image=primary_image,
                primary_image_evidence_digest=primary_image_evidence_digest,
                standby_image=standby_image,
                standby_image_evidence_digest=standby_image_evidence_digest,
                runtime_bootstrap_digest=runtime_bootstrap_digest,
                runtime_bootstrap_bytes=runtime_bootstrap_bytes,
                initial_peer=initial_peer,
                host_controller_digest=host_controller_digest,
                host_controller_bytes=host_controller_bytes,
                acceptance_probe_digest=acceptance_probe_digest,
                acceptance_probe_bytes=acceptance_probe_bytes,
            )
    else:
        if (
            any(
                value is not None
                for value in (
                    project,
                    zone,
                    windows_image,
                    linux_image,
                    cuda_fallback_zone,
                    primary_image,
                    primary_image_evidence_digest,
                    standby_image,
                    standby_image_evidence_digest,
                    runtime_bootstrap_digest,
                    runtime_bootstrap_bytes,
                    initial_peer,
                    host_controller_digest,
                    host_controller_bytes,
                    acceptance_probe_digest,
                    acceptance_probe_bytes,
                )
            )
            or cuda_shape != "n1-t4"
        ):
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
        "cleanup_required_for_pass": workload not in {FLY_DISCOVERY_SEED_WORKLOAD, GCP_PUBLIC_ROUTE_WORKLOAD},
        "failure_cleanup_required": True,
        "persistent_resources_after_pass": workload in {FLY_DISCOVERY_SEED_WORKLOAD, GCP_PUBLIC_ROUTE_WORKLOAD},
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
        choices=(
            GCP_QUALIFICATION_WORKLOAD,
            GCP_PUBLIC_ROUTE_WORKLOAD,
            FLY_RECOVERY_WORKLOAD,
            FLY_DISCOVERY_SEED_WORKLOAD,
        ),
    )
    parser.add_argument("--purpose", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--ledger", type=Path, default=Path("docs/RELEASE_READINESS.md"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--maximum-hours", type=_decimal_argument, default=MAX_GCP_FLEET_HOURS)
    parser.add_argument("--project")
    parser.add_argument("--zone")
    parser.add_argument("--windows-image")
    parser.add_argument("--linux-image")
    parser.add_argument("--cuda-fallback-zone")
    parser.add_argument("--cuda-shape", choices=("n1-t4", "g2-l4"), default="n1-t4")
    parser.add_argument("--manual-maximum-usd", type=_decimal_argument)
    parser.add_argument("--fly-app")
    parser.add_argument("--fly-region")
    parser.add_argument("--fly-image")
    parser.add_argument("--fly-image-evidence-digest")
    parser.add_argument("--primary-image")
    parser.add_argument("--primary-image-evidence-digest")
    parser.add_argument("--standby-image")
    parser.add_argument("--standby-image-evidence-digest")
    parser.add_argument("--runtime-bootstrap-digest")
    parser.add_argument("--runtime-bootstrap-bytes", type=int)
    parser.add_argument("--initial-peer")
    parser.add_argument("--host-controller-digest")
    parser.add_argument("--host-controller-bytes", type=int)
    parser.add_argument("--acceptance-probe-digest")
    parser.add_argument("--acceptance-probe-bytes", type=int)
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
            windows_image=args.windows_image,
            linux_image=args.linux_image,
            cuda_fallback_zone=args.cuda_fallback_zone,
            cuda_shape=args.cuda_shape,
            manual_maximum_usd=args.manual_maximum_usd,
            fly_app=args.fly_app,
            fly_region=args.fly_region,
            fly_image=args.fly_image,
            fly_image_evidence_digest=args.fly_image_evidence_digest,
            primary_image=args.primary_image,
            primary_image_evidence_digest=args.primary_image_evidence_digest,
            standby_image=args.standby_image,
            standby_image_evidence_digest=args.standby_image_evidence_digest,
            runtime_bootstrap_digest=args.runtime_bootstrap_digest,
            runtime_bootstrap_bytes=args.runtime_bootstrap_bytes,
            initial_peer=args.initial_peer,
            host_controller_digest=args.host_controller_digest,
            host_controller_bytes=args.host_controller_bytes,
            acceptance_probe_digest=args.acceptance_probe_digest,
            acceptance_probe_bytes=args.acceptance_probe_bytes,
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
