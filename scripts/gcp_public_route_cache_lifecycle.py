"""Fail-closed lifecycle for the Gate 11 private Artifact Registry route cache."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

try:
    from scripts import gcp_public_route_lifecycle as route_lifecycle, qualification_cost_guard as cost_guard
except ModuleNotFoundError:
    import gcp_public_route_lifecycle as route_lifecycle  # type: ignore[no-redef]
    import qualification_cost_guard as cost_guard  # type: ignore[no-redef]

SCHEMA_VERSION = 1
MAX_PROVIDER_SECONDS = 600
MAX_CREATE_SECONDS = 600
MAX_CACHE_WARM_SECONDS = 18_000
POLL_SECONDS = 30
READY_PATH = "/var/lib/communityai-cache/cache-ready.json"
READY_KEYS = {"schema_version", "scope", "result", "images_prefetched"}
PRIVACY = {
    "credentials_retained": False,
    "paths_retained": False,
    "provider_ids_retained": False,
    "provider_output_retained": False,
    "command_argv_retained": False,
}
Runner = Callable[[Sequence[str], int], route_lifecycle.CommandResult]
Clock = Callable[[], float]
Sleeper = Callable[[float], None]


class CacheLifecycleError(ValueError):
    """The bounded cache lifecycle cannot continue safely."""


@dataclass(frozen=True)
class BoundCachePlan:
    authorization: Mapping[str, Any]
    provider_plan: Mapping[str, Any]
    run_id: str
    source_commit: str
    provider_plan_digest: str
    project: str
    zone: str
    region: str
    primary: route_lifecycle.RouteBinding
    standby: route_lifecycle.RouteBinding


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = (json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(payload) > route_lifecycle.MAX_REPORT_BYTES:
        raise CacheLifecycleError("cache lifecycle evidence exceeds its bounded size")
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


def _success(result: route_lifecycle.CommandResult, field: str, *, empty: bool = False) -> bytes:
    if result.returncode != 0:
        raise CacheLifecycleError(f"{field} failed")
    if empty and result.stdout.strip():
        raise CacheLifecycleError(f"{field} did not prove absence")
    return result.stdout


def _image_spec(plan: Mapping[str, Any], role: str) -> Mapping[str, Any]:
    images = plan.get("images")
    if not isinstance(images, list) or len(images) != 2 or any(not isinstance(item, dict) for item in images):
        raise CacheLifecycleError("cache image plan is invalid")
    matches = [item for item in images if item.get("role") == role]
    if len(matches) != 1:
        raise CacheLifecycleError("cache image role is invalid")
    return matches[0]


def load_bound_cache_plan(
    *,
    authorization_path: Path,
    ledger_path: Path,
    primary_evidence_path: Path,
    standby_evidence_path: Path,
    cache_bootstrap_path: Path,
    expected_source_commit: str,
) -> BoundCachePlan:
    if route_lifecycle._COMMIT_RE.fullmatch(expected_source_commit) is None:
        raise CacheLifecycleError("cache lifecycle source commit is invalid")
    authorization, _payload = route_lifecycle._strict_json(authorization_path, "cache authorization")
    if (
        set(authorization) != route_lifecycle._AUTHORIZATION_KEYS
        or authorization.get("schema_version") != cost_guard.SCHEMA_VERSION
        or authorization.get("scope") != "communityai-cloud-cost-authorization"
        or authorization.get("result") != "passed"
        or authorization.get("provider") != "GCP"
        or authorization.get("workload") != cost_guard.GCP_PUBLIC_ROUTE_CACHE_WORKLOAD
        or authorization.get("source_commit") != expected_source_commit
        or authorization.get("reservation_recorded") is not True
        or authorization.get("provisioning_authorized") is not True
        or authorization.get("provider_calls_authorized_without_preflight") is not False
    ):
        raise CacheLifecycleError("cache authorization is not one exact reserved plan")
    run_id = authorization.get("run_id")
    if not isinstance(run_id, str) or route_lifecycle._RUN_RE.fullmatch(run_id) is None:
        raise CacheLifecycleError("cache authorization run ID is invalid")
    provider_plan = authorization.get("provider_plan")
    if not isinstance(provider_plan, dict):
        raise CacheLifecycleError("cache provider plan is invalid")
    plan_digest = cost_guard._provider_plan_digest(provider_plan)
    if authorization.get("provider_plan_digest") != plan_digest:
        raise CacheLifecycleError("cache provider plan digest is invalid")

    entries = cost_guard.load_spend_ledger(ledger_path)
    matching = [entry for entry in entries if entry.run_id == run_id]
    if (
        len(matching) != 1
        or matching[0].provider != "GCP"
        or matching[0].state != "PLANNED"
        or matching[0].purpose != authorization.get("ledger_purpose")
        or cost_guard._usd(matching[0].maximum_usd) != authorization.get("maximum_estimate_usd")
    ):
        raise CacheLifecycleError("cache authorization does not match the live ledger reservation")
    purpose_value = authorization.get("ledger_purpose")
    if not isinstance(purpose_value, str) or " [workload " not in purpose_value:
        raise CacheLifecycleError("cache ledger purpose is invalid")
    purpose = purpose_value.split(" [workload ", 1)[0]

    primary_spec = _image_spec(provider_plan, "primary")
    standby_spec = _image_spec(provider_plan, "standby")
    bootstrap = provider_plan.get("cache_bootstrap")
    builder = provider_plan.get("builder")
    if not isinstance(bootstrap, dict) or not isinstance(builder, dict):
        raise CacheLifecycleError("cache source or builder binding is missing")
    route_lifecycle._validate_source_binding(cache_bootstrap_path, bootstrap, "cache bootstrap")

    try:
        rebuilt = cost_guard.build_authorization(
            entries=entries,
            run_id=run_id,
            provider="gcp",
            workload=cost_guard.GCP_PUBLIC_ROUTE_CACHE_WORKLOAD,
            purpose=purpose,
            source_commit=expected_source_commit,
            maximum_hours=Decimal(str(provider_plan["maximum_runtime_hours"])),
            project=str(provider_plan["project"]),
            zone=str(provider_plan["zone"]),
            windows_image=None,
            linux_image=str(builder["os_image"]),
            cuda_fallback_zone=None,
            cuda_shape="n1-t4",
            manual_maximum_usd=None,
            primary_image=str(primary_spec["source"]),
            primary_image_evidence_digest=str(primary_spec["publication_evidence_digest"]),
            standby_image=str(standby_spec["source"]),
            standby_image_evidence_digest=str(standby_spec["publication_evidence_digest"]),
            cache_bootstrap_digest=str(bootstrap["sha256"]),
            cache_bootstrap_bytes=int(bootstrap["byte_size"]),
            today=date.today(),
        )
    except (KeyError, TypeError, ValueError, cost_guard.CostGuardError) as exc:
        raise CacheLifecycleError("cache authorization cannot be regenerated") from exc
    if rebuilt != authorization:
        raise CacheLifecycleError("cache authorization differs from the regenerated plan")

    primary = route_lifecycle._load_publication(
        primary_evidence_path,
        expected_digest=str(primary_spec["publication_evidence_digest"]),
        planned_route={
            "role": "primary",
            "candidate": "qwen3.5-2b",
            "image": primary_spec["cached"],
            "manifest_digest": cost_guard.GCP_PRIMARY_MANIFEST_DIGEST,
        },
    )
    standby = route_lifecycle._load_publication(
        standby_evidence_path,
        expected_digest=str(standby_spec["publication_evidence_digest"]),
        planned_route={
            "role": "standby",
            "candidate": "gemma-4-e2b",
            "image": standby_spec["cached"],
            "manifest_digest": cost_guard.GCP_STANDBY_MANIFEST_DIGEST,
        },
    )
    for spec, binding, publication_repository in (
        (primary_spec, primary, cost_guard.GCP_PRIMARY_PUBLICATION_IMAGE_REPOSITORY),
        (standby_spec, standby, cost_guard.GCP_STANDBY_PUBLICATION_IMAGE_REPOSITORY),
    ):
        digest = binding.image_reference.rsplit("@", 1)[1]
        if spec.get("cached") != binding.image_reference or spec.get("source") != f"{publication_repository}@{digest}":
            raise CacheLifecycleError("cache source and destination digest binding is invalid")

    project = provider_plan.get("project")
    zone = provider_plan.get("zone")
    region = provider_plan.get("region")
    if (
        project != cost_guard.GCP_ARTIFACT_REGISTRY_PROJECT
        or region != cost_guard.GCP_ARTIFACT_REGISTRY_LOCATION
        or not isinstance(zone, str)
    ):
        raise CacheLifecycleError("cache target is invalid")
    return BoundCachePlan(
        authorization=authorization,
        provider_plan=provider_plan,
        run_id=run_id,
        source_commit=expected_source_commit,
        provider_plan_digest=plan_digest,
        project=project,
        zone=zone,
        region=region,
        primary=primary,
        standby=standby,
    )


def _protected_bootstrap_running(plan: BoundCachePlan, runner: Runner) -> bool:
    result = runner(
        (
            "gcloud",
            "compute",
            "instances",
            "list",
            "--filter=name=communityai-bootstrap-1",
            "--format=value(status)",
            "--project",
            plan.project,
        ),
        MAX_PROVIDER_SECONDS,
    )
    return result.returncode == 0 and result.stdout.strip() == b"RUNNING"


def _api_enabled(plan: BoundCachePlan, runner: Runner) -> bool:
    result = runner(tuple(plan.provider_plan["verify_api_enabled_command"]), MAX_PROVIDER_SECONDS)
    return result.returncode == 0 and result.stdout.strip() == b"artifactregistry.googleapis.com"


def _repository_list(plan: BoundCachePlan, runner: Runner) -> bytes:
    return _success(
        runner(
            (
                "gcloud",
                "artifacts",
                "repositories",
                "list",
                "--location",
                plan.region,
                "--filter",
                f"name:{cost_guard.GCP_ARTIFACT_REGISTRY_REPOSITORY}",
                "--format=value(name)",
                "--project",
                plan.project,
            ),
            MAX_PROVIDER_SECONDS,
        ),
        "cache repository absence check",
    )


def _private_policy(plan: BoundCachePlan, runner: Runner) -> bool:
    payload = _success(
        runner(
            (
                "gcloud",
                "artifacts",
                "repositories",
                "get-iam-policy",
                cost_guard.GCP_ARTIFACT_REGISTRY_REPOSITORY,
                "--location",
                plan.region,
                "--format=json(bindings)",
                "--project",
                plan.project,
            ),
            MAX_PROVIDER_SECONDS,
        ),
        "cache policy verification",
    )
    value = route_lifecycle._bounded_json_object(payload, "cache policy")
    bindings = value.get("bindings", [])
    if not isinstance(bindings, list):
        raise CacheLifecycleError("cache policy is invalid")
    for binding in bindings:
        members = binding.get("members") if isinstance(binding, dict) else None
        if not isinstance(members, list) or any(not isinstance(member, str) for member in members):
            raise CacheLifecycleError("cache policy is invalid")
        if {"allUsers", "allAuthenticatedUsers"}.intersection(members):
            return False
    return True


def _repository_exact(plan: BoundCachePlan, runner: Runner) -> bool:
    payload = _success(
        runner(
            (
                "gcloud",
                "artifacts",
                "repositories",
                "describe",
                cost_guard.GCP_ARTIFACT_REGISTRY_REPOSITORY,
                "--location",
                plan.region,
                "--format=json(name,format,mode,remoteRepositoryConfig,vulnerabilityScanningConfig)",
                "--project",
                plan.project,
            ),
            MAX_PROVIDER_SECONDS,
        ),
        "cache repository verification",
    )
    value = route_lifecycle._bounded_json_object(payload, "cache repository")
    remote = value.get("remoteRepositoryConfig")
    scanning = value.get("vulnerabilityScanningConfig")
    docker = remote.get("dockerRepository") if isinstance(remote, dict) else None
    custom = docker.get("customRepository") if isinstance(docker, dict) else None
    return (
        value.get("name")
        == f"projects/{plan.project}/locations/{plan.region}/repositories/{cost_guard.GCP_ARTIFACT_REGISTRY_REPOSITORY}"
        and value.get("format") == "DOCKER"
        and value.get("mode") == "REMOTE_REPOSITORY"
        and isinstance(custom, dict)
        and custom.get("uri") == "https://ghcr.io"
        and isinstance(scanning, dict)
        and scanning.get("enablementConfig") == "DISABLED"
        and scanning.get("enablementState") == "SCANNING_DISABLED"
    )


def _cached_digests(plan: BoundCachePlan, runner: Runner) -> int:
    images = (
        plan.primary.image_reference,
        plan.primary.runtime_image_reference,
        plan.standby.image_reference,
        plan.standby.runtime_image_reference,
    )
    verified = 0
    for image in images:
        expected = image.rsplit("@", 1)[1].encode("ascii")
        observed = _success(
            runner(
                (
                    "gcloud",
                    "artifacts",
                    "docker",
                    "images",
                    "describe",
                    image,
                    "--format=value(image_summary.digest)",
                    "--project",
                    plan.project,
                ),
                MAX_PROVIDER_SECONDS,
            ),
            "cached image digest verification",
        )
        if observed.strip() != expected:
            raise CacheLifecycleError("cached image digest does not match publication evidence")
        verified += 1
    return verified


def _wait_instance(plan: BoundCachePlan, runner: Runner, *, clock: Clock, sleeper: Sleeper) -> None:
    instance = str(plan.provider_plan["builder"]["instance"])
    deadline = clock() + MAX_CREATE_SECONDS
    while True:
        payload = _success(
            runner(
                (
                    "gcloud",
                    "compute",
                    "instances",
                    "describe",
                    instance,
                    "--zone",
                    plan.zone,
                    "--format=json(status,machineType,scheduling.maxRunDuration)",
                    "--project",
                    plan.project,
                ),
                MAX_PROVIDER_SECONDS,
            ),
            "cache builder verification",
        )
        value = route_lifecycle._bounded_json_object(payload, "cache builder")
        if value.get("status") in {"PROVISIONING", "STAGING"}:
            if clock() >= deadline:
                raise CacheLifecycleError("cache builder create timed out")
            sleeper(5)
            continue
        machine = value.get("machineType")
        scheduling = value.get("scheduling")
        if (
            value.get("status") != "RUNNING"
            or not isinstance(machine, str)
            or not machine.endswith("/machineTypes/e2-standard-4")
            or not isinstance(scheduling, dict)
            or scheduling.get("maxRunDuration") != {"seconds": "21600", "nanos": 0}
        ):
            raise CacheLifecycleError("cache builder does not match the exact plan")
        return


def _wait_cache_ready(plan: BoundCachePlan, runner: Runner, *, clock: Clock, sleeper: Sleeper) -> None:
    instance = str(plan.provider_plan["builder"]["instance"])
    deadline = clock() + MAX_CACHE_WARM_SECONDS
    command = (
        "gcloud",
        "compute",
        "ssh",
        instance,
        "--zone",
        plan.zone,
        "--tunnel-through-iap",
        "--command",
        f"sudo -n cat {READY_PATH}",
        "--project",
        plan.project,
        "--quiet",
    )
    while True:
        result = runner(command, min(180, MAX_PROVIDER_SECONDS))
        if result.returncode == 0:
            value = route_lifecycle._bounded_json_object(result.stdout, "cache bootstrap acknowledgement")
            if (
                set(value) == READY_KEYS
                and value.get("schema_version") == 1
                and value.get("scope") == "communityai-public-route-cache-bootstrap"
                and value.get("result") == "passed"
                and value.get("images_prefetched") == 2
            ):
                return
            raise CacheLifecycleError("cache bootstrap acknowledgement is invalid")
        if clock() >= deadline:
            raise CacheLifecycleError("cache prewarm timed out")
        sleeper(POLL_SECONDS)


def _cleanup_builder(plan: BoundCachePlan, runner: Runner) -> tuple[int, list[bool]]:
    deleted = 0
    for command in plan.provider_plan["cleanup_commands"]:
        try:
            if runner(tuple(command), MAX_PROVIDER_SECONDS).returncode == 0:
                deleted += 1
        except Exception:
            continue
    absence = []
    for command in plan.provider_plan["verify_cleanup_commands"]:
        try:
            result = runner(tuple(command), MAX_PROVIDER_SECONDS)
            absence.append(result.returncode == 0 and not result.stdout.strip())
        except Exception:
            absence.append(False)
    return deleted, absence


def execute_cache_lifecycle(
    plan: BoundCachePlan,
    *,
    output_path: Path,
    runner: Runner = route_lifecycle._run_bounded,
    clock: Clock = time.monotonic,
    sleeper: Sleeper = time.sleep,
) -> Mapping[str, Any]:
    started = clock()
    stage = "native_authentication"
    repository_create_attempted_after_absence = False
    public_binding_added = False
    temporary_public_binding_removed = True
    repository_absent_after_failure: bool | None = None
    repository_retained = False
    cached_manifest_count = 0
    cleanup_deleted = 0
    absence = [False] * 5
    result = "failed"
    protected_running = False
    try:
        auth = _success(
            runner(
                ("gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"),
                route_lifecycle.MAX_AUTH_SECONDS,
            ),
            "gcloud authentication",
        )
        if not auth.strip() or len(auth) > 4096:
            raise CacheLifecycleError("gcloud authentication is unavailable")
        protected_running = _protected_bootstrap_running(plan, runner)
        if not protected_running:
            raise CacheLifecycleError("protected bootstrap is not running")
        for index, command in enumerate(plan.provider_plan["verify_cleanup_commands"]):
            _success(runner(tuple(command), MAX_PROVIDER_SECONDS), f"initial builder absence {index}", empty=True)

        stage = "api_enablement"
        create_commands = plan.provider_plan["create_commands"]
        try:
            runner(tuple(create_commands[0]), MAX_PROVIDER_SECONDS)
        except Exception:
            pass
        if not _api_enabled(plan, runner):
            raise CacheLifecycleError("Artifact Registry API enablement is not observable")
        if _repository_list(plan, runner).strip():
            raise CacheLifecycleError("cache repository already exists outside this run")

        stage = "repository_create"
        repository_create_attempted_after_absence = True
        _success(runner(tuple(create_commands[1]), MAX_PROVIDER_SECONDS), "cache repository creation")
        if not _repository_exact(plan, runner):
            raise CacheLifecycleError("created cache repository does not match the exact plan")

        stage = "temporary_public_binding"
        public_binding_added = True
        temporary_public_binding_removed = False
        _success(runner(tuple(create_commands[2]), MAX_PROVIDER_SECONDS), "temporary cache reader binding")

        stage = "builder_create"
        for command in create_commands[3:]:
            _success(runner(tuple(command), MAX_PROVIDER_SECONDS), "cache builder create command")
        _wait_instance(plan, runner, clock=clock, sleeper=sleeper)

        stage = "cache_warm"
        _wait_cache_ready(plan, runner, clock=clock, sleeper=sleeper)

        stage = "privacy_revoke"
        _success(
            runner(tuple(plan.provider_plan["revoke_public_command"]), MAX_PROVIDER_SECONDS),
            "temporary cache reader revocation",
        )
        public_binding_added = False
        if not _private_policy(plan, runner):
            raise CacheLifecycleError("cache remains public after prewarm")
        temporary_public_binding_removed = True

        stage = "digest_verification"
        cached_manifest_count = _cached_digests(plan, runner)
        if not _repository_exact(plan, runner):
            raise CacheLifecycleError("cache repository changed during prewarm")

        stage = "builder_cleanup"
        cleanup_deleted, absence = _cleanup_builder(plan, runner)
        if not all(absence):
            raise CacheLifecycleError("cache builder cleanup is incomplete")
        if not _private_policy(plan, runner):
            raise CacheLifecycleError("retained cache is not private")
        protected_running = _protected_bootstrap_running(plan, runner)
        if not protected_running:
            raise CacheLifecycleError("protected bootstrap is not running after cleanup")
        repository_retained = True
        result = "passed"
        stage = "complete"
    except Exception:
        result = "failed"
    finally:
        if public_binding_added:
            try:
                revoke = runner(tuple(plan.provider_plan["revoke_public_command"]), MAX_PROVIDER_SECONDS)
                if revoke.returncode == 0 and _private_policy(plan, runner):
                    public_binding_added = False
                    temporary_public_binding_removed = True
            except Exception:
                temporary_public_binding_removed = False
        if not all(absence):
            cleanup_deleted, absence = _cleanup_builder(plan, runner)
        if result != "passed" and repository_create_attempted_after_absence:
            try:
                runner(tuple(plan.provider_plan["delete_repository_command"]), MAX_PROVIDER_SECONDS)
            except Exception:
                pass
            try:
                repository_absent_after_failure = not _repository_list(plan, runner).strip()
            except Exception:
                repository_absent_after_failure = False
            if repository_absent_after_failure:
                public_binding_added = False
                temporary_public_binding_removed = True
            repository_retained = False
        try:
            protected_running = _protected_bootstrap_running(plan, runner)
        except Exception:
            protected_running = False

    evidence = {
        "schema_version": SCHEMA_VERSION,
        "scope": "gcp-public-route-cache-lifecycle",
        "result": result,
        "failure_stage": None if result == "passed" else stage,
        "run_id": plan.run_id,
        "source_commit": plan.source_commit,
        "provider_plan_digest": plan.provider_plan_digest,
        "maximum_estimate_usd": plan.authorization["maximum_estimate_usd"],
        "elapsed_seconds": round(max(0.0, clock() - started), 3),
        "repository": {
            "retained": repository_retained,
            "private": result == "passed" and repository_retained,
            "mode": "REMOTE_REPOSITORY",
            "location": plan.region,
            "upstream": "https://ghcr.io",
            "absent_after_failure": repository_absent_after_failure,
        },
        "cached_manifest_count": cached_manifest_count,
        "temporary_public_binding_removed": temporary_public_binding_removed,
        "builder_cleanup": {
            "delete_commands_succeeded": cleanup_deleted,
            "absence_checks": absence,
            "all_absent": all(absence),
        },
        "protected_bootstrap_running": protected_running,
        "privacy": dict(PRIVACY),
        "complete_release_qualification": False,
    }
    _atomic_json(output_path, evidence)
    return evidence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Operate the bounded Gate 11 private route cache")
    parser.add_argument("--authorization", required=True, type=Path)
    parser.add_argument("--ledger", default=Path("docs/RELEASE_READINESS.md"), type=Path)
    parser.add_argument("--primary-evidence", required=True, type=Path)
    parser.add_argument("--standby-evidence", required=True, type=Path)
    parser.add_argument("--cache-bootstrap", required=True, type=Path)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        plan = load_bound_cache_plan(
            authorization_path=args.authorization,
            ledger_path=args.ledger,
            primary_evidence_path=args.primary_evidence,
            standby_evidence_path=args.standby_evidence,
            cache_bootstrap_path=args.cache_bootstrap,
            expected_source_commit=args.source_commit,
        )
        evidence = execute_cache_lifecycle(plan, output_path=args.output)
    except (CacheLifecycleError, route_lifecycle.LifecycleError, cost_guard.CostGuardError) as exc:
        print(f"public-route cache lifecycle failed before execution: {exc}")
        return 1
    print(
        json.dumps(
            {
                "result": evidence["result"],
                "failure_stage": evidence["failure_stage"],
                "repository_retained": evidence["repository"]["retained"],
                "builder_cleanup_complete": evidence["builder_cleanup"]["all_absent"],
            },
            sort_keys=True,
        )
    )
    return 0 if evidence["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
