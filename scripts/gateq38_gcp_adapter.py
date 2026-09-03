"""Source-bound GCP adapter for the durable Qwen3.8 complete-route controller.

The adapter compiles exact private start specifications, observes run-scoped resources,
and performs retry-safe cleanup. Paid start and route collection remain fail-closed
until the protected Qwen3.8 host runtime and status transport are source-bound.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from scripts import gateq38_linux_host_transport as transport
from scripts import gateq38_route_controller as controller

MAX_OUTPUT_BYTES = 1_048_576
MAX_JSON_BYTES = controller.MAX_JSON_BYTES
SCOPE_LABEL = "q38-complete-route"
ROUTE_TAG_PREFIX = "communityai-q38-"
ROUTE_TCP_RULE = "tcp:31330-31339"
IAP_TCP_RULE = "tcp:22"
IAP_SOURCE_RANGE = "35.235.240.0/20"
GUEST_ATTRIBUTE_NAMESPACE = "communityai-q38"
GUEST_ATTRIBUTE_KEY = "status-v1"
GUEST_ATTRIBUTE_QUERY_PATH = f"{GUEST_ATTRIBUTE_NAMESPACE}/{GUEST_ATTRIBUTE_KEY}"
MAX_GUEST_ATTRIBUTE_OUTPUT_BYTES = transport.MAX_ENVELOPE_BYTES * 2 + 4_096
RUNTIME_ACTIONS_BLOCKED = "source-bound Qwen3.8 host runtime is not plan-bound"


class Q38GcpAdapterError(RuntimeError):
    """A provider observation or exact action failed closed."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


Runner = Callable[[Sequence[str], int], CommandResult]
StatusKeyResolver = Callable[[str, str], bytes]
StatusCheckpointResolver = Callable[[str, str], tuple[str | None, int]]


def _default_runner(argv: Sequence[str], timeout: int) -> CommandResult:
    if not argv or any(not isinstance(item, str) or not item for item in argv):
        raise Q38GcpAdapterError("provider command is invalid")
    try:
        result = subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            timeout=timeout,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise Q38GcpAdapterError("provider command failed") from exc
    if len(result.stdout) > MAX_OUTPUT_BYTES or len(result.stderr) > MAX_OUTPUT_BYTES:
        raise Q38GcpAdapterError("provider command output exceeded its bound")
    return CommandResult(result.returncode, result.stdout, result.stderr)


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Q38GcpAdapterError("duplicate provider JSON field")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise Q38GcpAdapterError("non-finite provider JSON value")


def _json_bytes(payload: bytes, label: str, *, maximum: int = MAX_OUTPUT_BYTES) -> Any:
    if not 1 <= len(payload) <= maximum:
        raise Q38GcpAdapterError(f"{label} output is invalid")
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Q38GcpAdapterError(f"{label} returned invalid JSON") from exc


def _basename(value: Any) -> str:
    return value.rsplit("/", 1)[-1] if isinstance(value, str) else ""


def _route_tag(plan: controller.RoutePlan) -> str:
    identity = f"{plan.run_id}\0{plan.plan_digest}".encode("utf-8")
    return ROUTE_TAG_PREFIX + hashlib.sha256(identity).hexdigest()[:20]


def _labels(plan: controller.RoutePlan) -> dict[str, str]:
    return {
        "communityai-run": plan.run_id,
        "communityai-scope": SCOPE_LABEL,
        "communityai-source": plan.source_commit,
    }


def _label_argument(value: Mapping[str, str]) -> str:
    return ",".join(f"{key}={value[key]}" for key in sorted(value))


def _resource_binding(
    plan: controller.RoutePlan,
    resource: controller.ResourcePlan,
) -> dict[str, Any]:
    return {
        "schema_version": controller.SCHEMA_VERSION,
        "run_id": plan.run_id,
        "source_commit": plan.source_commit,
        "deadline_unix": plan.deadline_unix,
        "plan_digest": plan.plan_digest,
        "execution_inventory_digest": plan.execution_inventory_digest,
        "start_action_id": controller._action_id(plan, "start_route"),
        "resource_name": resource.name,
        "kind": resource.kind,
        "worker_id": resource.worker_id,
    }


def _description(plan: controller.RoutePlan, resource: controller.ResourcePlan) -> str:
    return json.dumps(
        _resource_binding(plan, resource),
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _metadata(value: Mapping[str, Any]) -> dict[str, str]:
    raw = value.get("metadata")
    items = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        raise Q38GcpAdapterError("instance metadata is invalid")
    result: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict) or set(item) != {"key", "value"}:
            raise Q38GcpAdapterError("instance metadata item is invalid")
        key, field = item["key"], item["value"]
        if not isinstance(key, str) or not isinstance(field, str) or key in result:
            raise Q38GcpAdapterError("instance metadata item is ambiguous")
        result[key] = field
    return result


def _instance_metadata(
    plan: controller.RoutePlan,
    resource: controller.ResourcePlan,
) -> dict[str, str]:
    result = {key.replace("_", "-"): str(value) for key, value in _resource_binding(plan, resource).items()}
    result["worker-id"] = resource.worker_id or "none"
    result["worker-plan-digest"] = plan.worker_plan_digest
    result["manifest-digest"] = plan.manifest_digest
    if resource.worker_id is not None:
        worker = plan.worker_by_id[resource.worker_id]
        result.update(
            {
                "machine-id": worker.machine_id,
                "span": worker.span,
                "artifact-bytes": str(worker.artifact_bytes),
                "artifact-set-digest": worker.artifact_set_digest,
                "cache-root": worker.cache_root,
            }
        )
    return result


def _metadata_argument(value: Mapping[str, str]) -> str:
    if any("," in item or "=" in item for item in value.values()):
        raise Q38GcpAdapterError("instance metadata cannot be represented safely")
    return ",".join(f"{key}={value[key]}" for key in sorted(value))


def _assert_source_bound(plan: controller.RoutePlan, source_root: Path) -> None:
    root = source_root.resolve()
    expected = {
        controller.GCP_ADAPTER_SOURCE_PATH: Path(__file__).resolve(),
        "scripts/gateq38_route_controller.py": Path(controller.__file__).resolve(),
    }
    bindings = {item["relative_path"]: item for item in plan.source_bindings}
    for relative, imported in expected.items():
        candidate = (root / Path(*PurePosixPath(relative).parts)).resolve()
        binding = bindings.get(relative)
        if binding is None or imported != candidate:
            raise Q38GcpAdapterError("imported provider adapter sources are not source-bound")
        payload = controller._regular_bytes(candidate)
        if len(payload) != binding["byte_size"] or "sha256:" + hashlib.sha256(payload).hexdigest() != binding["sha256"]:
            raise Q38GcpAdapterError("provider adapter source binding changed")


def _absent_worker() -> dict[str, Any]:
    return {field: ("absent" if field == "state" else None) for field in controller._OBS_WORKER_FIELDS}


def _starting_worker(plan: controller.RoutePlan, worker: controller.WorkerPlan) -> dict[str, Any]:
    return {
        "state": "starting",
        "machine_id": worker.machine_id,
        "peer_id": None,
        "source_commit": plan.source_commit,
        "plan_digest": plan.plan_digest,
        "worker_plan_digest": plan.worker_plan_digest,
        "start_action_id": controller._action_id(plan, "start_route"),
        "span": worker.span,
        "manifest_digest": plan.manifest_digest,
        "artifact_bytes": worker.artifact_bytes,
        "artifact_set_digest": worker.artifact_set_digest,
        "cache_root": worker.cache_root,
    }


def _absent_job() -> dict[str, Any]:
    return {field: ("absent" if field == "state" else None) for field in controller._ROUTE_JOB_FIELDS}


def blank_host_status(plan: controller.RoutePlan) -> dict[str, Any]:
    return {
        "schema_version": controller.SCHEMA_VERSION,
        "run_id": plan.run_id,
        "workers": {worker.worker_id: _absent_worker() for worker in plan.workers},
        "route_job": _absent_job(),
    }


def validate_host_status(value: Mapping[str, Any], plan: controller.RoutePlan) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema_version", "run_id", "workers", "route_job"}:
        raise Q38GcpAdapterError("host status schema is invalid")
    if value["schema_version"] != controller.SCHEMA_VERSION or value["run_id"] != plan.run_id:
        raise Q38GcpAdapterError("host status identity is invalid")
    workers = value["workers"]
    if not isinstance(workers, dict) or set(workers) != set(plan.worker_by_id):
        raise Q38GcpAdapterError("host worker inventory is not exact")
    for worker_id, worker in workers.items():
        if not isinstance(worker, dict) or set(worker) != controller._OBS_WORKER_FIELDS:
            raise Q38GcpAdapterError(f"host worker status is invalid: {worker_id}")
    route_job = value["route_job"]
    if not isinstance(route_job, dict) or set(route_job) != controller._ROUTE_JOB_FIELDS:
        raise Q38GcpAdapterError("host route status is invalid")
    return dict(value)


def load_host_status(path: Path | None, plan: controller.RoutePlan, source_root: Path) -> dict[str, Any]:
    if path is None or not path.exists():
        return blank_host_status(plan)
    controller._assert_protected_path(path, plan, source_root, directory=False)
    return validate_host_status(
        controller._strict_json(controller._regular_bytes(path)),
        plan,
    )


class GcpAdapter:
    def __init__(
        self,
        plan: controller.RoutePlan,
        source_root: Path,
        *,
        runner: Runner = _default_runner,
        clock: Callable[[], float] = time.time,
        status_key_resolver: StatusKeyResolver | None = None,
        status_checkpoint_resolver: StatusCheckpointResolver | None = None,
    ) -> None:
        if (status_key_resolver is None) != (status_checkpoint_resolver is None):
            raise Q38GcpAdapterError(
                "authenticated host status requires key and checkpoint resolvers"
            )
        self.plan = plan
        self.source_root = source_root.resolve()
        self.runner = runner
        self.clock = clock
        self.status_key_resolver = status_key_resolver
        self.status_checkpoint_resolver = status_checkpoint_resolver
        _assert_source_bound(plan, self.source_root)

    def _gcloud(
        self,
        *arguments: str,
        timeout: int = 300,
        check: bool = True,
    ) -> CommandResult:
        result = self.runner(("gcloud", *arguments, "--quiet"), timeout)
        if check and result.returncode != 0:
            raise Q38GcpAdapterError("gcloud action failed")
        return result

    def _gcloud_json(self, *arguments: str, timeout: int = 300) -> Any:
        result = self._gcloud(*arguments, "--format=json", timeout=timeout)
        return _json_bytes(result.stdout, "gcloud")

    def _check_auth(self) -> None:
        result = self._gcloud(
            "auth",
            "list",
            "--filter=status:ACTIVE",
            "--format=value(account)",
            timeout=60,
        )
        accounts = [line for line in result.stdout.decode("utf-8", "strict").splitlines() if line.strip()]
        if len(accounts) != 1:
            raise Q38GcpAdapterError("exactly one active native gcloud account is required")
        project = self._gcloud_json("projects", "describe", controller.EXPECTED_PROJECT, timeout=60)
        if not isinstance(project, dict) or project.get("lifecycleState") != "ACTIVE":
            raise Q38GcpAdapterError("authorized GCP project is unavailable")

    def _describe(self, resource: controller.ResourcePlan) -> Mapping[str, Any] | None:
        if resource.kind.endswith("firewall"):
            arguments = (
                "compute",
                "firewall-rules",
                "describe",
                resource.name,
                f"--project={controller.EXPECTED_PROJECT}",
            )
        else:
            kind = "instances" if resource.kind.endswith("instance") else "disks"
            arguments = (
                "compute",
                kind,
                "describe",
                resource.name,
                f"--project={controller.EXPECTED_PROJECT}",
                f"--zone={controller.EXPECTED_ZONE}",
            )
        result = self._gcloud(*arguments, "--format=json", timeout=60, check=False)
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", "replace").casefold()
            if "not found" in message or "was not found" in message:
                return None
            raise Q38GcpAdapterError("provider inventory failed")
        value = _json_bytes(result.stdout, "resource inventory")
        if not isinstance(value, dict):
            raise Q38GcpAdapterError("provider inventory item is invalid")
        return value

    def _validate_description(
        self,
        resource: controller.ResourcePlan,
        value: Mapping[str, Any],
    ) -> None:
        description = value.get("description")
        if not isinstance(description, str) or len(description.encode("utf-8")) > 4_096:
            raise Q38GcpAdapterError("planned resource binding is invalid")
        parsed = _json_bytes(description.encode("utf-8"), "resource description", maximum=4_096)
        if parsed != _resource_binding(self.plan, resource):
            raise Q38GcpAdapterError("planned resource binding changed")

    def _validate_disk(
        self,
        resource: controller.ResourcePlan,
        value: Mapping[str, Any],
        *,
        cleanup: bool = False,
    ) -> None:
        if value.get("name") != resource.name:
            raise Q38GcpAdapterError("planned disk identity is invalid")
        if not cleanup and value.get("status") not in {"READY", "CREATING"}:
            raise Q38GcpAdapterError("planned disk state is invalid")
        if value.get("labels") != _labels(self.plan):
            raise Q38GcpAdapterError("planned disk ownership is invalid")
        source_image = value.get("sourceImage")
        api_prefix = "https://www.googleapis.com/compute/v1/"
        if isinstance(source_image, str) and source_image.startswith(api_prefix):
            source_image = source_image.removeprefix(api_prefix)
        if (
            _basename(value.get("type")) != controller.EXPECTED_DISK_TYPE
            or str(value.get("sizeGb")) != str(controller.EXPECTED_DISK_SIZE_GB)
            or source_image
            != f"projects/{controller.EXPECTED_SOURCE_IMAGE.split('/', 1)[0]}/global/images/{controller.EXPECTED_SOURCE_IMAGE.split('/', 1)[1]}"
        ):
            raise Q38GcpAdapterError("planned disk shape is invalid")
        self._validate_description(resource, value)

    def _disk_for_instance(self, resource: controller.ResourcePlan) -> controller.ResourcePlan:
        kind = "worker_disk" if resource.worker_id is not None else "bootstrap_disk"
        matches = [item for item in self.plan.resources if item.kind == kind and item.worker_id == resource.worker_id]
        if len(matches) != 1:
            raise Q38GcpAdapterError("instance disk plan is ambiguous")
        return matches[0]

    def _validate_instance(
        self,
        resource: controller.ResourcePlan,
        value: Mapping[str, Any],
        *,
        cleanup: bool = False,
    ) -> None:
        if value.get("name") != resource.name:
            raise Q38GcpAdapterError("planned instance identity is invalid")
        try:
            controller.instance_generation_digest(
                resource.name,
                value.get("id"),
                value.get("creationTimestamp"),
            )
        except controller.RouteControllerError as exc:
            raise Q38GcpAdapterError("planned instance generation is invalid") from exc
        if not cleanup and value.get("status") not in {
            "PROVISIONING",
            "STAGING",
            "RUNNING",
            "STOPPING",
        }:
            raise Q38GcpAdapterError("planned instance state is invalid")
        if value.get("labels") != _labels(self.plan):
            raise Q38GcpAdapterError("planned instance ownership is invalid")
        spec = controller._expected_resource_spec(resource)
        disks = value.get("disks")
        interfaces = value.get("networkInterfaces")
        accelerators = value.get("guestAccelerators", [])
        tags = value.get("tags")
        scheduling = value.get("scheduling")
        expected_disk = self._disk_for_instance(resource)
        if "serviceAccounts" in value and value["serviceAccounts"] not in (None, []):
            raise Q38GcpAdapterError("planned instance has a service account")
        if (
            _basename(value.get("machineType")) != spec["machine_type"]
            or not isinstance(disks, list)
            or len(disks) != 1
            or _basename(disks[0].get("source") if isinstance(disks[0], dict) else None) != expected_disk.name
            or disks[0].get("boot") is not True
            or disks[0].get("autoDelete") is not True
            or not isinstance(interfaces, list)
            or len(interfaces) != 1
            or _basename(interfaces[0].get("network") if isinstance(interfaces[0], dict) else None)
            != controller.EXPECTED_NETWORK
            or _basename(interfaces[0].get("subnetwork") if isinstance(interfaces[0], dict) else None)
            != controller.EXPECTED_SUBNET
            or bool(interfaces[0].get("accessConfigs"))
            or bool(interfaces[0].get("ipv6AccessConfigs"))
            or interfaces[0].get("externalIpv6") not in (None, "")
            or interfaces[0].get("stackType") != "IPV4_ONLY"
            or not isinstance(tags, dict)
            or tags.get("items") != [_route_tag(self.plan)]
            or value.get("canIpForward") not in (None, False)
            or value.get("deletionProtection") not in (None, False)
            or not isinstance(scheduling, dict)
            or scheduling.get("automaticRestart") is not True
            or scheduling.get("provisioningModel") != "STANDARD"
            or scheduling.get("onHostMaintenance") != "TERMINATE"
            or scheduling.get("instanceTerminationAction") != "DELETE"
            or scheduling.get("maxRunDuration")
            != {"seconds": str(controller.EXPECTED_MAX_LIFETIME_SECONDS), "nanos": 0}
            or _metadata(value) != _instance_metadata(self.plan, resource)
        ):
            raise Q38GcpAdapterError("planned instance shape is invalid")
        if resource.kind == "worker_instance":
            if (
                not isinstance(accelerators, list)
                or len(accelerators) != 1
                or _basename(accelerators[0].get("acceleratorType")) != controller.EXPECTED_ACCELERATOR_TYPE
                or accelerators[0].get("acceleratorCount") != 1
            ):
                raise Q38GcpAdapterError("planned worker accelerator is invalid")
        elif accelerators not in (None, []):
            raise Q38GcpAdapterError("bootstrap instance has an accelerator")

    def _validate_firewall(
        self,
        resource: controller.ResourcePlan,
        value: Mapping[str, Any],
    ) -> None:
        if resource.kind != "firewall" or value.get("name") != resource.name:
            raise Q38GcpAdapterError("planned firewall identity is invalid")
        allowed = value.get("allowed")
        if (
            value.get("direction") != "INGRESS"
            or _basename(value.get("network")) != controller.EXPECTED_NETWORK
            or value.get("sourceTags") != [_route_tag(self.plan)]
            or value.get("targetTags") != [_route_tag(self.plan)]
            or value.get("sourceRanges") not in (None, [])
            or allowed != [{"IPProtocol": "tcp", "ports": ["31330-31339"]}]
            or value.get("disabled") not in (None, False)
        ):
            raise Q38GcpAdapterError("planned firewall policy is invalid")
        self._validate_description(resource, value)

    def _validate_iap_firewall(
        self,
        resource: controller.ResourcePlan,
        value: Mapping[str, Any],
    ) -> None:
        if resource.kind != "iap_firewall" or value.get("name") != resource.name:
            raise Q38GcpAdapterError("planned IAP firewall identity is invalid")
        allowed = value.get("allowed")
        if (
            value.get("direction") != "INGRESS"
            or _basename(value.get("network")) != controller.EXPECTED_NETWORK
            or value.get("sourceTags") not in (None, [])
            or value.get("targetTags") != [_route_tag(self.plan)]
            or value.get("sourceRanges") != [IAP_SOURCE_RANGE]
            or allowed != [{"IPProtocol": "tcp", "ports": ["22"]}]
            or value.get("disabled") not in (None, False)
        ):
            raise Q38GcpAdapterError("planned IAP firewall policy is invalid")
        self._validate_description(resource, value)

    def _validate_resource(
        self,
        resource: controller.ResourcePlan,
        value: Mapping[str, Any],
        *,
        cleanup: bool = False,
    ) -> None:
        if resource.kind.endswith("disk"):
            self._validate_disk(resource, value, cleanup=cleanup)
        elif resource.kind.endswith("instance"):
            self._validate_instance(resource, value, cleanup=cleanup)
        elif resource.kind == "firewall":
            self._validate_firewall(resource, value)
        elif resource.kind == "iap_firewall":
            self._validate_iap_firewall(resource, value)
        else:
            raise Q38GcpAdapterError("planned resource kind is invalid")

    def _listed_names(self, kind: str) -> set[str]:
        command_kind = "firewall-rules" if kind == "firewall" else kind
        arguments = (
            "compute",
            command_kind,
            "list",
            f"--project={controller.EXPECTED_PROJECT}",
            f"--filter=name~'^{self.plan.run_id}-'",
        )
        value = self._gcloud_json(*arguments, timeout=60)
        if not isinstance(value, list):
            raise Q38GcpAdapterError("run-scoped provider inventory is invalid")
        result: set[str] = set()
        for item in value:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise Q38GcpAdapterError("run-scoped provider inventory item is invalid")
            if item["name"] in result:
                raise Q38GcpAdapterError("run-scoped provider inventory is ambiguous")
            result.add(item["name"])
        return result

    def _protected_bootstrap_running(self) -> bool:
        result = self._gcloud(
            "compute",
            "instances",
            "describe",
            controller.PROTECTED_INSTANCE,
            f"--project={controller.EXPECTED_PROJECT}",
            f"--zone={controller.EXPECTED_ZONE}",
            "--format=json",
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            return False
        value = _json_bytes(result.stdout, "protected bootstrap inventory")
        return (
            isinstance(value, dict)
            and value.get("name") == controller.PROTECTED_INSTANCE
            and value.get("status") == "RUNNING"
        )

    def _provider_inventory(
        self,
        *,
        cleanup: bool = False,
    ) -> tuple[dict[str, Mapping[str, Any] | None], bool]:
        self._check_auth()
        values: dict[str, Mapping[str, Any] | None] = {}
        for resource in self.plan.resources:
            value = self._describe(resource)
            if value is not None:
                self._validate_resource(resource, value, cleanup=cleanup)
            values[resource.name] = value
        expected_by_kind = {
            "instances": {
                item.name
                for item in self.plan.resources
                if item.kind.endswith("instance") and values[item.name] is not None
            },
            "disks": {
                item.name
                for item in self.plan.resources
                if item.kind.endswith("disk") and values[item.name] is not None
            },
            "firewall": {
                item.name
                for item in self.plan.resources
                if item.kind.endswith("firewall") and values[item.name] is not None
            },
        }
        for kind, expected in expected_by_kind.items():
            if self._listed_names(kind) != expected:
                raise Q38GcpAdapterError("run-scoped provider inventory is not exact")
        return values, self._protected_bootstrap_running()

    def _resource_observations(
        self,
        provider: Mapping[str, Mapping[str, Any] | None],
    ) -> dict[str, dict[str, Any]]:
        resources: dict[str, dict[str, Any]] = {}
        for resource in self.plan.resources:
            value = provider[resource.name]
            present = value is not None
            instance_id: str | None = None
            creation_timestamp: str | None = None
            instance_generation_digest: str | None = None
            if present and resource.kind.endswith("instance"):
                instance_id = value["id"]
                creation_timestamp = value["creationTimestamp"]
                instance_generation_digest = controller.instance_generation_digest(
                    resource.name,
                    instance_id,
                    creation_timestamp,
                )
            resources[resource.name] = {
                "present": present,
                "kind": resource.kind,
                "provider": resource.provider,
                "region": resource.region,
                "run_id": self.plan.run_id if present else None,
                "source_commit": self.plan.source_commit if present else None,
                "deadline_unix": self.plan.deadline_unix if present else None,
                "plan_digest": self.plan.plan_digest if present else None,
                "start_action_id": (
                    controller._action_id(self.plan, "start_route") if present else None
                ),
                "worker_id": resource.worker_id if present else None,
                "instance_id": instance_id,
                "creation_timestamp": creation_timestamp,
                "instance_generation_digest": instance_generation_digest,
            }
        return resources

    def _guest_attribute(self, resource: controller.ResourcePlan) -> bytes | None:
        if not resource.kind.endswith("instance"):
            raise Q38GcpAdapterError("guest attribute target is not an instance")
        result = self._gcloud(
            "compute",
            "instances",
            "get-guest-attributes",
            resource.name,
            f"--project={controller.EXPECTED_PROJECT}",
            f"--zone={controller.EXPECTED_ZONE}",
            f"--query-path={GUEST_ATTRIBUTE_QUERY_PATH}",
            "--format=json",
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            raise Q38GcpAdapterError("guest attribute read failed")
        value = _json_bytes(
            result.stdout,
            "guest attribute",
            maximum=MAX_GUEST_ATTRIBUTE_OUTPUT_BYTES,
        )
        allowed_fields = {"kind", "queryPath", "queryValue", "selfLink"}
        if (
            not isinstance(value, dict)
            or not set(value).issubset(allowed_fields)
            or value.get("queryPath") != GUEST_ATTRIBUTE_QUERY_PATH
            or value.get("kind", "compute#guestAttributes")
            != "compute#guestAttributes"
        ):
            raise Q38GcpAdapterError("guest attribute response is invalid")
        query_value = value.get("queryValue")
        if not isinstance(query_value, dict) or set(query_value) != {"items"}:
            raise Q38GcpAdapterError("guest attribute response is invalid")
        items = query_value["items"]
        if not isinstance(items, list) or len(items) > 1:
            raise Q38GcpAdapterError("guest attribute response is ambiguous")
        if not items:
            return None
        item = items[0]
        if (
            not isinstance(item, dict)
            or set(item) != {"namespace", "key", "value"}
            or item["namespace"] != GUEST_ATTRIBUTE_NAMESPACE
            or item["key"] != GUEST_ATTRIBUTE_KEY
            or not isinstance(item["value"], str)
        ):
            raise Q38GcpAdapterError("guest attribute item is invalid")
        try:
            payload = item["value"].encode("ascii")
        except UnicodeEncodeError as exc:
            raise Q38GcpAdapterError("guest attribute value is invalid") from exc
        if not 1 <= len(payload) <= transport.MAX_ENVELOPE_BYTES:
            raise Q38GcpAdapterError("guest attribute value exceeded its size bound")
        return payload

    def _authenticated_host_status(
        self,
        pre_provider: Mapping[str, Mapping[str, Any] | None],
        pre_protected: bool,
        *,
        now_unix: int,
    ) -> tuple[
        dict[str, Any],
        dict[str, Mapping[str, Any] | None],
        bool,
    ]:
        key_resolver = self.status_key_resolver
        checkpoint_resolver = self.status_checkpoint_resolver
        if key_resolver is None or checkpoint_resolver is None:
            raise Q38GcpAdapterError("authenticated host status is not configured")
        host = blank_host_status(self.plan)
        pre_resources = self._resource_observations(pre_provider)
        pre_digest = controller.observation_instance_generations_digest(
            pre_resources,
            self.plan,
        )
        for resource in self.plan.resources:
            if not resource.kind.endswith("instance"):
                continue
            observed = pre_resources[resource.name]
            generation = observed["instance_generation_digest"]
            if generation is None:
                continue
            payload = self._guest_attribute(resource)
            if payload is None:
                continue
            try:
                key = key_resolver(resource.name, generation)
                checkpoint = checkpoint_resolver(resource.name, generation)
            except Exception as exc:
                raise Q38GcpAdapterError(
                    "protected host status material is unavailable"
                ) from exc
            if (
                not isinstance(checkpoint, tuple)
                or len(checkpoint) != 2
                or checkpoint[0] is not None
                and not isinstance(checkpoint[0], str)
                or not isinstance(checkpoint[1], int)
                or isinstance(checkpoint[1], bool)
                or checkpoint[1] < 0
                or checkpoint[1] > 0
                and checkpoint[0] is None
            ):
                raise Q38GcpAdapterError("protected host status checkpoint is invalid")
            try:
                envelope = transport.validate_status_envelope(
                    transport.decode_status_envelope(payload),
                    self.plan,
                    key=key,
                    now_unix=now_unix,
                    expected_resource_name=resource.name,
                    expected_generation_digest=generation,
                    expected_boot_id=checkpoint[0],
                    minimum_revision=checkpoint[1],
                )
            except transport.Q38LinuxHostTransportError as exc:
                raise Q38GcpAdapterError(
                    "authenticated guest attribute is invalid"
                ) from exc
            context = envelope["context"]
            if (
                context["instance_id"] != observed["instance_id"]
                or context["creation_timestamp"] != observed["creation_timestamp"]
            ):
                raise Q38GcpAdapterError(
                    "authenticated guest attribute generation changed"
                )
            if resource.kind == "worker_instance":
                host["workers"][resource.worker_id] = envelope["payload"]
            else:
                host["route_job"] = envelope["payload"]
        post_provider, post_protected = self._provider_inventory()
        post_resources = self._resource_observations(post_provider)
        post_digest = controller.observation_instance_generations_digest(
            post_resources,
            self.plan,
        )
        if (
            pre_digest != post_digest
            or not pre_protected
            or not post_protected
            or pre_protected != post_protected
        ):
            raise Q38GcpAdapterError(
                "provider generation changed during authenticated host-status read"
            )
        return validate_host_status(host, self.plan), post_provider, post_protected

    def inventory(
        self,
        host_status: Mapping[str, Any],
        *,
        manifest_path: Path | None,
        artifact_root: Path | None,
        evidence_root: Path | None = None,
        cleanup_only: bool = False,
    ) -> dict[str, Any]:
        if cleanup_only:
            host = blank_host_status(self.plan)
        else:
            host = validate_host_status(host_status, self.plan)
            if host != blank_host_status(self.plan):
                raise Q38GcpAdapterError("protected Qwen3.8 host status transport is not plan-bound")
        provider, protected = self._provider_inventory()
        now = int(self.clock())
        if not cleanup_only and self.status_key_resolver is not None:
            host, provider, protected = self._authenticated_host_status(
                provider,
                protected,
                now_unix=now,
            )
        if cleanup_only:
            revalidation: Mapping[str, Any] | None = None
        else:
            if manifest_path is None or artifact_root is None:
                raise Q38GcpAdapterError("production artifact inputs are required")
            revalidation = controller.revalidate_production_artifact_plan(
                self.plan,
                manifest_path,
                artifact_root,
                self.source_root,
                verified_at_unix=now,
            )
        resources = self._resource_observations(provider)
        workers: dict[str, Any] = {}
        for worker in self.plan.workers:
            instance_value = provider[worker.instance]
            disk_value = provider[worker.disk]
            instance_present = instance_value is not None
            supplied = host["workers"][worker.worker_id]
            if not instance_present:
                if supplied["state"] != "absent":
                    raise Q38GcpAdapterError("host status survived an absent worker instance")
                workers[worker.worker_id] = _absent_worker()
            elif supplied["state"] == "absent":
                inferred = _starting_worker(self.plan, worker)
                if instance_value.get("status") == "STOPPING":
                    inferred["state"] = "failed"
                workers[worker.worker_id] = inferred
            else:
                if supplied["state"] == "ready" and (
                    instance_value.get("status") != "RUNNING"
                    or disk_value is None
                    or disk_value.get("status") != "READY"
                ):
                    raise Q38GcpAdapterError("ready host status lacks ready provider resources")
                workers[worker.worker_id] = dict(supplied)
        bootstrap = next(item for item in self.plan.resources if item.kind == "bootstrap_instance")
        bootstrap_disk = next(item for item in self.plan.resources if item.kind == "bootstrap_disk")
        route_job = dict(host["route_job"])
        if provider[bootstrap.name] is None:
            if route_job["state"] != "absent":
                raise Q38GcpAdapterError("route job survived an absent bootstrap instance")
            route_job = _absent_job()
        elif route_job["state"] != "absent" and (
            provider[bootstrap.name].get("status") != "RUNNING"
            or provider[bootstrap_disk.name] is None
            or provider[bootstrap_disk.name].get("status") != "READY"
        ):
            raise Q38GcpAdapterError("route job lacks ready provider resources")
        observation = {
            "schema_version": controller.SCHEMA_VERSION,
            "run_id": self.plan.run_id,
            "observed_at_unix": now,
            "protected_bootstrap_running": protected,
            "artifact_plan_revalidation": revalidation,
            "instance_generations_digest": controller.observation_instance_generations_digest(
                resources,
                self.plan,
            ),
            "resources": resources,
            "workers": workers,
            "route_job": route_job,
        }
        validated = controller.validate_observation(
            observation,
            self.plan,
            cleanup_only=cleanup_only,
        )
        if route_job["state"] == "passed":
            if evidence_root is None:
                raise Q38GcpAdapterError("protected route evidence is required")
            controller.revalidate_route_evidence(
                self.plan,
                validated,
                evidence_root,
                self.source_root,
            )
        return validated

    def _disk_create_arguments(self, resource: controller.ResourcePlan) -> tuple[str, ...]:
        image_project, image_name = controller.EXPECTED_SOURCE_IMAGE.split("/", 1)
        return (
            "compute",
            "disks",
            "create",
            resource.name,
            f"--project={controller.EXPECTED_PROJECT}",
            f"--zone={controller.EXPECTED_ZONE}",
            f"--type={controller.EXPECTED_DISK_TYPE}",
            f"--size={controller.EXPECTED_DISK_SIZE_GB}GB",
            f"--image={image_name}",
            f"--image-project={image_project}",
            f"--labels={_label_argument(_labels(self.plan))}",
            f"--description={_description(self.plan, resource)}",
        )

    def _firewall_create_arguments(self, resource: controller.ResourcePlan) -> tuple[str, ...]:
        return (
            "compute",
            "firewall-rules",
            "create",
            resource.name,
            f"--project={controller.EXPECTED_PROJECT}",
            f"--network={controller.EXPECTED_NETWORK}",
            "--direction=INGRESS",
            "--action=ALLOW",
            f"--rules={ROUTE_TCP_RULE}",
            f"--source-tags={_route_tag(self.plan)}",
            f"--target-tags={_route_tag(self.plan)}",
            f"--description={_description(self.plan, resource)}",
            "--no-enable-logging",
        )

    def _iap_firewall_create_arguments(self, resource: controller.ResourcePlan) -> tuple[str, ...]:
        if resource.kind != "iap_firewall":
            raise Q38GcpAdapterError("IAP firewall resource kind is invalid")
        return (
            "compute",
            "firewall-rules",
            "create",
            resource.name,
            f"--project={controller.EXPECTED_PROJECT}",
            f"--network={controller.EXPECTED_NETWORK}",
            "--direction=INGRESS",
            "--action=ALLOW",
            f"--rules={IAP_TCP_RULE}",
            f"--source-ranges={IAP_SOURCE_RANGE}",
            f"--target-tags={_route_tag(self.plan)}",
            f"--description={_description(self.plan, resource)}",
            "--no-enable-logging",
        )

    def _instance_create_arguments(self, resource: controller.ResourcePlan) -> tuple[str, ...]:
        spec = controller._expected_resource_spec(resource)
        disk = self._disk_for_instance(resource)
        return (
            "compute",
            "instances",
            "create",
            resource.name,
            f"--project={controller.EXPECTED_PROJECT}",
            f"--zone={controller.EXPECTED_ZONE}",
            f"--machine-type={spec['machine_type']}",
            f"--disk=name={disk.name},boot=yes,auto-delete=yes",
            f"--network={controller.EXPECTED_NETWORK}",
            f"--subnet={controller.EXPECTED_SUBNET}",
            "--stack-type=IPV4_ONLY",
            f"--tags={_route_tag(self.plan)}",
            f"--labels={_label_argument(_labels(self.plan))}",
            "--no-address",
            "--no-service-account",
            "--maintenance-policy=TERMINATE",
            "--restart-on-failure",
            f"--max-run-duration={controller.EXPECTED_MAX_LIFETIME_SECONDS}s",
            "--instance-termination-action=DELETE",
            f"--metadata={_metadata_argument(_instance_metadata(self.plan, resource))}",
        )

    def compiled_start_commands(self) -> tuple[tuple[str, ...], ...]:
        disks = tuple(
            ("gcloud", *self._disk_create_arguments(resource), "--quiet")
            for resource in self.plan.resources
            if resource.kind.endswith("disk")
        )
        route_firewall = next(item for item in self.plan.resources if item.kind == "firewall")
        iap_firewall = next(item for item in self.plan.resources if item.kind == "iap_firewall")
        firewalls = (
            ("gcloud", *self._firewall_create_arguments(route_firewall), "--quiet"),
            ("gcloud", *self._iap_firewall_create_arguments(iap_firewall), "--quiet"),
        )
        instances = tuple(
            ("gcloud", *self._instance_create_arguments(resource), "--quiet")
            for resource in self.plan.resources
            if resource.kind.endswith("instance")
        )
        return (*disks, *firewalls, *instances)

    def _delete_resource(self, resource: controller.ResourcePlan) -> None:
        value = self._describe(resource)
        if value is None:
            return
        self._validate_resource(resource, value, cleanup=True)
        if resource.kind.endswith("instance"):
            self._gcloud(
                "compute",
                "instances",
                "delete",
                resource.name,
                f"--project={controller.EXPECTED_PROJECT}",
                f"--zone={controller.EXPECTED_ZONE}",
                "--keep-disks=all",
                timeout=900,
            )
        elif resource.kind.endswith("disk"):
            self._gcloud(
                "compute",
                "disks",
                "delete",
                resource.name,
                f"--project={controller.EXPECTED_PROJECT}",
                f"--zone={controller.EXPECTED_ZONE}",
                timeout=900,
            )
        else:
            self._gcloud(
                "compute",
                "firewall-rules",
                "delete",
                resource.name,
                f"--project={controller.EXPECTED_PROJECT}",
                timeout=900,
            )

    def _cleanup_route(self) -> None:
        errors = 0
        order = (
            [item for item in self.plan.resources if item.kind.endswith("instance")]
            + [item for item in self.plan.resources if item.kind.endswith("disk")]
            + [item for item in self.plan.resources if item.kind.endswith("firewall")]
        )
        for resource in order:
            try:
                self._delete_resource(resource)
            except Q38GcpAdapterError:
                errors += 1
        remaining, protected = self._provider_inventory(cleanup=True)
        if errors or any(value is not None for value in remaining.values()) or not protected:
            raise Q38GcpAdapterError("provider cleanup is incomplete")

    def execute(
        self,
        state_value: Mapping[str, Any],
        decision_value: Mapping[str, Any],
        host_status: Mapping[str, Any],
        *,
        manifest_path: Path | None,
        artifact_root: Path | None,
        evidence_root: Path | None = None,
    ) -> dict[str, Any]:
        state = controller.validate_state(state_value, self.plan)
        expected_decision = controller.action_record(state, self.plan)
        if dict(decision_value) != expected_decision:
            raise Q38GcpAdapterError("controller decision is stale or unbound")
        action = state["next_action"]
        if action in {"start_route", "collect_route"}:
            raise Q38GcpAdapterError(RUNTIME_ACTIONS_BLOCKED)
        if action == "cleanup_route":
            # Cleanup deliberately skips aggregate preflight. Each deletion performs
            # its own exact binding check so one foreign resource cannot strand the
            # rest of the independently verified, paid run inventory.
            self._cleanup_route()
            return self.inventory(
                blank_host_status(self.plan),
                manifest_path=None,
                artifact_root=None,
                cleanup_only=True,
            )
        if action != "none":
            raise Q38GcpAdapterError("controller action is unsupported")
        cleanup_only = state["phase"] == "CLEANING" or int(self.clock()) >= self.plan.deadline_unix
        observation = self.inventory(
            host_status,
            manifest_path=manifest_path,
            artifact_root=artifact_root,
            evidence_root=evidence_root,
            cleanup_only=cleanup_only,
        )
        reconciled = controller.reconcile(
            "status",
            state,
            observation,
            self.plan,
            now_unix=int(self.clock()),
            route_evidence_validated=False,
            start_was_issued=False,
        )
        if controller.action_record(reconciled, self.plan) != expected_decision:
            raise Q38GcpAdapterError("controller action is no longer current")
        return observation


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = (json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(payload) > MAX_JSON_BYTES:
        raise Q38GcpAdapterError("output exceeded its size bound")
    path.parent.mkdir(parents=True, exist_ok=True)
    parent = path.parent.lstat()
    parent_reparse = bool(getattr(parent, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if parent_reparse or path.parent.is_symlink() or not stat.S_ISDIR(parent.st_mode):
        raise Q38GcpAdapterError("output parent is unsafe")
    if path.exists() or path.is_symlink():
        metadata = path.lstat()
        reparse = bool(getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        if reparse or path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise Q38GcpAdapterError("output target is unsafe")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _load_mapping(path: Path) -> Mapping[str, Any]:
    return controller._strict_json(controller._regular_bytes(path))


def _assert_distinct_paths(paths: Sequence[Path | None]) -> None:
    observed: set[Path] = set()
    for path in paths:
        if path is None:
            continue
        resolved = path.resolve()
        if resolved in observed:
            raise Q38GcpAdapterError("adapter input and output paths must be distinct")
        observed.add(resolved)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("observe", "step"))
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--decision", type=Path)
    parser.add_argument("--host-status", type=Path)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cleanup-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _assert_distinct_paths(
            (
                args.plan,
                args.manifest,
                args.state,
                args.decision,
                args.host_status,
                args.output,
            )
        )
        plan = controller.load_plan(args.plan, args.source_root)
        adapter = GcpAdapter(plan, args.source_root)
        if args.state is not None:
            controller._assert_protected_path(args.state, plan, args.source_root, directory=False)
        if args.decision is not None:
            controller._assert_protected_path(args.decision, plan, args.source_root, directory=False)
        host_status = load_host_status(args.host_status, plan, args.source_root)
        if args.operation == "observe":
            result = adapter.inventory(
                host_status,
                manifest_path=args.manifest,
                artifact_root=args.artifact_root,
                evidence_root=args.evidence_root,
                cleanup_only=args.cleanup_only,
            )
        else:
            if args.state is None or args.decision is None:
                raise Q38GcpAdapterError("step inputs are required")
            result = adapter.execute(
                _load_mapping(args.state),
                _load_mapping(args.decision),
                host_status,
                manifest_path=args.manifest,
                artifact_root=args.artifact_root,
                evidence_root=args.evidence_root,
            )
        _atomic_json(args.output, result)
    except (Q38GcpAdapterError, controller.RouteControllerError) as exc:
        raise SystemExit(str(exc)) from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
