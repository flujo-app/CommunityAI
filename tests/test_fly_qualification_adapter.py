import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from drift.model_manifest import ModelManifest
from scripts import (
    fly_qualification_adapter as adapter,
    fly_qualification_node as node,
    qualify_model_multimachine as multi,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = (
    REPOSITORY_ROOT / "manifests" / "candidates" / "qwen3.5-2b-bfloat16-eager.json",
    REPOSITORY_ROOT / "manifests" / "candidates" / "gemma-4-e2b-it-bfloat16-eager.json",
)


def _peer(letter: str) -> str:
    return "Qm" + letter * 44


def test_fly_api_uses_existing_flyctl_login_when_environment_token_is_absent(monkeypatch):
    monkeypatch.delenv("FLY_API_TOKEN", raising=False)
    calls = []

    def runner(command, *, timeout):
        calls.append((command, timeout))
        return adapter._CompletedExec(returncode=0, stdout="native-fly-session-token\n")

    api = adapter.FlyAPI.from_authentication(
        "qualification-app",
        timeout=45,
        flyctl="custom-flyctl",
        runner=runner,
    )

    assert calls == [(["custom-flyctl", "auth", "token"], 30)]
    assert api._token == "native-fly-session-token"


def test_fly_api_keeps_explicit_headless_token_without_calling_flyctl(monkeypatch):
    monkeypatch.setenv("FLY_API_TOKEN", "headless-session-token")

    def unexpected_runner(command, *, timeout):
        raise AssertionError(f"flyctl should not run: {command}, {timeout}")

    api = adapter.FlyAPI.from_authentication(
        "qualification-app",
        runner=unexpected_runner,
    )

    assert api._token == "headless-session-token"


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("headless token", id="space"),
        pytest.param("headless\ntoken", id="newline"),
        pytest.param("x" * 8193, id="oversized"),
    ],
)
def test_fly_api_rejects_malformed_explicit_token_without_flyctl_or_leak(monkeypatch, token):
    monkeypatch.setenv("FLY_API_TOKEN", token)

    def unexpected_runner(command, *, timeout):
        raise AssertionError(f"flyctl should not run: {command}, {timeout}")

    with pytest.raises(adapter.AdapterError, match="missing or invalid") as captured:
        adapter.FlyAPI.from_authentication(
            "qualification-app",
            runner=unexpected_runner,
        )

    assert token not in str(captured.value)


def test_fly_api_rejects_unavailable_or_malformed_native_authentication(monkeypatch):
    monkeypatch.delenv("FLY_API_TOKEN", raising=False)

    with pytest.raises(adapter.AdapterError, match="existing flyctl login is unavailable"):
        adapter.FlyAPI.from_authentication(
            "qualification-app",
            runner=lambda command, *, timeout: adapter._CompletedExec(returncode=1, stdout="private provider error"),
        )

    with pytest.raises(adapter.AdapterError, match="missing or invalid"):
        adapter.FlyAPI.from_authentication(
            "qualification-app",
            runner=lambda command, *, timeout: adapter._CompletedExec(returncode=0, stdout="token\nextra-output\n"),
        )


class FakeFlyAPI:
    def __init__(self, *, fail_after_create=None):
        self.app = "qualification-app"
        self.machines = {}
        self.created = 0
        self.fail_after_create = fail_after_create
        self.hard_kills = []
        self.destroyed = []

    def list_run_machines(self, run_id):
        return [
            machine
            for machine in self.machines.values()
            if machine["config"]["metadata"]["communityai_qualification_run"] == run_id
            and machine["state"] != "destroyed"
        ]

    def create_machine(self, payload):
        self.created += 1
        machine_id = f"machine{self.created}"
        machine = {
            "id": machine_id,
            "private_ip": f"fdaa::{self.created}",
            "instance_id": f"instance{self.created}",
            "state": "started",
            "config": payload["config"],
        }
        self.machines[machine_id] = machine
        if self.fail_after_create == self.created:
            raise adapter.AdapterError("simulated create failure")
        return machine

    def wait_state(
        self,
        machine_id,
        state,
        *,
        timeout,
        instance_id=None,
        allow_not_found=False,
    ):
        machine = self.machines.get(machine_id)
        if machine is None:
            assert allow_not_found
            return
        assert state == machine["state"] or (state == "destroyed" and machine["state"] == "destroyed")

    def get_machine(self, machine_id, *, allow_not_found=False):
        machine = self.machines.get(machine_id)
        if machine is None or machine["state"] == "destroyed":
            if allow_not_found:
                return None
            raise adapter.ProviderNotFound("missing")
        return machine

    def hard_kill(self, record, *, run_id, timeout):
        machine = self.machines[record.provider_machine_id]
        assert machine["state"] == "started"
        metadata = machine["config"]["metadata"]
        assert metadata["communityai_qualification_run"] == run_id
        assert metadata["communityai_qualification_resource"] == record.resource_id
        machine["state"] = "stopped"
        self.hard_kills.append(record.resource_id)

    def destroy_machine(self, machine_id, *, timeout):
        machine = self.machines[machine_id]
        machine["state"] = "destroyed"
        self.destroyed.append(machine_id)


class FakeIdentityReader:
    def read_peer_id(self, app, machine_id):
        assert app == "qualification-app"
        index = int(machine_id.removeprefix("machine"))
        return _peer(chr(ord("A") + index - 1))


class DelayedVisibilityFlyAPI(FakeFlyAPI):
    """Hide an ambiguous create from the first cleanup list response."""

    def __init__(self):
        super().__init__(fail_after_create=3)
        self.cleanup_scans = 0

    def list_run_machines(self, run_id):
        machines = super().list_run_machines(run_id)
        if self.created >= 3:
            self.cleanup_scans += 1
            if self.cleanup_scans == 1:
                return [machine for machine in machines if machine["id"] != "machine3"]
        return machines


class RepeatedIdentityReader:
    def read_peer_id(self, app, machine_id):
        assert app == "qualification-app"
        return _peer("R")


def _options(tmp_path, *, run_id="fly-qualification-a"):
    return adapter.ProvisionOptions(
        run_id=run_id,
        app="qualification-app",
        image="registry.fly.io/communityai-qualification@sha256:" + "a" * 64,
        bootstrap_region="iad",
        worker_regions=("iad", "ord", "dfw", "sjc"),
        remote_node_script="/workspace/scripts/fly_qualification_node.py",
        remote_manifest="/workspace/model-manifest.json",
        remote_cache_dir="/cache",
        identity_path="/tmp/communityai-qualification.id",
        device="cpu",
        port=31337,
        cpu_kind="performance",
        cpus=4,
        memory_mb=16384,
        rootfs_size_gb=9,
        machine_timeout=30,
        identity_timeout=30,
        state_output=tmp_path / "private" / "state.json",
        topology_output=tmp_path / "private" / "topology.json",
        control_output=tmp_path / "private" / "control.json",
    )


@pytest.mark.parametrize("manifest_path", CANDIDATES)
def test_provision_builds_controller_accepted_disjoint_split_topology(tmp_path, manifest_path):
    manifest = ModelManifest.load(manifest_path)
    options = _options(tmp_path)
    api = FakeFlyAPI()

    state = adapter.provision(manifest, options, api=api, identity_reader=FakeIdentityReader())

    assert state.status == "ready"
    assert len(state.resources) == 5
    assert len({record.provider_machine_id for record in state.resources}) == 5
    topology = multi.load_topology(options.topology_output, manifest)
    assert topology.num_blocks == manifest.model.num_blocks
    assert topology.routes[0].peer_ids == (_peer("B"), _peer("C"))
    assert topology.routes[1].peer_ids == (_peer("D"), _peer("E"))
    split = manifest.model.num_blocks // 2
    assert topology.expected_peers(0) == frozenset({_peer("B"), _peer("D")})
    assert topology.expected_peers(split) == frozenset({_peer("C"), _peer("E")})
    for machine in api.machines.values():
        assert machine["config"]["guest"] == {
            "cpu_kind": "performance",
            "cpus": 4,
            "memory_mb": 16384,
        }
        assert machine["config"]["rootfs"] == {"size_gb": 9}
        assert machine["config"]["env"]["COMMUNITYAI_QUALIFICATION_DEVICE"] == "cpu"

    plan = multi.load_control_plan(options.control_output, topology)
    assert set(plan.interrupt_commands) == set(topology.worker_by_peer)
    assert all(isinstance(command, tuple) for command in plan.interrupt_commands.values())
    assert plan.execution_directory == options.control_output.resolve().parent
    control_text = options.control_output.read_text(encoding="utf-8")
    assert str(options.state_output.resolve()) not in control_text
    commands = (*plan.interrupt_commands.values(), plan.cleanup_command)
    for command in commands:
        state_argument = command[command.index("--state") + 1]
        assert state_argument == options.state_output.name
        assert not Path(state_argument).is_absolute()
    assert "FLY_API_TOKEN" not in control_text
    assert "FLY_API_TOKEN" not in options.state_output.read_text(encoding="utf-8")


@pytest.mark.parametrize("rootfs_size_gb", [0, 1025])
def test_provision_options_reject_out_of_bounds_rootfs_before_create(tmp_path, rootfs_size_gb):
    args = adapter.build_parser().parse_args(
        [
            "provision",
            str(CANDIDATES[0]),
            "--run-id",
            "fly-qualification-a",
            "--app",
            "qualification-app",
            "--image",
            "registry.fly.io/communityai-qualification:test",
            "--region",
            "iad",
            "--remote-manifest",
            "/workspace/model-manifest.json",
            "--rootfs-size-gb",
            str(rootfs_size_gb),
            "--state-output",
            str(tmp_path / "state.json"),
            "--topology-output",
            str(tmp_path / "topology.json"),
            "--control-output",
            str(tmp_path / "control.json"),
        ]
    )

    with pytest.raises(adapter.AdapterError, match="rootfs-size-gb"):
        adapter._options_from_args(args)


def test_provision_rejects_separate_state_and_control_directories_before_create(tmp_path):
    manifest = ModelManifest.load(CANDIDATES[0])
    options = replace(_options(tmp_path), control_output=tmp_path / "other-private" / "control.json")
    api = FakeFlyAPI()

    with pytest.raises(adapter.AdapterError, match="must share one directory"):
        adapter.provision(manifest, options, api=api, identity_reader=FakeIdentityReader())

    assert api.created == 0


def test_provision_rejects_non_cpu_device_before_create(tmp_path):
    manifest = ModelManifest.load(CANDIDATES[0])
    options = replace(_options(tmp_path), device="cuda")
    api = FakeFlyAPI()

    with pytest.raises(adapter.AdapterError, match="CPU-only; --device must be cpu"):
        adapter.provision(manifest, options, api=api, identity_reader=FakeIdentityReader())

    assert api.created == 0


def test_provision_rejects_duplicate_peer_and_cleans_every_machine(tmp_path):
    manifest = ModelManifest.load(CANDIDATES[0])
    options = _options(tmp_path)
    api = FakeFlyAPI()

    with pytest.raises(adapter.AdapterError, match="repeated a stable PeerID"):
        adapter.provision(manifest, options, api=api, identity_reader=RepeatedIdentityReader())

    assert api.created == 2
    assert all(machine["state"] == "destroyed" for machine in api.machines.values())
    state = adapter.load_state(options.state_output, require_ready=False)
    assert state.status == "cleaned_after_failure"
    assert [record.resource_id for record in state.resources] == ["bootstrap-a"]


def test_provision_outer_trap_cleans_ambiguous_partial_create(tmp_path):
    manifest = ModelManifest.load(CANDIDATES[0])
    options = _options(tmp_path)
    api = FakeFlyAPI(fail_after_create=3)

    with pytest.raises(adapter.AdapterError, match="simulated create failure"):
        adapter.provision(manifest, options, api=api, identity_reader=FakeIdentityReader())

    assert api.created == 3
    assert all(machine["state"] == "destroyed" for machine in api.machines.values())
    state = adapter.load_state(options.state_output, require_ready=False)
    assert state.status == "cleaned_after_failure"


def test_provision_outer_trap_reconciles_delayed_partial_create(tmp_path):
    manifest = ModelManifest.load(CANDIDATES[0])
    options = _options(tmp_path)
    api = DelayedVisibilityFlyAPI()

    with pytest.raises(adapter.AdapterError, match="simulated create failure"):
        adapter.provision(manifest, options, api=api, identity_reader=FakeIdentityReader())

    assert api.cleanup_scans >= adapter.CLEANUP_RECONCILIATION_CONFIRMATIONS + 1
    assert api.machines["machine3"]["state"] == "destroyed"
    assert all(machine["state"] == "destroyed" for machine in api.machines.values())
    assert adapter.load_state(options.state_output, require_ready=False).status == "cleaned_after_failure"


def test_provision_outer_trap_cleans_when_private_input_finalization_fails(tmp_path, monkeypatch):
    manifest = ModelManifest.load(CANDIDATES[0])
    options = _options(tmp_path)
    api = FakeFlyAPI()
    atomic_json = adapter._atomic_json

    def fail_topology(path, value, *, private):
        if path == options.topology_output:
            raise OSError("simulated topology write failure")
        atomic_json(path, value, private=private)

    monkeypatch.setattr(adapter, "_atomic_json", fail_topology)

    with pytest.raises(OSError, match="simulated topology write failure"):
        adapter.provision(manifest, options, api=api, identity_reader=FakeIdentityReader())

    assert api.created == 5
    assert all(machine["state"] == "destroyed" for machine in api.machines.values())
    assert adapter.load_state(options.state_output, require_ready=False).status == "cleaned_after_failure"


def test_control_proves_selected_sigkill_and_complete_cleanup(tmp_path):
    manifest = ModelManifest.load(CANDIDATES[0])
    options = _options(tmp_path)
    api = FakeFlyAPI()
    state = adapter.provision(manifest, options, api=api, identity_reader=FakeIdentityReader())
    worker = state.by_resource["worker-a"]
    common = {
        "COMMUNITYAI_QUALIFICATION_RUN_ID": state.run_id,
        "COMMUNITYAI_QUALIFICATION_NONCE": "fresh-controller-nonce",
    }

    interrupt = adapter.control(
        options.state_output,
        expect_resource=worker.resource_id,
        environment={
            **common,
            "COMMUNITYAI_QUALIFICATION_ACTION": "interrupt",
            "COMMUNITYAI_QUALIFICATION_PEER_ID": worker.peer_id,
            "COMMUNITYAI_QUALIFICATION_MACHINE_ID": worker.machine_label,
            "COMMUNITYAI_QUALIFICATION_RESOURCE_ID": worker.resource_id,
        },
        api_factory=lambda app: api,
    )

    assert interrupt == {
        "schema_version": 1,
        "action": "interrupt",
        "run_id": state.run_id,
        "nonce": "fresh-controller-nonce",
        "peer_id": worker.peer_id,
        "machine_id": worker.machine_label,
        "resource_id": worker.resource_id,
        "hard_kill": True,
        "process_exited": True,
    }
    assert api.hard_kills == ["worker-a"]

    cleanup = adapter.control(
        options.state_output,
        expect_resource=None,
        environment={
            **common,
            "COMMUNITYAI_QUALIFICATION_ACTION": "cleanup",
        },
        api_factory=lambda app: api,
    )
    assert cleanup["cleaned"] is True
    assert cleanup["remaining_resources"] == []
    assert cleanup["destroyed_resources"] == [
        "bootstrap-a",
        "worker-a",
        "worker-b",
        "worker-c",
        "worker-d",
    ]
    assert adapter.load_state(options.state_output, require_ready=True).status == "cleaned"


def test_cleanup_rejects_changed_metadata_after_destroying_run_resources(tmp_path):
    manifest = ModelManifest.load(CANDIDATES[0])
    options = _options(tmp_path)
    api = FakeFlyAPI()
    state = adapter.provision(manifest, options, api=api, identity_reader=FakeIdentityReader())
    worker = state.by_resource["worker-a"]
    api.machines[worker.provider_machine_id]["config"]["metadata"]["communityai_qualification_resource"] = "worker-z"

    with pytest.raises(adapter.AdapterError, match="metadata changed"):
        adapter.control(
            options.state_output,
            expect_resource=None,
            environment={
                "COMMUNITYAI_QUALIFICATION_ACTION": "cleanup",
                "COMMUNITYAI_QUALIFICATION_RUN_ID": state.run_id,
                "COMMUNITYAI_QUALIFICATION_NONCE": "fresh-controller-nonce",
            },
            api_factory=lambda app: api,
        )

    assert len(api.destroyed) == 5
    assert all(machine["state"] == "destroyed" for machine in api.machines.values())


def test_cleanup_does_not_claim_destruction_for_missing_tracked_machine(tmp_path):
    manifest = ModelManifest.load(CANDIDATES[0])
    options = _options(tmp_path)
    api = FakeFlyAPI()
    state = adapter.provision(manifest, options, api=api, identity_reader=FakeIdentityReader())
    missing = state.by_resource["worker-a"]
    del api.machines[missing.provider_machine_id]

    with pytest.raises(adapter.AdapterError, match="could not prove destruction"):
        adapter.control(
            options.state_output,
            expect_resource=None,
            environment={
                "COMMUNITYAI_QUALIFICATION_ACTION": "cleanup",
                "COMMUNITYAI_QUALIFICATION_RUN_ID": state.run_id,
                "COMMUNITYAI_QUALIFICATION_NONCE": "fresh-controller-nonce",
            },
            api_factory=lambda app: api,
        )

    assert adapter.load_state(options.state_output, require_ready=True).status == "ready"
    assert all(machine["state"] == "destroyed" for machine in api.machines.values())


def test_control_rejects_worker_identity_mismatch_before_provider_call(tmp_path):
    manifest = ModelManifest.load(CANDIDATES[0])
    options = _options(tmp_path)
    api = FakeFlyAPI()
    state = adapter.provision(manifest, options, api=api, identity_reader=FakeIdentityReader())
    worker = state.by_resource["worker-a"]

    with pytest.raises(adapter.AdapterError, match="does not match"):
        adapter.control(
            options.state_output,
            expect_resource=worker.resource_id,
            environment={
                "COMMUNITYAI_QUALIFICATION_ACTION": "interrupt",
                "COMMUNITYAI_QUALIFICATION_RUN_ID": state.run_id,
                "COMMUNITYAI_QUALIFICATION_NONCE": "fresh-controller-nonce",
                "COMMUNITYAI_QUALIFICATION_PEER_ID": _peer("Z"),
                "COMMUNITYAI_QUALIFICATION_MACHINE_ID": worker.machine_label,
                "COMMUNITYAI_QUALIFICATION_RESOURCE_ID": worker.resource_id,
            },
            api_factory=lambda app: api,
        )

    assert api.hard_kills == []


def test_machine_exec_uses_shell_free_argv_and_bounded_identity_marker():
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=adapter._IDENTITY_MARKER + json.dumps({"schema_version": 1, "peer_id": _peer("P")}) + "\n",
            stderr="",
        )

    reader = adapter.FlyMachineExec(
        executable="flyctl",
        remote_node_script="/workspace/scripts/fly_qualification_node.py",
        timeout=2,
        runner=runner,
        poll_interval=0,
    )

    assert reader.read_peer_id("qualification-app", "machine123") == _peer("P")
    command, kwargs = calls[0]
    assert command == [
        "flyctl",
        "machine",
        "exec",
        "machine123",
        "python -u /workspace/scripts/fly_qualification_node.py identity",
        "--app",
        "qualification-app",
        "--timeout",
        "15",
    ]
    assert kwargs["shell"] is False
    with pytest.raises(adapter.AdapterError, match="exactly one"):
        reader.parse_peer_output(
            adapter._IDENTITY_MARKER + json.dumps({"schema_version": 1, "peer_id": _peer("P")}),
            adapter._IDENTITY_MARKER + json.dumps({"schema_version": 1, "peer_id": _peer("Q")}),
        )


def test_default_machine_exec_runner_enforces_output_bound():
    command = [
        sys.executable,
        "-c",
        f"import sys; sys.stdout.write('x' * {adapter.MAX_EXEC_OUTPUT_BYTES + 1})",
    ]

    with pytest.raises(adapter.AdapterError, match="bounded limit"):
        adapter._run_bounded_argv(command, timeout=5)


def test_default_machine_exec_runner_detects_sleeping_one_byte_overflow_immediately():
    command = [
        sys.executable,
        "-c",
        (
            "import sys,time;"
            f"sys.stdout.buffer.write(b'x' * {adapter.MAX_EXEC_OUTPUT_BYTES + 1});"
            "sys.stdout.buffer.flush();time.sleep(30)"
        ),
    ]
    started = time.monotonic()

    with pytest.raises(adapter.AdapterError, match="bounded limit"):
        adapter._run_bounded_argv(command, timeout=10)

    assert time.monotonic() - started < 5


def test_bounded_runner_keeps_stderr_separate_from_stdout():
    command = [
        sys.executable,
        "-c",
        "import sys;sys.stdout.write('token');sys.stderr.write('diagnostic')",
    ]

    completed = adapter._run_bounded_argv(command, timeout=5)

    assert completed.stdout == "token"
    assert completed.stderr == "diagnostic"


def test_state_read_enforces_bound_before_json_decode(tmp_path):
    state_path = tmp_path / "oversized-state.json"
    state_path.write_bytes(b"{" + b"x" * adapter.MAX_PROVIDER_RESPONSE_BYTES)

    with pytest.raises(adapter.AdapterError, match="bounded JSON limit"):
        adapter.load_state(state_path, require_ready=False)


def test_state_rejects_duplicate_provider_resources(tmp_path):
    manifest = ModelManifest.load(CANDIDATES[0])
    options = _options(tmp_path)
    state = adapter.provision(manifest, options, api=FakeFlyAPI(), identity_reader=FakeIdentityReader())
    document = state.to_dict()
    document["resources"][1]["provider_machine_id"] = document["resources"][0]["provider_machine_id"]
    options.state_output.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(adapter.AdapterError, match="repeats a provider machine"):
        adapter.load_state(options.state_output, require_ready=True)


@pytest.mark.parametrize("manifest_path", CANDIDATES)
def test_worker_entrypoint_uses_exact_manifest_runtime_and_split(monkeypatch, manifest_path):
    manifest = ModelManifest.load(manifest_path)
    split = manifest.model.num_blocks // 2
    monkeypatch.setenv("FLY_PRIVATE_IP", "fdaa::12")
    monkeypatch.setenv("COMMUNITYAI_QUALIFICATION_MANIFEST", str(manifest_path))
    monkeypatch.setenv("COMMUNITYAI_QUALIFICATION_BLOCKS", f"0:{split}")
    monkeypatch.setenv(
        "COMMUNITYAI_QUALIFICATION_INITIAL_PEER",
        f"/ip6/fdaa::1/tcp/31337/p2p/{_peer('S')}",
    )
    monkeypatch.setenv("COMMUNITYAI_QUALIFICATION_DEVICE", "cpu")

    command = node.build_worker_args()

    assert command[:3] == ["drift", "server", manifest.source.repository]
    assert command[command.index("--torch_dtype") + 1] == manifest.runtime.dtype
    assert command[command.index("--attn_implementation") + 1] == manifest.runtime.attention_implementation
    assert command[command.index("--quant_type") + 1] == manifest.runtime.quantization
    assert command[command.index("--block_indices") + 1] == f"0:{split}"
    assert command[command.index("--announce_maddrs") + 1] == "/ip6/fdaa::12/tcp/31337"


@pytest.mark.parametrize(
    "address",
    [
        "not-an-ip",
        "192.0.2.1",
        "2001:4860:4860::8888",
        "fe80::1",
        "fc00::1",
        "fdab::1",
    ],
)
def test_node_entrypoint_rejects_non_6pn_address(monkeypatch, address):
    monkeypatch.setenv("FLY_PRIVATE_IP", address)

    with pytest.raises(node.NodeConfigurationError, match="private|IPv6"):
        node.build_bootstrap_args()


@pytest.mark.parametrize("address", ["fc00::1", "fdab::1"])
def test_adapter_rejects_ula_addresses_outside_fly_6pn_prefix(address):
    with pytest.raises(adapter.AdapterError, match="6PN"):
        adapter._private_ip({"private_ip": address})


def test_worker_entrypoint_rejects_full_range(monkeypatch):
    manifest = ModelManifest.load(CANDIDATES[0])
    monkeypatch.setenv("FLY_PRIVATE_IP", "fdaa::12")
    monkeypatch.setenv("COMMUNITYAI_QUALIFICATION_MANIFEST", str(CANDIDATES[0]))
    monkeypatch.setenv(
        "COMMUNITYAI_QUALIFICATION_BLOCKS",
        f"0:{manifest.model.num_blocks}",
    )
    monkeypatch.setenv(
        "COMMUNITYAI_QUALIFICATION_INITIAL_PEER",
        f"/ip6/fdaa::1/tcp/31337/p2p/{_peer('S')}",
    )

    with pytest.raises(node.NodeConfigurationError, match="full manifested range"):
        node.build_worker_args()


def test_fly_api_accepts_current_deploy_token_shape():
    token = "FlyV1 fm2_" + "a" * 128

    api = adapter.FlyAPI(app="qualification-app", token=token)

    assert api._token == token


@pytest.mark.parametrize(
    "token",
    [
        "",
        " FlyV1 fm2_payload",
        "FlyV1 ",
        "FlyV1  fm2_payload",
        "FlyV1\tfm2_payload",
        "opaque token",
        "opaque\ntoken",
        "opaque\x7ftoken",
    ],
)
def test_fly_api_rejects_missing_or_malformed_auth_tokens(token):
    with pytest.raises(adapter.AdapterError, match="authentication token"):
        adapter.FlyAPI(app="qualification-app", token=token)


def test_hard_kill_wait_is_bound_to_the_selected_machine_instance(monkeypatch):
    api = adapter.FlyAPI(app="qualification-app", token="test-token")
    calls = []
    machine_reads = 0

    def request(method, suffix, *, payload=None, allow_not_found=False):
        nonlocal machine_reads
        calls.append((method, suffix, payload))
        if method == "GET" and "/wait?" not in suffix:
            machine_reads += 1
            return {
                "id": "machine123",
                "instance_id": "instanceABC123",
                "state": "started" if machine_reads == 1 else "stopped",
                "config": {
                    "metadata": {
                        "communityai_qualification_run": "qualification-run-a",
                        "communityai_qualification_resource": "worker-a",
                    }
                },
            }
        return {}

    monkeypatch.setattr(api, "_request", request)
    record = adapter.MachineRecord(
        resource_id="worker-a",
        machine_label="host-a",
        provider_machine_id="machine123",
        role="worker",
        peer_id=_peer("A"),
        spans=((0, 12),),
    )

    api.hard_kill(record, run_id="qualification-run-a", timeout=30)

    stop_call = next(call for call in calls if call[0] == "POST")
    assert stop_call[2] == {"signal": "SIGKILL", "timeout": "0"}
    wait_call = next(call for call in calls if "/wait?" in call[1])
    assert "state=stopped" in wait_call[1]
    assert "instance_id=instanceABC123" in wait_call[1]


def test_api_errors_do_not_expose_token_endpoint_or_provider_body():
    token = "super-secret-fly-token"

    def opener(request, timeout):
        raise adapter.urllib.error.HTTPError(
            request.full_url,
            500,
            "provider body contains private endpoint fdaa::1",
            {},
            None,
        )

    api = adapter.FlyAPI(
        app="qualification-app",
        token=token,
        opener=opener,
    )
    with pytest.raises(adapter.AdapterError) as captured:
        api.list_run_machines("qualification-run-a")

    message = str(captured.value)
    assert token not in message
    assert "fdaa::1" not in message
    assert adapter.DEFAULT_API_BASE not in message
