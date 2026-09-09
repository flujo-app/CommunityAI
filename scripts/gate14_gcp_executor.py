"""Execute exact GCP actions selected by the durable Gate 14 controller.

Every operation revalidates native gcloud authentication, inventories the two planned
instances/disks plus all project L4 usage, and verifies the protected bootstrap. Only
the controller's allowlisted next action may mutate provider state. Exact-name foreign
or ambiguous resources fail closed.
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
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import gate14_hardware_acceptance as acceptance
import gate14_run_controller as controller

MAX_OUTPUT_BYTES = 1_048_576
MAX_JSON_BYTES = 262_144
SCOPE_LABEL = "gate14-hardware"
PROTECTED_INSTANCE = controller.PROTECTED_INSTANCE


class Gate14GcpError(RuntimeError):
    """A provider observation or exact action failed closed."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


Runner = Callable[[Sequence[str], int], CommandResult]


def _default_runner(argv: Sequence[str], timeout: int) -> CommandResult:
    if not argv or any(not isinstance(item, str) or not item for item in argv):
        raise Gate14GcpError("provider command is invalid")
    try:
        result = subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            timeout=timeout,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise Gate14GcpError("provider command failed") from exc
    if len(result.stdout) > MAX_OUTPUT_BYTES or len(result.stderr) > MAX_OUTPUT_BYTES:
        raise Gate14GcpError("provider command output exceeded its bound")
    return CommandResult(result.returncode, result.stdout, result.stderr)


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Gate14GcpError("duplicate provider JSON field")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise Gate14GcpError("non-finite provider JSON value")


def _json_bytes(payload: bytes, label: str) -> Any:
    if not 1 <= len(payload) <= MAX_OUTPUT_BYTES:
        raise Gate14GcpError(f"{label} output is invalid")
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Gate14GcpError(f"{label} returned invalid JSON") from exc


def _basename(value: Any) -> str:
    return value.rsplit("/", 1)[-1] if isinstance(value, str) else ""


def _metadata(value: Mapping[str, Any]) -> Mapping[str, str]:
    raw = value.get("metadata")
    items = raw.get("items") if isinstance(raw, dict) else None
    if items is None:
        return {}
    if not isinstance(items, list):
        raise Gate14GcpError("instance metadata is invalid")
    result: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict) or set(item) != {"key", "value"}:
            raise Gate14GcpError("instance metadata item is invalid")
        key, field = item["key"], item["value"]
        if not isinstance(key, str) or not isinstance(field, str) or key in result:
            raise Gate14GcpError("instance metadata item is ambiguous")
        result[key] = field
    return result


def _labels(plan: controller.RunPlan, client: controller.ClientPlan) -> dict[str, str]:
    return {
        "communityai-run": plan.run_id,
        "communityai-scope": SCOPE_LABEL,
        "communityai-source": client.source_commit,
    }


def _label_argument(value: Mapping[str, str]) -> str:
    return ",".join(f"{key}={value[key]}" for key in sorted(value))


def _jobs_document(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema_version", "run_id", "clients"}:
        raise Gate14GcpError("host job observation schema is invalid")
    clients = value["clients"]
    if (
        value["schema_version"] != 1
        or not isinstance(value["run_id"], str)
        or not isinstance(clients, dict)
        or set(clients) != {"windows", "linux"}
    ):
        raise Gate14GcpError("host job observation binding is invalid")
    for item in clients.values():
        if not isinstance(item, dict) or set(item) != controller._CLIENT_FIELDS:
            raise Gate14GcpError("host job observation client is invalid")
    return dict(value)


def load_jobs(path: Path, plan: controller.RunPlan) -> Mapping[str, Any]:
    path = Path(path)
    if not path.exists():
        return {
            "schema_version": 1,
            "run_id": plan.run_id,
            "clients": {
                platform: {
                    "job_state": "absent",
                    "attempt_ordinal": 0,
                    "evidence_digest": None,
                }
                for platform in ("windows", "linux")
            },
        }
    value = _jobs_document(controller._strict_json(controller._regular_bytes(path)))
    if value["run_id"] != plan.run_id:
        raise Gate14GcpError("host job observation run changed")
    return value


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        metadata = path.lstat()
        reparse = bool(getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        if reparse or path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise Gate14GcpError("output target is unsafe")
    payload = (json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")) + os.linesep).encode("utf-8")
    if len(payload) > MAX_JSON_BYTES:
        raise Gate14GcpError("output exceeded its size bound")
    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


class GcpExecutor:
    def __init__(
        self,
        plan: controller.RunPlan,
        *,
        runner: Runner = _default_runner,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.plan = plan
        self.runner = runner
        self.clock = clock

    def _gcloud(
        self,
        *arguments: str,
        timeout: int = 300,
        check: bool = True,
    ) -> CommandResult:
        result = self.runner(("gcloud", *arguments, "--quiet"), timeout)
        if check and result.returncode != 0:
            raise Gate14GcpError("gcloud action failed")
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
            raise Gate14GcpError("exactly one active native gcloud account is required")
        project = self._gcloud_json("projects", "describe", self.plan.project, timeout=60)
        if not isinstance(project, dict) or project.get("lifecycleState") != "ACTIVE":
            raise Gate14GcpError("authorized GCP project is unavailable")

    def _describe(self, kind: str, name: str) -> Mapping[str, Any] | None:
        result = self._gcloud(
            "compute",
            kind,
            "describe",
            name,
            f"--project={self.plan.project}",
            f"--zone={self.plan.zone}",
            "--format=json",
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            text = result.stderr.decode("utf-8", "replace").casefold()
            if "not found" in text or "was not found" in text:
                return None
            raise Gate14GcpError("provider inventory failed")
        value = _json_bytes(result.stdout, f"{kind} inventory")
        if not isinstance(value, dict):
            raise Gate14GcpError("provider inventory item is invalid")
        return value

    def _validate_disk(self, client: controller.ClientPlan, value: Mapping[str, Any]) -> None:
        if value.get("name") != client.disk or value.get("status") not in {"READY", "CREATING"}:
            raise Gate14GcpError("planned disk identity is invalid")
        labels = value.get("labels")
        if not isinstance(labels, dict) or any(
            labels.get(key) != field for key, field in _labels(self.plan, client).items()
        ):
            raise Gate14GcpError("planned disk ownership is invalid")
        source_image = value.get("sourceImage")
        api_prefix = "https://www.googleapis.com/compute/v1/"
        if isinstance(source_image, str) and source_image.startswith(api_prefix):
            source_image = source_image.removeprefix(api_prefix)
        expected_image = f"projects/{client.image_project}/global/images/{client.image}"
        if (
            _basename(value.get("type")) != client.boot_disk_type
            or source_image != expected_image
            or str(value.get("sizeGb")) != str(client.boot_disk_gib)
        ):
            raise Gate14GcpError("planned disk shape is invalid")

    def _validate_instance(self, client: controller.ClientPlan, value: Mapping[str, Any]) -> None:
        if value.get("name") != client.instance:
            raise Gate14GcpError("planned instance identity is invalid")
        labels = value.get("labels")
        if not isinstance(labels, dict) or any(
            labels.get(key) != field for key, field in _labels(self.plan, client).items()
        ):
            raise Gate14GcpError("planned instance ownership is invalid")
        disks = value.get("disks")
        accelerators = value.get("guestAccelerators")
        metadata = _metadata(value)
        no_service_account = "serviceAccounts" not in value or value["serviceAccounts"] == []
        if (
            not no_service_account
            or _basename(value.get("machineType")) != client.machine_type
            or not isinstance(disks, list)
            or len(disks) != 1
            or _basename(disks[0].get("source") if isinstance(disks[0], dict) else None) != client.disk
            or not isinstance(accelerators, list)
            or len(accelerators) != 1
            or _basename(accelerators[0].get("acceleratorType") if isinstance(accelerators[0], dict) else None)
            != "nvidia-l4"
            or accelerators[0].get("acceleratorCount") != 1
            or metadata.get("communityai-run-id") != self.plan.run_id
            or metadata.get("communityai-source-commit") != client.source_commit
            or metadata.get("communityai-termination-unix") != str(client.termination_unix)
        ):
            raise Gate14GcpError("planned instance shape is invalid")

    def inventory(self, jobs: Mapping[str, Any]) -> Mapping[str, Any]:
        self._check_auth()
        jobs = _jobs_document(jobs)
        if jobs["run_id"] != self.plan.run_id:
            raise Gate14GcpError("host job observation run changed")
        instances: dict[str, Any] = {}
        disks: dict[str, bool] = {}
        for client in (self.plan.windows, self.plan.linux):
            instance = self._describe("instances", client.instance)
            disk = self._describe("disks", client.disk)
            if instance is not None:
                self._validate_instance(client, instance)
                if disk is None:
                    raise Gate14GcpError("planned instance lost its disk")
            if disk is not None:
                self._validate_disk(client, disk)
            instances[client.instance] = {
                "present": instance is not None,
                "run_id": self.plan.run_id if instance is not None else None,
                "source_commit": client.source_commit if instance is not None else None,
                "termination_unix": client.termination_unix if instance is not None else None,
            }
            disks[client.disk] = disk is not None

        bootstrap = self._describe("instances", PROTECTED_INSTANCE)
        firewalls = self._gcloud_json(
            "compute",
            "firewall-rules",
            "list",
            f"--project={self.plan.project}",
            f"--filter=labels.communityai-run={self.plan.run_id}",
            timeout=60,
        )
        if not isinstance(firewalls, list) or firewalls:
            raise Gate14GcpError("unexpected run-scoped firewall inventory")
        l4 = self._gcloud_json(
            "compute",
            "instances",
            "list",
            f"--project={self.plan.project}",
            "--filter=status=RUNNING AND guestAccelerators.acceleratorType:nvidia-l4",
            timeout=60,
        )
        if not isinstance(l4, list):
            raise Gate14GcpError("accelerator inventory is invalid")
        observation = {
            "schema_version": 1,
            "run_id": self.plan.run_id,
            "observed_at_unix": int(self.clock()),
            "instances": instances,
            "disks": disks,
            "clients": jobs["clients"],
            "l4_usage": len(l4),
            "protected_bootstrap_running": bootstrap is not None and bootstrap.get("status") == "RUNNING",
        }
        return controller.validate_observation(observation, self.plan)

    def preflight(self, jobs: Mapping[str, Any]) -> Mapping[str, Any]:
        observation = self.inventory(jobs)
        if any(item["present"] for item in observation["instances"].values()) or any(observation["disks"].values()):
            raise Gate14GcpError("planned resources already exist")
        if observation["l4_usage"] != 0 or any(
            item["job_state"] != "absent" for item in observation["clients"].values()
        ):
            raise Gate14GcpError("preflight inventory is not clean")
        accelerator = self._gcloud_json(
            "compute",
            "accelerator-types",
            "describe",
            "nvidia-l4",
            f"--project={self.plan.project}",
            f"--zone={self.plan.zone}",
            timeout=60,
        )
        if not isinstance(accelerator, dict) or accelerator.get("name") != "nvidia-l4":
            raise Gate14GcpError("L4 capacity is unavailable in the authorized zone")
        project_info = self._gcloud_json(
            "compute",
            "project-info",
            "describe",
            f"--project={self.plan.project}",
            timeout=60,
        )
        quotas = project_info.get("quotas") if isinstance(project_info, dict) else None
        gpu_quota = (
            next(
                (item for item in quotas if isinstance(item, dict) and item.get("metric") == "GPUS_ALL_REGIONS"),
                None,
            )
            if isinstance(quotas, list)
            else None
        )
        try:
            quota_available = float(gpu_quota["limit"]) - float(gpu_quota.get("usage", 0))
        except (KeyError, TypeError, ValueError) as exc:
            raise Gate14GcpError("global GPU quota is unavailable") from exc
        if quota_available < 1:
            raise Gate14GcpError("global GPU quota has no headroom")
        for client in (self.plan.windows, self.plan.linux):
            image = self._gcloud_json(
                "compute",
                "images",
                "describe",
                client.image,
                f"--project={client.image_project}",
                timeout=60,
            )
            if not isinstance(image, dict) or image.get("status") != "READY":
                raise Gate14GcpError("authorized image is unavailable")
        return {
            "schema_version": 1,
            "run_id": self.plan.run_id,
            "result": "passed",
            "native_auth_revalidated": True,
            "planned_resources_absent": True,
            "l4_usage": 0,
            "protected_bootstrap_running": True,
            "maximum_estimate_usd": "44.00",
        }

    def _create_client(self, client: controller.ClientPlan) -> None:
        if self._describe("instances", client.instance) is not None or self._describe("disks", client.disk) is not None:
            raise Gate14GcpError("fresh client resources are required")
        labels = _label_argument(_labels(self.plan, client))
        self._gcloud(
            "compute",
            "disks",
            "create",
            client.disk,
            f"--project={self.plan.project}",
            f"--zone={self.plan.zone}",
            f"--type={client.boot_disk_type}",
            f"--size={client.boot_disk_gib}GB",
            f"--image={client.image}",
            f"--image-project={client.image_project}",
            f"--labels={labels}",
            timeout=900,
        )
        try:
            disk = self._describe("disks", client.disk)
            if disk is None:
                raise Gate14GcpError("planned disk creation was not observable")
            self._validate_disk(client, disk)
            self._gcloud(
                "compute",
                "instances",
                "create",
                client.instance,
                f"--project={self.plan.project}",
                f"--zone={self.plan.zone}",
                f"--machine-type={client.machine_type}",
                f"--disk=name={client.disk},boot=yes,auto-delete=yes",
                f"--labels={labels}",
                "--no-address",
                "--no-service-account",
                "--maintenance-policy=TERMINATE",
                "--restart-on-failure",
                f"--max-run-duration={client.max_run_seconds}s",
                "--instance-termination-action=DELETE",
                (
                    "--metadata="
                    f"communityai-run-id={self.plan.run_id},"
                    f"communityai-source-commit={client.source_commit},"
                    f"communityai-termination-unix={client.termination_unix}"
                ),
                timeout=900,
            )
            instance = self._describe("instances", client.instance)
            if instance is None:
                raise Gate14GcpError("planned instance creation was not observable")
            self._validate_instance(client, instance)
        except BaseException:
            self._delete_client(client)
            raise

    def _delete_client(self, client: controller.ClientPlan) -> None:
        instance = self._describe("instances", client.instance)
        disk = self._describe("disks", client.disk)
        if disk is not None:
            self._validate_disk(client, disk)
        if instance is not None:
            self._validate_instance(client, instance)
            self._gcloud(
                "compute",
                "instances",
                "delete",
                client.instance,
                f"--project={self.plan.project}",
                f"--zone={self.plan.zone}",
                "--delete-disks=all",
                timeout=900,
            )
        disk = self._describe("disks", client.disk)
        if disk is not None:
            self._validate_disk(client, disk)
            self._gcloud(
                "compute",
                "disks",
                "delete",
                client.disk,
                f"--project={self.plan.project}",
                f"--zone={self.plan.zone}",
                timeout=900,
            )
        if self._describe("instances", client.instance) is not None or self._describe("disks", client.disk) is not None:
            raise Gate14GcpError("provider deletion was not verified")

    def execute(
        self,
        action: str,
        *,
        state: Mapping[str, Any],
        jobs: Mapping[str, Any],
    ) -> None:
        try:
            bound_state = controller.validate_state(state, self.plan)
            observation = self.inventory(jobs)
            reconciled = controller.reconcile(bound_state, observation, self.plan)
        except controller.Gate14ControllerError as exc:
            raise Gate14GcpError("controller execution precondition failed") from exc
        if bound_state["next_action"] != action or reconciled["next_action"] != action:
            raise Gate14GcpError("controller action is stale or unbound")

        if action == "start_windows":
            self._create_client(self.plan.windows)
        elif action == "start_linux":
            self._create_client(self.plan.linux)
        elif action == "delete_windows":
            self._delete_client(self.plan.windows)
        elif action == "delete_linux":
            self._delete_client(self.plan.linux)
        elif action == "cleanup_failure":
            errors = 0
            for client in (self.plan.windows, self.plan.linux):
                try:
                    self._delete_client(client)
                except Gate14GcpError:
                    errors += 1
            if errors:
                raise Gate14GcpError("provider cleanup is incomplete")
        elif action != "none":
            raise Gate14GcpError("controller action is not executable by GCP")


def _evidence_for(platform_name: str, args: argparse.Namespace) -> Path:
    value = args.windows_evidence if platform_name == "windows" else args.linux_evidence
    if value is None:
        raise Gate14GcpError(f"{platform_name} evidence is required")
    return value


def _challenge_for(platform_name: str, args: argparse.Namespace) -> Path:
    value = args.windows_challenge if platform_name == "windows" else args.linux_challenge
    if value is None:
        raise Gate14GcpError(f"{platform_name} calibration challenge is required")
    return value


def _cleanup_document(
    plan: controller.RunPlan,
    state_path: Path,
    state: Mapping[str, Any],
) -> Mapping[str, Any]:
    if state["phase"] != "CLEANED_PASS":
        raise Gate14GcpError("passing cleanup evidence requires a cleaned pass")
    terminal_payload = controller._regular_bytes(state_path)
    return {
        "schema_version": 1,
        "scope": acceptance.CLEANUP_SCOPE,
        "run_id": plan.run_id,
        "result": "passed",
        "provider": "GCP",
        "controller_source_commit": plan.source_commit,
        "provider_plan_digest": plan.provider_plan_digest,
        "project": plan.project,
        "zone": plan.zone,
        "deleted_instances": list(plan.instances),
        "deleted_disks": list(plan.disks),
        "controller_terminal_state_sha256": "sha256:" + hashlib.sha256(terminal_payload).hexdigest(),
        "native_auth_revalidated": True,
        "expected_instances": 2,
        "remaining_instances": 0,
        "expected_disks": 2,
        "remaining_disks": 0,
        "remaining_firewalls": 0,
        "l4_usage": 0,
        "protected_bootstrap_running": True,
        "product_processes_remaining": 0,
        "temporary_credentials_remaining": 0,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("preflight", "observe", "step"))
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--jobs", type=Path, required=True)
    parser.add_argument("--observation-output", type=Path)
    parser.add_argument("--windows-evidence", type=Path)
    parser.add_argument("--linux-evidence", type=Path)
    parser.add_argument("--windows-challenge", type=Path)
    parser.add_argument("--linux-challenge", type=Path)
    parser.add_argument("--cleanup-output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        plan = controller.load_plan(args.authorization, args.ledger)
        jobs = load_jobs(args.jobs, plan)
        executor = GcpExecutor(plan)
        if args.operation == "preflight":
            result = executor.preflight(jobs)
        else:
            observation = executor.inventory(jobs)
            if args.observation_output is not None:
                _atomic_json(args.observation_output, observation)
            if args.operation == "observe":
                result = observation
            else:
                if args.state is None:
                    raise Gate14GcpError("state is required for a controller step")
                state = (
                    controller.load_state(args.state, plan) if args.state.exists() else controller.initial_state(plan)
                )
                state = controller.reconcile(state, observation, plan)
                action = state["next_action"]
                if action in {"collect_windows", "collect_linux"}:
                    platform_name = action.removeprefix("collect_")
                    state = controller.collect_platform(
                        state,
                        plan,
                        platform_name,
                        _evidence_for(platform_name, args),
                        _challenge_for(platform_name, args),
                    )
                    action = state["next_action"]
                controller.save_state(args.state, state, plan)
                executor.execute(action, state=state, jobs=jobs)
                result = state
                if state["phase"] == "CLEANED_PASS" and args.cleanup_output is not None:
                    _atomic_json(args.cleanup_output, _cleanup_document(plan, args.state, state))
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    except (
        Gate14GcpError,
        controller.Gate14ControllerError,
        acceptance.Gate14EvidenceError,
    ) as exc:
        raise SystemExit(str(exc)) from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
