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

from scripts import (  # noqa: E402
    gateq38_gcp_adapter as adapter,
    gateq38_linux_host_transport as transport,
    gateq38_route_controller as route,
)

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
        self.guest_attributes: dict[str, bytes] = {}
        self.guest_response_overrides: dict[str, object] = {}
        self.recreate_after_guest_read: str | None = None

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
            "id": str(20_000_000 + list(self.plan.resource_by_name).index(resource.name)),
            "creationTimestamp": "2026-09-03T01:20:00+00:00",
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
        if resource.kind == "iap_firewall":
            return {
                "name": resource.name,
                "network": f"global/networks/{route.EXPECTED_NETWORK}",
                "direction": "INGRESS",
                "sourceRanges": [adapter.IAP_SOURCE_RANGE],
                "targetTags": [adapter._route_tag(self.plan)],
                "allowed": [{"IPProtocol": "tcp", "ports": ["22"]}],
                "disabled": False,
                "description": adapter._description(self.plan, resource),
            }
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
                item.name
                for item in self.plan.resources
                if item.kind.endswith("firewall") and item.name in self.resources
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
        if args[:3] == ["compute", "instances", "get-guest-attributes"]:
            name = args[3]
            if name not in self.resources:
                return self.result(returncode=1, stderr=b"resource was not found")
            if name in self.guest_response_overrides:
                response = self.guest_response_overrides[name]
            else:
                items = []
                payload = self.guest_attributes.get(name)
                if payload is not None:
                    items.append(
                        {
                            "namespace": adapter.GUEST_ATTRIBUTE_NAMESPACE,
                            "key": adapter.GUEST_ATTRIBUTE_KEY,
                            "value": payload.decode("ascii"),
                        }
                    )
                response = {
                    "kind": "compute#guestAttributes",
                    "queryPath": adapter.GUEST_ATTRIBUTE_QUERY_PATH,
                    "queryValue": {"items": items},
                }
            if self.recreate_after_guest_read == name:
                self.resources[name]["id"] = str(int(self.resources[name]["id"]) + 1)
                self.recreate_after_guest_read = None
            return self.result(response)
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
    observation = route_test._observation(
        plan,
        resource_state="present",
        worker_state="ready",
        observed_at=NOW,
    )
    state = route.initial_state(plan)
    state.update(
        revision=3,
        phase="COLLECTING",
        next_action="collect_route",
        instance_generations_digest=observation["instance_generations_digest"],
    )
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


STATUS_KEY = b"q" * 32
STATUS_BOOT_ID = "123e4567-e89b-12d3-a456-426614174000"
PREPARED_RECORD_DIGEST = "sha256:" + "a" * 64


def _publish_authenticated_status(
    plan: route.RoutePlan,
    fake: FakeGcloud,
    *,
    key: bytes = STATUS_KEY,
    revision: int = 1,
) -> None:
    status = _ready_status(plan)
    for resource in plan.resources:
        if not resource.kind.endswith("instance"):
            continue
        provider_value = fake.resources[resource.name]
        context = transport.build_instance_context(
            plan,
            resource.name,
            provider_value["id"],
            provider_value["creationTimestamp"],
            issued_at_unix=NOW - 60,
            expires_at_unix=min(plan.deadline_unix, NOW + 600),
            key=key,
        )
        payload = status["workers"][resource.worker_id] if resource.kind == "worker_instance" else status["route_job"]
        envelope = transport.build_status_envelope(
            context,
            payload,
            plan,
            key=key,
            boot_id=STATUS_BOOT_ID,
            revision=revision,
            published_at_unix=NOW,
            prepared_record_digest=PREPARED_RECORD_DIGEST,
        )
        fake.guest_attributes[resource.name] = transport.encode_status_envelope(envelope)


def _authenticated_provider(
    plan: route.RoutePlan,
    fake: FakeGcloud,
    tmp_path: Path,
    *,
    key: bytes = STATUS_KEY,
    checkpoint: tuple[str | None, int] = (None, 0),
) -> adapter.GcpAdapter:
    return adapter.GcpAdapter(
        plan,
        tmp_path / "source",
        runner=fake,
        clock=lambda: NOW,
        status_key_resolver=lambda _resource, _generation: key,
        status_checkpoint_resolver=lambda _resource, _generation: checkpoint,
    )


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


def test_compiled_start_is_exact_private_twelve_resource_inventory(
    tmp_path: Path,
) -> None:
    plan, fake, provider = _make(tmp_path)

    creates = list(provider.compiled_start_commands())

    assert len(creates) == 12
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
    firewall_creates = [call for call in creates if call[1:4] == ("compute", "firewall-rules", "create")]
    assert len(firewall_creates) == 2
    route_firewall = next(
        call for call in firewall_creates if call[4].endswith("-firewall") and not call[4].endswith("-iap-firewall")
    )
    iap_firewall = next(call for call in firewall_creates if call[4].endswith("-iap-firewall"))
    assert f"--source-tags={adapter._route_tag(plan)}" in route_firewall
    assert not any(
        "0.0.0.0/0" in item or "source-ranges" in item or item.startswith("--labels=") for item in route_firewall
    )
    assert f"--source-ranges={adapter.IAP_SOURCE_RANGE}" in iap_firewall
    assert "--rules=tcp:22" in iap_firewall
    assert f"--target-tags={adapter._route_tag(plan)}" in iap_firewall
    assert not any(item.startswith("--source-tags=") or item.startswith("--labels=") for item in iap_firewall)
    assert all(route.PROTECTED_INSTANCE not in call for call in creates)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sourceRanges", ["0.0.0.0/0"]),
        ("sourceTags", ["foreign"]),
        ("targetTags", ["foreign"]),
        ("allowed", [{"IPProtocol": "tcp", "ports": ["2222"]}]),
        ("direction", "EGRESS"),
        ("disabled", True),
        ("network", "global/networks/default"),
    ],
)
def test_iap_firewall_policy_is_exact_and_fail_closed(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    plan, fake, provider = _make(tmp_path)
    fake.populate_all()
    resource = next(item for item in plan.resources if item.kind == "iap_firewall")
    fake.resources[resource.name][field] = value

    with pytest.raises(adapter.Q38GcpAdapterError, match="IAP firewall policy"):
        provider.inventory(
            adapter.blank_host_status(plan),
            manifest_path=tmp_path / "manifest.json",
            artifact_root=tmp_path / "artifacts",
        )


def test_route_firewall_cannot_substitute_for_iap_firewall(tmp_path: Path) -> None:
    plan, fake, provider = _make(tmp_path)
    fake.populate_all()
    route_firewall = next(item for item in plan.resources if item.kind == "firewall")
    iap_firewall = next(item for item in plan.resources if item.kind == "iap_firewall")
    fake.resources[iap_firewall.name] = fake.firewall_value(route_firewall)
    fake.resources[iap_firewall.name]["name"] = iap_firewall.name
    fake.resources[iap_firewall.name]["description"] = adapter._description(plan, iap_firewall)

    with pytest.raises(adapter.Q38GcpAdapterError, match="IAP firewall policy"):
        provider.inventory(
            adapter.blank_host_status(plan),
            manifest_path=tmp_path / "manifest.json",
            artifact_root=tmp_path / "artifacts",
        )


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


@pytest.mark.parametrize("kind", ["instances", "disks", "firewall"])
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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", None),
        ("id", True),
        ("id", "0"),
        ("id", "wrong"),
        ("id", "18446744073709551616"),
        ("creationTimestamp", None),
        ("creationTimestamp", True),
        ("creationTimestamp", "2026-09-03T01:20:00Z"),
        ("creationTimestamp", "2026-13-03T01:20:00+00:00"),
        ("creationTimestamp", "2026-09-03T01:20:00+24:00"),
    ],
)
def test_provider_instance_generation_is_required(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    plan, fake, provider = _make(tmp_path)
    fake.populate_all()
    instance = next(item for item in plan.resources if item.kind.endswith("instance"))
    fake.resources[instance.name][field] = value

    with pytest.raises(adapter.Q38GcpAdapterError, match="instance generation"):
        provider.inventory(
            adapter.blank_host_status(plan),
            manifest_path=tmp_path / "manifest.json",
            artifact_root=tmp_path / "artifacts",
        )


def test_provider_observation_carries_exact_instance_generation_inventory(
    tmp_path: Path,
) -> None:
    plan, fake, provider = _make(tmp_path)
    fake.populate_all()

    observation = provider.inventory(
        adapter.blank_host_status(plan),
        manifest_path=tmp_path / "manifest.json",
        artifact_root=tmp_path / "artifacts",
    )

    assert observation["instance_generations_digest"] == route.observation_instance_generations_digest(
        observation["resources"],
        plan,
    )
    for resource in plan.resources:
        observed = observation["resources"][resource.name]
        if resource.kind.endswith("instance"):
            provider_value = fake.resources[resource.name]
            assert observed["instance_generation_digest"] == route.instance_generation_digest(
                resource.name,
                provider_value["id"],
                provider_value["creationTimestamp"],
            )
        else:
            assert observed["instance_id"] is None
            assert observed["creation_timestamp"] is None
            assert observed["instance_generation_digest"] is None


def test_authenticated_guest_status_requires_both_protected_resolvers(
    tmp_path: Path,
) -> None:
    plan, fake, _provider = _make(tmp_path)

    with pytest.raises(
        adapter.Q38GcpAdapterError,
        match="key and checkpoint resolvers",
    ):
        adapter.GcpAdapter(
            plan,
            tmp_path / "source",
            runner=fake,
            status_key_resolver=lambda _resource, _generation: STATUS_KEY,
        )


def test_authenticated_guest_status_is_consumed_between_stable_inventories(
    tmp_path: Path,
) -> None:
    plan, fake, _provider = _make(tmp_path)
    fake.populate_all()
    _publish_authenticated_status(plan, fake)
    provider = _authenticated_provider(plan, fake, tmp_path)

    observation = provider.inventory(
        adapter.blank_host_status(plan),
        manifest_path=tmp_path / "manifest.json",
        artifact_root=tmp_path / "artifacts",
    )

    assert {item["state"] for item in observation["workers"].values()} == {"ready"}
    assert observation["route_job"]["state"] == "absent"
    guest_calls = [call for call in fake.calls if call[1:4] == ("compute", "instances", "get-guest-attributes")]
    assert len(guest_calls) == 5
    assert all(
        f"--query-path={adapter.GUEST_ATTRIBUTE_QUERY_PATH}" in call
        and f"--project={route.EXPECTED_PROJECT}" in call
        and f"--zone={route.EXPECTED_ZONE}" in call
        and "--format=json" in call
        for call in guest_calls
    )
    worker = next(item for item in plan.resources if item.kind == "worker_instance")
    worker_describes = [call for call in fake.calls if call[1:5] == ("compute", "instances", "describe", worker.name)]
    assert len(worker_describes) == 2


def test_absent_guest_attributes_do_not_resolve_keys_or_promote_workers(
    tmp_path: Path,
) -> None:
    plan, fake, _provider = _make(tmp_path)
    fake.populate_all()
    resolutions: list[tuple[str, str]] = []
    provider = adapter.GcpAdapter(
        plan,
        tmp_path / "source",
        runner=fake,
        clock=lambda: NOW,
        status_key_resolver=lambda resource, generation: (resolutions.append((resource, generation)) or STATUS_KEY),
        status_checkpoint_resolver=lambda _resource, _generation: (None, 0),
    )

    observation = provider.inventory(
        adapter.blank_host_status(plan),
        manifest_path=tmp_path / "manifest.json",
        artifact_root=tmp_path / "artifacts",
    )

    assert resolutions == []
    assert {item["state"] for item in observation["workers"].values()} == {"starting"}
    assert observation["route_job"]["state"] == "absent"


def test_authenticated_guest_status_rejects_wrong_key(
    tmp_path: Path,
) -> None:
    plan, fake, _provider = _make(tmp_path)
    fake.populate_all()
    _publish_authenticated_status(plan, fake)
    provider = _authenticated_provider(
        plan,
        fake,
        tmp_path,
        key=b"x" * 32,
    )

    with pytest.raises(
        adapter.Q38GcpAdapterError,
        match="authenticated guest attribute is invalid",
    ):
        provider.inventory(
            adapter.blank_host_status(plan),
            manifest_path=tmp_path / "manifest.json",
            artifact_root=tmp_path / "artifacts",
        )


def test_authenticated_guest_status_rejects_replayed_revision(
    tmp_path: Path,
) -> None:
    plan, fake, _provider = _make(tmp_path)
    fake.populate_all()
    _publish_authenticated_status(plan, fake, revision=1)
    provider = _authenticated_provider(
        plan,
        fake,
        tmp_path,
        checkpoint=(STATUS_BOOT_ID, 1),
    )

    with pytest.raises(
        adapter.Q38GcpAdapterError,
        match="authenticated guest attribute is invalid",
    ):
        provider.inventory(
            adapter.blank_host_status(plan),
            manifest_path=tmp_path / "manifest.json",
            artifact_root=tmp_path / "artifacts",
        )


def test_guest_attribute_response_rejects_ambiguous_items(
    tmp_path: Path,
) -> None:
    plan, fake, _provider = _make(tmp_path)
    fake.populate_all()
    resource = next(item for item in plan.resources if item.kind.endswith("instance"))
    item = {
        "namespace": adapter.GUEST_ATTRIBUTE_NAMESPACE,
        "key": adapter.GUEST_ATTRIBUTE_KEY,
        "value": "{}",
    }
    fake.guest_response_overrides[resource.name] = {
        "kind": "compute#guestAttributes",
        "queryPath": adapter.GUEST_ATTRIBUTE_QUERY_PATH,
        "queryValue": {"items": [item, item]},
    }
    provider = _authenticated_provider(plan, fake, tmp_path)

    with pytest.raises(
        adapter.Q38GcpAdapterError,
        match="guest attribute response is ambiguous",
    ):
        provider.inventory(
            adapter.blank_host_status(plan),
            manifest_path=tmp_path / "manifest.json",
            artifact_root=tmp_path / "artifacts",
        )


def test_authenticated_guest_status_rejects_generation_drift_during_read(
    tmp_path: Path,
) -> None:
    plan, fake, _provider = _make(tmp_path)
    fake.populate_all()
    _publish_authenticated_status(plan, fake)
    resource = next(item for item in plan.resources if item.kind.endswith("instance"))
    fake.recreate_after_guest_read = resource.name
    provider = _authenticated_provider(plan, fake, tmp_path)

    with pytest.raises(
        adapter.Q38GcpAdapterError,
        match="provider generation changed during authenticated host-status read",
    ):
        provider.inventory(
            adapter.blank_host_status(plan),
            manifest_path=tmp_path / "manifest.json",
            artifact_root=tmp_path / "artifacts",
        )


def test_cleanup_never_reads_authenticated_guest_attributes(
    tmp_path: Path,
) -> None:
    plan, fake, _provider = _make(tmp_path)
    fake.populate_all()
    _publish_authenticated_status(plan, fake)
    provider = _authenticated_provider(plan, fake, tmp_path)

    _execute(
        provider,
        plan,
        _cleanup_state(plan),
        adapter.blank_host_status(plan),
        tmp_path,
    )

    assert not any(call[1:4] == ("compute", "instances", "get-guest-attributes") for call in fake.calls)


DELIVERY_KEY = b"d" * transport.KEY_BYTES


def _delivery(
    plan: route.RoutePlan,
    fake: FakeGcloud,
) -> transport.InstanceDelivery:
    resource = next(item for item in plan.resources if item.kind == "worker_instance")
    provider_value = fake.resources[resource.name]
    record = route._instance_key_record(
        plan,
        resource,
        provider_value["id"],
        provider_value["creationTimestamp"],
        key=DELIVERY_KEY,
        key_epoch=1,
        issued_at_unix=NOW - 10,
        previous_record_digest=None,
    )
    return transport.build_instance_delivery(
        plan,
        route.InstanceGenerationKey(record, DELIVERY_KEY),
        now_unix=NOW,
    )


def test_compiled_delivery_uses_fixed_iap_stdin_command_without_secret(
    tmp_path: Path,
) -> None:
    plan, fake, provider = _make(tmp_path)
    fake.populate_all()
    delivery = _delivery(plan, fake)

    command = provider.compiled_instance_delivery_command(
        delivery,
        now_unix=NOW,
    )

    assert command[:3] == ("gcloud", "compute", "ssh")
    assert command[3] == delivery.record["resource_name"]
    assert f"--project={route.EXPECTED_PROJECT}" in command
    assert f"--zone={route.EXPECTED_ZONE}" in command
    assert "--tunnel-through-iap" in command
    assert "--ssh-flag=-T" in command
    assert "--ssh-flag=-oBatchMode=yes" in command
    assert command[-1] == "--quiet"
    joined = "\0".join(command).encode()
    assert DELIVERY_KEY not in joined
    assert DELIVERY_KEY.hex().encode() not in joined
    assert "install-delivery" in joined.decode()
    assert delivery.record["instance_generation_digest"] in joined.decode()


def test_delivery_runs_between_two_stable_exact_provider_inventories(
    tmp_path: Path,
) -> None:
    plan, fake, _provider = _make(tmp_path)
    fake.populate_all()
    delivery = _delivery(plan, fake)
    calls: list[tuple[tuple[str, ...], bytes, int]] = []

    def deliver(argv, payload, timeout):
        calls.append((tuple(argv), payload, timeout))
        receipt = transport.build_instance_delivery_receipt(
            delivery,
            plan,
            installed_at_unix=NOW,
        )
        return adapter.CommandResult(
            0,
            json.dumps(receipt, sort_keys=True).encode(),
            b"",
        )

    provider = adapter.GcpAdapter(
        plan,
        tmp_path / "source",
        runner=fake,
        delivery_runner=deliver,
        clock=lambda: NOW,
    )
    receipt = provider.deliver_instance(delivery, now_unix=NOW)

    assert len(calls) == 1
    assert calls[0][1] == delivery.payload
    assert calls[0][2] == adapter.DELIVERY_TIMEOUT_SECONDS
    assert receipt["delivery_digest"] == delivery.record["delivery_digest"]
    assert sum(call[1:3] == ("auth", "list") for call in fake.calls) == 2
    assert DELIVERY_KEY not in json.dumps(receipt, sort_keys=True).encode()


def test_delivery_rejects_generation_drift_after_remote_install(
    tmp_path: Path,
) -> None:
    plan, fake, _provider = _make(tmp_path)
    fake.populate_all()
    delivery = _delivery(plan, fake)

    def deliver(_argv, _payload, _timeout):
        resource = delivery.record["resource_name"]
        fake.resources[resource]["id"] = str(int(fake.resources[resource]["id"]) + 1)
        receipt = transport.build_instance_delivery_receipt(
            delivery,
            plan,
            installed_at_unix=NOW,
        )
        return adapter.CommandResult(0, json.dumps(receipt).encode(), b"")

    provider = adapter.GcpAdapter(
        plan,
        tmp_path / "source",
        runner=fake,
        delivery_runner=deliver,
        clock=lambda: NOW,
    )

    with pytest.raises(
        adapter.Q38GcpAdapterError,
        match="provider generation changed during instance delivery",
    ):
        provider.deliver_instance(delivery, now_unix=NOW)


def test_invalid_delivery_is_blocked_before_provider_or_delivery_runner(
    tmp_path: Path,
) -> None:
    plan, fake, _provider = _make(tmp_path)
    fake.populate_all()
    delivery = _delivery(plan, fake)
    changed = bytearray(delivery.payload)
    changed[-1] ^= 1
    forged = transport.InstanceDelivery(delivery.record, bytes(changed))
    delivery_calls: list[object] = []
    provider = adapter.GcpAdapter(
        plan,
        tmp_path / "source",
        runner=fake,
        delivery_runner=lambda *args: delivery_calls.append(args),
        clock=lambda: NOW,
    )

    with pytest.raises(adapter.Q38GcpAdapterError, match="delivery is invalid"):
        provider.deliver_instance(forged, now_unix=NOW)

    assert fake.calls == []
    assert delivery_calls == []


def test_delivery_requires_exact_iap_firewall_before_secret_crosses_boundary(
    tmp_path: Path,
) -> None:
    plan, fake, _provider = _make(tmp_path)
    fake.populate_all()
    delivery = _delivery(plan, fake)
    iap = next(item for item in plan.resources if item.kind == "iap_firewall")
    fake.resources.pop(iap.name)
    delivered: list[bytes] = []
    provider = adapter.GcpAdapter(
        plan,
        tmp_path / "source",
        runner=fake,
        delivery_runner=lambda _argv, payload, _timeout: delivered.append(payload),
        clock=lambda: NOW,
    )

    with pytest.raises(
        adapter.Q38GcpAdapterError,
        match="provider inventory is not ready",
    ):
        provider.deliver_instance(delivery, now_unix=NOW)

    assert delivered == []


def test_delivery_failure_does_not_expose_key_or_provider_output(
    tmp_path: Path,
) -> None:
    plan, fake, _provider = _make(tmp_path)
    fake.populate_all()
    delivery = _delivery(plan, fake)
    provider = adapter.GcpAdapter(
        plan,
        tmp_path / "source",
        runner=fake,
        delivery_runner=lambda _argv, _payload, _timeout: adapter.CommandResult(
            1,
            b"",
            DELIVERY_KEY,
        ),
        clock=lambda: NOW,
    )

    with pytest.raises(adapter.Q38GcpAdapterError) as captured:
        provider.deliver_instance(delivery, now_unix=NOW)

    message = str(captured.value).encode()
    assert DELIVERY_KEY not in message
    assert DELIVERY_KEY.hex().encode() not in message
