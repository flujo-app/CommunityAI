"""Run a fail-closed multi-machine parity and interruption-recovery gate.

Workers and bootstrap peers are provisioned outside this script. A public topology
document binds privacy-safe machine/resource labels to stable signed PeerIDs and exact
block spans. A private control plan supplies shell-free commands that interrupt one
selected worker and clean up every provisioned resource. Credentials belong in the
control adapter's environment and are never serialized into qualification evidence.

A passing report proves one controlled multi-machine run only. It always retains
complete_release_qualification=false because cross-platform, public-worker, resource,
and catalog publication gates remain independent.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import torch
from hivemind import DHT
from transformers import AutoTokenizer

import drift
from drift import AutoDistributedModelForCausalLM
from drift.data_structures import UID_DELIMITER
from drift.model_manifest import ManifestArtifactVerifier, ManifestError, ModelManifest, resolve_manifest_loading
from drift.utils.auto_config import AutoDistributedConfig
from drift.utils.dht import get_remote_module_infos
from drift.utils.reference_model import load_reference_model_for_causal_lm

if __package__:
    from scripts.qualify_model_manifest import _absolute, _write_report, infer_source_commit, redact_host_paths
else:
    from qualify_model_manifest import _absolute, _write_report, infer_source_commit, redact_host_paths


MULTI_MACHINE_SCHEMA_VERSION = 1
TOPOLOGY_SCHEMA_VERSION = 1
CONTROL_SCHEMA_VERSION = 1
MATRIX_SCHEMA_VERSION = 1
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_LABEL_RE = _RUN_ID_RE
_PEER_ID_RE = re.compile(r"^[A-Za-z0-9]{20,128}$")
_SOURCE_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_MAX_COMMAND_ARGUMENTS = 64
_MAX_COMMAND_ARGUMENT_LENGTH = 4096
_MAX_JSON_BYTES = 1_000_000
_MAX_JSON_DEPTH = 16
_MAX_JSON_NODES = 20_000
_MAX_CONTROL_OUTPUT_BYTES = 65_536
_MAX_BOOTSTRAP_PEERS = 16
_MAX_BOOTSTRAP_RESOURCES = 16
_MAX_WORKERS = 256
_WINDOWS_PATH_LINE_RE = re.compile(r"(?im)(?<![A-Za-z0-9])(?:[A-Z]:[\\/]|\\\\)[^\r\n]*")
_POSIX_PATH_LINE_RE = re.compile(r"(?m)(?<![:/A-Za-z0-9])/(?!/)[^\r\n]*")
_URL_RE = re.compile(r"(?i)\b(?:https?|tcp)://[^\s]+")
_MULTIADDR_RE = re.compile(r"/(?:ip4|ip6|dns4|dns6|dnsaddr)/[^\s]+")
_IP_ENDPOINT_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?\b")
_SECRET_ASSIGNMENT_RE = re.compile(r"(?i)\b(token|secret|password|api[-_ ]?key|authorization)\s*([:=])\s*[^\s,;]+")
REQUIRED_MATRIX_PROFILES = frozenset(
    {
        "windows:cpu",
        "windows:cuda",
        "linux:cpu",
        "linux:cuda",
        "macos:cpu",
        "macos:mps",
    }
)
INCOMPLETE_MISSING_PROFILES = ("macos:cpu", "macos:mps")
INCOMPLETE_MATRIX_PROFILES = REQUIRED_MATRIX_PROFILES.difference(INCOMPLETE_MISSING_PROFILES)


class QualificationError(ValueError):
    """Input or observed evidence cannot support the multi-machine gate."""


@dataclass(frozen=True)
class Worker:
    machine_id: str
    peer_id: str
    resource_id: str
    spans: tuple[tuple[int, int], ...]

    def covers(self, block_index: int) -> bool:
        return any(start <= block_index < end for start, end in self.spans)

    def to_evidence(self) -> dict[str, Any]:
        return {
            "machine_id": self.machine_id,
            "peer_id": self.peer_id,
            "spans": [list(span) for span in self.spans],
        }


@dataclass(frozen=True)
class Route:
    name: str
    peer_ids: tuple[str, ...]


@dataclass(frozen=True)
class Topology:
    run_id: str
    bootstrap_peers: tuple[str, ...]
    bootstrap_resources: tuple[str, ...]
    workers: tuple[Worker, ...]
    routes: tuple[Route, Route]
    num_blocks: int

    @property
    def worker_by_peer(self) -> dict[str, Worker]:
        return {worker.peer_id: worker for worker in self.workers}

    @property
    def expected_resources(self) -> frozenset[str]:
        return frozenset((*self.bootstrap_resources, *(worker.resource_id for worker in self.workers)))

    def expected_peers(self, block_index: int) -> frozenset[str]:
        return frozenset(worker.peer_id for worker in self.workers if worker.covers(block_index))

    def to_evidence(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "num_blocks": self.num_blocks,
            "bootstrap_resource_count": len(self.bootstrap_resources),
            "workers": [worker.to_evidence() for worker in self.workers],
            "routes": [{"name": route.name, "peer_ids": list(route.peer_ids)} for route in self.routes],
        }


@dataclass(frozen=True)
class ControlPlan:
    run_id: str
    interrupt_commands: Mapping[str, tuple[str, ...]]
    cleanup_command: tuple[str, ...]
    execution_directory: Path


@dataclass(frozen=True)
class ActiveSpan:
    start: int
    end: int
    peer_id: str

    def to_evidence(self, topology: Topology) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "peer_id": self.peer_id,
            "machine_id": topology.worker_by_peer[self.peer_id].machine_id,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Qualify exact manifested inference across controlled workers on separate machines",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("manifest", type=Path, help="Exact ModelManifest v1 candidate")
    parser.add_argument("--matrix-report", type=Path, required=True, help="Passed strict local cross-platform matrix")
    parser.add_argument(
        "--allow-incomplete-matrix",
        action="store_true",
        help="Authorize only the four-profile Windows/Linux recovery exercise with both macOS profiles missing",
    )
    parser.add_argument("--topology", type=Path, required=True, help="Public run topology JSON")
    parser.add_argument("--control-plan", type=Path, required=True, help="Private shell-free interruption/cleanup plan")
    parser.add_argument("--artifact-root", type=Path, required=True, help="Complete verified publisher snapshot")
    parser.add_argument("--cache-dir", type=Path, help="Existing immutable Hub/runtime cache")
    parser.add_argument("--source-commit", required=True, help="Exact checkout commit bound by the matrix")
    parser.add_argument("--device", default="cpu", help="Client stock-reference device")
    parser.add_argument("--prompt", default="Hello", help="Synthetic qualification prompt; never persisted")
    parser.add_argument("--tokens", type=int, default=8, help="Exact number of generated tokens")
    parser.add_argument("--coverage-timeout", type=float, default=300)
    parser.add_argument("--request-timeout", type=float, default=5)
    parser.add_argument("--control-timeout", type=float, default=60)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True, help="Atomic bounded qualification report")
    return parser


def _validate_json_shape(value: Any, *, depth: int = 0) -> int:
    if depth > _MAX_JSON_DEPTH:
        raise QualificationError(f"JSON nesting exceeds {_MAX_JSON_DEPTH}")
    if isinstance(value, dict):
        count = 1 + sum(
            _validate_json_shape(key, depth=depth + 1) + _validate_json_shape(item, depth=depth + 1)
            for key, item in value.items()
        )
    elif isinstance(value, list):
        count = 1 + sum(_validate_json_shape(item, depth=depth + 1) for item in value)
    else:
        count = 1
    if count > _MAX_JSON_NODES:
        raise QualificationError(f"JSON structure exceeds {_MAX_JSON_NODES} nodes")
    return count


def _load_json(path: Path, field: str) -> Any:
    try:
        if path.stat().st_size > _MAX_JSON_BYTES:
            raise QualificationError(f"{field} exceeds {_MAX_JSON_BYTES} bytes")
        value = json.loads(path.read_text(encoding="utf-8"))
        _validate_json_shape(value)
        return value
    except QualificationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QualificationError(f"{field} is not readable bounded JSON: {type(exc).__name__}") from exc


def _require_object(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise QualificationError(f"{field} must be a JSON object")
    return value


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    observed = set(value)
    if observed != expected:
        missing = sorted(expected - observed)
        extra = sorted(observed - expected)
        raise QualificationError(f"{field} keys differ: missing={missing}, extra={extra}")


def _require_label(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _LABEL_RE.fullmatch(value):
        raise QualificationError(f"{field} must be a privacy-safe 1-64 character label")
    return value


def _require_peer_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _PEER_ID_RE.fullmatch(value):
        raise QualificationError(f"{field} must be a stable base58/base32 PeerID")
    return value


def _parse_spans(value: Any, field: str, num_blocks: int) -> tuple[tuple[int, int], ...]:
    if not isinstance(value, list) or not value or len(value) > num_blocks:
        raise QualificationError(f"{field} must be a non-empty JSON array")
    spans: list[tuple[int, int]] = []
    for index, raw_span in enumerate(value):
        if (
            not isinstance(raw_span, list)
            or len(raw_span) != 2
            or isinstance(raw_span[0], bool)
            or isinstance(raw_span[1], bool)
            or not isinstance(raw_span[0], int)
            or not isinstance(raw_span[1], int)
        ):
            raise QualificationError(f"{field}[{index}] must be [start, end] integers")
        start, end = raw_span
        if start < 0 or end <= start or end > num_blocks:
            raise QualificationError(f"{field}[{index}] is outside the manifested block range")
        spans.append((start, end))
    spans.sort()
    for previous, current in zip(spans, spans[1:]):
        if current[0] < previous[1]:
            raise QualificationError(f"{field} contains overlapping spans")
    return tuple(spans)


def _route_covers_all_blocks(route: Route, workers: Mapping[str, Worker], num_blocks: int) -> bool:
    return all(any(workers[peer_id].covers(block) for peer_id in route.peer_ids) for block in range(num_blocks))


def load_topology(path: Path, manifest: ModelManifest) -> Topology:
    raw = _require_object(_load_json(path, "topology"), "topology")
    _require_exact_keys(
        raw,
        {"schema_version", "run_id", "bootstrap_peers", "bootstrap_resources", "workers", "routes"},
        "topology",
    )
    if raw["schema_version"] != TOPOLOGY_SCHEMA_VERSION:
        raise QualificationError(f"topology.schema_version must be {TOPOLOGY_SCHEMA_VERSION}")
    run_id = _require_label(raw["run_id"], "topology.run_id")

    raw_bootstrap_peers = raw["bootstrap_peers"]
    if (
        not isinstance(raw_bootstrap_peers, list)
        or not raw_bootstrap_peers
        or len(raw_bootstrap_peers) > _MAX_BOOTSTRAP_PEERS
    ):
        raise QualificationError("topology.bootstrap_peers must be a non-empty bounded JSON array")
    bootstrap_peers: list[str] = []
    for index, peer in enumerate(raw_bootstrap_peers):
        if not isinstance(peer, str) or not peer or len(peer) > 2048:
            raise QualificationError(f"topology.bootstrap_peers[{index}] must be a bounded multiaddr string")
        if "/p2p/" not in peer:
            raise QualificationError(f"topology.bootstrap_peers[{index}] must include an authenticated /p2p/ identity")
        bootstrap_peers.append(peer)
    if len(set(bootstrap_peers)) != len(bootstrap_peers):
        raise QualificationError("topology.bootstrap_peers contains duplicates")

    raw_bootstrap_resources = raw["bootstrap_resources"]
    if (
        not isinstance(raw_bootstrap_resources, list)
        or not raw_bootstrap_resources
        or len(raw_bootstrap_resources) > _MAX_BOOTSTRAP_RESOURCES
    ):
        raise QualificationError("topology.bootstrap_resources must be a non-empty bounded JSON array")
    bootstrap_resources = tuple(
        _require_label(value, f"topology.bootstrap_resources[{index}]")
        for index, value in enumerate(raw_bootstrap_resources)
    )

    raw_workers = raw["workers"]
    if not isinstance(raw_workers, list) or not 4 <= len(raw_workers) <= _MAX_WORKERS:
        raise QualificationError("topology.workers must contain 4-256 split-route workers")
    workers: list[Worker] = []
    for index, raw_worker in enumerate(raw_workers):
        worker = _require_object(raw_worker, f"topology.workers[{index}]")
        _require_exact_keys(worker, {"machine_id", "peer_id", "resource_id", "spans"}, f"topology.workers[{index}]")
        workers.append(
            Worker(
                machine_id=_require_label(worker["machine_id"], f"topology.workers[{index}].machine_id"),
                peer_id=_require_peer_id(worker["peer_id"], f"topology.workers[{index}].peer_id"),
                resource_id=_require_label(worker["resource_id"], f"topology.workers[{index}].resource_id"),
                spans=_parse_spans(worker["spans"], f"topology.workers[{index}].spans", manifest.model.num_blocks),
            )
        )

    for field, values in (
        ("machine_id", [worker.machine_id for worker in workers]),
        ("peer_id", [worker.peer_id for worker in workers]),
        ("resource_id", [worker.resource_id for worker in workers]),
    ):
        if len(set(values)) != len(values):
            raise QualificationError(f"topology workers must have unique {field} values")
    all_resources = [*bootstrap_resources, *(worker.resource_id for worker in workers)]
    if len(set(all_resources)) != len(all_resources):
        raise QualificationError("topology resource labels must be globally unique")

    raw_routes = raw["routes"]
    if not isinstance(raw_routes, list) or len(raw_routes) != 2:
        raise QualificationError("topology.routes must contain exactly two independent routes")
    routes: list[Route] = []
    worker_by_peer = {worker.peer_id: worker for worker in workers}
    for index, raw_route in enumerate(raw_routes):
        route = _require_object(raw_route, f"topology.routes[{index}]")
        _require_exact_keys(route, {"name", "peer_ids"}, f"topology.routes[{index}]")
        name = _require_label(route["name"], f"topology.routes[{index}].name")
        raw_peer_ids = route["peer_ids"]
        if not isinstance(raw_peer_ids, list) or not 2 <= len(raw_peer_ids) <= len(workers):
            raise QualificationError(f"topology.routes[{index}].peer_ids must contain a split route")
        peer_ids = tuple(
            _require_peer_id(value, f"topology.routes[{index}].peer_ids[{peer_index}]")
            for peer_index, value in enumerate(raw_peer_ids)
        )
        if len(set(peer_ids)) != len(peer_ids):
            raise QualificationError(f"topology.routes[{index}] repeats a worker")
        unknown = sorted(set(peer_ids) - set(worker_by_peer))
        if unknown:
            raise QualificationError(f"topology.routes[{index}] contains unknown workers")
        routes.append(Route(name=name, peer_ids=peer_ids))
    if routes[0].name == routes[1].name:
        raise QualificationError("topology route names must be unique")
    if set(routes[0].peer_ids) & set(routes[1].peer_ids):
        raise QualificationError("topology routes must use disjoint PeerIDs")
    route_machines = [{worker_by_peer[peer_id].machine_id for peer_id in route.peer_ids} for route in routes]
    if route_machines[0] & route_machines[1]:
        raise QualificationError("topology routes must run on disjoint machines")
    if set(routes[0].peer_ids) | set(routes[1].peer_ids) != set(worker_by_peer):
        raise QualificationError("every topology worker must belong to exactly one declared route")
    for route in routes:
        if not _route_covers_all_blocks(route, worker_by_peer, manifest.model.num_blocks):
            raise QualificationError(f"topology route {route.name!r} does not cover every manifested block")
        if any((0, manifest.model.num_blocks) in worker_by_peer[peer_id].spans for peer_id in route.peer_ids):
            raise QualificationError(
                f"topology route {route.name!r} contains a full-range worker; split routing is required"
            )
    for block in range(manifest.model.num_blocks):
        machines = {worker.machine_id for worker in workers if worker.covers(block)}
        if len(machines) < 2:
            raise QualificationError(f"manifested block {block} lacks two-machine redundancy")

    return Topology(
        run_id=run_id,
        bootstrap_peers=tuple(bootstrap_peers),
        bootstrap_resources=bootstrap_resources,
        workers=tuple(workers),
        routes=(routes[0], routes[1]),
        num_blocks=manifest.model.num_blocks,
    )


def _parse_command(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > _MAX_COMMAND_ARGUMENTS:
        raise QualificationError(f"{field} must be a non-empty bounded argv array")
    command: list[str] = []
    for index, argument in enumerate(value):
        if (
            not isinstance(argument, str)
            or not argument
            or "\x00" in argument
            or len(argument) > _MAX_COMMAND_ARGUMENT_LENGTH
        ):
            raise QualificationError(f"{field}[{index}] is not a safe bounded argv string")
        command.append(argument)
    return tuple(command)


def load_cleanup_command(path: Path, topology: Topology) -> tuple[str, ...]:
    raw = _require_object(_load_json(path, "control plan"), "control plan")
    if raw.get("schema_version") != CONTROL_SCHEMA_VERSION:
        raise QualificationError(f"control plan.schema_version must be {CONTROL_SCHEMA_VERSION}")
    if _require_label(raw.get("run_id"), "control plan.run_id") != topology.run_id:
        raise QualificationError("control plan run_id does not match topology")
    return _parse_command(raw.get("cleanup_command"), "control plan.cleanup_command")


def load_control_plan(path: Path, topology: Topology) -> ControlPlan:
    raw = _require_object(_load_json(path, "control plan"), "control plan")
    _require_exact_keys(raw, {"schema_version", "run_id", "interrupt_commands", "cleanup_command"}, "control plan")
    if raw["schema_version"] != CONTROL_SCHEMA_VERSION:
        raise QualificationError(f"control plan.schema_version must be {CONTROL_SCHEMA_VERSION}")
    run_id = _require_label(raw["run_id"], "control plan.run_id")
    if run_id != topology.run_id:
        raise QualificationError("control plan run_id does not match topology")
    raw_interrupt = _require_object(raw["interrupt_commands"], "control plan.interrupt_commands")
    expected_peers = set(topology.worker_by_peer)
    if set(raw_interrupt) != expected_peers:
        raise QualificationError("control plan must contain exactly one interrupt command per topology worker")
    commands = {
        peer_id: _parse_command(raw_interrupt[peer_id], f"control plan.interrupt_commands[{peer_id!r}]")
        for peer_id in sorted(raw_interrupt)
    }
    return ControlPlan(
        run_id=run_id,
        interrupt_commands=commands,
        cleanup_command=_parse_command(raw["cleanup_command"], "control plan.cleanup_command"),
        execution_directory=path.resolve().parent,
    )


def _validate_matrix_coverage_entry(
    value: Any,
    *,
    profile: str,
    source_commit: str,
) -> tuple[str, str]:
    entry = _require_object(value, f"matrix report.coverage[{profile!r}] entry")
    _require_exact_keys(
        entry,
        {"report", "generated_at", "machine_id", "system", "device", "profile", "source_commit", "drift"},
        f"matrix report.coverage[{profile!r}] entry",
    )
    report_id = entry["report"]
    if not isinstance(report_id, str) or not re.fullmatch(r"input-[1-9][0-9]*", report_id):
        raise QualificationError("matrix coverage report IDs must be opaque input-N labels")
    machine_id = _require_label(entry["machine_id"], "matrix coverage machine_id")
    system, _, device = profile.partition(":")
    if (
        entry["profile"] != profile
        or entry["system"] != system
        or entry["device"] != device
        or entry["source_commit"] != source_commit
        or entry["drift"] != drift.__version__
    ):
        raise QualificationError(f"matrix coverage entry does not match required profile {profile}")
    try:
        generated_at = datetime.fromisoformat(str(entry["generated_at"]))
    except ValueError as exc:
        raise QualificationError("matrix coverage generated_at must be ISO-8601") from exc
    if generated_at.tzinfo is None:
        raise QualificationError("matrix coverage generated_at must include a timezone")
    return machine_id, system


def validate_matrix_report(
    path: Path,
    manifest: ModelManifest,
    *,
    source_commit: str,
    allow_incomplete: bool = False,
) -> dict[str, Any]:
    raw = _require_object(_load_json(path, "matrix report"), "matrix report")
    _require_exact_keys(
        raw,
        {
            "schema_version",
            "generated_at",
            "scope",
            "model",
            "requirements",
            "source_identity",
            "coverage",
            "missing_profiles",
            "report_errors",
            "matrix_errors",
            "result",
            "complete_release_qualification",
            "not_covered",
        },
        "matrix report",
    )
    if raw.get("schema_version") != MATRIX_SCHEMA_VERSION:
        raise QualificationError(f"matrix report schema_version must be {MATRIX_SCHEMA_VERSION}")
    if raw.get("scope") != "cross-platform-local-matrix":
        raise QualificationError("matrix report scope must be cross-platform-local-matrix")
    expected_result = "incomplete" if allow_incomplete else "passed"
    if raw.get("result") != expected_result:
        raise QualificationError(
            f"matrix report result must be {expected_result!r} for the selected qualification mode"
        )
    if raw.get("complete_release_qualification") is not False:
        raise QualificationError("matrix report must retain complete_release_qualification=false")
    if raw.get("not_covered") != [
        "multi-machine routing and interruption recovery",
        "cold-client resource envelope",
        "public-worker route redundancy and soak",
        "signed catalog publication and release bootstrap",
    ]:
        raise QualificationError("matrix report not_covered gates differ from the strict aggregator schema")
    try:
        matrix_generated_at = datetime.fromisoformat(str(raw["generated_at"]))
    except ValueError as exc:
        raise QualificationError("matrix report generated_at must be ISO-8601") from exc
    if matrix_generated_at.tzinfo is None:
        raise QualificationError("matrix report generated_at must include a timezone")
    model = _require_object(raw.get("model"), "matrix report.model")
    _require_exact_keys(
        model,
        {"name", "repository", "revision", "manifest_digest", "runtime"},
        "matrix report.model",
    )
    if model.get("name") != manifest.name or model.get("manifest_digest") != manifest.digest_id:
        raise QualificationError("matrix report model name or manifest digest does not match")
    if model.get("repository") != manifest.source.repository or model.get("revision") != manifest.source.revision:
        raise QualificationError("matrix report source identity does not match")
    if model.get("runtime") != manifest.runtime.to_dict():
        raise QualificationError("matrix report runtime does not match")
    source_identity = _require_object(raw.get("source_identity"), "matrix report.source_identity")
    _require_exact_keys(source_identity, {"source_commit", "drift"}, "matrix report.source_identity")
    if source_identity.get("source_commit") != source_commit:
        raise QualificationError("matrix report source commit does not match")
    if source_identity.get("drift") != drift.__version__:
        raise QualificationError("matrix report DRIFT build does not match the running controller")
    if raw.get("report_errors") != [] or raw.get("matrix_errors") != []:
        raise QualificationError("matrix report contains validation errors")
    missing_profiles = raw.get("missing_profiles")
    expected_missing_profiles = list(INCOMPLETE_MISSING_PROFILES) if allow_incomplete else []
    if (
        not isinstance(missing_profiles, list)
        or any(not isinstance(profile, str) for profile in missing_profiles)
        or len(missing_profiles) != len(set(missing_profiles))
        or set(missing_profiles) != set(expected_missing_profiles)
    ):
        raise QualificationError("matrix report missing profiles do not match the selected qualification mode")
    requirements = _require_object(raw.get("requirements"), "matrix report.requirements")
    _require_exact_keys(
        requirements,
        {"profiles", "source_commit", "drift_version"},
        "matrix report.requirements",
    )
    if requirements.get("source_commit") != source_commit:
        raise QualificationError("matrix requirements source commit does not match")
    if requirements.get("drift_version") != drift.__version__:
        raise QualificationError("matrix requirements must pin the running DRIFT version")
    profiles = requirements.get("profiles")
    if (
        not isinstance(profiles, list)
        or any(not isinstance(profile, str) for profile in profiles)
        or len(profiles) != len(set(profiles))
        or set(profiles) != set(REQUIRED_MATRIX_PROFILES)
    ):
        raise QualificationError("matrix report must require the exact six release profiles")
    coverage = _require_object(raw.get("coverage"), "matrix report.coverage")
    expected_coverage = INCOMPLETE_MATRIX_PROFILES if allow_incomplete else REQUIRED_MATRIX_PROFILES
    if set(coverage) != set(expected_coverage) or any(
        not isinstance(coverage[profile], list) or not coverage[profile] for profile in expected_coverage
    ):
        requirement = "the four Windows/Linux profiles" if allow_incomplete else "all six release profiles"
        raise QualificationError(f"matrix report must contain evidence for exactly {requirement}")
    machine_systems: dict[str, str] = {}
    report_ids: set[str] = set()
    for profile in sorted(expected_coverage):
        profile_machines: set[str] = set()
        for entry in coverage[profile]:
            machine_id, system = _validate_matrix_coverage_entry(
                entry,
                profile=profile,
                source_commit=source_commit,
            )
            if machine_id in profile_machines:
                raise QualificationError(f"matrix profile {profile} repeats a machine")
            profile_machines.add(machine_id)
            previous_system = machine_systems.setdefault(machine_id, system)
            if previous_system != system:
                raise QualificationError("matrix reuses one machine ID across operating systems")
            report_id = entry["report"]
            if report_id in report_ids:
                raise QualificationError("matrix coverage repeats a report ID")
            report_ids.add(report_id)
    return {
        "result": expected_result,
        "missing_profiles": expected_missing_profiles,
        "source_identity": {"source_commit": source_commit, "drift": drift.__version__},
    }


def _parse_control_ack(
    stdout: str,
    *,
    action: str,
    topology: Topology,
    worker: Worker | None,
    nonce: str,
) -> Mapping[str, Any]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise QualificationError(f"{action} control adapter must emit exactly one JSON acknowledgement line")
    try:
        ack = _require_object(json.loads(lines[0]), f"{action} acknowledgement")
        _validate_json_shape(ack)
    except json.JSONDecodeError as exc:
        raise QualificationError(f"{action} control acknowledgement is not JSON") from exc
    common_keys = {"schema_version", "action", "run_id", "nonce"}
    if action == "interrupt":
        _require_exact_keys(
            ack,
            common_keys | {"peer_id", "machine_id", "resource_id", "hard_kill", "process_exited"},
            "interrupt acknowledgement",
        )
    elif action == "cleanup":
        _require_exact_keys(
            ack,
            common_keys | {"cleaned", "destroyed_resources", "remaining_resources"},
            "cleanup acknowledgement",
        )
    else:
        raise QualificationError("control action must be interrupt or cleanup")
    if ack.get("schema_version") != CONTROL_SCHEMA_VERSION:
        raise QualificationError(f"{action} acknowledgement schema_version is invalid")
    if (
        ack.get("action") != action
        or ack.get("run_id") != topology.run_id
        or not secrets.compare_digest(str(ack.get("nonce")), nonce)
    ):
        raise QualificationError(f"{action} acknowledgement identity does not match the run")
    if action == "interrupt":
        assert worker is not None
        if (
            ack.get("peer_id") != worker.peer_id
            or ack.get("machine_id") != worker.machine_id
            or ack.get("resource_id") != worker.resource_id
            or ack.get("hard_kill") is not True
            or ack.get("process_exited") is not True
        ):
            raise QualificationError("interrupt acknowledgement does not prove the selected worker hard-exited")
    else:
        destroyed = ack.get("destroyed_resources")
        remaining = ack.get("remaining_resources")
        if (
            ack.get("cleaned") is not True
            or not isinstance(destroyed, list)
            or any(not isinstance(item, str) for item in destroyed)
            or len(destroyed) != len(set(destroyed))
            or set(destroyed) != set(topology.expected_resources)
            or remaining != []
        ):
            raise QualificationError("cleanup acknowledgement does not prove every provisioned resource was destroyed")
    return ack


def _read_bounded_stream(
    stream: Any,
    buffer: bytearray,
    *,
    process: subprocess.Popen[bytes],
    state: dict[str, int],
    lock: threading.Lock,
    overflow: threading.Event,
) -> None:
    while True:
        chunk = stream.read(4096)
        if not chunk:
            return
        with lock:
            remaining = _MAX_CONTROL_OUTPUT_BYTES - state["bytes"]
            buffer.extend(chunk[: max(0, remaining)])
            state["bytes"] += len(chunk)
            exceeded = state["bytes"] > _MAX_CONTROL_OUTPUT_BYTES
        if exceeded:
            overflow.set()
            process.kill()
            return


def run_control_command(
    command: Sequence[str],
    *,
    action: str,
    topology: Topology,
    timeout: float,
    worker: Worker | None = None,
    cwd: Path | None = None,
) -> Mapping[str, Any]:
    if action not in {"interrupt", "cleanup"} or (action == "interrupt") != (worker is not None):
        raise QualificationError("control action and selected worker do not match")
    nonce = secrets.token_urlsafe(24)
    environment = os.environ.copy()
    environment.update(
        {
            "COMMUNITYAI_QUALIFICATION_ACTION": action,
            "COMMUNITYAI_QUALIFICATION_RUN_ID": topology.run_id,
            "COMMUNITYAI_QUALIFICATION_NONCE": nonce,
        }
    )
    if worker is not None:
        environment.update(
            {
                "COMMUNITYAI_QUALIFICATION_PEER_ID": worker.peer_id,
                "COMMUNITYAI_QUALIFICATION_MACHINE_ID": worker.machine_id,
                "COMMUNITYAI_QUALIFICATION_RESOURCE_ID": worker.resource_id,
            }
        )
    try:
        process = subprocess.Popen(
            list(command),
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            cwd=cwd,
        )
    except OSError as exc:
        raise QualificationError(f"{action} control adapter failed: {type(exc).__name__}") from exc
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    state = {"bytes": 0}
    lock = threading.Lock()
    overflow = threading.Event()
    readers = [
        threading.Thread(
            target=_read_bounded_stream,
            args=(stream, buffer),
            kwargs={"process": process, "state": state, "lock": lock, "overflow": overflow},
            daemon=True,
        )
        for stream, buffer in ((process.stdout, stdout_buffer), (process.stderr, stderr_buffer))
    ]
    for reader in readers:
        reader.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        raise QualificationError(f"{action} control adapter timed out") from exc
    finally:
        for reader in readers:
            reader.join(timeout=5)
        process.stdout.close()
        process.stderr.close()
    if overflow.is_set():
        raise QualificationError(f"{action} control adapter output exceeded {_MAX_CONTROL_OUTPUT_BYTES} bytes")
    if process.returncode != 0:
        raise QualificationError(f"{action} control adapter exited nonzero")
    stdout = stdout_buffer.decode("utf-8", errors="replace")
    return _parse_control_ack(
        stdout,
        action=action,
        topology=topology,
        worker=worker,
        nonce=nonce,
    )


def _observed_peer_ids(module_info: Any) -> frozenset[str]:
    return frozenset(str(peer_id) for peer_id in module_info.servers)


def wait_for_topology_coverage(
    dht: DHT,
    manifest: ModelManifest,
    topology: Topology,
    dht_prefix: str,
    timeout: float,
) -> list[list[str]]:
    uids = [f"{dht_prefix}{UID_DELIMITER}{block}" for block in range(topology.num_blocks)]
    deadline = time.monotonic() + timeout
    last_observed: list[list[str]] = [[] for _ in uids]
    while time.monotonic() < deadline:
        module_infos = get_remote_module_infos(
            dht,
            uids,
            manifest_digest=manifest.digest,
            manifest_execution_profile=manifest.runtime.to_dict(),
            latest=True,
        )
        last_observed = [sorted(_observed_peer_ids(info)) for info in module_infos]
        if all(set(peers) == set(topology.expected_peers(block)) for block, peers in enumerate(last_observed)):
            return last_observed
        time.sleep(1)
    counts = [len(peers) for peers in last_observed]
    raise QualificationError(f"controlled topology did not reach exact manifested coverage; observed_counts={counts}")


def _active_spans(inference_session: Any, topology: Topology) -> tuple[ActiveSpan, ...]:
    spans = tuple(
        ActiveSpan(
            start=int(server_session.span.start),
            end=int(server_session.span.end),
            peer_id=str(server_session.span.peer_id),
        )
        for server_session in inference_session._server_sessions
    )
    if len(spans) < 2:
        raise QualificationError("the active inference route is not split across at least two workers")
    known_peers = set(topology.worker_by_peer)
    if any(span.peer_id not in known_peers for span in spans):
        raise QualificationError("the active inference route contains a worker outside the controlled topology")
    if any(span.start < 0 or span.end <= span.start or span.end > topology.num_blocks for span in spans):
        raise QualificationError("the active inference route contains an invalid block span")
    for block in range(topology.num_blocks):
        covering = [span for span in spans if span.start <= block < span.end]
        if len(covering) != 1:
            raise QualificationError(f"the active route does not cover block {block} exactly once")
    return spans


def validate_replacement(
    before: Sequence[ActiveSpan],
    after: Sequence[ActiveSpan],
    victim: ActiveSpan,
    topology: Topology,
) -> dict[str, Any]:
    if victim not in before:
        raise QualificationError("the interrupted worker was not selected on the active route")
    if any(span.peer_id == victim.peer_id for span in after):
        raise QualificationError("the interrupted PeerID remained on the recovered route")
    victim_worker = topology.worker_by_peer[victim.peer_id]
    replacement = [span for span in after if span.start < victim.end and victim.start < span.end]
    if not replacement:
        raise QualificationError("the recovered route has no replacement for the interrupted span")
    replacement_workers = [topology.worker_by_peer[span.peer_id] for span in replacement]
    if any(worker.machine_id == victim_worker.machine_id for worker in replacement_workers):
        raise QualificationError("the interrupted span recovered on the same machine")
    for block in range(victim.start, victim.end):
        if not any(span.start <= block < span.end for span in replacement):
            raise QualificationError(f"the replacement route does not cover interrupted block {block}")
    return {
        "victim": victim.to_evidence(topology),
        "replacement_spans": [span.to_evidence(topology) for span in replacement],
    }


def close_distributed_client(model: Any) -> bool:
    if model is None:
        return True
    try:
        sequence_manager = model.transformer.h.sequence_manager
    except AttributeError:
        return False
    client_dht = getattr(sequence_manager, "dht", None)
    sequence_manager.shutdown()
    if client_dht is not None:
        if client_dht.is_alive():
            client_dht.shutdown()
        client_dht.join(timeout=5)
        if client_dht.is_alive():
            return False
    return True


def execute_gate(
    manifest: ModelManifest,
    topology: Topology,
    control: ControlPlan,
    *,
    artifact_root: Path,
    cache_dir: Path | None,
    device: str,
    prompt: str,
    tokens: int,
    coverage_timeout: float,
    request_timeout: float,
    control_timeout: float,
    max_retries: int,
) -> dict[str, Any]:
    revision, dht_prefix = resolve_manifest_loading(
        manifest,
        model_name_or_path=manifest.source.repository,
        revision=None,
        dht_prefix=None,
    )
    verifier = ManifestArtifactVerifier(
        manifest,
        manifest.source.repository,
        revision,
        artifact_root=artifact_root,
        cache_dir=cache_dir,
    )
    config_source = verifier.ensure_startup_metadata(include_tokenizer=True)

    coverage_dht = DHT(initial_peers=list(topology.bootstrap_peers), client_mode=True, start=True)
    try:
        observed_coverage = wait_for_topology_coverage(
            coverage_dht,
            manifest,
            topology,
            dht_prefix,
            coverage_timeout,
        )
    finally:
        coverage_dht.shutdown()
        coverage_dht.join()

    config = AutoDistributedConfig.from_pretrained(
        config_source,
        local_files_only=True,
        dht_prefix=dht_prefix,
        initial_peers=list(topology.bootstrap_peers),
        manifest_digest=manifest.digest,
        manifest_execution_profile=manifest.runtime.to_dict(),
        request_timeout=request_timeout,
        max_retries=max_retries,
        min_backoff=0.1,
        max_backoff=1,
    )
    if manifest.runtime.attention_implementation != "auto":
        config._attn_implementation = manifest.runtime.attention_implementation
    tokenizer = AutoTokenizer.from_pretrained(config_source, local_files_only=True, cache_dir=cache_dir)
    torch_dtype = getattr(torch, manifest.runtime.dtype)
    model = AutoDistributedModelForCausalLM.from_pretrained(
        manifest.source.repository,
        config=config,
        revision=revision,
        artifact_verifier=verifier,
        torch_dtype=torch_dtype,
    )
    inputs = tokenizer(prompt, return_tensors="pt")["input_ids"]
    victim: ActiveSpan | None = None
    interrupted_at: str | None = None
    recovered_at: str | None = None
    recovery_seconds: float | None = None
    before: tuple[ActiveSpan, ...] = ()
    after: tuple[ActiveSpan, ...] = ()
    distributed = None
    interruption_ack: Mapping[str, Any] | None = None
    replay_position = 0
    final_position = 0
    post_recovery_route: tuple[ActiveSpan, ...] = ()
    post_recovery_output = None
    post_recovery_session_closed = False
    client_dht_stopped = False
    try:
        with torch.inference_mode(), model.inference_session(max_length=inputs.shape[1] + tokens) as session:
            first = model.generate(inputs, max_new_tokens=1, min_new_tokens=1, do_sample=False)
            before = _active_spans(session, topology)
            replay_position = int(session.position)
            if replay_position <= 0:
                raise QualificationError("the session recorded no activation history before interruption")
            victim = sorted(before, key=lambda span: (span.start, span.end, span.peer_id))[0]
            victim_worker = topology.worker_by_peer[victim.peer_id]
            interrupted_at = datetime.now(timezone.utc).isoformat()
            recovery_started = time.monotonic()
            interruption_ack = run_control_command(
                control.interrupt_commands[victim.peer_id],
                action="interrupt",
                topology=topology,
                timeout=control_timeout,
                worker=victim_worker,
                cwd=control.execution_directory,
            )
            remaining = model.generate(
                None,
                max_new_tokens=tokens - 1,
                min_new_tokens=tokens - 1,
                do_sample=False,
            )
            recovery_seconds = time.monotonic() - recovery_started
            recovered_at = datetime.now(timezone.utc).isoformat()
            distributed = torch.cat([first, remaining], dim=1)
            after = _active_spans(session, topology)
            final_position = int(session.position)
            if final_position <= replay_position:
                raise QualificationError("the same inference session did not advance after recovery")
            replacement = validate_replacement(before, after, victim, topology)
        with torch.inference_mode(), model.inference_session(max_length=inputs.shape[1] + 1) as probe_session:
            post_recovery_output = model.generate(
                inputs,
                max_new_tokens=1,
                min_new_tokens=1,
                do_sample=False,
            )
            post_recovery_route = _active_spans(probe_session, topology)
            if any(span.peer_id == victim.peer_id for span in post_recovery_route):
                raise QualificationError("a clean post-recovery request reused the interrupted PeerID")
        post_recovery_session_closed = True
    finally:
        client_dht_stopped = close_distributed_client(model)
    if not client_dht_stopped:
        raise QualificationError("the distributed client DHT did not stop and join")

    for artifact in manifest.artifacts_for_roles({"weight", "weight_index", "converted_weight", "quantized_weight"}):
        verifier.ensure_path(
            artifact.path,
            allowed_roles={"weight", "weight_index", "converted_weight", "quantized_weight"},
        )
    reference_kwargs: dict[str, Any] = {
        "local_files_only": True,
        "cache_dir": cache_dir,
        "dtype": torch_dtype,
    }
    if manifest.runtime.attention_implementation != "auto":
        reference_kwargs["attn_implementation"] = manifest.runtime.attention_implementation
    reference_model = load_reference_model_for_causal_lm(
        verifier.snapshot_root,
        **reference_kwargs,
    ).to(device)
    reference_model.eval()
    with torch.inference_mode():
        reference = reference_model.generate(
            inputs.to(device),
            max_new_tokens=tokens,
            min_new_tokens=tokens,
            do_sample=False,
        ).cpu()
    distributed_cpu = distributed.cpu()
    if distributed_cpu.numel() == 0 or not torch.equal(distributed_cpu, reference):
        raise QualificationError("recovered distributed token IDs do not exactly match the stock model")
    post_recovery_cpu = post_recovery_output.cpu()
    reference_prefix = reference[:, : post_recovery_cpu.shape[1]]
    if post_recovery_cpu.numel() == 0 or not torch.equal(post_recovery_cpu, reference_prefix):
        raise QualificationError("clean post-recovery token IDs do not match the stock model")
    assert victim is not None
    assert interruption_ack is not None
    assert recovery_seconds is not None
    return {
        "exact_topology_coverage": True,
        "replicas_per_block": [len(peers) for peers in observed_coverage],
        "initial_route": [span.to_evidence(topology) for span in before],
        "recovered_route": [span.to_evidence(topology) for span in after],
        "selected_worker_interrupted": True,
        "hard_kill_acknowledged": interruption_ack.get("hard_kill") is True,
        "interrupted_at": interrupted_at,
        "recovered_at": recovered_at,
        "recovery_seconds": round(recovery_seconds, 6),
        "same_inference_session": True,
        "activation_replay_observed": True,
        "replayed_prefix_tokens": replay_position,
        "final_session_position": final_position,
        "replacement": replacement,
        "distributed_output_ids": distributed_cpu.tolist(),
        "reference_output_ids": reference.tolist(),
        "stock_token_parity": True,
        "post_recovery_clean_request": True,
        "post_recovery_route": [span.to_evidence(topology) for span in post_recovery_route],
        "post_recovery_output_ids": post_recovery_cpu.tolist(),
        "post_recovery_session_closed": post_recovery_session_closed,
        "client_dht_stopped": client_dht_stopped,
    }


def validate_gate_evidence(evidence: Mapping[str, Any], topology: Topology) -> None:
    required_true = (
        "exact_topology_coverage",
        "selected_worker_interrupted",
        "hard_kill_acknowledged",
        "same_inference_session",
        "activation_replay_observed",
        "stock_token_parity",
        "post_recovery_clean_request",
        "post_recovery_session_closed",
        "client_dht_stopped",
    )
    for field in required_true:
        if evidence.get(field) is not True:
            raise QualificationError(f"multi-machine evidence field {field} must be true")
    replicas = evidence.get("replicas_per_block")
    if (
        not isinstance(replicas, list)
        or len(replicas) != topology.num_blocks
        or any(isinstance(value, bool) or not isinstance(value, int) or value < 2 for value in replicas)
    ):
        raise QualificationError("multi-machine evidence must record two-machine redundancy for every block")
    distributed = evidence.get("distributed_output_ids")
    reference = evidence.get("reference_output_ids")
    if not isinstance(distributed, list) or not distributed or distributed != reference:
        raise QualificationError("multi-machine evidence must directly retain equal non-empty token ID arrays")
    post_recovery = evidence.get("post_recovery_output_ids")
    if (
        not isinstance(reference, list)
        or not isinstance(post_recovery, list)
        or not post_recovery
        or len(post_recovery) != len(reference)
        or any(
            not isinstance(probe_row, list)
            or not probe_row
            or not isinstance(reference_row, list)
            or len(probe_row) > len(reference_row)
            or probe_row != reference_row[: len(probe_row)]
            for probe_row, reference_row in zip(post_recovery, reference)
        )
    ):
        raise QualificationError("multi-machine evidence must retain a stock-equal clean post-recovery request")
    post_recovery_route = evidence.get("post_recovery_route")
    replacement_record = _require_object(evidence.get("replacement"), "multi-machine evidence.replacement")
    victim_record = _require_object(replacement_record.get("victim"), "multi-machine evidence.replacement.victim")
    victim_peer_id = victim_record.get("peer_id")
    if (
        not isinstance(post_recovery_route, list)
        or not post_recovery_route
        or any(not isinstance(span, dict) or span.get("peer_id") == victim_peer_id for span in post_recovery_route)
    ):
        raise QualificationError("clean post-recovery route is empty, malformed, or reused the interrupted PeerID")
    replayed = evidence.get("replayed_prefix_tokens")
    final_position = evidence.get("final_session_position")
    if (
        isinstance(replayed, bool)
        or not isinstance(replayed, int)
        or replayed <= 0
        or isinstance(final_position, bool)
        or not isinstance(final_position, int)
        or final_position <= replayed
    ):
        raise QualificationError("multi-machine evidence does not prove activation replay and session progress")
    recovery_seconds = evidence.get("recovery_seconds")
    if (
        isinstance(recovery_seconds, bool)
        or not isinstance(recovery_seconds, (int, float))
        or not math.isfinite(recovery_seconds)
        or recovery_seconds <= 0
    ):
        raise QualificationError("multi-machine recovery duration must be finite and positive")
    try:
        interrupted_at = datetime.fromisoformat(str(evidence["interrupted_at"]))
        recovered_at = datetime.fromisoformat(str(evidence["recovered_at"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise QualificationError("multi-machine evidence timestamps must be ISO-8601") from exc
    if interrupted_at.tzinfo is None or recovered_at.tzinfo is None or recovered_at < interrupted_at:
        raise QualificationError("multi-machine evidence timestamps are missing a timezone or out of order")
    replacement = _require_object(evidence.get("replacement"), "multi-machine evidence.replacement")
    if not replacement.get("victim") or not replacement.get("replacement_spans"):
        raise QualificationError("multi-machine evidence does not identify the victim and replacement route")


def sanitize_diagnostic(
    value: str,
    sensitive_paths: Sequence[tuple[str, str]] = (),
) -> str:
    redacted = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}<redacted>", value)
    redacted = _URL_RE.sub("<network-endpoint>", redacted)
    redacted = _MULTIADDR_RE.sub("<network-endpoint>", redacted)
    redacted = _IP_ENDPOINT_RE.sub("<network-endpoint>", redacted)
    redacted = _WINDOWS_PATH_LINE_RE.sub("<private-path>", redacted)
    redacted = _POSIX_PATH_LINE_RE.sub("<private-path>", redacted)
    return redact_host_paths(redacted, sensitive_paths)


def _stage_error(
    exc: BaseException,
    sensitive_paths: Sequence[tuple[str, str]] = (),
) -> str:
    return sanitize_diagnostic(f"{type(exc).__name__}: {exc}", sensitive_paths)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not _SOURCE_COMMIT_RE.fullmatch(args.source_commit):
        parser.error("--source-commit must be exactly 40 lowercase hexadecimal characters")
    if not args.prompt or len(args.prompt) > 4096:
        parser.error("--prompt must contain 1-4096 characters")
    if args.tokens < 2 or args.tokens > 4096:
        parser.error("--tokens must be between 2 and 4096")
    if args.coverage_timeout <= 0 or args.request_timeout <= 0 or args.control_timeout <= 0:
        parser.error("all timeouts must be positive")
    if args.coverage_timeout > 7200 or args.control_timeout > 600:
        parser.error("--coverage-timeout must be <= 7200 and --control-timeout must be <= 600")
    if args.request_timeout > 60 or args.max_retries < 1 or args.max_retries > 5:
        parser.error("--request-timeout must be <= 60 and --max-retries must be between 1 and 5")
    inferred_commit = infer_source_commit()
    if inferred_commit is not None and inferred_commit != args.source_commit:
        parser.error("--source-commit does not match the current checkout")

    manifest_path = _absolute(args.manifest)
    matrix_path = _absolute(args.matrix_report)
    topology_path = _absolute(args.topology)
    control_path = _absolute(args.control_plan)
    artifact_root = _absolute(args.artifact_root)
    cache_dir = _absolute(args.cache_dir) if args.cache_dir is not None else None
    sensitive_paths = [
        (str(manifest_path), "<manifest>"),
        (str(matrix_path), "<matrix-report>"),
        (str(topology_path), "<topology>"),
        (str(control_path), "<control-plan>"),
        (str(artifact_root), "<artifact-root>"),
    ]
    if cache_dir is not None:
        sensitive_paths.append((str(cache_dir), "<cache-dir>"))

    try:
        manifest = ModelManifest.load(manifest_path)
        manifest.validate_runtime(drift.__version__)
        topology = load_topology(topology_path, manifest)
        cleanup_command = load_cleanup_command(control_path, topology)
    except (ManifestError, OSError, QualificationError) as exc:
        parser.error(str(exc))

    report: dict[str, Any] = {
        "schema_version": MULTI_MACHINE_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "controlled-multi-machine",
        "qualification_mode": (
            "incomplete-windows-linux-recovery" if args.allow_incomplete_matrix else "strict-six-profile-recovery"
        ),
        "missing_profiles": list(INCOMPLETE_MISSING_PROFILES) if args.allow_incomplete_matrix else [],
        "run_id": topology.run_id,
        "model": {
            "name": manifest.name,
            "repository": manifest.source.repository,
            "revision": manifest.source.revision,
            "manifest_digest": manifest.digest_id,
            "runtime": manifest.runtime.to_dict(),
        },
        "source_identity": {"source_commit": args.source_commit, "drift": drift.__version__},
        "requested": {
            "artifact_verification": True,
            "split_routes": True,
            "independent_machine_redundancy": True,
            "selected_worker_hard_interruption": True,
            "exact_stock_token_parity": True,
            "allow_incomplete_matrix": args.allow_incomplete_matrix,
            "device": args.device,
            "tokens": args.tokens,
            "request_timeout_seconds": args.request_timeout,
            "max_retries": args.max_retries,
        },
        "topology": topology.to_evidence(),
        "stages": [],
        "not_covered": [
            "cross-platform multi-machine execution",
            "cold-client resource envelope",
            "public-worker route redundancy and soak",
            "signed catalog publication and release bootstrap",
        ],
    }

    try:
        control = load_control_plan(control_path, topology)
        matrix_evidence = validate_matrix_report(
            matrix_path,
            manifest,
            source_commit=args.source_commit,
            allow_incomplete=args.allow_incomplete_matrix,
        )
        manifest.verify_artifacts(artifact_root)
        report["stages"].append(
            {
                "name": "local_matrix_and_artifacts",
                "status": "passed",
                "evidence": {
                    "matrix_result": matrix_evidence["result"],
                    "missing_profiles": matrix_evidence["missing_profiles"],
                    "source_identity": matrix_evidence["source_identity"],
                    "artifacts_verified": True,
                    "declared_artifact_bytes": sum(artifact.size for artifact in manifest.artifacts),
                },
            }
        )
        report["stages"].append(
            {
                "name": "controlled_topology",
                "status": "passed",
                "evidence": {
                    "independent_routes": 2,
                    "worker_machines": len(topology.workers),
                    "resources_to_cleanup": len(topology.expected_resources),
                },
            }
        )
        gate_started = time.perf_counter()
        gate_evidence = execute_gate(
            manifest,
            topology,
            control,
            artifact_root=artifact_root,
            cache_dir=cache_dir,
            device=args.device,
            prompt=args.prompt,
            tokens=args.tokens,
            coverage_timeout=args.coverage_timeout,
            request_timeout=args.request_timeout,
            control_timeout=args.control_timeout,
            max_retries=args.max_retries,
        )
        validate_gate_evidence(gate_evidence, topology)
        report["stages"].append(
            {
                "name": "multi_machine_in_generation_recovery",
                "status": "passed",
                "duration_seconds": round(time.perf_counter() - gate_started, 6),
                "evidence": gate_evidence,
            }
        )
    except Exception as exc:
        report["stages"].append(
            {
                "name": "multi_machine_in_generation_recovery",
                "status": "failed",
                "evidence": {"error": _stage_error(exc, sensitive_paths)},
            }
        )
    finally:
        cleanup_started = time.perf_counter()
        try:
            cleanup_ack = run_control_command(
                cleanup_command,
                action="cleanup",
                topology=topology,
                timeout=args.control_timeout,
                cwd=control_path.parent,
            )
            cleanup_stage = {
                "name": "provisioned_resource_cleanup",
                "status": "passed",
                "duration_seconds": round(time.perf_counter() - cleanup_started, 6),
                "evidence": {
                    "cleaned": True,
                    "destroyed_resource_count": len(cleanup_ack["destroyed_resources"]),
                    "remaining_resource_count": 0,
                },
            }
        except Exception as exc:
            cleanup_stage = {
                "name": "provisioned_resource_cleanup",
                "status": "failed",
                "duration_seconds": round(time.perf_counter() - cleanup_started, 6),
                "evidence": {"error": _stage_error(exc, sensitive_paths)},
            }
        report["stages"].append(cleanup_stage)

    stages_passed = all(stage["status"] == "passed" for stage in report["stages"])
    if stages_passed:
        report["result"] = "incomplete" if args.allow_incomplete_matrix else "passed"
    else:
        report["result"] = "failed"
    report["complete_release_qualification"] = False
    _write_report(args.output, report)
    return 0 if report["result"] in {"passed", "incomplete"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
