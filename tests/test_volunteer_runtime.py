"""Volunteer startup must require a fresh Start and keep local inference off GPUs."""

import json
import sys
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from drift.cli import run_node
from drift.node.config import NodeConfig
from drift.node.worker_supervisor import WorkerLaunch, WorkerSupervisor, WorkerSupervisorSettings


@pytest.mark.parametrize("saved_device", [None, "auto", "cpu", "cuda:1"])
def test_cpu_only_runtime_preserves_saved_config_and_finite_budgets(tmp_path, monkeypatch, saved_device):
    path = tmp_path / "node.json"
    local = {
        "manifest": "local.json",
        "initial_peers": [],
        "execution": "local",
        "local_max_memory": "1GiB",
        "local_max_disk_space": "2GiB",
        "local_max_context": 512,
    }
    if saved_device is not None:
        local["local_device"] = saved_device
    source = {
        "schema_version": 1,
        "models": [local, {"manifest": "remote.json", "initial_peers": ["peer"]}],
    }
    path.write_text(json.dumps(source), encoding="utf-8")
    original = path.read_bytes()
    args = run_node.build_parser().parse_args(["--config", str(path), "--local_inference_cpu_only"])
    monkeypatch.setattr(run_node.torch.cuda, "is_available", lambda: pytest.fail("CPU selection must not probe CUDA"))
    # Every serve cycle applies the override, including a verified catalog restart.
    for _ in range(2):
        persisted, runtime = run_node._load_persisted_and_runtime_config(args)
        assert persisted == NodeConfig.load(path)
        assert runtime.models[0].local_device == "cpu"
        assert runtime.models[1] == persisted.models[1]
        assert runtime.models[0] == replace(persisted.models[0], local_device="cpu")
        assert runtime.models[0].local_max_memory_bytes == 1024**3
        assert runtime.models[0].local_max_disk_bytes == 2 * 1024**3
        assert path.read_bytes() == original
    normal = run_node.build_parser().parse_args(["--config", str(path)])
    persisted, runtime = run_node._load_persisted_and_runtime_config(normal)
    assert persisted == runtime


def test_start_paused_survives_replacement_and_policy_changes_until_explicit_start(tmp_path):
    marker = tmp_path / "ran.txt"
    launch = WorkerLaunch(
        "worker",
        "model",
        (
            sys.executable,
            "-c",
            "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('started'); time.sleep(30)",
            str(marker),
        ),
        auto_start=True,
        device="cpu",
    )
    supervisor = WorkerSupervisor([launch], poll_period=0.01, stop_timeout=2)
    args = run_node.build_parser().parse_args(["--config", "unused.json", "--pause_sharing_on_start"])
    try:
        run_node._apply_startup_pause(args, supervisor)
        supervisor.start_service()
        assert supervisor.snapshot("worker")["operator_paused"]
        assert not supervisor.snapshot("worker")["desired_running"]
        replacement = replace(launch, model_id="replacement-model")
        supervisor.replace_launch(replacement, start=True)
        supervisor.reconfigure(WorkerSupervisorSettings((replacement,), 2), persist=lambda: None)
        time.sleep(0.1)  # Several real monitor ticks must not start the paused launch.
        assert not marker.exists()
        assert supervisor.snapshot("worker")["pid"] is None
        supervisor.start_worker("worker")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            # Creation precedes write/close; an empty newly created file is not
            # the child's completed acknowledgement.
            if marker.exists() and marker.read_text() == "started":
                break
            time.sleep(0.01)
        assert marker.read_text() == "started"
        assert not supervisor.snapshot("worker")["operator_paused"]
        supervisor.pause_worker("worker")
        assert supervisor.snapshot("worker")["pid"] is None
    finally:
        supervisor.shutdown()


def test_standard_startup_retains_existing_auto_start_intent():
    launch = WorkerLaunch("worker", "model", (sys.executable, "-c", "pass"), auto_start=True)
    supervisor = WorkerSupervisor([launch])
    try:
        args = run_node.build_parser().parse_args(["--config", "unused.json"])
        run_node._apply_startup_pause(args, supervisor)
        assert supervisor.snapshot("worker")["desired_running"]
        assert not supervisor.snapshot("worker")["operator_paused"]
    finally:
        supervisor.shutdown()


@pytest.mark.parametrize("reload_requested", [False, True])
def test_node_applies_profile_guards_before_services_and_registering_local_loaders(
    tmp_path, monkeypatch, reload_requested
):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "models": [
                    {"manifest": "local.json", "initial_peers": [], "execution": "local", "local_device": "cuda:0"}
                ],
            }
        )
    )
    parser = run_node.build_parser()
    args = parser.parse_args(
        ["--config", str(path), "--data_dir", str(tmp_path), "--pause_sharing_on_start", "--local_inference_cpu_only"]
    )
    launch = WorkerLaunch("worker", "model", (sys.executable, "-c", "pass"), auto_start=True)
    forbidden_spawn = Mock(side_effect=AssertionError("startup must not spawn contribution work"))
    supervisor = WorkerSupervisor([launch], popen=forbidden_spawn)
    manager = Mock()
    discovery = Mock()
    observations = []

    def manager_factory(config, **kwargs):
        assert config.models[0].local_device == "cpu"
        observations.append("cpu-loader-config")
        return manager, [SimpleNamespace(model_id="local")], discovery

    def placement_factory(*args, **kwargs):
        assert kwargs["require_explicit_start"] is True
        assert supervisor.snapshot("worker")["operator_paused"]
        assert not supervisor.snapshot("worker")["desired_running"]
        observations.append("paused-before-placement")
        return None

    monkeypatch.setattr(run_node, "_merge_cached_initial_peers", lambda config, cache: config)
    monkeypatch.setattr(run_node, "_build_model_manager", manager_factory)
    monkeypatch.setattr(run_node, "_build_worker_supervisor", lambda *args, **kwargs: supervisor)
    monkeypatch.setattr(run_node, "_build_automatic_placement_service", placement_factory)
    monkeypatch.setattr(run_node, "load_configured_catalog", lambda config: None)
    monkeypatch.setattr(run_node, "ContributionPolicyStore", Mock())
    monkeypatch.setattr(run_node, "_prepare_api_key_store", lambda *args: (Mock(), None, False))
    monkeypatch.setattr(run_node, "_prepare_control_key", lambda *args, **kwargs: ("test", None, False))
    monkeypatch.setattr(run_node, "tie_child_processes_to_this_process", lambda: None)
    monkeypatch.setattr("drift.node.hardware_status.HardwareStatus", Mock())
    create_app = Mock()
    server = Mock(should_exit=False)
    if reload_requested:
        server.run.side_effect = lambda: create_app.call_args.kwargs["request_restart"]()
    monkeypatch.setattr("drift.node.server.create_node_app", create_app)
    monkeypatch.setattr("uvicorn.Server", Mock(return_value=server))
    assert run_node._serve_once(args, parser) is reload_requested
    assert server.should_exit is reload_requested
    manager.shutdown.assert_called_once_with()
    assert observations == ["cpu-loader-config", "paused-before-placement"]
    forbidden_spawn.assert_not_called()
    assert NodeConfig.load(path).models[0].local_device == "cuda:0"
