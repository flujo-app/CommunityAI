from __future__ import annotations

import copy
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import test_gateq38_route_controller as route_test  # noqa: E402

from scripts import gateq38_gcp_adapter as adapter, gateq38_route_controller as route  # noqa: E402

NOW = 1_900_000_000


def _revalidation(plan: route.RoutePlan, verified_at_unix: int) -> dict[str, object]:
    verifier = next(
        item["sha256"] for item in plan.source_bindings if item["relative_path"] == route.VERIFIER_SOURCE_PATH
    )
    return {
        "verified_at_unix": verified_at_unix,
        "source_commit": plan.source_commit,
        "manifest_digest": plan.manifest_digest,
        "model_revision": plan.model_revision,
        "index_digest": route.EXPECTED_INDEX_DIGEST,
        "block_prefix": route.EXPECTED_BLOCK_PREFIX,
        "worker_plan_digest": plan.worker_plan_digest,
        "verifier_source_sha256": verifier,
    }


@pytest.fixture(autouse=True)
def _trusted_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter, "_assert_source_bound", lambda *args, **kwargs: None)
    monkeypatch.setattr(route, "_assert_protected_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(route, "revalidate_authorization_evidence", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        route,
        "revalidate_production_artifact_plan",
        lambda plan, manifest, artifacts, source, *, verified_at_unix: _revalidation(plan, verified_at_unix),
    )


class FakeGcloud:
    def __init__(self, plan: route.RoutePlan) -> None:
        self.plan = plan
        self.resources: dict[str, dict[str, object]] = {}
        self.calls: list[tuple[str, ...]] = []
        self.extra: dict[str, set[str]] = {
            "instances": set(),
            "disks": set(),
            "firewall": set(),
        }
        self.fail_delete_once: set[str] = set()
        self.fail_create_once: set[str] = set()

    @staticmethod
    def result(value: object | None = None, *, returncode: int = 0, stderr: bytes = b""):
        stdout = b"" if value is None else json.dumps(value).encode("utf-8")
        return adapter.CommandResult(returncode, stdout, stderr)

    def resource(self, name: str) -> route.ResourcePlan:
        return self.plan.resource_by_name[name]

    def disk_value(self, resource: route.ResourcePlan) -> dict[str, object]:
        image_project, image = route.EXPECTED_SOURCE_IMAGE.split("/", 1)
        return {
            "name": resource.name,
            "status": "READY",
            "labels": adapter._labels(self.plan),
            "type": f"zones/{route.EXPECTED_ZONE}/diskTypes/{route.EXPECTED_DISK_TYPE}",
            "sizeGb": str(route.EXPECTED_DISK_SIZE_GB),
            "sourceImage": f"projects/{image_project}/global/images/{image}",
            "description": adapter._description(self.plan, resource),
        }

    def instance_value(self, resource: route.ResourcePlan) -> dict[str, object]:
        spec = route._expected_resource_spec(resource)
        disk = next(
            item
            for item in self.plan.resources
            if item.kind == ("worker_disk" if resource.worker_id else "bootstrap_disk")
            and item.worker_id == resource.worker_id
        )
        value: dict[str, object] = {
            "name": resource.name,
            "status": "RUNNING",
            "labels": adapter._labels(self.plan),
            "machineType": f"zones/{route.EXPECTED_ZONE}/machineTypes/{spec['machine_type']}",
            "disks": [
                {
                    "source": f"zones/{route.EXPECTED_ZONE}/disks/{disk.name}",
                    "boot": True,
                    "autoDelete": True,
                }
            ],
            "networkInterfaces": [
                {
                    "network": f"global/networks/{route.EXPECTED_NETWORK}",
                    "subnetwork": f"regions/{route.EXPECTED_REGION}/subnetworks/{route.EXPECTED_SUBNET}",
                    "accessConfigs": [],
                    "ipv6AccessConfigs": [],
                    "stackType": "IPV4_ONLY",
                }
            ],
            "tags": {"items": [adapter._route_tag(self.plan)]},
            "metadata": {
                "items": [
                    {"key": key, "value": field}
                    for key, field in adapter._instance_metadata(self.plan, resource).items()
                ]
            },
            "guestAccelerators": [],
            "canIpForward": False,
            "deletionProtection": False,
            "scheduling": {
                "automaticRestart": True,
                "provisioningModel": "STANDARD",
                "onHostMaintenance": "TERMINATE",
                "instanceTerminationAction": "DELETE",
                "maxRunDuration": {
                    "seconds": str(route.EXPECTED_MAX_LIFETIME_SECONDS),
                    "nanos": 0,
                },
            },
        }
        if resource.kind == "worker_instance":
            value["guestAccelerators"] = [
                {
                    "acceleratorType": (
                        f"zones/{route.EXPECTED_ZONE}/acceleratorTypes/" f"{route.EXPECTED_ACCELERATOR_TYPE}"
                    ),
                    "acceleratorCount": 1,
                }
            ]
        return value

    def firewall_value(self, resource: route.ResourcePlan) -> dict[str, object]:
        return {
            "name": resource.name,
            "network": f"global/networks/{route.EXPECTED_NETWORK}",
            "direction": "INGRESS",
            "sourceTags": [adapter._route_tag(self.plan)],
            "targetTags": [adapter._route_tag(self.plan)],
            "allowed": [{"IPProtocol": "tcp", "ports": ["31330-31339"]}],
            "disabled": False,
            "description": adapter._description(self.plan, resource),
        }

    def value(self, resource: route.ResourcePlan) -> dict[str, object]:
        if resource.kind.endswith("disk"):
            return self.disk_value(resource)
        if resource.kind.endswith("instance"):
            return self.instance_value(resource)
        return self.firewall_value(resource)

    def populate_all(self) -> None:
        self.resources = {item.name: self.value(item) for item in self.plan.resources}

    def _listed(self, kind: str) -> list[dict[str, str]]:
        if kind == "instances":
            names = {
                item.name
                for item in self.plan.resources
                if item.kind.endswith("instance") and item.name in self.resources
            }
        elif kind == "disks":
            names = {
                item.name for item in self.plan.resources if item.kind.endswith("disk") and item.name in self.resources
            }
        else:
            names = {
                item.name for item in self.plan.resources if item.kind == "firewall" and item.name in self.resources
            }
        return [{"name": name} for name in sorted(names | self.extra[kind])]

    def __call__(self, argv, timeout):
        del timeout
        command = tuple(argv)
        self.calls.append(command)
        args = list(command[1:])
        assert args.pop() == "--quiet"
        if "--format=json" in args:
            args.remove("--format=json")

        if args[:2] == ["auth", "list"]:
            return adapter.CommandResult(0, b"operator@example.invalid\n", b"")
        if args[:2] == ["projects", "describe"]:
            return self.result({"lifecycleState": "ACTIVE"})
        if args[:3] == ["compute", "instances", "describe"]:
            name = args[3]
            if name == route.PROTECTED_INSTANCE:
                return self.result({"name": name, "status": "RUNNING"})
            if name not in self.resources:
                return self.result(returncode=1, stderr=b"resource was not found")
            return self.result(self.resources[name])
        if args[:3] == ["compute", "disks", "describe"]:
            name = args[3]
            if name not in self.resources:
                return self.result(returncode=1, stderr=b"resource was not found")
            return self.result(self.resources[name])
        if args[:3] == ["compute", "firewall-rules", "describe"]:
            name = args[3]
            if name not in self.resources:
                return self.result(returncode=1, stderr=b"resource was not found")
            return self.result(self.resources[name])
        if args[:3] == ["compute", "instances", "list"]:
            return self.result(self._listed("instances"))
        if args[:3] == ["compute", "disks", "list"]:
            return self.result(self._listed("disks"))
        if args[:3] == ["compute", "firewall-rules", "list"]:
            return self.result(self._listed("firewall"))

        if args[:3] in (
            ["compute", "disks", "create"],
            ["compute", "instances", "create"],
            ["compute", "firewall-rules", "create"],
        ):
            name = args[3]
            if name in self.fail_create_once:
                self.fail_create_once.remove(name)
                return self.result(returncode=1, stderr=b"injected create failure")
            resource = self.resource(name)
            self.resources[name] = self.value(resource)
            return self.result()
        if args[:3] in (
            ["compute", "instances", "delete"],
            ["compute", "disks", "delete"],
            ["compute", "firewall-rules", "delete"],
        ):
            name = args[3]
            if name in self.fail_delete_once:
                self.fail_delete_once.remove(name)
                return self.result(returncode=1, stderr=b"injected delete failure")
            self.resources.pop(name, None)
            return self.result()
        if args[:2] == ["compute", "ssh"]:
            return self.result()

        raise AssertionError(f"unexpected command: {command}")


def _make(tmp_path: Path) -> tuple[route.RoutePlan, FakeGcloud, adapter.GcpAdapter]:
    plan = route_test._load_plan(tmp_path)
    fake = FakeGcloud(plan)
    return plan, fake, adapter.GcpAdapter(plan, tmp_path / "source", runner=fake, clock=lambda: NOW)


def _start_state(plan: route.RoutePlan) -> dict[str, object]:
    return route_test._advance(
        "start",
        route.initial_state(plan),
        route_test._observation(plan, observed_at=NOW),
        plan,
    )


def _cleanup_state(plan: route.RoutePlan) -> dict[str, object]:
    state = route.initial_state(plan)
    state.update(
        revision=1,
        phase="CLEANING",
        failure_code="operator-cleanup",
        next_action="cleanup_route",
    )
    return state


def _collect_state(plan: route.RoutePlan) -> dict[str, object]:
    state = route.initial_state(plan)
    state.update(revision=3, phase="COLLECTING", next_action="collect_route")
    return state


def _ready_status(plan: route.RoutePlan) -> dict[str, object]:
    observation = route_test._observation(
        plan,
        resource_state="present",
        worker_state="ready",
        observed_at=NOW,
    )
    return {
        "schema_version": route.SCHEMA_VERSION,
        "run_id": plan.run_id,
        "workers": observation["workers"],
        "route_job": observation["route_job"],
    }


def _execute(
    provider: adapter.GcpAdapter,
    plan: route.RoutePlan,
    state: dict[str, object],
    status: dict[str, object],
    tmp_path: Path,
) -> dict[str, object]:
    return provider.execute(
        state,
        route.action_record(state, plan),
        status,
        manifest_path=tmp_path / "manifest.json",
        artifact_root=tmp_path / "artifacts",
    )


def test_absent_inventory_is_exact_and_read_only(tmp_path: Path) -> None:
    plan, fake, provider = _make(tmp_path)

    observation = provider.inventory(
        adapter.blank_host_status(plan),
        manifest_path=tmp_path / "manifest.json",
        artifact_root=tmp_path / "artifacts",
    )

    assert set(observation["resources"]) == set(plan.resource_by_name)
    assert all(not item["present"] for item in observation["resources"].values())
    assert all(item["state"] == "absent" for item in observation["workers"].values())
    assert observation["route_job"]["state"] == "absent"
    assert not any("create" in call or "delete" in call for call in fake.calls)


def test_running_instance_without_host_record_is_only_starting(tmp_path: Path) -> None:
    plan, fake, provider = _make(tmp_path)
    fake.populate_all()

    observation = provider.inventory(
        adapter.blank_host_status(plan),
        manifest_path=tmp_path / "manifest.json",
        artifact_root=tmp_path / "artifacts",
    )

    assert {item["state"] for item in observation["workers"].values()} == {"starting"}
    assert all(item["peer_id"] is None for item in observation["workers"].values())


def test_compiled_start_is_exact_private_eleven_resource_inventory(
    tmp_path: Path,
) -> None:
    plan, fake, provider = _make(tmp_path)

    creates = list(provider.compiled_start_commands())

    assert len(creates) == 11
    assert fake.calls == []
    assert fake.resources == {}
    instance_creates = [call for call in creates if call[1:4] == ("compute", "instances", "create")]
    assert len(instance_creates) == 5
    assert all(
        "--no-address" in call
        and "--no-service-account" in call
        and "--stack-type=IPV4_ONLY" in call
        and any("boot=yes,auto-delete=yes" in item for item in call)
        for call in instance_creates
    )
    assert not any(any(item.startswith("--accelerator=") for item in call) for call in instance_creates)
    assert all("--maintenance-policy=TERMINATE" in call for call in instance_creates)
    assert all("--restart-on-failure" in call for call in instance_creates)
    assert all(f"--max-run-duration={route.EXPECTED_MAX_LIFETIME_SECONDS}s" in call for call in instance_creates)
    assert all("--instance-termination-action=DELETE" in call for call in instance_creates)
    disk_creates = [call for call in creates if call[1:4] == ("compute", "disks", "create")]
    assert len(disk_creates) == 5
    assert all("--image=common-cu129-ubuntu-2404-nvidia-580-v20260831" in call for call in disk_creates)
    assert all("--image-project=deeplearning-platform-release" in call for call in disk_creates)
    firewall = next(call for call in creates if call[1:4] == ("compute", "firewall-rules", "create"))
    assert f"--source-tags={adapter._route_tag(plan)}" in firewall
    assert not any("0.0.0.0/0" in item or "source-ranges" in item or item.startswith("--labels=") for item in firewall)
    assert all(route.PROTECTED_INSTANCE not in call for call in creates)


def test_route_network_tags_are_plan_scoped_and_rfc1035(tmp_path: Path) -> None:
    plan, _fake, _provider = _make(tmp_path)
    other = replace(plan, run_id="q38route-002")

    first = adapter._route_tag(plan)
    second = adapter._route_tag(other)

    assert first != second
    assert route._GCP_RESOURCE_RE.fullmatch(first)
    assert route._GCP_RESOURCE_RE.fullmatch(second)


def test_stale_decision_rejects_before_provider_inventory(tmp_path: Path) -> None:
    plan, fake, provider = _make(tmp_path)
    state = _start_state(plan)
    decision = route.action_record(state, plan)
    decision["plan_digest"] = "sha256:" + "0" * 64

    with pytest.raises(adapter.Q38GcpAdapterError, match="stale or unbound"):
        provider.execute(
            state,
            decision,
            adapter.blank_host_status(plan),
            manifest_path=tmp_path / "manifest.json",
            artifact_root=tmp_path / "artifacts",
        )

    assert fake.calls == []


def test_start_execution_is_blocked_before_provider_access(tmp_path: Path) -> None:
    plan, fake, provider = _make(tmp_path)

    with pytest.raises(
        adapter.Q38GcpAdapterError,
        match="host runtime is not plan-bound",
    ):
        _execute(
            provider,
            plan,
            _start_state(plan),
            adapter.blank_host_status(plan),
            tmp_path,
        )

    assert fake.calls == []
    assert fake.resources == {}


@pytest.mark.parametrize("kind", ["instances", "disks"])
def test_extra_run_scoped_unlabelled_resource_fails_closed(
    tmp_path: Path,
    kind: str,
) -> None:
    plan, fake, provider = _make(tmp_path)
    fake.extra[kind].add(f"{plan.run_id}-unplanned")

    with pytest.raises(adapter.Q38GcpAdapterError, match="not exact"):
        provider.inventory(
            adapter.blank_host_status(plan),
            manifest_path=tmp_path / "manifest.json",
            artifact_root=tmp_path / "artifacts",
        )


def test_foreign_exact_name_does_not_strand_other_cleanup(tmp_path: Path) -> None:
    plan, fake, provider = _make(tmp_path)
    fake.populate_all()
    foreign = next(item for item in plan.resources if item.kind == "worker_disk")
    fake.resources[foreign.name]["labels"] = {"communityai-run": "foreign"}

    with pytest.raises(adapter.Q38GcpAdapterError, match="ownership"):
        _execute(
            provider,
            plan,
            _cleanup_state(plan),
            adapter.blank_host_status(plan),
            tmp_path,
        )

    assert set(fake.resources) == {foreign.name}
    deleted = {
        call[4]
        for call in fake.calls
        if call[1:4]
        in {
            ("compute", "instances", "delete"),
            ("compute", "disks", "delete"),
            ("compute", "firewall-rules", "delete"),
        }
    }
    assert deleted == set(plan.resource_by_name) - {foreign.name}
    assert all(route.PROTECTED_INSTANCE not in call for call in fake.calls)


@pytest.mark.parametrize("field", ["serviceAccounts", "networkInterfaces"])
def test_public_or_privileged_instance_shape_fails_closed(
    tmp_path: Path,
    field: str,
) -> None:
    plan, fake, provider = _make(tmp_path)
    fake.populate_all()
    resource = next(item for item in plan.resources if item.kind == "worker_instance")
    if field == "serviceAccounts":
        fake.resources[resource.name][field] = [{"email": "privileged@example.invalid"}]
    else:
        fake.resources[resource.name][field][0]["accessConfigs"] = [{"natIP": "203.0.113.1"}]

    with pytest.raises(adapter.Q38GcpAdapterError, match="service account|shape"):
        provider.inventory(
            adapter.blank_host_status(plan),
            manifest_path=tmp_path / "manifest.json",
            artifact_root=tmp_path / "artifacts",
        )


def test_public_ipv6_instance_shape_fails_closed(tmp_path: Path) -> None:
    plan, fake, provider = _make(tmp_path)
    fake.populate_all()
    resource = next(item for item in plan.resources if item.kind == "worker_instance")
    interface = fake.resources[resource.name]["networkInterfaces"][0]
    interface["stackType"] = "IPV4_IPV6"
    interface["ipv6AccessConfigs"] = [{"externalIpv6": "2001:db8::1"}]
    interface["externalIpv6"] = "2001:db8::1"

    with pytest.raises(adapter.Q38GcpAdapterError, match="shape"):
        provider.inventory(
            adapter.blank_host_status(plan),
            manifest_path=tmp_path / "manifest.json",
            artifact_root=tmp_path / "artifacts",
        )


def test_cleanup_continues_after_failure_and_retries_only_remaining(
    tmp_path: Path,
) -> None:
    plan, fake, provider = _make(tmp_path)
    fake.populate_all()
    failed = next(item.name for item in plan.resources if item.kind == "worker_disk")
    fake.fail_delete_once.add(failed)
    state = _cleanup_state(plan)

    with pytest.raises(adapter.Q38GcpAdapterError, match="cleanup is incomplete"):
        _execute(provider, plan, state, adapter.blank_host_status(plan), tmp_path)

    assert set(fake.resources) == {failed}
    mutations = [call for call in fake.calls if any(action in call for action in ("create", "delete", "ssh"))]
    assert all(route.PROTECTED_INSTANCE not in call for call in mutations)

    observation = _execute(provider, plan, state, adapter.blank_host_status(plan), tmp_path)

    assert fake.resources == {}
    assert all(not item["present"] for item in observation["resources"].values())
    assert observation["protected_bootstrap_running"] is True


def test_terminal_owned_resources_are_deleted_and_retryable(
    tmp_path: Path,
) -> None:
    plan, fake, provider = _make(tmp_path)
    fake.populate_all()
    instance = next(item for item in plan.resources if item.kind == "worker_instance")
    disk = next(item for item in plan.resources if item.kind == "worker_disk" and item.worker_id == instance.worker_id)
    fake.resources[instance.name]["status"] = "TERMINATED"
    fake.resources[disk.name]["status"] = "FAILED"
    fake.fail_delete_once.update({instance.name, disk.name})
    state = _cleanup_state(plan)

    with pytest.raises(adapter.Q38GcpAdapterError, match="cleanup is incomplete"):
        _execute(
            provider,
            plan,
            state,
            adapter.blank_host_status(plan),
            tmp_path,
        )

    assert set(fake.resources) == {instance.name, disk.name}

    observation = _execute(
        provider,
        plan,
        state,
        adapter.blank_host_status(plan),
        tmp_path,
    )

    assert fake.resources == {}
    assert all(not item["present"] for item in observation["resources"].values())


@pytest.mark.parametrize("field", ["canIpForward", "deletionProtection"])
def test_forwarding_or_deletion_protection_fails_closed(
    tmp_path: Path,
    field: str,
) -> None:
    plan, fake, provider = _make(tmp_path)
    fake.populate_all()
    resource = next(item for item in plan.resources if item.kind == "worker_instance")
    fake.resources[resource.name][field] = True

    with pytest.raises(adapter.Q38GcpAdapterError, match="shape"):
        provider.inventory(
            adapter.blank_host_status(plan),
            manifest_path=tmp_path / "manifest.json",
            artifact_root=tmp_path / "artifacts",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("boot", False),
        ("autoDelete", False),
        ("automaticRestart", False),
        ("provisioningModel", "SPOT"),
        ("onHostMaintenance", "MIGRATE"),
        ("instanceTerminationAction", "STOP"),
        ("maxRunDuration", {"seconds": "1", "nanos": 0}),
    ],
)
def test_instance_lifetime_or_disk_binding_change_fails_closed(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    plan, fake, provider = _make(tmp_path)
    fake.populate_all()
    resource = next(item for item in plan.resources if item.kind == "worker_instance")
    instance = fake.resources[resource.name]
    if field in {"boot", "autoDelete"}:
        instance["disks"][0][field] = value
    else:
        instance["scheduling"][field] = value

    with pytest.raises(adapter.Q38GcpAdapterError, match="shape"):
        provider.inventory(
            adapter.blank_host_status(plan),
            manifest_path=tmp_path / "manifest.json",
            artifact_root=tmp_path / "artifacts",
        )


def test_collect_execution_is_blocked_before_provider_access(tmp_path: Path) -> None:
    plan, fake, provider = _make(tmp_path)

    with pytest.raises(
        adapter.Q38GcpAdapterError,
        match="host runtime is not plan-bound",
    ):
        _execute(
            provider,
            plan,
            _collect_state(plan),
            _ready_status(plan),
            tmp_path,
        )

    assert fake.calls == []


def test_cleanup_does_not_require_manifest_revalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, fake, provider = _make(tmp_path)
    fake.populate_all()
    state = _cleanup_state(plan)
    monkeypatch.setattr(
        route,
        "revalidate_production_artifact_plan",
        lambda *args, **kwargs: pytest.fail("cleanup revalidated stale artifact inputs"),
    )

    observation = provider.execute(
        state,
        route.action_record(state, plan),
        adapter.blank_host_status(plan),
        manifest_path=None,
        artifact_root=None,
    )

    assert all(not item["present"] for item in observation["resources"].values())


def test_nonblank_host_status_is_rejected_without_bound_transport(
    tmp_path: Path,
) -> None:
    plan, _fake, provider = _make(tmp_path)
    status = _ready_status(plan)

    with pytest.raises(
        adapter.Q38GcpAdapterError,
        match="status transport is not plan-bound",
    ):
        provider.inventory(
            status,
            manifest_path=tmp_path / "manifest.json",
            artifact_root=tmp_path / "artifacts",
        )


def test_stopping_instance_cannot_be_promoted_by_static_ready_status(
    tmp_path: Path,
) -> None:
    plan, fake, provider = _make(tmp_path)
    fake.populate_all()
    worker = next(item for item in plan.resources if item.kind == "worker_instance")
    fake.resources[worker.name]["status"] = "STOPPING"

    with pytest.raises(
        adapter.Q38GcpAdapterError,
        match="status transport is not plan-bound",
    ):
        provider.inventory(
            _ready_status(plan),
            manifest_path=tmp_path / "manifest.json",
            artifact_root=tmp_path / "artifacts",
        )

    assert fake.calls == []


def test_adapter_never_targets_protected_bootstrap(tmp_path: Path) -> None:
    plan, fake, provider = _make(tmp_path)
    fake.populate_all()
    _execute(
        provider,
        plan,
        _cleanup_state(plan),
        adapter.blank_host_status(plan),
        tmp_path,
    )

    mutation_calls = [call for call in fake.calls if any(action in call for action in ("create", "delete", "ssh"))]
    assert mutation_calls
    assert all(route.PROTECTED_INSTANCE not in call for call in mutation_calls)
