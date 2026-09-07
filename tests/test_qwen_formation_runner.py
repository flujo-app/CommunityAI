import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from qwen_formation_node import LOCAL, REMOTE, node_config, selected
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


@pytest.mark.parametrize("zone", ["us-central1-b", "us-central1-c", "us-central1-f"])
def test_capacity_retry_can_use_another_approved_zone(zone):
    proposed = config()
    proposed["zone"] = zone
    validate_config(proposed)


def test_exact_manifest_selection_is_required():
    assert selected({"auto_selection": {"status": "selected", "manifest_digest": LOCAL}}, "local")
    assert not selected({"auto_selection": {"status": "selected", "manifest_digest": REMOTE}}, "local")
    assert not selected({"auto_selection": {"status": "waiting", "manifest_digest": REMOTE}}, "community")


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
    from drift.cli import run_node
    from types import SimpleNamespace

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
    from drift.cli import run_node
    from types import SimpleNamespace

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
