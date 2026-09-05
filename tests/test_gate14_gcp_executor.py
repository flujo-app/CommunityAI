from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gate14_gcp_executor as executor  # noqa: E402
import gate14_run_controller as controller  # noqa: E402

RUN_ID = "gate14-20260902-a"
SOURCE = "1" * 40
NOW = 2_000_000_000


def client(platform: str) -> controller.ClientPlan:
    return controller.ClientPlan(
        platform=platform,
        instance=f"{RUN_ID}-{platform}",
        disk=f"{RUN_ID}-{platform}-disk",
        source_commit=SOURCE,
        termination_unix=NOW + 7_200,
        package_sha256="sha256:" + ("a" if platform == "windows" else "b") * 64,
        model_id="Qwen3.5 2B" if platform == "windows" else "Gemma 4 E2B IT",
        manifest_digest=(
            "sha256:3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33"
            if platform == "windows"
            else "sha256:2f8debbe0fcdf5af8d4c56c982210fa50aa584314968ae2617e2ccc2de9eafdd"
        ),
        machine_type="g2-standard-8",
        image_project="windows-cloud" if platform == "windows" else "ubuntu-os-cloud",
        image=("windows-server-2022-dc-v20260814" if platform == "windows" else "ubuntu-2404-noble-amd64-v20260826"),
        boot_disk_gib=100,
        boot_disk_type="pd-balanced",
        max_run_seconds=7_200,
    )


def plan() -> controller.RunPlan:
    return controller.RunPlan(
        run_id=RUN_ID,
        authorization_sha256="sha256:" + "c" * 64,
        provider_plan_digest="sha256:" + "d" * 64,
        source_commit=SOURCE,
        ledger_state="RESERVED",
        project="community-ai-506321",
        zone="us-central1-a",
        windows=client("windows"),
        linux=client("linux"),
    )


def jobs(**states) -> dict:
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "clients": {
            platform: {
                "job_state": states.get(platform, "absent"),
                "attempt_ordinal": 0 if states.get(platform, "absent") == "absent" else 1,
                "evidence_digest": None,
            }
            for platform in ("windows", "linux")
        },
    }


def action_state(run_plan: controller.RunPlan, action: str) -> dict:
    state = controller.initial_state(run_plan)
    if action == "start_windows":
        state["next_action"] = action
        return state
    if action == "delete_windows":
        state.update(
            revision=1,
            phase="WINDOWS_DELETING",
            windows_consumed=True,
            windows_evidence_digest="sha256:" + "e" * 64,
            windows_challenge_sha256="sha256:" + "f" * 64,
            windows_challenge_consumed=True,
            next_action=action,
        )
        return state
    raise AssertionError(f"unsupported test action: {action}")


class FakeGcloud:
    def __init__(self, run_plan: controller.RunPlan):
        self.plan = run_plan
        self.instances: dict[str, dict] = {
            controller.PROTECTED_INSTANCE: {
                "name": controller.PROTECTED_INSTANCE,
                "status": "RUNNING",
            }
        }
        self.disks: dict[str, dict] = {}
        self.calls: list[tuple[str, ...]] = []

    @staticmethod
    def result(value=None, *, returncode=0, stderr=b""):
        stdout = b"" if value is None else json.dumps(value).encode("utf-8")
        return executor.CommandResult(returncode, stdout, stderr)

    @staticmethod
    def option(arguments: list[str], name: str) -> str:
        prefix = name + "="
        return next(item[len(prefix) :] for item in arguments if item.startswith(prefix))

    def disk_value(self, item: controller.ClientPlan) -> dict:
        return {
            "name": item.disk,
            "status": "READY",
            "labels": executor._labels(self.plan, item),
            "type": f"zones/{self.plan.zone}/diskTypes/{item.boot_disk_type}",
            "sourceImage": f"projects/{item.image_project}/global/images/{item.image}",
            "sizeGb": str(item.boot_disk_gib),
        }

    def instance_value(self, item: controller.ClientPlan) -> dict:
        return {
            "name": item.instance,
            "status": "RUNNING",
            "labels": executor._labels(self.plan, item),
            "machineType": f"zones/{self.plan.zone}/machineTypes/{item.machine_type}",
            "disks": [{"source": f"zones/{self.plan.zone}/disks/{item.disk}"}],
            "guestAccelerators": [
                {
                    "acceleratorType": f"zones/{self.plan.zone}/acceleratorTypes/nvidia-l4",
                    "acceleratorCount": 1,
                }
            ],
            "metadata": {
                "items": [
                    {"key": "communityai-run-id", "value": self.plan.run_id},
                    {"key": "communityai-source-commit", "value": item.source_commit},
                    {"key": "communityai-termination-unix", "value": str(item.termination_unix)},
                ]
            },
        }

    def __call__(self, argv, timeout):
        command = tuple(argv)
        self.calls.append(command)
        arguments = list(command[1:])
        assert arguments.pop() == "--quiet"

        if arguments[:2] == ["auth", "list"]:
            return executor.CommandResult(0, b"operator@example.invalid\n", b"")
        if arguments[:2] == ["projects", "describe"]:
            return self.result({"lifecycleState": "ACTIVE"})
        if arguments[:3] == ["compute", "accelerator-types", "describe"]:
            return self.result({"name": "nvidia-l4"})
        if arguments[:3] == ["compute", "images", "describe"]:
            return self.result({"name": arguments[3], "status": "READY"})
        if arguments[:3] == ["compute", "firewall-rules", "list"]:
            return self.result([])
        if arguments[:3] == ["compute", "project-info", "describe"]:
            return self.result({"quotas": [{"metric": "GPUS_ALL_REGIONS", "limit": 1, "usage": 0}]})
        if arguments[:3] == ["compute", "instances", "list"]:
            values = [
                value
                for name, value in self.instances.items()
                if name != controller.PROTECTED_INSTANCE and value.get("status") == "RUNNING"
            ]
            return self.result(values)

        if arguments[:2] == ["compute", "instances"] and arguments[2] == "describe":
            name = arguments[3]
            if name not in self.instances:
                return self.result(returncode=1, stderr=b"resource was not found")
            return self.result(self.instances[name])
        if arguments[:2] == ["compute", "disks"] and arguments[2] == "describe":
            name = arguments[3]
            if name not in self.disks:
                return self.result(returncode=1, stderr=b"resource was not found")
            return self.result(self.disks[name])

        if arguments[:3] == ["compute", "disks", "create"]:
            name = arguments[3]
            item = next(value for value in (self.plan.windows, self.plan.linux) if value.disk == name)
            self.disks[name] = self.disk_value(item)
            return self.result()
        if arguments[:3] == ["compute", "instances", "create"]:
            name = arguments[3]
            item = next(value for value in (self.plan.windows, self.plan.linux) if value.instance == name)
            self.instances[name] = self.instance_value(item)
            return self.result()
        if arguments[:3] == ["compute", "instances", "delete"]:
            name = arguments[3]
            item = next(value for value in (self.plan.windows, self.plan.linux) if value.instance == name)
            self.instances.pop(name, None)
            self.disks.pop(item.disk, None)
            return self.result()
        if arguments[:3] == ["compute", "disks", "delete"]:
            self.disks.pop(arguments[3], None)
            return self.result()

        raise AssertionError(f"unexpected command: {command}")


def test_clean_preflight_revalidates_auth_images_l4_and_bootstrap():
    run_plan = plan()
    fake = FakeGcloud(run_plan)
    provider = executor.GcpExecutor(run_plan, runner=fake, clock=lambda: NOW)

    result = provider.preflight(jobs())

    assert result["result"] == "passed"
    assert result["maximum_estimate_usd"] == "44.00"
    assert result["planned_resources_absent"] is True
    assert any(call[1:3] == ("auth", "list") for call in fake.calls)
    assert len([call for call in fake.calls if call[1:4] == ("compute", "images", "describe")]) == 2


def test_exact_start_and_delete_use_bound_disk_image_and_no_service_account():
    run_plan = plan()
    fake = FakeGcloud(run_plan)
    provider = executor.GcpExecutor(run_plan, runner=fake, clock=lambda: NOW)

    provider.execute(
        "start_windows",
        state=action_state(run_plan, "start_windows"),
        jobs=jobs(),
    )

    assert run_plan.windows.instance in fake.instances
    assert run_plan.windows.disk in fake.disks
    create = next(call for call in fake.calls if call[1:4] == ("compute", "instances", "create"))
    assert "--no-service-account" in create
    assert "--no-address" in create
    assert "--max-run-duration=7200s" in create
    observation = provider.inventory(jobs(windows="starting"))
    assert observation["l4_usage"] == 1
    assert observation["instances"][run_plan.windows.instance]["present"] is True

    completed_jobs = jobs(windows="passed")
    completed_jobs["clients"]["windows"]["evidence_digest"] = "sha256:" + "e" * 64
    provider.execute(
        "delete_windows",
        state=action_state(run_plan, "delete_windows"),
        jobs=completed_jobs,
    )

    assert run_plan.windows.instance not in fake.instances
    assert run_plan.windows.disk not in fake.disks


def test_foreign_exact_name_instance_fails_closed_before_mutation():
    run_plan = plan()
    fake = FakeGcloud(run_plan)
    fake.instances[run_plan.windows.instance] = fake.instance_value(run_plan.windows)
    fake.instances[run_plan.windows.instance]["labels"]["communityai-run"] = "foreign-run"
    fake.disks[run_plan.windows.disk] = fake.disk_value(run_plan.windows)
    provider = executor.GcpExecutor(run_plan, runner=fake, clock=lambda: NOW)

    with pytest.raises(executor.Gate14GcpError, match="ownership"):
        provider.inventory(jobs(windows="starting"))

    assert not any(call[1:4] == ("compute", "instances", "delete") for call in fake.calls)


def test_same_image_name_from_foreign_project_fails_closed():
    run_plan = plan()
    fake = FakeGcloud(run_plan)
    fake.disks[run_plan.windows.disk] = fake.disk_value(run_plan.windows)
    fake.disks[run_plan.windows.disk][
        "sourceImage"
    ] = f"projects/foreign-project/global/images/{run_plan.windows.image}"
    provider = executor.GcpExecutor(run_plan, runner=fake, clock=lambda: NOW)

    with pytest.raises(executor.Gate14GcpError, match="shape"):
        provider.inventory(jobs())

    assert not any(call[1:4] == ("compute", "disks", "delete") for call in fake.calls)


def test_attached_service_account_fails_closed():
    run_plan = plan()
    fake = FakeGcloud(run_plan)
    fake.instances[run_plan.windows.instance] = fake.instance_value(run_plan.windows)
    fake.instances[run_plan.windows.instance]["serviceAccounts"] = [
        {
            "email": "unexpected@example.invalid",
            "scopes": ["https://www.googleapis.com/auth/cloud-platform"],
        }
    ]
    fake.disks[run_plan.windows.disk] = fake.disk_value(run_plan.windows)
    provider = executor.GcpExecutor(run_plan, runner=fake, clock=lambda: NOW)

    with pytest.raises(executor.Gate14GcpError, match="shape"):
        provider.inventory(jobs(windows="starting"))

    assert not any(call[1:4] == ("compute", "instances", "delete") for call in fake.calls)


def test_execute_requires_fresh_inventory_and_bound_controller_action():
    run_plan = plan()
    fake = FakeGcloud(run_plan)
    provider = executor.GcpExecutor(run_plan, runner=fake, clock=lambda: NOW)

    with pytest.raises(executor.Gate14GcpError, match="stale or unbound"):
        provider.execute(
            "start_windows",
            state=controller.initial_state(run_plan),
            jobs=jobs(),
        )

    assert any(call[1:3] == ("auth", "list") for call in fake.calls)
    assert not any(call[1:4] == ("compute", "instances", "create") for call in fake.calls)


def test_jobs_default_to_absent_and_reject_wrong_run(tmp_path):
    run_plan = plan()
    missing = executor.load_jobs(tmp_path / "missing.json", run_plan)
    assert missing["clients"]["windows"]["job_state"] == "absent"

    path = tmp_path / "jobs.json"
    value = jobs()
    value["run_id"] = "gate14-20260902-b"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(executor.Gate14GcpError, match="run changed"):
        executor.load_jobs(path, run_plan)
