import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from qwen_formation_node import LOCAL, REMOTE, coverage, coverage_observed, node_config, selected
from run_qwen_formation import ROOT, FormationRun, validate_config

from drift.node.config import NodeConfig


def config():
    return json.loads((ROOT / "config/qwen_formation.json").read_text())


def test_formation_configuration_only_supplies_capacity(tmp_path):
    value = node_config(
        ROOT, tmp_path, {"peers": ["/ip4/127.0.0.1/tcp/31330"], "capacity_blocks": 16, "ip": "127.0.0.1"}
    )
    parsed = NodeConfig.from_dict(value, base_dir=ROOT)
    assert parsed.workers[0].model == "auto"
    assert parsed.workers[0].num_blocks == 16
    assert parsed.workers[0].block_indices is None
    assert parsed.inference_mode == "auto"
    assert value["catalog_path"] == str(ROOT / "public-alpha/catalog-qwen-v2/catalog.signed.json")


@pytest.mark.parametrize(
    "field,value",
    [
        ("spans", ["0:16"]),
        ("block_indices", "0:16"),
        ("worker_machine_type", "e2-highmem-4"),
        ("max_duration_seconds", 86400),
        ("zone", "europe-west1-b"),
    ],
)
def test_formation_rejects_assignments_or_unbounded_topology(field, value):
    proposed = config()
    proposed[field] = value
    with pytest.raises(ValueError):
        validate_config(proposed)


def test_host_configuration_cannot_smuggle_an_assignment(tmp_path):
    with pytest.raises(ValueError, match="assigned"):
        node_config(ROOT, tmp_path, {"span": "0:16"})


def test_desktop_policy_gates_startup_without_disabling_restart(tmp_path):
    value = node_config(
        ROOT,
        tmp_path,
        {
            "peers": ["/ip4/127.0.0.1/tcp/31330"],
            "capacity_blocks": 16,
            "ip": "203.0.113.4",
            "public_port": 43210,
            "desktop_driven_sharing": True,
        },
    )
    worker = NodeConfig.from_dict(value, base_dir=ROOT).workers[0]
    assert worker.enabled is True
    assert value["contribution_policy"]["sharing_enabled"] is False
    assert worker.block_indices is None
    assert worker.port == 31330
    assert worker.public_port == 43210


@pytest.mark.parametrize("automatic_start", [False, True])
def test_desktop_replays_literal_start_after_policy_save(tmp_path, automatic_start):
    from types import SimpleNamespace

    sys.path.insert(0, str(ROOT / "desktop/src"))
    from qwen_formation_desktop import FormationDesktop

    identity = "a" * 24
    (tmp_path / "desktop-command.json").write_text(
        json.dumps({"id": identity, "action": "start-sharing", "source": "local"})
    )
    calls = []
    contribution = {"policy": {"sharing_enabled": True}, "intent_enabled": automatic_start}

    def pause():
        calls.append("model-pause")
        contribution["intent_enabled"] = False

    def start():
        calls.append("master-start")
        contribution["intent_enabled"] = True

    checkbox = SimpleNamespace(
        accessibleName=lambda: "Share compute with Qwen",
        isChecked=lambda: contribution["intent_enabled"],
        isEnabled=lambda: True,
        click=pause,
    )
    window = SimpleNamespace(
        _controller=object(),
        _busy=False,
        _snapshot={"contribution": contribution, "workers": [{"model": "Qwen", "desired_running": True}]},
        _page_buttons=[None, None, SimpleNamespace(click=lambda: None)],
        findChildren=lambda kind: [checkbox],
        master_share_button=SimpleNamespace(
            isEnabled=lambda: True,
            text=lambda: "Pause sharing" if contribution["intent_enabled"] else "Start sharing",
            click=start,
        ),
    )
    automation = FormationDesktop(tmp_path)
    automation.window = window
    automation.qt = {"QCheckBox": object}
    automation.tick()
    if automatic_start:
        assert calls == ["model-pause"]
        assert identity not in automation.clicked
        automation.tick()
    assert calls == (["model-pause", "master-start"] if automatic_start else ["master-start"])
    assert identity in automation.clicked


@pytest.mark.parametrize(
    "patch", [{"public_port": 0}, {"public_port": 65536}, {"public_port": True}, {"port": None}, {"public_ip": None}]
)
def test_public_tunnel_configuration_rejects_unusable_endpoints(tmp_path, patch):
    from drift.node.config import NodeConfigError

    value = node_config(
        ROOT,
        tmp_path,
        {"peers": ["/ip4/127.0.0.1/tcp/31330"], "capacity_blocks": 16, "ip": "203.0.113.4", "public_port": 43210},
    )
    value["workers"][0].update(patch)
    with pytest.raises(NodeConfigError):
        NodeConfig.from_dict(value, base_dir=ROOT)


@pytest.mark.parametrize("zone", ["us-central1-b", "us-central1-c", "us-central1-f"])
def test_capacity_retry_can_use_another_approved_zone(zone):
    proposed = config()
    proposed["zone"] = zone
    validate_config(proposed)


@pytest.mark.parametrize("machine", ["c3-highmem-4", "n2-highmem-4"])
def test_capacity_retry_keeps_the_same_cpu_and_memory_profile(machine):
    proposed = config()
    proposed["worker_machine_type"] = machine
    validate_config(proposed)


def test_exact_manifest_selection_is_required():
    assert selected({"auto_selection": {"status": "selected", "manifest_digest": LOCAL}}, "local")
    assert not selected({"auto_selection": {"status": "selected", "manifest_digest": REMOTE}}, "local")
    assert not selected({"auto_selection": {"status": "waiting", "manifest_digest": REMOTE}}, "community")


def test_unknown_discovery_cannot_satisfy_a_positive_coverage_checkpoint():
    snapshot = {"models": [{"manifest_digest": REMOTE, "route": {"status": "unknown", "covered_blocks": None}}]}
    assert not coverage(snapshot) >= 32
    assert not coverage_observed(snapshot)


def test_initial_discovery_requires_a_fresh_observation_even_with_no_workers():
    route = {"status": "incomplete", "covered_blocks": 0, "last_updated_age": 1}
    snapshot = {"models": [{"manifest_digest": REMOTE, "route": route}]}
    assert coverage_observed(snapshot)
    route["last_updated_age"] = 61
    assert not coverage_observed(snapshot)


@pytest.mark.parametrize(
    "stage",
    [
        "preflight",
        "bundle",
        "create_firewalls",
        "create_gcp",
        "stage",
        "wait_setup",
        "start_job",
        "start_participant",
        "start_desktop",
        "exercise",
    ],
)
def test_failure_always_records_outcome_and_cleans_after_mutation(tmp_path, monkeypatch, stage):
    import run_qwen_formation as module

    run = FormationRun(tmp_path / "q38af-test", config())
    events = []

    def step(name, result=None):
        def execute(*args, **kwargs):
            events.append(name)
            if name == stage:
                raise RuntimeError("injected " + name)
            return result

        return execute

    for name in (
        "preflight",
        "bundle",
        "create_firewalls",
        "stage",
        "wait_setup",
        "start_job",
        "start_participant",
        "start_desktop",
        "write_remote",
        "cloud",
        "exercise",
    ):
        monkeypatch.setattr(run, name, step(name))
    run.config["admin_ip"] = "127.0.0.1"
    monkeypatch.setattr(module.MixedRun, "create_gcp", step("create_gcp"))
    monkeypatch.setattr(run, "wait_file", step("wait_file", {"peers": []}))
    monkeypatch.setattr(run, "host_config", lambda *a: {})
    monkeypatch.setattr(run, "stop_desktop", step("stop_desktop"))
    monkeypatch.setattr(run, "capture", step("capture"))
    monkeypatch.setattr(run, "cleanup", step("cleanup", {"verified": True}))
    result = run.run()
    assert result["result"] == "failed"
    assert result["error"] == "RuntimeError: injected " + stage
    assert "stop_desktop" in events
    assert ("cleanup" in events) == (stage not in {"preflight", "bundle"})
    assert json.loads((run.path / "result.json").read_text())["result"] == "failed"
    assert json.loads((run.path / "qualification/run-state.json").read_text())["result"] == "failed"


def test_diagnostics_failure_cannot_prevent_cloud_cleanup(tmp_path, monkeypatch):
    run = FormationRun(tmp_path / "q38af-test", config())
    monkeypatch.setattr(run, "preflight", lambda: None)
    monkeypatch.setattr(run, "bundle", lambda: None)
    monkeypatch.setattr(run, "create_firewalls", lambda: (_ for _ in ()).throw(RuntimeError("mutation")))
    monkeypatch.setattr(run, "capture", lambda: (_ for _ in ()).throw(RuntimeError("diagnostics")))
    calls = []
    monkeypatch.setattr(run, "cleanup", lambda: calls.append("cleanup") or {"verified": True})
    result = run.run()
    assert calls == ["cleanup"]
    assert result["diagnostic_error"] == "diagnostics"


def test_assignment_seed_uses_persistent_identity_not_installation_path(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from drift.cli import run_node

    # Same conventional basename on different machines must not be a common seed.
    paths = [tmp_path / name / "worker-identity.key" for name in ("a", "b")]
    seeds = []
    monkeypatch.setattr(run_node, "AutomaticContributionPlanner", lambda **kwargs: seeds.append(kwargs["jitter_seed"]))
    for path in [*paths, paths[0]]:
        worker = SimpleNamespace(worker_id="automatic", model="auto", num_blocks=16, identity_path=path)
        config = SimpleNamespace(workers=[worker], discovery_update_period=5, route_demand_authority_roots=())
        run_node._build_automatic_placement_service(
            config, None, None, None, None, token=None, config_path=None, peer_cache=None
        )
    assert seeds[0] != seeds[1]
    assert seeds[0] == seeds[2]


def test_unavailable_contribution_identity_does_not_abort_local_service(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from drift.cli import run_node

    def unavailable(*args):
        raise PermissionError("key temporarily inaccessible")

    monkeypatch.setattr(run_node.NodeIdentity, "ensure", unavailable)
    worker = SimpleNamespace(
        worker_id="automatic", model="auto", num_blocks=16, identity_path=tmp_path / "identity.key"
    )
    config = SimpleNamespace(workers=[worker], discovery_update_period=5, route_demand_authority_roots=())
    service = run_node._build_automatic_placement_service(
        config, None, None, None, None, token=None, config_path=None, peer_cache=None
    )
    assert service is not None
    assert run_node._automatic_placement_seed(worker) != run_node._automatic_placement_seed(worker)
