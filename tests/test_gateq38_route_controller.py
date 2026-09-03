from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from scripts import gateq38_route_controller as route

REAL_ASSERT_PROTECTED_PATH = route._assert_protected_path
RUN_ID = "q38route-001"
SOURCE_BYTES = b"# production artifact verifier fixture\n"
LEDGER_BYTES = SOURCE_BYTES + (
    b"Q38_ROUTE_RESERVATION run_id=q38route-001 "
    b"reservation_id=q38route-reservation-001 maximum_usd=44.00 "
    b"deadline_unix=2000000000\n"
)


def _source_bytes(relative_path: str) -> bytes:
    return LEDGER_BYTES if relative_path == route.READINESS_LEDGER_PATH else SOURCE_BYTES


@pytest.fixture(autouse=True)
def _trusted_controller_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(route, "_assert_protected_path", lambda *args, **kwargs: None)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _source_root(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    for relative_path in route.REQUIRED_SOURCE_PATHS:
        source = root / relative_path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(_source_bytes(relative_path))
    return root


def _binding(relative_path: str = route.VERIFIER_SOURCE_PATH) -> dict[str, object]:
    payload = _source_bytes(relative_path)
    return {
        "relative_path": relative_path,
        "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "byte_size": len(payload),
    }


def _runtime_package(source_bindings: list[dict[str, object]]) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": route.RUNTIME_PACKAGE_SCHEMA_VERSION,
        "scope": route.RUNTIME_PACKAGE_SCOPE,
        "platform": route.RUNTIME_PACKAGE_PLATFORM,
        "source_commit": "a" * 40,
        "source_tree": "d" * 40,
        "source_bindings_digest": route._source_bindings_digest(source_bindings),
        "release_archive_name": route.RUNTIME_PACKAGE_ARCHIVE,
        "release_archive_sha256": "sha256:" + "1" * 64,
        "release_archive_bytes": 3_360_000_000,
        "checksums_sha256": "sha256:" + "2" * 64,
        "checksums_bytes": 100_000,
        "provenance_sha256": "sha256:" + "3" * 64,
        "provenance_bytes": 100_000,
        "desktop_metrics_sha256": "sha256:" + "4" * 64,
        "desktop_metrics_bytes": 10_000,
        "manifest_digest": route.EXPECTED_MANIFEST_DIGEST,
        "manifest_sha256": "sha256:" + "5" * 64,
        "manifest_bytes": 50_000,
        "node_root": route.RUNTIME_PACKAGE_NODE_ROOT,
        "node_executable": route.RUNTIME_PACKAGE_NODE_EXECUTABLE,
        "node_executable_sha256": "sha256:" + "6" * 64,
        "node_executable_bytes": 10_000_000,
        "node_runtime_entry_count": 2_000,
        "node_runtime_bytes": 2_500_000_000,
        "node_runtime_inventory_digest": "sha256:" + "7" * 64,
        "runtime_package_digest": "",
    }
    record["runtime_package_digest"] = route._runtime_package_digest(record)
    return record


def _workers() -> list[dict[str, object]]:
    result = []
    for index, (span, (artifact_bytes, artifact_digest)) in enumerate(route.EXPECTED_SPANS.items()):
        result.append(
            {
                "worker_id": f"worker-{index}",
                "machine_id": f"machine-{index}",
                "instance": f"{RUN_ID}-worker-{index}",
                "disk": f"{RUN_ID}-worker-{index}-disk",
                "span": span,
                "artifact_bytes": artifact_bytes,
                "artifact_set_digest": artifact_digest,
                "cache_root": f"/var/lib/communityai/q38/worker-{index}",
            }
        )
    return result


def _resources(workers: list[dict[str, object]]) -> list[dict[str, object]]:
    result = [
        {
            "name": f"{RUN_ID}-bootstrap-disk",
            "kind": "bootstrap_disk",
            "provider": "gcp",
            "region": "us-central1",
            "worker_id": None,
        },
        {
            "name": f"{RUN_ID}-bootstrap-instance",
            "kind": "bootstrap_instance",
            "provider": "gcp",
            "region": "us-central1",
            "worker_id": None,
        },
        {
            "name": f"{RUN_ID}-firewall",
            "kind": "firewall",
            "provider": "gcp",
            "region": "us-central1",
            "worker_id": None,
        },
        {
            "name": f"{RUN_ID}-iap-firewall",
            "kind": "iap_firewall",
            "provider": "gcp",
            "region": "us-central1",
            "worker_id": None,
        },
    ]
    for worker in workers:
        result.extend(
            [
                {
                    "name": worker["disk"],
                    "kind": "worker_disk",
                    "provider": "gcp",
                    "region": "us-central1",
                    "worker_id": worker["worker_id"],
                },
                {
                    "name": worker["instance"],
                    "kind": "worker_instance",
                    "provider": "gcp",
                    "region": "us-central1",
                    "worker_id": worker["worker_id"],
                },
            ]
        )
    return sorted(result, key=lambda item: str(item["name"]))


def _plan_value(*, authorized: bool = True) -> dict[str, object]:
    workers = _workers()
    source_bindings = [_binding(relative_path) for relative_path in sorted(route.REQUIRED_SOURCE_PATHS)]
    return {
        "schema_version": route.SCHEMA_VERSION,
        "gate": route.GATE,
        "run_id": RUN_ID,
        "route_job_id": "q38route-job-001",
        "source_commit": "a" * 40,
        "manifest_digest": route.EXPECTED_MANIFEST_DIGEST,
        "model_revision": route.EXPECTED_MODEL_REVISION,
        "deadline_unix": 2_000_000_000,
        "authorization": {
            "combined_cloud_ceiling_usd": "100.00",
            "ledger_committed_before_run_usd": "56.00",
            "maximum_estimate_usd": "44.00",
            "reservation_recorded": authorized,
            "native_auth_revalidated": authorized,
            "inventory_revalidated": authorized,
            "pricing_revalidated": authorized,
            "provisioning_authorized": authorized,
            "reservation_id": "q38route-reservation-001",
            "reservation_record_path": "reservation.json",
            "reservation_record_sha256": "sha256:" + "b" * 64,
            "reservation_record_byte_size": 100,
            "preflight_record_path": "preflight.json",
            "preflight_record_sha256": "sha256:" + "c" * 64,
            "preflight_record_byte_size": 100,
            "readiness_ledger_sha256": _binding(route.READINESS_LEDGER_PATH)["sha256"],
        },
        "source_bindings": source_bindings,
        "runtime_package": _runtime_package(source_bindings),
        "resources": _resources(workers),
        "workers": workers,
    }


def _load_plan(tmp_path: Path, value: dict[str, object] | None = None) -> route.RoutePlan:
    source_root = _source_root(tmp_path)
    plan_path = tmp_path / "plan.json"
    _write_json(plan_path, _plan_value() if value is None else value)
    return route.load_plan(plan_path, source_root)


def _route_record(
    plan: route.RoutePlan,
    workers: dict[str, object],
) -> dict[str, object]:
    results = []
    for worker_plan in plan.workers:
        worker = workers[worker_plan.worker_id]
        results.append(
            {
                "worker_id": worker_plan.worker_id,
                "machine_id": worker_plan.machine_id,
                "peer_id": worker["peer_id"],
                "span": worker_plan.span,
                "source_commit": plan.source_commit,
                "manifest_digest": plan.manifest_digest,
                "artifact_bytes": worker_plan.artifact_bytes,
                "artifact_set_digest": worker_plan.artifact_set_digest,
                "cache_root": worker_plan.cache_root,
                "worker_evidence_digest": "sha256:" + hashlib.sha256(worker_plan.worker_id.encode()).hexdigest(),
            }
        )
    return {
        "schema_version": route.SCHEMA_VERSION,
        "result": "passed",
        "run_id": plan.run_id,
        "job_id": plan.route_job_id,
        "collect_action_id": route._action_id(plan, "collect_route"),
        "plan_digest": plan.plan_digest,
        "source_commit": plan.source_commit,
        "manifest_digest": plan.manifest_digest,
        "worker_plan_digest": plan.worker_plan_digest,
        "route_span": "0:64",
        "session_id": "q38route-session-001",
        "route_rpc_evidence_digest": "sha256:" + "d" * 64,
        "cleanup_ready": True,
        "worker_results": results,
    }


def _job(
    plan: route.RoutePlan,
    state: str = "absent",
    workers: dict[str, object] | None = None,
) -> dict[str, object]:
    if state == "absent":
        return {
            "state": state,
            "job_id": None,
            "collect_action_id": None,
            "run_id": None,
            "plan_digest": None,
            "source_commit": None,
            "manifest_digest": None,
            "worker_plan_digest": None,
            "evidence_digest": None,
            "route_record": None,
        }
    record = _route_record(plan, workers) if state == "passed" and workers is not None else None
    return {
        "state": state,
        "job_id": plan.route_job_id,
        "collect_action_id": route._action_id(plan, "collect_route"),
        "run_id": plan.run_id,
        "plan_digest": plan.plan_digest,
        "source_commit": plan.source_commit,
        "manifest_digest": plan.manifest_digest,
        "worker_plan_digest": plan.worker_plan_digest,
        "evidence_digest": route._canonical_digest(record) if record is not None else None,
        "route_record": record,
    }


def _observation(
    plan: route.RoutePlan,
    *,
    resource_state: str = "absent",
    worker_state: str = "absent",
    job_state: str = "absent",
    observed_at: int = 1_900_000_000,
) -> dict[str, object]:
    resources: dict[str, object] = {}
    for resource in plan.resources:
        present = resource_state == "present"
        is_instance = present and resource.kind.endswith("instance")
        instance_id = str(10_000_000 + len(resources)) if is_instance else None
        creation_timestamp = "2026-09-03T01:20:00+00:00" if is_instance else None
        generation_digest = (
            route.instance_generation_digest(
                resource.name,
                instance_id,
                creation_timestamp,
            )
            if is_instance
            else None
        )
        resources[resource.name] = {
            "present": present,
            "kind": resource.kind,
            "provider": resource.provider,
            "region": resource.region,
            "run_id": plan.run_id if present else None,
            "source_commit": plan.source_commit if present else None,
            "deadline_unix": plan.deadline_unix if present else None,
            "plan_digest": plan.plan_digest if present else None,
            "start_action_id": route._action_id(plan, "start_route") if present else None,
            "worker_id": resource.worker_id if present else None,
            "instance_id": instance_id,
            "creation_timestamp": creation_timestamp,
            "instance_generation_digest": generation_digest,
        }
    workers: dict[str, object] = {}
    for index, worker in enumerate(plan.workers):
        present = worker_state != "absent"
        workers[worker.worker_id] = {
            "state": worker_state,
            "machine_id": worker.machine_id if present else None,
            "peer_id": f"12D3KooWQwenRoutePeer{index:02d}" if worker_state == "ready" else None,
            "source_commit": plan.source_commit if present else None,
            "plan_digest": plan.plan_digest if present else None,
            "worker_plan_digest": plan.worker_plan_digest if present else None,
            "start_action_id": route._action_id(plan, "start_route") if present else None,
            "span": worker.span if present else None,
            "manifest_digest": plan.manifest_digest if present else None,
            "artifact_bytes": worker.artifact_bytes if present else None,
            "artifact_set_digest": worker.artifact_set_digest if present else None,
            "cache_root": worker.cache_root if present else None,
        }
    verifier_digest = next(
        item["sha256"] for item in plan.source_bindings if item["relative_path"] == route.VERIFIER_SOURCE_PATH
    )
    return {
        "schema_version": route.SCHEMA_VERSION,
        "run_id": plan.run_id,
        "observed_at_unix": observed_at,
        "protected_bootstrap_running": True,
        "artifact_plan_revalidation": {
            "verified_at_unix": observed_at - 1,
            "source_commit": plan.source_commit,
            "manifest_digest": plan.manifest_digest,
            "model_revision": plan.model_revision,
            "index_digest": route.EXPECTED_INDEX_DIGEST,
            "block_prefix": route.EXPECTED_BLOCK_PREFIX,
            "worker_plan_digest": plan.worker_plan_digest,
            "verifier_source_sha256": verifier_digest,
        },
        "instance_generations_digest": route.observation_instance_generations_digest(
            resources,
            plan,
        ),
        "resources": resources,
        "workers": workers,
        "route_job": _job(plan, job_state, workers),
    }


def _advance(
    operation: str,
    state: dict[str, object],
    observation: dict[str, object],
    plan: route.RoutePlan,
) -> dict[str, object]:
    return route.reconcile(
        operation,
        state,
        observation,
        plan,
        route_evidence_validated=observation["route_job"]["state"] == "passed",
        start_was_issued=not route._all_absent(observation),
    )


def test_load_plan_binds_exact_route_and_current_ledger(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)

    assert tuple(worker.span for worker in plan.workers) == tuple(route.EXPECTED_SPANS)
    assert sum(worker.artifact_bytes for worker in plan.workers) == 24_383_317_332
    assert plan.authorization["combined_cloud_ceiling_usd"] == "100.00"
    assert plan.authorization["ledger_committed_before_run_usd"] == "56.00"
    assert plan.worker_plan_digest.startswith("sha256:")
    assert plan.runtime_package["runtime_package_digest"].startswith("sha256:")
    assert route.EXPECTED_BLOCK_PREFIX == "model.language_model.layers"


def test_runtime_package_is_immutable_and_bound_to_every_action_identity(tmp_path: Path) -> None:
    original_value = _plan_value()
    changed_value = copy.deepcopy(original_value)
    changed_value["runtime_package"]["node_executable_sha256"] = "sha256:" + "8" * 64
    changed_value["runtime_package"]["runtime_package_digest"] = route._runtime_package_digest(
        changed_value["runtime_package"]
    )
    original = _load_plan(tmp_path / "original", original_value)
    changed = _load_plan(tmp_path / "changed", changed_value)

    assert original.plan_digest != changed.plan_digest
    assert original.execution_inventory_digest != changed.execution_inventory_digest
    assert route._action_id(original, "start_route") != route._action_id(changed, "start_route")
    assert (
        route.action_record(route.initial_state(changed), changed)["runtime_package"]
        == changed_value["runtime_package"]
    )
    with pytest.raises(TypeError):
        original.runtime_package["node_runtime_bytes"] = 1
    with pytest.raises(TypeError):
        original.source_bindings[0]["sha256"] = "sha256:" + "0" * 64
    with pytest.raises(TypeError):
        original.authorization["maximum_estimate_usd"] = "0.00"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.pop("runtime_package"), "plan schema"),
        (lambda value: value["runtime_package"].update(extra=True), "runtime_package schema"),
        (
            lambda value: value["runtime_package"].update(source_commit="b" * 40),
            "source commit changed",
        ),
        (
            lambda value: value["runtime_package"].update(manifest_digest="sha256:" + "0" * 64),
            "manifest binding changed",
        ),
        (
            lambda value: value["runtime_package"].update(source_bindings_digest="sha256:" + "0" * 64),
            "source bindings changed",
        ),
        (
            lambda value: value["runtime_package"].update(runtime_package_digest="sha256:" + "0" * 64),
            "record digest changed",
        ),
        (
            lambda value: value["runtime_package"].update(node_runtime_bytes=True),
            "node_runtime_bytes",
        ),
    ],
)
def test_load_plan_rejects_runtime_package_substitution(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    value = _plan_value()
    mutation(value)

    with pytest.raises(route.RouteControllerError, match=message):
        _load_plan(tmp_path, value)


def test_runtime_package_digest_domain_is_distinct() -> None:
    value = _plan_value()["runtime_package"]
    without_self = {key: item for key, item in value.items() if key != "runtime_package_digest"}

    assert value["runtime_package_digest"] == route._runtime_package_digest(value)
    assert value["runtime_package_digest"] != route._canonical_digest(without_self)
    serialized = json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n"
    assert "sha256:" + hashlib.sha256(serialized.encode()).hexdigest() != value["runtime_package_digest"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(manifest_digest="sha256:" + "0" * 64), "model binding"),
        (lambda value: value.update(model_revision="0" * 40), "model binding"),
        (
            lambda value: value["authorization"].update(ledger_committed_before_run_usd="55.00"),
            "current combined cloud ledger",
        ),
        (
            lambda value: value["authorization"].update(maximum_estimate_usd="44.01"),
            "current combined cloud ledger",
        ),
        (lambda value: value["workers"][0].update(artifact_bytes=1), "artifact plan changed"),
        (
            lambda value: value["workers"][0].update(artifact_set_digest="sha256:" + "0" * 64),
            "artifact plan changed",
        ),
        (lambda value: value["workers"].reverse(), "canonical exact route"),
        (lambda value: value["resources"].reverse(), "sorted by name"),
        (
            lambda value: value["workers"][1].update(cache_root=value["workers"][0]["cache_root"]),
            "unique cache_root",
        ),
        (
            lambda value: value["workers"][1].update(machine_id=value["workers"][0]["machine_id"]),
            "unique machine_id",
        ),
        (
            lambda value: value["workers"][0].update(instance=route.PROTECTED_INSTANCE),
            "protected bootstrap",
        ),
        (
            lambda value: value["resources"][0].update(provider="fly"),
            "one exact provider",
        ),
        (
            lambda value: value["resources"][0].update(name="foreign-disk"),
            "not run-scoped",
        ),
        (
            lambda value: [resource.update(provider="fly") for resource in value["resources"]],
            "one exact provider",
        ),
        (
            lambda value: next(
                resource for resource in value["resources"] if resource["kind"] == "iap_firewall"
            ).update(kind="firewall"),
            "resource kind inventory",
        ),
    ],
)
def test_load_plan_rejects_substitution(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    value = _plan_value()
    mutation(value)

    with pytest.raises(route.RouteControllerError, match=message):
        _load_plan(tmp_path, value)


@pytest.mark.parametrize(
    "name",
    [
        "1leading-digit",
        f"{RUN_ID}-has.dot",
        f"{RUN_ID}-has_under",
        "a" * 64,
        f"{RUN_ID}-trailing-",
    ],
)
def test_load_plan_rejects_non_rfc1035_gcp_resource_names(
    tmp_path: Path,
    name: str,
) -> None:
    value = _plan_value()
    value["resources"][0]["name"] = name

    with pytest.raises(route.RouteControllerError, match="resource name is invalid"):
        _load_plan(tmp_path, value)


def test_load_plan_rejects_missing_execution_source_binding(tmp_path: Path) -> None:
    value = _plan_value()
    value["source_bindings"] = value["source_bindings"][:-1]
    source_root = _source_root(tmp_path)
    plan_path = tmp_path / "plan.json"
    _write_json(plan_path, value)

    with pytest.raises(route.RouteControllerError, match="exact route execution sources"):
        route.load_plan(plan_path, source_root)


def test_load_plan_rejects_changed_source_binding(tmp_path: Path) -> None:
    source_root = _source_root(tmp_path)
    (source_root / route.VERIFIER_SOURCE_PATH).write_bytes(b"changed")
    plan_path = tmp_path / "plan.json"
    _write_json(plan_path, _plan_value())

    with pytest.raises(route.RouteControllerError, match="source binding"):
        route.load_plan(plan_path, source_root)


def test_load_plan_rejects_changed_gcp_adapter_binding(tmp_path: Path) -> None:
    source_root = _source_root(tmp_path)
    (source_root / route.GCP_ADAPTER_SOURCE_PATH).write_bytes(b"changed")
    plan_path = tmp_path / "plan.json"
    _write_json(plan_path, _plan_value())

    with pytest.raises(route.RouteControllerError, match="source binding"):
        route.load_plan(plan_path, source_root)


def test_load_plan_rejects_duplicate_json_field(tmp_path: Path) -> None:
    source_root = _source_root(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text('{"schema_version":1,"schema_version":1}\n', encoding="utf-8")

    with pytest.raises(route.RouteControllerError, match="duplicate JSON field"):
        route.load_plan(plan_path, source_root)


def test_unauthorized_plan_loads_but_start_fails_closed(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path, _plan_value(authorized=False))
    observation = _observation(plan)

    with pytest.raises(route.RouteControllerError, match="not authorized"):
        _advance("start", route.initial_state(plan), observation, plan)


def test_initial_start_emits_one_durable_exact_action(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)
    observation = _observation(plan)

    started = _advance("start", route.initial_state(plan), observation, plan)
    repeated = _advance("start", started, observation, plan)
    first_action = route.action_record(started, plan)
    repeated_action = route.action_record(repeated, plan)

    assert started["phase"] == "STARTING"
    assert started["next_action"] == "start_route"
    assert repeated == started
    assert first_action == repeated_action
    assert first_action["action_id"].startswith("sha256:")
    assert first_action["worker_plan_digest"] == plan.worker_plan_digest
    assert len(first_action["resources"]) == 12
    assert first_action["resource_specs"] == [route._expected_resource_spec(resource) for resource in plan.resources]
    worker_spec = next(spec for spec in first_action["resource_specs"] if spec["kind"] == "worker_instance")
    bootstrap_spec = next(spec for spec in first_action["resource_specs"] if spec["kind"] == "bootstrap_instance")
    iap_spec = next(spec for spec in first_action["resource_specs"] if spec["kind"] == "iap_firewall")
    assert iap_spec["resource_name"] == f"{RUN_ID}-iap-firewall"
    assert iap_spec["network"] == route.EXPECTED_NETWORK
    assert worker_spec["machine_type"] == "g2-standard-8"
    assert worker_spec["accelerator_type"] == "nvidia-l4"
    assert worker_spec["accelerator_count"] == 1
    assert bootstrap_spec["machine_type"] == "e2-standard-2"
    assert bootstrap_spec["accelerator_type"] == "none"
    assert bootstrap_spec["accelerator_count"] == 0


def test_start_reattaches_complete_ready_route_without_recreating(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)
    observation = _observation(plan, resource_state="present", worker_state="ready")

    state = _advance("start", route.initial_state(plan), observation, plan)

    assert state["phase"] == "READY"
    assert state["next_action"] == "none"


def test_start_reattaches_starting_route_without_recreating(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)
    observation = _observation(plan, resource_state="present", worker_state="starting")

    state = _advance("start", route.initial_state(plan), observation, plan)

    assert state["phase"] == "STARTING"
    assert state["next_action"] == "none"


def test_partial_reattach_cleans_instead_of_starting(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)
    observation = _observation(plan, resource_state="present", worker_state="starting")
    first_resource = next(iter(observation["resources"].values()))
    first_resource.update(
        present=False,
        run_id=None,
        source_commit=None,
        deadline_unix=None,
        plan_digest=None,
        start_action_id=None,
        worker_id=None,
    )

    state = _advance("start", route.initial_state(plan), observation, plan)

    assert state["phase"] == "CLEANING"
    assert state["failure_code"] == "partial-reattach"
    assert state["next_action"] == "cleanup_route"


def test_starting_acknowledgement_clears_pending_action(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)
    started = _advance("start", route.initial_state(plan), _observation(plan), plan)
    observation = _observation(plan, resource_state="present", worker_state="starting")

    acknowledged = _advance("status", started, observation, plan)

    assert acknowledged["phase"] == "STARTING"
    assert acknowledged["next_action"] == "none"
    assert acknowledged["revision"] == started["revision"] + 1


def test_starting_becomes_ready_only_with_all_exact_workers(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)
    started = _advance("start", route.initial_state(plan), _observation(plan), plan)
    observation = _observation(plan, resource_state="present", worker_state="ready")

    ready = _advance("status", started, observation, plan)

    assert ready["phase"] == "READY"
    assert ready["next_action"] == "none"


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("manifest_digest", "sha256:" + "0" * 64),
        ("worker_plan_digest", "sha256:" + "0" * 64),
        ("index_digest", "sha256:" + "0" * 64),
        ("source_commit", "0" * 40),
        ("block_prefix", "layers"),
        ("verifier_source_sha256", "sha256:" + "0" * 64),
    ],
)
def test_observation_rejects_stale_production_plan_revalidation(
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    plan = _load_plan(tmp_path)
    observation = _observation(plan)
    observation["artifact_plan_revalidation"][field] = replacement

    with pytest.raises(route.RouteControllerError, match="plan revalidation"):
        route.validate_observation(observation, plan)


def test_observation_rejects_future_plan_revalidation(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)
    observation = _observation(plan)
    observation["artifact_plan_revalidation"]["verified_at_unix"] = observation["observed_at_unix"] + 1

    with pytest.raises(route.RouteControllerError, match="plan revalidation"):
        route.validate_observation(observation, plan)


@pytest.mark.parametrize("field", ["kind", "provider", "region"])
def test_observation_rejects_foreign_resource_identity(tmp_path: Path, field: str) -> None:
    plan = _load_plan(tmp_path)
    observation = _observation(plan)
    first = next(iter(observation["resources"].values()))
    first[field] = "foreign"

    with pytest.raises(route.RouteControllerError, match="resource identity"):
        route.validate_observation(observation, plan)


def test_observation_rejects_worker_span_substitution_before_collection(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)
    observation = _observation(plan, resource_state="present", worker_state="ready")
    worker = next(iter(observation["workers"].values()))
    worker["span"] = "16:32"

    with pytest.raises(route.RouteControllerError, match="worker binding"):
        route.validate_observation(observation, plan)


def test_observation_rejects_duplicate_ready_peer(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)
    observation = _observation(plan, resource_state="present", worker_state="ready")
    values = list(observation["workers"].values())
    values[1]["peer_id"] = values[0]["peer_id"]

    with pytest.raises(route.RouteControllerError, match="unique peer"):
        route.validate_observation(observation, plan)


def test_collect_action_is_durable_until_job_acknowledges(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)
    ready = _advance(
        "start",
        route.initial_state(plan),
        _observation(plan, resource_state="present", worker_state="ready"),
        plan,
    )
    observation = _observation(plan, resource_state="present", worker_state="ready")

    collecting = _advance("collect", ready, observation, plan)
    repeated = _advance("collect", collecting, observation, plan)

    assert collecting["phase"] == "COLLECTING"
    assert collecting["next_action"] == "collect_route"
    assert repeated == collecting
    assert route.action_record(repeated, plan)["action_id"] == route.action_record(collecting, plan)["action_id"]


def test_running_job_acknowledges_collect_action(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)
    ready = _advance(
        "start",
        route.initial_state(plan),
        _observation(plan, resource_state="present", worker_state="ready"),
        plan,
    )
    collecting = _advance(
        "collect",
        ready,
        _observation(plan, resource_state="present", worker_state="ready"),
        plan,
    )

    acknowledged = _advance(
        "status",
        collecting,
        _observation(plan, resource_state="present", worker_state="ready", job_state="running"),
        plan,
    )

    assert acknowledged["phase"] == "COLLECTING"
    assert acknowledged["next_action"] == "none"


def test_passed_job_binds_evidence_then_requires_cleanup(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)
    ready = _advance(
        "start",
        route.initial_state(plan),
        _observation(plan, resource_state="present", worker_state="ready"),
        plan,
    )
    collecting = _advance(
        "collect",
        ready,
        _observation(plan, resource_state="present", worker_state="ready"),
        plan,
    )

    observation = _observation(
        plan,
        resource_state="present",
        worker_state="ready",
        job_state="passed",
    )
    cleaning = _advance("status", collecting, observation, plan)

    assert cleaning["phase"] == "CLEANING"
    assert cleaning["evidence_digest"] == observation["route_job"]["evidence_digest"]
    assert cleaning["next_action"] == "cleanup_route"


@pytest.mark.parametrize("field", ["run_id", "plan_digest", "source_commit", "manifest_digest", "worker_plan_digest"])
def test_route_job_rejects_wrong_binding(tmp_path: Path, field: str) -> None:
    plan = _load_plan(tmp_path)
    observation = _observation(plan, resource_state="present", worker_state="ready", job_state="passed")
    observation["route_job"][field] = "sha256:" + "0" * 64 if "digest" in field else "0" * 40

    with pytest.raises(route.RouteControllerError, match="route job binding"):
        route.validate_observation(observation, plan)


def test_failed_job_forces_cleanup(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)
    state = route.initial_state(plan)
    observation = _observation(plan, resource_state="present", worker_state="ready", job_state="failed")
    state.update(
        phase="COLLECTING",
        revision=2,
        instance_generations_digest=observation["instance_generations_digest"],
    )

    cleaning = _advance("status", state, observation, plan)

    assert cleaning["phase"] == "CLEANING"
    assert cleaning["failure_code"] == "qualification-failed"


def test_cleanup_retries_same_action_until_all_resources_absent(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)
    state = route.initial_state(plan)
    state.update(phase="CLEANING", failure_code="route-failed", next_action="cleanup_route", revision=3)
    observation = _observation(plan, resource_state="present", worker_state="failed")

    repeated = _advance("cleanup", state, observation, plan)

    assert repeated == state
    assert route.action_record(repeated, plan)["action_id"] == route.action_record(state, plan)["action_id"]


def test_cleanup_becomes_passing_terminal_only_after_absence_proof(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)
    state = route.initial_state(plan)
    state.update(
        phase="CLEANING",
        evidence_digest="sha256:" + "e" * 64,
        next_action="cleanup_route",
        revision=4,
    )

    terminal = _advance("cleanup", state, _observation(plan), plan)

    assert terminal["phase"] == "CLEANED_PASS"
    assert terminal["cleanup_verified"] is True
    assert terminal["next_action"] == "none"


def test_cleanup_without_evidence_is_terminal_failure(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)
    state = route.initial_state(plan)
    state.update(phase="CLEANING", failure_code="route-failed", next_action="cleanup_route", revision=2)

    terminal = _advance("cleanup", state, _observation(plan), plan)

    assert terminal["phase"] == "CLEANED_FAILURE"
    assert terminal["failure_code"] == "route-failed"
    assert terminal["cleanup_verified"] is True


def test_deadline_forces_cleanup_then_terminal_failure(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)
    expired_present = _observation(
        plan,
        resource_state="present",
        worker_state="starting",
        observed_at=plan.deadline_unix,
    )

    cleaning = _advance("status", route.initial_state(plan), expired_present, plan)
    terminal = _advance(
        "cleanup",
        cleaning,
        _observation(plan, observed_at=plan.deadline_unix + 1),
        plan,
    )

    assert cleaning["phase"] == "CLEANING"
    assert cleaning["failure_code"] == "run-expired"
    assert terminal["phase"] == "CLEANED_FAILURE"
    assert terminal["cleanup_verified"] is True


def test_terminal_state_retains_latched_generation_after_cleanup(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)
    present = _observation(plan, resource_state="present", worker_state="starting")
    state = route.initial_state(plan)
    state.update(
        phase="CLEANED_FAILURE",
        failure_code="route-failed",
        cleanup_verified=True,
        instance_generations_digest=present["instance_generations_digest"],
        revision=5,
    )

    assert _advance("status", state, _observation(plan), plan) == state


def test_terminal_state_rejects_resource_reappearance(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)
    state = route.initial_state(plan)
    state.update(
        phase="CLEANED_FAILURE",
        failure_code="route-failed",
        cleanup_verified=True,
        revision=5,
    )

    with pytest.raises(route.RouteControllerError, match="returned after terminal"):
        _advance(
            "status",
            state,
            _observation(plan, resource_state="present", worker_state="starting"),
            plan,
        )


def test_cli_recovers_identical_pending_action_after_decision_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = _source_root(tmp_path)
    plan_path = tmp_path / "plan.json"
    _write_json(plan_path, _plan_value())
    plan = route.load_plan(plan_path, source_root)
    observation_path = tmp_path / "observation.json"
    observation = _observation(plan)
    _write_json(observation_path, observation)
    state_path = tmp_path / "state.json"
    decision_path = tmp_path / "decision.json"
    original = route._atomic_json
    calls = 0

    def fail_final_decision(path: Path, value: dict[str, object]) -> None:
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError("injected final decision write failure")
        original(path, value)

    monkeypatch.setattr(route.time, "time", lambda: 1_900_000_000)
    monkeypatch.setattr(route, "revalidate_authorization_evidence", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        route,
        "revalidate_production_artifact_plan",
        lambda *args, **kwargs: observation["artifact_plan_revalidation"],
    )
    monkeypatch.setattr(route, "_atomic_json", fail_final_decision)
    argv = [
        "start",
        "--plan",
        os.fspath(plan_path),
        "--source-root",
        os.fspath(source_root),
        "--observation",
        os.fspath(observation_path),
        "--manifest",
        os.fspath(tmp_path / "manifest.json"),
        "--artifact-root",
        os.fspath(tmp_path / "artifacts"),
        "--authorization-root",
        os.fspath(tmp_path / "authorization"),
        "--state",
        os.fspath(state_path),
        "--decision",
        os.fspath(decision_path),
    ]
    assert route.main(argv) == 2
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    tombstone = json.loads(decision_path.read_text(encoding="utf-8"))
    assert persisted["next_action"] == "start_route"
    assert tombstone["action"] == "none"
    assert tombstone["action_id"] is None

    monkeypatch.setattr(route, "_atomic_json", original)
    assert route.main(argv) == 0
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    assert decision["action"] == "start_route"
    assert decision["revision"] == persisted["revision"]


def test_cli_rejects_overlapping_input_output_paths(tmp_path: Path) -> None:
    source_root = _source_root(tmp_path)
    plan_path = tmp_path / "plan.json"
    _write_json(plan_path, _plan_value())
    plan = route.load_plan(plan_path, source_root)
    observation_path = tmp_path / "observation.json"
    _write_json(observation_path, _observation(plan))

    result = route.main(
        [
            "start",
            "--plan",
            os.fspath(plan_path),
            "--source-root",
            os.fspath(source_root),
            "--observation",
            os.fspath(observation_path),
            "--state",
            os.fspath(plan_path),
            "--decision",
            os.fspath(tmp_path / "decision.json"),
        ]
    )

    assert result == 2


def test_atomic_output_rejects_symlink_target(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}\n", encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable for this identity")

    with pytest.raises(route.RouteControllerError, match="output target is unsafe"):
        route._atomic_json(link, {"ok": True})


def test_authorized_plan_rejects_zero_estimate(tmp_path: Path) -> None:
    value = _plan_value()
    value["authorization"]["maximum_estimate_usd"] = "0.00"

    with pytest.raises(route.RouteControllerError, match="positive bounded estimate"):
        _load_plan(tmp_path, value)


@pytest.mark.parametrize("field", ["reservation_record_sha256", "preflight_record_sha256"])
def test_plan_rejects_unbound_authorization_evidence(tmp_path: Path, field: str) -> None:
    value = _plan_value()
    value["authorization"][field] = "sha256:" + "x" * 64

    with pytest.raises(route.RouteControllerError, match=field):
        _load_plan(tmp_path, value)


def test_start_rejects_stale_production_plan_revalidation(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)
    observation = _observation(plan)
    observation["artifact_plan_revalidation"]["verified_at_unix"] = (
        observation["observed_at_unix"] - route.MAX_PLAN_REVALIDATION_AGE_SECONDS - 1
    )

    with pytest.raises(route.RouteControllerError, match="artifact plan is stale"):
        _advance("start", route.initial_state(plan), observation, plan)


def test_cleanup_allows_stale_plan_revalidation_so_teardown_cannot_be_blocked(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)
    state = route.initial_state(plan)
    state.update(phase="CLEANING", failure_code="route-failed", next_action="cleanup_route", revision=2)
    observation = _observation(plan, resource_state="present", worker_state="failed")
    observation["artifact_plan_revalidation"]["verified_at_unix"] = 1

    repeated = _advance("cleanup", state, observation, plan)

    assert repeated == state


@pytest.mark.parametrize("field", ["source_commit", "plan_digest", "worker_plan_digest"])
def test_observation_rejects_worker_execution_identity_substitution(
    tmp_path: Path,
    field: str,
) -> None:
    plan = _load_plan(tmp_path)
    observation = _observation(plan, resource_state="present", worker_state="ready")
    first = next(iter(observation["workers"].values()))
    first[field] = "sha256:" + "0" * 64 if "digest" in field else "0" * 40

    with pytest.raises(route.RouteControllerError, match="worker binding"):
        route.validate_observation(observation, plan)


def test_observation_rejects_resource_plan_substitution(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)
    observation = _observation(plan, resource_state="present", worker_state="starting")
    first = next(iter(observation["resources"].values()))
    first["plan_digest"] = "sha256:" + "0" * 64

    with pytest.raises(route.RouteControllerError, match="resource binding"):
        route.validate_observation(observation, plan)


def test_state_rejects_evidence_before_verified_job_result(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)
    state = route.initial_state(plan)
    state["evidence_digest"] = "sha256:" + "e" * 64

    with pytest.raises(route.RouteControllerError, match="evidence state"):
        route.validate_state(state, plan)


def _write_authorization_records(
    tmp_path: Path,
    plan_value: dict[str, object],
    *,
    checked_at: int = 1_899_999_999,
) -> Path:
    root = tmp_path / "authorization"
    authorization = plan_value["authorization"]
    preview_source_root = _source_root(tmp_path)
    preview_path = tmp_path / "authorization-plan-preview.json"
    _write_json(preview_path, plan_value)
    preview_plan = route.load_plan(preview_path, preview_source_root)
    resource_specs = [route._expected_resource_spec(resource) for resource in preview_plan.resources]
    resource_costs = []
    for resource, spec in zip(preview_plan.resources, resource_specs):
        if resource.kind == "worker_instance":
            unit_rate, maximum = "0.80", "8.80"
        elif resource.kind == "bootstrap_instance":
            unit_rate, maximum = "0.40", "4.40"
        elif resource.kind in {"bootstrap_disk", "worker_disk"}:
            unit_rate, maximum = "0.08", "0.88"
        else:
            unit_rate, maximum = "0.00", "0.00"
        resource_costs.append(
            {
                "resource_name": resource.name,
                "resource_spec_digest": route._canonical_digest(spec),
                "unit_rate_usd": unit_rate,
                "quantity": "1.00",
                "duration_hours": "11.00",
                "maximum_usd": maximum,
            }
        )
    reservation = {
        "schema_version": route.SCHEMA_VERSION,
        "reservation_id": authorization["reservation_id"],
        "run_id": plan_value["run_id"],
        "combined_cloud_ceiling_usd": authorization["combined_cloud_ceiling_usd"],
        "ledger_committed_before_run_usd": authorization["ledger_committed_before_run_usd"],
        "maximum_estimate_usd": authorization["maximum_estimate_usd"],
        "deadline_unix": plan_value["deadline_unix"],
        "plan_digest": preview_plan.plan_digest,
        "execution_inventory_digest": preview_plan.execution_inventory_digest,
        "worker_plan_digest": preview_plan.worker_plan_digest,
        "resource_costs": resource_costs,
        "readiness_ledger_sha256": authorization["readiness_ledger_sha256"],
        "recorded_at_unix": checked_at - 10,
        "expires_at_unix": plan_value["deadline_unix"],
        "reservation_recorded": True,
    }
    reservation_path = root / authorization["reservation_record_path"]
    _write_json(reservation_path, reservation)
    reservation_payload = reservation_path.read_bytes()
    authorization["reservation_record_byte_size"] = len(reservation_payload)
    authorization["reservation_record_sha256"] = "sha256:" + hashlib.sha256(reservation_payload).hexdigest()
    preflight = {
        "schema_version": route.SCHEMA_VERSION,
        "run_id": plan_value["run_id"],
        "source_commit": plan_value["source_commit"],
        "plan_digest": preview_plan.plan_digest,
        "execution_inventory_digest": preview_plan.execution_inventory_digest,
        "worker_plan_digest": preview_plan.worker_plan_digest,
        "provider": "gcp",
        "resource_names": [resource.name for resource in preview_plan.resources],
        "resource_specs": resource_specs,
        "pricing_source": "gcp-catalog",
        "pricing_currency": "USD",
        "pricing_checked_at_unix": checked_at,
        "gpu_quota_limit": 4,
        "gpu_quota_usage": 0,
        "required_gpu_count": 4,
        "checked_at_unix": checked_at,
        "native_auth_revalidated": True,
        "inventory_revalidated": True,
        "pricing_revalidated": True,
        "provisioning_authorized": True,
        "protected_bootstrap_running": True,
        "reservation_record_sha256": authorization["reservation_record_sha256"],
    }
    preflight_path = root / authorization["preflight_record_path"]
    _write_json(preflight_path, preflight)
    preflight_payload = preflight_path.read_bytes()
    authorization["preflight_record_byte_size"] = len(preflight_payload)
    authorization["preflight_record_sha256"] = "sha256:" + hashlib.sha256(preflight_payload).hexdigest()
    return root


def test_authorization_revalidation_opens_exact_fresh_records(tmp_path: Path) -> None:
    value = _plan_value()
    authorization_root = _write_authorization_records(tmp_path, value)
    plan = _load_plan(tmp_path, value)

    route.revalidate_authorization_evidence(
        plan,
        authorization_root,
        now_unix=1_900_000_000,
    )


def test_authorization_revalidation_rejects_record_mutation(tmp_path: Path) -> None:
    value = _plan_value()
    authorization_root = _write_authorization_records(tmp_path, value)
    plan = _load_plan(tmp_path, value)
    reservation_path = authorization_root / value["authorization"]["reservation_record_path"]
    reservation_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(route.RouteControllerError, match="record size changed"):
        route.revalidate_authorization_evidence(
            plan,
            authorization_root,
            now_unix=1_900_000_000,
        )


def test_authorization_revalidation_rejects_stale_preflight(tmp_path: Path) -> None:
    value = _plan_value()
    authorization_root = _write_authorization_records(
        tmp_path,
        value,
        checked_at=1_899_999_000,
    )
    plan = _load_plan(tmp_path, value)

    with pytest.raises(route.RouteControllerError, match="preflight record"):
        route.revalidate_authorization_evidence(
            plan,
            authorization_root,
            now_unix=1_900_000_000,
        )


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("route_span",), "0:63"),
        (("cleanup_ready",), False),
        (("worker_results", 0, "span"), "16:32"),
        (("worker_results", 0, "peer_id"), "12D3KooWSubstitutedPeer000"),
    ],
)
def test_passed_route_record_rejects_substitution(
    tmp_path: Path,
    path: tuple[object, ...],
    replacement: object,
) -> None:
    plan = _load_plan(tmp_path)
    observation = _observation(
        plan,
        resource_state="present",
        worker_state="ready",
        job_state="passed",
    )
    target = observation["route_job"]["route_record"]
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = replacement
    observation["route_job"]["evidence_digest"] = route._canonical_digest(observation["route_job"]["route_record"])

    with pytest.raises(route.RouteControllerError, match="route record"):
        route.validate_observation(observation, plan)


def test_passed_route_record_digest_rejects_mutation(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)
    observation = _observation(
        plan,
        resource_state="present",
        worker_state="ready",
        job_state="passed",
    )
    observation["route_job"]["route_record"]["session_id"] = "q38route-session-002"

    with pytest.raises(route.RouteControllerError, match="evidence digest"):
        route.validate_observation(observation, plan)


def test_cleanup_cannot_be_blocked_by_stale_passed_job_or_bootstrap_loss(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)
    state = route.initial_state(plan)
    state.update(
        phase="CLEANING",
        evidence_digest="sha256:" + "e" * 64,
        next_action="cleanup_route",
        revision=3,
    )
    observation = _observation(
        plan,
        resource_state="present",
        worker_state="ready",
        job_state="passed",
    )
    observation["artifact_plan_revalidation"]["verified_at_unix"] = 1
    observation["protected_bootstrap_running"] = False

    cleaning = _advance("cleanup", state, observation, plan)

    assert cleaning["phase"] == "CLEANING"
    assert cleaning["failure_code"] == "protected-bootstrap-lost"
    assert cleaning["next_action"] == "cleanup_route"


def test_cli_rejects_output_inside_artifact_root(tmp_path: Path) -> None:
    source_root = _source_root(tmp_path)
    plan_path = tmp_path / "plan.json"
    _write_json(plan_path, _plan_value())
    plan = route.load_plan(plan_path, source_root)
    observation_path = tmp_path / "observation.json"
    _write_json(observation_path, _observation(plan))
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()

    result = route.main(
        [
            "start",
            "--plan",
            os.fspath(plan_path),
            "--source-root",
            os.fspath(source_root),
            "--observation",
            os.fspath(observation_path),
            "--manifest",
            os.fspath(tmp_path / "manifest.json"),
            "--artifact-root",
            os.fspath(artifact_root),
            "--state",
            os.fspath(artifact_root / "state.json"),
            "--decision",
            os.fspath(tmp_path / "decision.json"),
        ]
    )

    assert result == 2


def test_production_revalidation_uses_source_bound_module_and_exact_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from drift.model_manifest import ModelManifest, select_manifest_block_artifacts

    revision = "1" * 40
    artifact_root = tmp_path / "metadata"
    artifact_root.mkdir()
    config_payload = b"{}"
    weight_map = {}
    shard_names = [f"span-{index}.safetensors" for index in range(4)]
    for block in range(64):
        weight_map[f"model.language_model.layers.{block}.weight"] = shard_names[block // 16]
    index_payload = json.dumps({"weight_map": weight_map}, sort_keys=True).encode()
    (artifact_root / "config.json").write_bytes(config_payload)
    (artifact_root / "model.safetensors.index.json").write_bytes(index_payload)
    artifacts = [
        {
            "role": "config",
            "path": "config.json",
            "sha256": hashlib.sha256(config_payload).hexdigest(),
            "size": len(config_payload),
        },
        {
            "role": "weight_index",
            "path": "model.safetensors.index.json",
            "sha256": hashlib.sha256(index_payload).hexdigest(),
            "size": len(index_payload),
        },
        {
            "role": "tokenizer",
            "path": "tokenizer.json",
            "sha256": hashlib.sha256(b"tokenizer").hexdigest(),
            "size": len(b"tokenizer"),
        },
    ]
    for name in shard_names:
        artifacts.append(
            {
                "role": "weight",
                "path": name,
                "sha256": hashlib.sha256(name.encode()).hexdigest(),
                "size": 1,
            }
        )
    manifest_value = {
        "schema_version": 1,
        "name": "Synthetic Qwen route",
        "aliases": [],
        "source": {"repository": "example/synthetic", "revision": revision},
        "model": {
            "architecture": "SyntheticForCausalLM",
            "num_blocks": 64,
            "context_length": 1024,
            "license": "apache-2.0",
            "gated": False,
        },
        "runtime": {
            "implementation": "drift",
            "minimum_version": "2.3.0.dev0",
            "maximum_version_exclusive": "2.4.0",
            "protocol_version": 1,
            "tensor_schema": "hidden-states-v1",
            "attention_implementation": "eager",
            "dtype": "bfloat16",
            "quantization": "none",
            "adapter_profile": "none",
        },
        "artifacts": artifacts,
    }
    manifest = ModelManifest.from_dict(manifest_value)
    manifest_path = tmp_path / "manifest.json"
    _write_json(manifest_path, manifest_value)
    expected_spans = {}
    for index, span in enumerate(("0:16", "16:32", "32:48", "48:64")):
        start, end = map(int, span.split(":"))
        derived = select_manifest_block_artifacts(
            manifest,
            block_prefix="model.language_model.layers",
            start_block=start,
            end_block=end,
            weight_map=weight_map,
        )
        expected_spans[span] = (
            derived.artifact_bytes,
            "sha256:" + derived.artifact_set_digest,
        )
        assert derived.artifact_paths[-1] == shard_names[index]

    monkeypatch.setattr(route, "EXPECTED_MANIFEST_DIGEST", manifest.digest_id)
    monkeypatch.setattr(route, "EXPECTED_MODEL_REVISION", revision)
    monkeypatch.setattr(
        route,
        "EXPECTED_INDEX_DIGEST",
        "sha256:" + hashlib.sha256(index_payload).hexdigest(),
    )
    monkeypatch.setattr(route, "EXPECTED_SPANS", expected_spans)
    monkeypatch.setattr(route, "EXPECTED_ARTIFACTS_PER_SPAN", 3)
    source_root = Path(route.__file__).resolve().parents[1]
    value = _plan_value()
    source_bindings = []
    for relative_path in sorted(route.REQUIRED_SOURCE_PATHS):
        payload = (source_root / relative_path).read_bytes()
        source_bindings.append(
            {
                "relative_path": relative_path,
                "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "byte_size": len(payload),
            }
        )
    value["source_bindings"] = source_bindings
    value["runtime_package"]["source_bindings_digest"] = route._source_bindings_digest(source_bindings)
    value["runtime_package"]["runtime_package_digest"] = route._runtime_package_digest(value["runtime_package"])
    value["authorization"]["readiness_ledger_sha256"] = next(
        binding["sha256"] for binding in source_bindings if binding["relative_path"] == route.READINESS_LEDGER_PATH
    )
    plan_path = tmp_path / "synthetic-plan.json"
    _write_json(plan_path, value)
    plan = route.load_plan(plan_path, source_root)

    record = route.revalidate_production_artifact_plan(
        plan,
        manifest_path,
        artifact_root,
        source_root,
        verified_at_unix=1_900_000_000,
    )

    assert record["worker_plan_digest"] == plan.worker_plan_digest
    assert record["verifier_source_sha256"] == next(
        binding["sha256"] for binding in source_bindings if binding["relative_path"] == route.VERIFIER_SOURCE_PATH
    )

    (artifact_root / "model.safetensors.index.json").write_bytes(b"tampered")
    with pytest.raises(route.RouteControllerError, match="could not be revalidated"):
        route.revalidate_production_artifact_plan(
            plan,
            manifest_path,
            artifact_root,
            source_root,
            verified_at_unix=1_900_000_000,
        )


def _rebind_authorization_record(
    plan_value: dict[str, object],
    authorization_root: Path,
    record_name: str,
) -> None:
    authorization = plan_value["authorization"]
    record_path = authorization_root / authorization[f"{record_name}_record_path"]
    payload = record_path.read_bytes()
    authorization[f"{record_name}_record_byte_size"] = len(payload)
    authorization[f"{record_name}_record_sha256"] = "sha256:" + hashlib.sha256(payload).hexdigest()


def test_trusted_time_rejects_stale_observation(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)
    observation = _observation(plan, observed_at=1_900_000_000)

    with pytest.raises(route.RouteControllerError, match="stale"):
        route.reconcile(
            "status",
            route.initial_state(plan),
            observation,
            plan,
            now_unix=1_900_000_000 + route.MAX_PLAN_REVALIDATION_AGE_SECONDS + 1,
        )


def test_trusted_time_forces_cleanup_even_when_observation_predates_deadline(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)
    observation = _observation(
        plan,
        resource_state="present",
        worker_state="starting",
        observed_at=1_900_000_000,
    )

    cleaning = route.reconcile(
        "status",
        route.initial_state(plan),
        observation,
        plan,
        now_unix=plan.deadline_unix,
    )

    assert cleaning["phase"] == "CLEANING"
    assert cleaning["failure_code"] == "run-expired"
    assert cleaning["next_action"] == "cleanup_route"


def test_state_loss_recovers_exact_passed_evidence_before_cleanup(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)
    observation = _observation(
        plan,
        resource_state="present",
        worker_state="ready",
        job_state="passed",
    )

    cleaning = route.reconcile(
        "status",
        route.initial_state(plan),
        observation,
        plan,
        now_unix=observation["observed_at_unix"],
        route_evidence_validated=True,
        start_was_issued=True,
    )

    assert cleaning["phase"] == "CLEANING"
    assert cleaning["failure_code"] is None
    assert cleaning["evidence_digest"] == observation["route_job"]["evidence_digest"]
    assert cleaning["next_action"] == "cleanup_route"


def test_plan_rejects_readiness_ledger_binding_mismatch(tmp_path: Path) -> None:
    value = _plan_value()
    value["authorization"]["readiness_ledger_sha256"] = "sha256:" + "0" * 64

    with pytest.raises(route.RouteControllerError, match="bound to the readiness ledger"):
        _load_plan(tmp_path, value)


def test_stable_plan_digest_excludes_only_record_self_bindings() -> None:
    value = _plan_value()
    baseline = route._stable_plan_digest(value)
    rebound = copy.deepcopy(value)
    rebound["authorization"]["reservation_record_sha256"] = "sha256:" + "0" * 64
    rebound["authorization"]["reservation_record_byte_size"] = 999
    rebound["authorization"]["preflight_record_sha256"] = "sha256:" + "1" * 64
    rebound["authorization"]["preflight_record_byte_size"] = 1_000
    assert route._stable_plan_digest(rebound) == baseline

    rebound["source_bindings"][0]["sha256"] = "sha256:" + "2" * 64
    assert route._stable_plan_digest(rebound) != baseline


@pytest.mark.parametrize("field", ["source_bindings", "plan_digest"])
def test_authorization_revalidation_binds_exact_source_and_plan_identity(
    tmp_path: Path,
    field: str,
) -> None:
    value = _plan_value()
    authorization_root = _write_authorization_records(tmp_path, value)
    plan = _load_plan(tmp_path, value)
    if field == "source_bindings":
        bindings = [dict(binding) for binding in plan.source_bindings]
        target = next(binding for binding in bindings if binding["relative_path"] == "src/drift/server/server.py")
        target["sha256"] = "sha256:" + "0" * 64
        substituted = replace(plan, source_bindings=tuple(bindings))
    else:
        substituted = replace(plan, plan_digest="sha256:" + "0" * 64)

    assert substituted.execution_inventory_digest != plan.execution_inventory_digest
    with pytest.raises(route.RouteControllerError, match="reservation record"):
        route.revalidate_authorization_evidence(
            substituted,
            authorization_root,
            now_unix=1_900_000_000,
        )


def test_authorization_revalidation_rejects_insufficient_gpu_quota(tmp_path: Path) -> None:
    value = _plan_value()
    authorization_root = _write_authorization_records(tmp_path, value)
    preflight_path = authorization_root / value["authorization"]["preflight_record_path"]
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    preflight["gpu_quota_limit"] = 1
    _write_json(preflight_path, preflight)
    _rebind_authorization_record(value, authorization_root, "preflight")
    plan = _load_plan(tmp_path, value)

    with pytest.raises(route.RouteControllerError, match="provider preflight record"):
        route.revalidate_authorization_evidence(
            plan,
            authorization_root,
            now_unix=1_900_000_000,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda record: record["resource_costs"][0].update(maximum_usd="43.99"),
            "resource cost was not recomputed",
        ),
        (
            lambda record: record["resource_costs"][0].update(
                resource_name=record["resource_costs"][1]["resource_name"]
            ),
            "reservation cost inventory is not exact",
        ),
    ],
)
def test_authorization_revalidation_rejects_cost_substitution(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    value = _plan_value()
    authorization_root = _write_authorization_records(tmp_path, value)
    reservation_path = authorization_root / value["authorization"]["reservation_record_path"]
    reservation = json.loads(reservation_path.read_text(encoding="utf-8"))
    mutation(reservation)
    _write_json(reservation_path, reservation)
    _rebind_authorization_record(value, authorization_root, "reservation")

    preflight_path = authorization_root / value["authorization"]["preflight_record_path"]
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    preflight["reservation_record_sha256"] = value["authorization"]["reservation_record_sha256"]
    _write_json(preflight_path, preflight)
    _rebind_authorization_record(value, authorization_root, "preflight")
    plan = _load_plan(tmp_path, value)

    with pytest.raises(route.RouteControllerError, match=message):
        route.revalidate_authorization_evidence(
            plan,
            authorization_root,
            now_unix=1_900_000_000,
        )


def test_authorization_rejects_pricing_shorter_or_longer_than_resource_lifetime(
    tmp_path: Path,
) -> None:
    value = _plan_value()
    authorization_root = _write_authorization_records(tmp_path, value)
    reservation_path = authorization_root / value["authorization"]["reservation_record_path"]
    reservation = json.loads(reservation_path.read_text(encoding="utf-8"))
    cost = next(item for item in reservation["resource_costs"] if item["resource_name"] == f"{RUN_ID}-worker-0")
    cost["duration_hours"] = "24.00"
    cost["maximum_usd"] = "19.20"
    _write_json(reservation_path, reservation)
    _rebind_authorization_record(value, authorization_root, "reservation")

    preflight_path = authorization_root / value["authorization"]["preflight_record_path"]
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    preflight["reservation_record_sha256"] = value["authorization"]["reservation_record_sha256"]
    _write_json(preflight_path, preflight)
    _rebind_authorization_record(value, authorization_root, "preflight")
    plan = _load_plan(tmp_path, value)

    with pytest.raises(route.RouteControllerError, match="pricing horizon"):
        route.revalidate_authorization_evidence(
            plan,
            authorization_root,
            now_unix=1_900_000_000,
        )


def test_controller_state_lock_is_exclusive_and_reusable(tmp_path: Path) -> None:
    lock_path = tmp_path / ".route-state.json.lock"

    with route._controller_lock(lock_path):
        with pytest.raises(route.RouteControllerError, match="another controller invocation"):
            with route._controller_lock(lock_path):
                pytest.fail("contended invocation acquired the state lock")

    with route._controller_lock(lock_path):
        assert lock_path.is_file()


def test_terminal_pass_alerts_if_protected_bootstrap_is_lost(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)
    state = route.initial_state(plan)
    state.update(
        phase="CLEANED_PASS",
        evidence_digest="sha256:" + "e" * 64,
        cleanup_verified=True,
        revision=5,
    )
    observation = _observation(plan)
    observation["protected_bootstrap_running"] = False

    with pytest.raises(route.RouteControllerError, match="protected bootstrap was lost"):
        route.reconcile(
            "status",
            state,
            observation,
            plan,
            now_unix=observation["observed_at_unix"],
        )


def _write_protected_route_evidence(
    tmp_path: Path,
    plan: route.RoutePlan,
    observation: dict[str, object],
) -> Path:
    root = tmp_path / "route-evidence"
    root.mkdir()
    route_record = observation["route_job"]["route_record"]
    rpc = {
        "schema_version": route.SCHEMA_VERSION,
        "result": "passed",
        "run_id": plan.run_id,
        "job_id": plan.route_job_id,
        "collect_action_id": route._action_id(plan, "collect_route"),
        "plan_digest": plan.plan_digest,
        "source_commit": plan.source_commit,
        "manifest_digest": plan.manifest_digest,
        "worker_plan_digest": plan.worker_plan_digest,
        "route_span": "0:64",
        "session_id": route_record["session_id"],
    }
    rpc_path = root / "route-rpc.json"
    _write_json(rpc_path, rpc)
    route_record["route_rpc_evidence_digest"] = "sha256:" + hashlib.sha256(rpc_path.read_bytes()).hexdigest()
    for worker_plan, result in zip(plan.workers, route_record["worker_results"]):
        evidence = {
            "schema_version": route.SCHEMA_VERSION,
            "result": "passed",
            "run_id": plan.run_id,
            "job_id": plan.route_job_id,
            "collect_action_id": route._action_id(plan, "collect_route"),
            "plan_digest": plan.plan_digest,
            "source_commit": plan.source_commit,
            "manifest_digest": plan.manifest_digest,
            "worker_plan_digest": plan.worker_plan_digest,
            "start_action_id": route._action_id(plan, "start_route"),
            "worker_id": worker_plan.worker_id,
            "machine_id": worker_plan.machine_id,
            "peer_id": result["peer_id"],
            "span": worker_plan.span,
            "artifact_bytes": worker_plan.artifact_bytes,
            "artifact_set_digest": worker_plan.artifact_set_digest,
            "cache_root": worker_plan.cache_root,
        }
        evidence_path = root / f"{worker_plan.worker_id}-evidence.json"
        _write_json(evidence_path, evidence)
        result["worker_evidence_digest"] = "sha256:" + hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    observation["route_job"]["evidence_digest"] = route._canonical_digest(route_record)
    _write_json(root / "route-terminal.json", route_record)
    return root


def test_protected_route_evidence_revalidates_exact_terminal_and_children(
    tmp_path: Path,
) -> None:
    plan = _load_plan(tmp_path)
    observation = _observation(
        plan,
        resource_state="present",
        worker_state="ready",
        job_state="passed",
    )
    evidence_root = _write_protected_route_evidence(tmp_path, plan, observation)
    validated = route.validate_observation(observation, plan)

    digest = route.revalidate_route_evidence(
        plan,
        validated,
        evidence_root,
        tmp_path / "source",
    )

    assert digest == observation["route_job"]["evidence_digest"]


def test_protected_route_evidence_rejects_child_mutation(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)
    observation = _observation(
        plan,
        resource_state="present",
        worker_state="ready",
        job_state="passed",
    )
    evidence_root = _write_protected_route_evidence(tmp_path, plan, observation)
    validated = route.validate_observation(observation, plan)
    (evidence_root / "worker-0-evidence.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(route.RouteControllerError, match="worker evidence digest changed"):
        route.revalidate_route_evidence(
            plan,
            validated,
            evidence_root,
            tmp_path / "source",
        )


def test_protected_route_evidence_rejects_extra_file(tmp_path: Path) -> None:
    plan = _load_plan(tmp_path)
    observation = _observation(
        plan,
        resource_state="present",
        worker_state="ready",
        job_state="passed",
    )
    evidence_root = _write_protected_route_evidence(tmp_path, plan, observation)
    validated = route.validate_observation(observation, plan)
    (evidence_root / "extra.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(route.RouteControllerError, match="inventory is not exact"):
        route.revalidate_route_evidence(
            plan,
            validated,
            evidence_root,
            tmp_path / "source",
        )


def test_fabricated_embedded_pass_cannot_advance_without_protected_revalidation(
    tmp_path: Path,
) -> None:
    plan = _load_plan(tmp_path)
    observation = _observation(
        plan,
        resource_state="present",
        worker_state="ready",
        job_state="passed",
    )

    with pytest.raises(route.RouteControllerError, match="protected records"):
        route.reconcile(
            "status",
            route.initial_state(plan),
            observation,
            plan,
            now_unix=observation["observed_at_unix"],
            start_was_issued=True,
        )


def test_authorization_rejects_resource_spec_substitution(tmp_path: Path) -> None:
    value = _plan_value()
    authorization_root = _write_authorization_records(tmp_path, value)
    preflight_path = authorization_root / value["authorization"]["preflight_record_path"]
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    spec = next(item for item in preflight["resource_specs"] if item["kind"] == "worker_instance")
    spec["machine_type"] = "substituted-machine"
    _write_json(preflight_path, preflight)
    _rebind_authorization_record(value, authorization_root, "preflight")
    plan = _load_plan(tmp_path, value)

    with pytest.raises(route.RouteControllerError, match="exact launch profile"):
        route.revalidate_authorization_evidence(
            plan,
            authorization_root,
            now_unix=1_900_000_000,
        )


@pytest.mark.parametrize(
    ("kind", "field", "replacement"),
    [
        ("worker_instance", "accelerator_count", 0),
        ("bootstrap_instance", "accelerator_type", "nvidia-l4"),
        ("worker_disk", "source_image", "communityai-q38-v1"),
        ("firewall", "machine_type", "g2-standard-8"),
        ("worker_instance", "zone", "europe-west1-b"),
        ("worker_instance", "max_lifetime_seconds", 100_000_002),
    ],
)
def test_authorization_rejects_invalid_resource_spec_semantics(
    tmp_path: Path,
    kind: str,
    field: str,
    replacement: object,
) -> None:
    value = _plan_value()
    authorization_root = _write_authorization_records(tmp_path, value)
    preflight_path = authorization_root / value["authorization"]["preflight_record_path"]
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    spec = next(item for item in preflight["resource_specs"] if item["kind"] == kind)
    spec[field] = replacement

    reservation_path = authorization_root / value["authorization"]["reservation_record_path"]
    reservation = json.loads(reservation_path.read_text(encoding="utf-8"))
    cost = next(item for item in reservation["resource_costs"] if item["resource_name"] == spec["resource_name"])
    cost["resource_spec_digest"] = route._canonical_digest(spec)
    _write_json(reservation_path, reservation)
    _rebind_authorization_record(value, authorization_root, "reservation")

    preflight["reservation_record_sha256"] = value["authorization"]["reservation_record_sha256"]
    _write_json(preflight_path, preflight)
    _rebind_authorization_record(value, authorization_root, "preflight")
    plan = _load_plan(tmp_path, value)

    with pytest.raises(route.RouteControllerError, match="exact launch profile"):
        route.revalidate_authorization_evidence(
            plan,
            authorization_root,
            now_unix=1_900_000_000,
        )


def test_authorization_rejects_unprotected_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = _plan_value()
    authorization_root = _write_authorization_records(tmp_path, value)
    plan = _load_plan(tmp_path, value)
    monkeypatch.setattr(route, "_assert_protected_path", REAL_ASSERT_PROTECTED_PATH)

    with pytest.raises(route.RouteControllerError, match="protection verifier|not protected"):
        route.revalidate_authorization_evidence(
            plan,
            authorization_root,
            tmp_path / "source",
            now_unix=1_900_000_000,
        )


def test_issuance_journal_prevents_paid_start_reissue_after_state_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = _source_root(tmp_path)
    plan_path = tmp_path / "plan.json"
    _write_json(plan_path, _plan_value())
    plan = route.load_plan(plan_path, source_root)
    observation_path = tmp_path / "observation.json"
    observation = _observation(plan)
    _write_json(observation_path, observation)
    state_path = tmp_path / "state.json"
    decision_path = tmp_path / "decision.json"
    monkeypatch.setattr(route.time, "time", lambda: 1_900_000_000)
    authorization_revalidations = 0

    def revalidate_once(*args, **kwargs) -> None:
        nonlocal authorization_revalidations
        authorization_revalidations += 1
        if authorization_revalidations > 1:
            raise AssertionError("recovery revalidated expired paid-start authorization")

    monkeypatch.setattr(route, "revalidate_authorization_evidence", revalidate_once)
    monkeypatch.setattr(
        route,
        "revalidate_production_artifact_plan",
        lambda *args, **kwargs: observation["artifact_plan_revalidation"],
    )
    argv = [
        "start",
        "--plan",
        os.fspath(plan_path),
        "--source-root",
        os.fspath(source_root),
        "--observation",
        os.fspath(observation_path),
        "--manifest",
        os.fspath(tmp_path / "manifest.json"),
        "--artifact-root",
        os.fspath(tmp_path / "artifacts"),
        "--authorization-root",
        os.fspath(tmp_path / "authorization"),
        "--state",
        os.fspath(state_path),
        "--decision",
        os.fspath(decision_path),
    ]

    assert route.main(argv) == 0
    first = json.loads(decision_path.read_text(encoding="utf-8"))
    journal_path = state_path.with_name(f".{state_path.name}.issuance.json")
    issued = json.loads(journal_path.read_text(encoding="utf-8"))
    assert first["action"] == "start_route"
    assert issued["status"] == "issued"

    state_path.unlink()
    decision_path.unlink()
    assert route.main(argv) == 0

    recovered = json.loads(decision_path.read_text(encoding="utf-8"))
    completed = json.loads(journal_path.read_text(encoding="utf-8"))
    assert recovered["action"] == "none"
    assert completed["status"] == "completed"
    assert completed["terminal_phase"] == "CLEANED_FAILURE"
    assert completed["failure_code"] == "state-lost-after-start"
    assert authorization_revalidations == 1


def test_one_cent_paid_authorization_cannot_bypass_readiness_ledger(
    tmp_path: Path,
) -> None:
    value = _plan_value()
    value["authorization"]["maximum_estimate_usd"] = "0.01"
    authorization_root = _write_authorization_records(tmp_path, value)
    plan = _load_plan(tmp_path, value)

    with pytest.raises(route.RouteControllerError, match="exact reservation"):
        route.revalidate_authorization_evidence(
            plan,
            authorization_root,
            now_unix=1_900_000_000,
        )


def test_completed_journal_recovers_pass_after_state_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = _source_root(tmp_path)
    plan_path = tmp_path / "plan.json"
    _write_json(plan_path, _plan_value())
    plan = route.load_plan(plan_path, source_root)
    observation_path = tmp_path / "observation.json"
    _write_json(observation_path, _observation(plan))
    state_path = tmp_path / "state.json"
    decision_path = tmp_path / "decision.json"
    journal_path = state_path.with_name(f".{state_path.name}.issuance.json")
    issued = route._issued_journal(plan, 1_899_999_990)
    terminal = route.initial_state(plan)
    terminal.update(
        revision=7,
        phase="CLEANED_PASS",
        evidence_digest="sha256:" + "e" * 64,
        cleanup_verified=True,
    )
    _write_json(
        journal_path,
        route._completed_journal(issued, terminal, 1_899_999_999),
    )
    monkeypatch.setattr(route.time, "time", lambda: 1_900_000_000)
    argv = [
        "status",
        "--plan",
        os.fspath(plan_path),
        "--source-root",
        os.fspath(source_root),
        "--observation",
        os.fspath(observation_path),
        "--state",
        os.fspath(state_path),
        "--decision",
        os.fspath(decision_path),
    ]

    assert route.main(argv) == 0

    recovered = json.loads(state_path.read_text(encoding="utf-8"))
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    assert recovered["phase"] == "CLEANED_PASS"
    assert recovered["evidence_digest"] == "sha256:" + "e" * 64
    assert recovered["cleanup_verified"] is True
    assert decision["action"] == "none"


def test_complete_instance_generations_are_latched_and_recreation_forces_cleanup(
    tmp_path: Path,
) -> None:
    plan = _load_plan(tmp_path)
    starting = _advance(
        "start",
        route.initial_state(plan),
        _observation(plan),
        plan,
    )
    observation = _observation(
        plan,
        resource_state="present",
        worker_state="starting",
    )

    latched = _advance("status", starting, observation, plan)

    assert latched["phase"] == "STARTING"
    assert latched["instance_generations_digest"] == observation["instance_generations_digest"]
    assert (
        route.action_record(latched, plan)["instance_generations_digest"] == observation["instance_generations_digest"]
    )

    recreated = copy.deepcopy(observation)
    instance = next(item for item in plan.resources if item.kind.endswith("instance"))
    resource = recreated["resources"][instance.name]
    resource["instance_id"] = str(int(resource["instance_id"]) + 1)
    resource["instance_generation_digest"] = route.instance_generation_digest(
        instance.name,
        resource["instance_id"],
        resource["creation_timestamp"],
    )
    recreated["instance_generations_digest"] = route.observation_instance_generations_digest(
        recreated["resources"],
        plan,
    )

    cleaning = _advance("status", latched, recreated, plan)

    assert cleaning["phase"] == "CLEANING"
    assert cleaning["failure_code"] == "instance-generation-changed"
    assert cleaning["instance_generations_digest"] == latched["instance_generations_digest"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("instance_id", None),
        ("instance_id", True),
        ("instance_id", "0"),
        ("instance_id", "not-numeric"),
        ("instance_id", "18446744073709551616"),
        ("creation_timestamp", None),
        ("creation_timestamp", True),
        ("creation_timestamp", "2026-09-03"),
        ("creation_timestamp", "2026-09-03T01:20:00Z"),
        ("creation_timestamp", "2026-13-03T01:20:00+00:00"),
        ("creation_timestamp", "2026-09-03T01:20:00+24:00"),
    ],
)
def test_instance_generation_fields_fail_closed(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    plan = _load_plan(tmp_path)
    observation = _observation(
        plan,
        resource_state="present",
        worker_state="starting",
    )
    instance = next(item for item in plan.resources if item.kind.endswith("instance"))
    observation["resources"][instance.name][field] = value

    with pytest.raises(route.RouteControllerError, match="instance generation|provider instance"):
        route.validate_observation(observation, plan)
