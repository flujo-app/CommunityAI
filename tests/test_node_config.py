import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from drift.cli import run_node as run_node_module
from drift.cli.run_node import (
    _build_automatic_placement_service,
    _build_model_manager,
    _build_worker_supervisor,
    _load_persisted_and_runtime_config,
    _merge_cached_initial_peers,
    _prepare_route_identity,
    _reuse_runtime_initial_peers,
)
from drift.model_manifest import ModelManifest
from drift.node.config import (
    NODE_CONFIG_SCHEMA_VERSION,
    ContributionPolicyConfig,
    ContributionScheduleConfig,
    NodeConfig,
    NodeConfigError,
    NodeModelConfig,
    WorkerConfig,
)
from drift.node.contribution_planner import (
    MAX_AUTOMATIC_PLACEMENT_BLOCKS,
    MAX_AUTOMATIC_PLACEMENT_CANDIDATES,
    PlacementDecision,
    PlacementPlan,
    PlacementRegistry,
)
from drift.node.discovery import PeerCache
from drift.node.model_manager import ModelRuntime, ModelState
from drift.node.route_metrics import RouteOutcomeTracker
from drift.node.worker_supervisor import WorkerPolicyError
from drift.protocol_identity import NodeIdentity, ProtocolSecurityError


def _config_dict(**overrides):
    source = {
        "schema_version": 1,
        "max_loaded_models": 1,
        "discovery_update_period": 12,
        "discovery_startup_timeout": 4,
        "models": [
            {
                "manifest": "manifests/one.json",
                "initial_peers": ["/ip4/127.0.0.1/tcp/31337/p2p/one"],
                "cache_dir": "cache/one",
                "revocation_files": ["trust/revoked.json"],
                "request_timeout": 7.5,
                "max_retries": 2,
            }
        ],
    }
    source.update(overrides)
    return source


def test_contribution_telemetry_providers_are_core_runtime_dependencies():
    pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    core_dependencies = pyproject.split("dependencies = [", 1)[1].split("\n]\n", 1)[0]

    assert '"psutil>=5.9"' in core_dependencies
    assert '"nvidia-ml-py>=12.535"' in core_dependencies


def test_node_config_resolves_paths_relative_to_its_own_directory(tmp_path):
    config = NodeConfig.from_json(json.dumps(_config_dict()), base_dir=tmp_path)

    assert config.schema_version == NODE_CONFIG_SCHEMA_VERSION
    assert config.max_loaded_models == 1
    assert config.discovery_update_period == 12
    assert config.discovery_startup_timeout == 4
    assert config.auto_model_priority == ()
    assert config.route_demand_authority_roots == ()
    model = config.models[0]
    assert model.manifest_path == (tmp_path / "manifests/one.json").resolve()
    assert model.cache_dir == (tmp_path / "cache/one").resolve()
    assert model.revocation_files == ((tmp_path / "trust/revoked.json").resolve(),)
    assert model.request_timeout == 7.5
    assert model.max_retries == 2


def test_node_config_accepts_a_unique_catalog_auto_priority(tmp_path):
    source = _config_dict(auto_model_priority=["sha256:" + "a" * 64, "Standby Model"])
    config = NodeConfig.from_dict(source, base_dir=tmp_path)

    assert config.auto_model_priority == ("sha256:" + "a" * 64, "Standby Model")

    source["auto_model_priority"] = ["Standby Model", "standby model"]
    with pytest.raises(NodeConfigError, match="case-insensitive duplicates"):
        NodeConfig.from_dict(source, base_dir=tmp_path)


@pytest.mark.parametrize(
    "roots, error",
    [
        (["sha256:" + "a" * 64], "between 2 and 32"),
        (["sha256:INVALID", "sha256:" + "b" * 64], "canonical sha256"),
        (["sha256:" + "a" * 64, "sha256:" + "a" * 64], "duplicates"),
        (["sha256:" + "b" * 64, "sha256:" + "a" * 64], "sorted"),
        ([f"sha256:{index:064x}" for index in range(33)], "between 2 and 32"),
    ],
)
def test_node_config_route_demand_authority_roots_are_strict_and_bounded(tmp_path, roots, error):
    with pytest.raises(NodeConfigError, match=error):
        NodeConfig.from_dict(_config_dict(route_demand_authority_roots=roots), base_dir=tmp_path)


def test_cached_peers_extend_runtime_config_without_mutating_persisted_config(tmp_path):
    configured = NodeConfig.from_json(json.dumps(_config_dict()), base_dir=tmp_path)
    cached = "/ip4/8.8.8.8/tcp/31337/p2p/Qm" + "A" * 44
    peer_cache = PeerCache(tmp_path / "discovery-peers.json", clock=lambda: 2_000_000_000.0)
    assert peer_cache.store(configured.models[0].initial_peers, (cached,)) is True

    runtime = _merge_cached_initial_peers(configured, peer_cache)

    assert runtime is not configured
    assert runtime.models[0].initial_peers == (configured.models[0].initial_peers[0], cached)
    assert configured.models[0].initial_peers == ("/ip4/127.0.0.1/tcp/31337/p2p/one",)
    assert _merge_cached_initial_peers(configured, PeerCache(tmp_path / "empty.json")) is configured


def test_hot_reconciliation_keeps_the_process_start_peer_set(tmp_path):
    configured = NodeConfig.from_json(json.dumps(_config_dict()), base_dir=tmp_path)
    cached = "/ip4/8.8.8.8/tcp/31337/p2p/Qm" + "A" * 44
    peer_cache = PeerCache(tmp_path / "discovery-peers.json", clock=lambda: 2_000_000_000.0)
    assert peer_cache.store(configured.models[0].initial_peers, (cached,)) is True
    runtime = _merge_cached_initial_peers(configured, peer_cache)

    reconciled = _reuse_runtime_initial_peers(configured, runtime)

    assert reconciled.models[0].initial_peers == runtime.models[0].initial_peers


def test_node_config_is_strict_and_does_not_accept_secrets(tmp_path):
    source = _config_dict()
    source["models"][0]["token"] = "must-not-live-here"

    with pytest.raises(NodeConfigError, match="unknown field.*token"):
        NodeConfig.from_dict(source, base_dir=tmp_path)


def test_node_config_parses_strict_worker_controls(tmp_path):
    source = _config_dict(
        workers=[
            {
                "id": "gpu-0",
                "model": "tiny-test",
                "identity_path": "secrets/gpu-0.key",
                "block_indices": "0:4",
                "enabled": True,
                "auto_restart": False,
                "restart_backoff": 2,
                "device": "cuda:0",
                "cache_dir": "worker-cache",
                "max_disk_space": "20GiB",
                "max_bandwidth_mbps": 25,
                "max_power_watts": 175.5,
                "throughput": 1.5,
                "port": 31337,
                "public_ip": "203.0.113.4",
            }
        ]
    )

    worker = NodeConfig.from_dict(source, base_dir=tmp_path).workers[0]

    assert worker.worker_id == "gpu-0"
    assert worker.identity_path == (tmp_path / "secrets/gpu-0.key").resolve()
    assert worker.block_indices == "0:4"
    assert worker.enabled is True
    assert worker.auto_restart is False
    assert worker.max_bandwidth_mbps == 25.0
    assert worker.max_power_watts == 175.5
    assert worker.throughput == 1.5
    assert worker.port == 31337


def test_node_config_accepts_only_count_based_automatic_workers(tmp_path):
    source = _config_dict(
        workers=[
            {
                "id": "automatic",
                "model": "auto",
                "identity_path": "identities/automatic.key",
                "num_blocks": 1,
                "enabled": True,
            }
        ]
    )

    worker = NodeConfig.from_dict(source, base_dir=tmp_path).workers[0]

    assert worker.model == "auto"
    assert worker.num_blocks == 1
    source["workers"][0].pop("num_blocks")
    source["workers"][0]["block_indices"] = "0:1"
    with pytest.raises(NodeConfigError, match="automatic model placement requires num_blocks"):
        NodeConfig.from_dict(source, base_dir=tmp_path)


@pytest.mark.parametrize(
    "source, message",
    [
        ('{"schema_version":1,"schema_version":1,"models":[]}', "duplicate object key"),
        (json.dumps(_config_dict(max_loaded_models=0)), "max_loaded_models"),
        (json.dumps(_config_dict(models=[])), "non-empty JSON array"),
        (
            json.dumps(
                _config_dict(
                    models=[{"manifest": "one.json", "initial_peers": []}],
                )
            ),
            "initial_peers",
        ),
    ],
)
def test_node_config_rejects_malformed_inputs(tmp_path, source, message):
    with pytest.raises(NodeConfigError, match=message):
        NodeConfig.from_json(source, base_dir=tmp_path)


def test_build_manager_registers_multiple_manifests_without_loading(monkeypatch, tmp_path):
    first = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    second_dict = first.to_dict()
    second_dict["name"] = "Second Test Model"
    second_dict["aliases"] = ["second-test"]
    second = ModelManifest.from_dict(second_dict)
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    first_path.write_text(first.canonical_json(), encoding="utf-8")
    second_path.write_text(second.canonical_json(), encoding="utf-8")
    loader_calls = []

    def fake_make_loader(manifest, **kwargs):
        loader_calls.append((manifest.digest_id, kwargs))
        return lambda: ModelRuntime(object(), object())

    monkeypatch.setattr("drift.cli.run_node.make_manifest_loader", fake_make_loader)
    config = NodeConfig(
        schema_version=1,
        max_loaded_models=1,
        models=(
            NodeModelConfig(first_path, ("peer-one",)),
            NodeModelConfig(second_path, ("peer-two",), cache_dir=Path("cache"), request_timeout=9, max_retries=4),
        ),
        auto_model_priority=(first.digest_id, second.digest_id),
    )

    cache_scopes = {first_path: ("shipped-one",), second_path: ("shipped-two",)}
    replay_history_dir = tmp_path / "replay-history"
    manager, descriptors, discovery = _build_model_manager(
        config,
        token="provider-token",
        peer_cache_scopes=cache_scopes,
        replay_history_dir=replay_history_dir,
    )

    assert [descriptor.model_id for descriptor in descriptors] == [first.name, second.name]
    assert [snapshot.state for snapshot in manager.snapshots()] == [ModelState.KNOWN, ModelState.KNOWN]
    assert manager.residency() == {"max_loaded_models": 1, "resident_models": 0}
    assert manager.auto_selection_snapshot()["status"] == "unavailable"
    assert loader_calls[0][1]["initial_peers"] == ("peer-one",)
    assert loader_calls[1][1]["initial_peers"] == ("peer-two",)
    assert loader_calls[1][1]["cache_dir"] == "cache"
    assert loader_calls[1][1]["request_timeout"] == 9
    assert loader_calls[1][1]["max_retries"] == 4
    assert all(call[1]["token"] == "provider-token" for call in loader_calls)
    assert discovery.snapshot(first.digest_id)["status"] == "unknown"
    assert discovery._states[first.digest_id].target.cache_scope == ("shipped-one",)
    assert discovery._states[second.digest_id].target.cache_scope == ("shipped-two",)
    first_history_path = discovery._states[first.digest_id].replay_guard.path
    second_history_path = discovery._states[second.digest_id].replay_guard.path
    assert first_history_path == replay_history_dir / f"{first.digest}.json"
    assert second_history_path == replay_history_dir / f"{second.digest}.json"
    assert ":" not in first_history_path.name
    assert ":" not in second_history_path.name
    manager.shutdown()


def test_worker_supervisor_command_is_pinned_to_configured_manifest(monkeypatch, tmp_path):
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.canonical_json(), encoding="utf-8")
    monkeypatch.setattr(
        "drift.cli.run_node.make_manifest_loader",
        lambda *args, **kwargs: lambda: ModelRuntime(object(), object()),
    )
    config = NodeConfig(
        schema_version=1,
        max_loaded_models=1,
        models=(NodeModelConfig(manifest_path, ("peer-one",)),),
        workers=(
            WorkerConfig(
                worker_id="worker",
                model=manifest.aliases[0],
                identity_path=tmp_path / "worker.key",
                num_blocks=2,
                throughput=1.25,
            ),
        ),
    )
    manager, _, _ = _build_model_manager(config, token=None)

    supervisor = _build_worker_supervisor(config, manager, token="provider-token")
    launch = supervisor.launches[0]

    assert launch.model_id == manifest.name
    assert launch.command[:4] == (sys.executable, "-m", "drift.cli", "server")
    assert "--model_manifest" in launch.command
    assert str(manifest_path) in launch.command
    assert launch.command[launch.command.index("--num_blocks") + 1] == "2"
    assert launch.command[launch.command.index("--throughput") + 1] == "1.25"
    assert "provider-token" not in launch.command
    assert launch.environment == (("HF_TOKEN", "provider-token"),)
    supervisor.shutdown()

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    frozen_supervisor = _build_worker_supervisor(config, manager, token=None)
    assert frozen_supervisor.launches[0].command[:2] == (sys.executable, "server")
    assert "-m" not in frozen_supervisor.launches[0].command
    frozen_supervisor.shutdown()
    manager.shutdown()


def test_automatic_worker_waits_then_binds_exact_model_and_block_range(monkeypatch, tmp_path):
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.canonical_json(), encoding="utf-8")
    monkeypatch.setattr(
        "drift.cli.run_node.make_manifest_loader",
        lambda *args, **kwargs: lambda: ModelRuntime(object(), object()),
    )
    config = NodeConfig.from_dict(
        _config_dict(
            models=[{"manifest": str(manifest_path), "initial_peers": ["peer-one"]}],
            auto_model_priority=[manifest.digest_id],
            workers=[
                {
                    "id": "automatic",
                    "model": "auto",
                    "identity_path": "automatic.key",
                    "num_blocks": 1,
                    "enabled": True,
                    "device": "cpu",
                }
            ],
            contribution_policy={
                "sharing_enabled": True,
                "max_disk_space": "1GiB",
            },
        ),
        base_dir=tmp_path,
    )
    manager, _, _ = _build_model_manager(config, token=None)

    waiting = _build_worker_supervisor(config, manager)
    waiting_launch = waiting.launches[0]
    assert waiting_launch.model_id == "auto"
    assert waiting_launch.policy_admitted is False
    assert waiting_launch.block_indices == "0:1"
    assert "waiting for fresh eligible coverage" in waiting_launch.policy_reason
    waiting.shutdown()

    decision = PlacementDecision(
        model_id=manifest.name,
        manifest_digest=manifest.digest_id,
        block_indices="1:2",
        artifact_bytes=sum(artifact.size for artifact in manifest.artifacts),
        replica_counts=(0,),
        score=100,
        reason="selected 1:2 from fresh verified coverage",
    )
    unacknowledged = _build_worker_supervisor(
        config,
        manager,
        automatic_placements={
            "automatic": PlacementPlan(decision, decision.reason, 1),
        },
    )
    unacknowledged_launch = unacknowledged.launches[0]
    assert unacknowledged_launch.policy_admitted is False
    assert unacknowledged_launch.intent_published is False
    assert unacknowledged_launch.remote_acknowledged is False
    assert "not remotely acknowledged" in unacknowledged_launch.policy_reason
    unacknowledged.shutdown()

    placed = _build_worker_supervisor(
        config,
        manager,
        automatic_placements={
            "automatic": PlacementPlan(
                decision,
                decision.reason,
                1,
                intent_published=True,
                remote_acknowledged=True,
            ),
        },
    )
    launch = placed.launches[0]
    assert launch.model_id == manifest.name
    assert launch.policy_admitted is True
    assert launch.automatic is True
    assert launch.block_indices == "1:2"
    assert launch.intent_published is True
    assert launch.remote_acknowledged is True
    assert launch.command[launch.command.index("--block_indices") + 1] == "1:2"
    assert "--num_blocks" not in launch.command
    placed.shutdown()
    manager.shutdown()


@pytest.mark.parametrize(
    "pause_while_waiting, publish_succeeds, expected_desired",
    [(False, True, True), (True, True, False), (False, False, None)],
)
def test_automatic_placement_service_reconciles_fresh_coverage_into_supervision(
    monkeypatch, tmp_path, pause_while_waiting, publish_succeeds, expected_desired
):
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.canonical_json(), encoding="utf-8")
    route_identity = NodeIdentity.create(tmp_path / "route-demand.key")
    second_authority = NodeIdentity.create(tmp_path / "second-route-demand.key")
    authority_roots = tuple(sorted((route_identity.key_id, second_authority.key_id)))
    monkeypatch.setattr(
        "drift.cli.run_node.make_manifest_loader",
        lambda *args, **kwargs: lambda: ModelRuntime(object(), object()),
    )
    config_source = _config_dict(
        models=[{"manifest": str(manifest_path), "initial_peers": ["peer-one"]}],
        auto_model_priority=[manifest.digest_id],
        route_demand_authority_roots=list(authority_roots),
        workers=[
            {
                "id": "automatic",
                "model": "auto",
                "identity_path": "automatic.key",
                "num_blocks": 1,
                "enabled": True,
                "device": "cpu",
            }
        ],
        contribution_policy={
            "sharing_enabled": True,
            "max_disk_space": "1GiB",
        },
    )
    config_path = tmp_path / "node-config.json"
    config_path.write_text(json.dumps(config_source), encoding="utf-8")
    config = NodeConfig.load(config_path)
    manager, _, discovery = _build_model_manager(config, token=None)
    counts = [1] * manifest.model.num_blocks
    counts[1] = 0
    state = discovery._states[manifest.digest_id]
    state.last_health = {
        "status": "incomplete",
        "total_blocks": manifest.model.num_blocks,
        "covered_blocks": manifest.model.num_blocks - 1,
        "missing_blocks": [1],
        "minimum_replicas": 0,
        "replica_counts": counts,
        "peer_count": 1,
        "last_updated_age": 0.0,
    }
    state.last_updated = time.monotonic()

    publish_calls = []

    def publish_intent(digest_id, source):
        publish_calls.append((digest_id, source))
        return publish_succeeds

    monkeypatch.setattr(discovery, "publish_intent", publish_intent)
    demand_calls = []
    monkeypatch.setattr(
        discovery,
        "publish_route_demand",
        lambda digest_id, source: demand_calls.append((digest_id, source)) or True,
    )
    route_outcomes = RouteOutcomeTracker()
    route_outcomes.record(
        manifest_digest=manifest.digest_id,
        succeeded=True,
        completion_tokens=8,
        duration_seconds=2,
    )
    monkeypatch.setattr(
        route_outcomes,
        "closed_snapshot",
        lambda digest_id: {
            "schema_version": 1,
            "manifest_digest": digest_id,
            "window_seconds": 300,
            "attempts_bucket": 4,
            "successes_bucket": 2,
            "useful_tokens_per_second_milli": 2_000,
            "reliability_milli": 500,
            "age_seconds_bucket": 15,
        },
    )
    registry = PlacementRegistry()
    supervisor = _build_worker_supervisor(
        config,
        manager,
        automatic_placements=registry.snapshot(),
    )
    service = _build_automatic_placement_service(
        config,
        manager,
        discovery,
        supervisor,
        registry,
        token=None,
        config_path=config_path,
        peer_cache=PeerCache(tmp_path / "peers.json"),
        route_outcomes=route_outcomes,
        route_identity_path=tmp_path / "route-demand.key",
    )
    router_key_id = NodeIdentity.load(tmp_path / "route-demand.key").key_id
    assert router_key_id == route_identity.key_id
    assert router_key_id in discovery._local_route_demand_keys
    if pause_while_waiting:
        supervisor.pause_worker("automatic")

    service.reconcile_once()

    launch = supervisor.launches[0]
    snapshot = supervisor.snapshot("automatic")
    assert len(publish_calls) == 1
    assert publish_calls[0][0] == manifest.digest_id
    published = publish_calls[0][1]
    assert published["kind"] == "intent_lease"
    assert published["payload"]["manifest_digest"] == manifest.digest
    assert published["payload"]["start_block"] == 1
    assert published["payload"]["end_block"] == 2
    assert "private_path" not in published["payload"]["resource_claims"]
    assert len(demand_calls) == 1
    demand = demand_calls[0][1]
    assert demand["kind"] == "route_demand"
    assert demand["key_id"] != published["key_id"]
    assert set(demand["payload"]["observation"]) == {
        "schema_version",
        "manifest_digest",
        "window_seconds",
        "attempts_bucket",
        "successes_bucket",
        "useful_tokens_per_second_milli",
        "reliability_milli",
        "age_seconds_bucket",
    }
    if publish_succeeds:
        assert launch.model_id == manifest.name
        assert launch.block_indices == "1:2"
        assert launch.policy_admitted is True
        assert launch.intent_published is True
        assert launch.remote_acknowledged is True
        assert snapshot["intent_published"] is True
        assert snapshot["remote_acknowledged"] is True
        assert "local demand bucket 1" in launch.placement_reason
        assert snapshot["desired_running"] is expected_desired
        assert snapshot["operator_paused"] is pause_while_waiting
        assert registry.snapshot()["automatic"].decision.manifest_digest == manifest.digest_id
    else:
        assert launch.model_id == "auto"
        assert launch.policy_admitted is False
        assert launch.intent_published is False
        assert launch.remote_acknowledged is False
        assert "signed placement intent" in launch.policy_reason
        assert snapshot["pid"] is None
        assert registry.snapshot()["automatic"].decision is None

    original_candidates = run_node_module._automatic_placement_candidates
    remote_consumption = []

    def capture_remote_consumption(*args, **kwargs):
        remote_consumption.append(kwargs["allow_remote_route_demand"])
        return original_candidates(*args, **kwargs)

    monkeypatch.setattr(run_node_module, "_automatic_placement_candidates", capture_remote_consumption)
    config_source["route_demand_authority_roots"] = ["sha256:" + "0" * 64, "sha256:" + "f" * 64]
    config_path.write_text(json.dumps(config_source), encoding="utf-8")
    demand_calls.clear()
    service.reconcile_once()
    assert demand_calls == []
    assert remote_consumption == [False]

    service.close()
    supervisor.shutdown()
    manager.shutdown()


def test_contribution_policy_defaults_off_and_requires_a_disk_ceiling_when_enabled(tmp_path):
    config = NodeConfig.from_dict(_config_dict(), base_dir=tmp_path)
    assert config.contribution_policy.sharing_enabled is False

    with pytest.raises(NodeConfigError, match="max_disk_space is required"):
        NodeConfig.from_dict(
            _config_dict(contribution_policy={"sharing_enabled": True}),
            base_dir=tmp_path,
        )


@pytest.mark.parametrize("field", ["max_bandwidth_mbps", "max_power_watts"])
@pytest.mark.parametrize("value", [True, 0, -1, float("inf"), "10"])
def test_contribution_policy_rejects_invalid_measured_resource_budgets(field, value):
    with pytest.raises(NodeConfigError, match=field):
        ContributionPolicyConfig.from_dict(
            {
                "sharing_enabled": True,
                "max_disk_space": "1GiB",
                field: value,
            }
        )


@pytest.mark.parametrize("field", ["max_bandwidth_mbps", "max_power_watts"])
def test_worker_config_rejects_invalid_measured_resource_budgets(tmp_path, field):
    worker = {
        "id": "worker",
        "model": "model",
        "identity_path": "worker.key",
        "num_blocks": 1,
        field: 0,
    }
    with pytest.raises(NodeConfigError, match=field):
        NodeConfig.from_dict(_config_dict(workers=[worker]), base_dir=tmp_path)


def test_contribution_schedule_enforces_boundaries_and_overnight_windows():
    schedule = ContributionScheduleConfig.from_dict(
        {
            "timezone": "UTC",
            "windows": [
                {"days": ["mon"], "start": "09:00", "end": "17:00"},
                {"days": ["fri"], "start": "22:00", "end": "02:00"},
            ],
        }
    )

    assert schedule.allows(datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc))
    assert schedule.allows(datetime(2026, 8, 24, 16, 59, tzinfo=timezone.utc))
    assert not schedule.allows(datetime(2026, 8, 24, 17, 0, tzinfo=timezone.utc))
    assert schedule.allows(datetime(2026, 8, 28, 23, 0, tzinfo=timezone.utc))
    assert schedule.allows(datetime(2026, 8, 29, 1, 59, tzinfo=timezone.utc))
    assert not schedule.allows(datetime(2026, 8, 29, 2, 0, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="timezone-aware"):
        schedule.allows(datetime(2026, 8, 24, 12, 0))


@pytest.mark.parametrize(
    ("schedule", "message"),
    [
        ({"timezone": "Not/A_Zone", "windows": []}, "available IANA timezone"),
        (
            {
                "timezone": "UTC",
                "windows": [{"days": ["mon"], "start": "09:00", "end": "09:00"}],
            },
            "start and end must differ",
        ),
        (
            {
                "timezone": "UTC",
                "windows": [{"days": ["mon", "MON"], "start": "09:00", "end": "17:00"}],
            },
            "case-insensitive duplicates",
        ),
        (
            {
                "timezone": "UTC",
                "windows": [{"days": ["funday"], "start": "09:00", "end": "17:00"}],
            },
            "unsupported weekday",
        ),
    ],
)
def test_contribution_schedule_rejects_ambiguous_or_invalid_values(schedule, message):
    with pytest.raises(NodeConfigError, match=message):
        ContributionPolicyConfig.from_dict(
            {
                "sharing_enabled": True,
                "max_disk_space": "1GiB",
                "schedule": schedule,
            }
        )


def test_worker_supervisor_enforces_resolved_model_policy_and_disk_ceiling(monkeypatch, tmp_path):
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.canonical_json(), encoding="utf-8")
    monkeypatch.setattr(
        "drift.cli.run_node.make_manifest_loader",
        lambda *args, **kwargs: lambda: ModelRuntime(object(), object()),
    )
    config = NodeConfig.from_dict(
        _config_dict(
            models=[{"manifest": str(manifest_path), "initial_peers": ["peer-one"]}],
            workers=[
                {
                    "id": "worker",
                    "model": manifest.digest_id,
                    "identity_path": "worker.key",
                    "num_blocks": 2,
                    "enabled": True,
                    "device": "cpu",
                    "max_disk_space": "2GiB",
                    "max_bandwidth_mbps": 12,
                    "max_power_watts": 250,
                }
            ],
            contribution_policy={
                "sharing_enabled": True,
                "allowed_models": [manifest.aliases[0]],
                "preferred_models": [manifest.name],
                "max_disk_space": "1GiB",
                "max_bandwidth_mbps": 20,
                "max_power_watts": 200,
                "pause_timeout": 1.5,
            },
        ),
        base_dir=tmp_path,
    )
    manager, _, _ = _build_model_manager(config, token=None)
    supervisor = _build_worker_supervisor(config, manager)
    launch = supervisor.launches[0]

    assert launch.policy_admitted is True
    assert launch.policy_reason is None
    assert launch.preferred is True
    assert launch.max_disk_bytes == 1024**3
    assert launch.max_bandwidth_mbps == 12
    assert launch.max_power_watts == 200
    assert launch.command[launch.command.index("--max_disk_space") + 1] == "1GiB"
    assert supervisor._stop_timeout == 1.5

    supervisor.shutdown()
    manager.shutdown()


def test_disabled_contribution_policy_blocks_auto_start_and_control_start(monkeypatch, tmp_path):
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.canonical_json(), encoding="utf-8")
    monkeypatch.setattr(
        "drift.cli.run_node.make_manifest_loader",
        lambda *args, **kwargs: lambda: ModelRuntime(object(), object()),
    )
    config = NodeConfig(
        schema_version=1,
        max_loaded_models=1,
        models=(NodeModelConfig(manifest_path, ("peer-one",)),),
        workers=(
            WorkerConfig(
                worker_id="worker",
                model=manifest.name,
                identity_path=tmp_path / "worker.key",
                num_blocks=2,
                enabled=True,
            ),
        ),
    )
    manager, _, _ = _build_model_manager(config, token=None)
    supervisor = _build_worker_supervisor(config, manager)
    supervisor.start_service()

    snapshot = supervisor.snapshot("worker")
    assert snapshot["desired_running"] is False
    assert snapshot["policy_admitted"] is False
    assert snapshot["policy_reason"] == "sharing is disabled by contribution policy"
    with pytest.raises(WorkerPolicyError, match="sharing is disabled"):
        supervisor.start_worker("worker")

    supervisor.shutdown()
    manager.shutdown()


def test_denied_model_cannot_be_started_through_an_alias(monkeypatch, tmp_path):
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.canonical_json(), encoding="utf-8")
    monkeypatch.setattr(
        "drift.cli.run_node.make_manifest_loader",
        lambda *args, **kwargs: lambda: ModelRuntime(object(), object()),
    )
    config = NodeConfig(
        schema_version=1,
        max_loaded_models=1,
        models=(NodeModelConfig(manifest_path, ("peer-one",)),),
        workers=(
            WorkerConfig(
                worker_id="worker",
                model=manifest.digest_id,
                identity_path=tmp_path / "worker.key",
                num_blocks=2,
                enabled=True,
            ),
        ),
        contribution_policy=ContributionPolicyConfig.from_dict(
            {
                "sharing_enabled": True,
                "denied_models": [manifest.aliases[0]],
                "max_disk_space": "1GiB",
            }
        ),
    )
    manager, _, _ = _build_model_manager(config, token=None)
    supervisor = _build_worker_supervisor(config, manager)

    snapshot = supervisor.snapshot("worker")
    assert snapshot["desired_running"] is False
    assert snapshot["policy_admitted"] is False
    assert "denied by contribution policy" in snapshot["policy_reason"]
    with pytest.raises(WorkerPolicyError, match="denied by contribution policy"):
        supervisor.start_worker("worker")

    supervisor.shutdown()
    manager.shutdown()


def test_contribution_policy_rejects_alias_based_allow_deny_conflicts(monkeypatch, tmp_path):
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.canonical_json(), encoding="utf-8")
    monkeypatch.setattr(
        "drift.cli.run_node.make_manifest_loader",
        lambda *args, **kwargs: lambda: ModelRuntime(object(), object()),
    )
    config = NodeConfig(
        schema_version=1,
        max_loaded_models=1,
        models=(NodeModelConfig(manifest_path, ("peer-one",)),),
        contribution_policy=ContributionPolicyConfig.from_dict(
            {
                "sharing_enabled": True,
                "allowed_models": [manifest.name],
                "denied_models": [manifest.aliases[0]],
                "max_disk_space": "1GiB",
            }
        ),
    )
    manager, _, _ = _build_model_manager(config, token=None)

    with pytest.raises(NodeConfigError, match="same model as both allowed and denied"):
        _build_worker_supervisor(config, manager)

    manager.shutdown()


def test_max_loaded_models_override_preserves_the_single_persisted_startup_snapshot(monkeypatch, tmp_path):
    configured = NodeConfig(
        schema_version=1,
        max_loaded_models=1,
        models=(NodeModelConfig(tmp_path / "manifest.json", ("peer-one",)),),
        auto_model_priority=("sha256:" + "a" * 64,),
        contribution_policy=ContributionPolicyConfig.from_dict(
            {"sharing_enabled": False, "denied_models": ["blocked-model"]}
        ),
    )
    loaded_paths = []

    def load(path):
        loaded_paths.append(path)
        return configured

    monkeypatch.setattr(NodeConfig, "load", load)
    args = type("Args", (), {"config": tmp_path / "node.json", "max_loaded_models": 2})()

    persisted, runtime = _load_persisted_and_runtime_config(args)

    assert loaded_paths == [args.config]
    assert persisted is configured
    assert runtime.max_loaded_models == 2
    assert runtime.auto_model_priority is configured.auto_model_priority
    assert runtime.contribution_policy is configured.contribution_policy


def test_contribution_policy_parses_absolute_and_percentage_vram_limits():
    absolute = ContributionPolicyConfig.from_dict(
        {"sharing_enabled": True, "max_disk_space": "1GiB", "max_vram": "8GiB"}
    )
    assert absolute.max_vram == "8GiB"
    assert absolute.max_vram_bytes == 8 * 1024**3
    assert absolute.max_vram_fraction is None

    percentage = ContributionPolicyConfig.from_dict(
        {"sharing_enabled": True, "max_disk_space": "1GiB", "max_vram": "62.5%"}
    )
    assert percentage.max_vram == "62.5%"
    assert percentage.max_vram_bytes is None
    assert percentage.max_vram_fraction == 0.625


@pytest.mark.parametrize("value", ["0%", "-1%", "100.1%", "NaN%", "Infinity%", "not-a-limit"])
def test_contribution_policy_rejects_invalid_vram_limits(value):
    with pytest.raises(NodeConfigError, match="max_vram"):
        ContributionPolicyConfig.from_dict(
            {
                "sharing_enabled": True,
                "max_disk_space": "1GiB",
                "max_vram": value,
            }
        )


def test_accelerator_worker_inherits_tighter_resolved_vram_limit(monkeypatch, tmp_path):
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.canonical_json(), encoding="utf-8")
    monkeypatch.setattr(
        "drift.cli.run_node.make_manifest_loader",
        lambda *args, **kwargs: lambda: ModelRuntime(object(), object()),
    )
    monkeypatch.setattr("drift.cli.run_node.get_device_total_memory", lambda device: 16 * 1024**3)
    config = NodeConfig.from_dict(
        _config_dict(
            models=[{"manifest": str(manifest_path), "initial_peers": ["peer-one"]}],
            workers=[
                {
                    "id": "gpu-worker",
                    "model": manifest.name,
                    "identity_path": "worker.key",
                    "num_blocks": 2,
                    "device": "cuda:0",
                    "max_vram": "8GiB",
                }
            ],
            contribution_policy={
                "sharing_enabled": True,
                "max_disk_space": "1GiB",
                "max_vram": "75%",
            },
        ),
        base_dir=tmp_path,
    )
    manager, _, _ = _build_model_manager(config, token=None)
    supervisor = _build_worker_supervisor(config, manager)
    launch = supervisor.launches[0]

    assert launch.max_vram_bytes == 8 * 1024**3
    assert launch.vram_pool_bytes == 12 * 1024**3
    assert launch.vram_device == "cuda:0"
    assert launch.command[launch.command.index("--max_device_memory") + 1] == str(8 * 1024**3)
    assert supervisor.snapshot("gpu-worker")["max_vram_bytes"] == 8 * 1024**3

    supervisor.shutdown()
    manager.shutdown()


def test_power_monitor_is_scoped_to_each_cuda_workers_device(monkeypatch, tmp_path):
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.canonical_json(), encoding="utf-8")
    monkeypatch.setattr(
        "drift.cli.run_node.make_manifest_loader",
        lambda *args, **kwargs: lambda: ModelRuntime(object(), object()),
    )
    monkeypatch.setattr("drift.cli.run_node.get_device_total_memory", lambda device: 16 * 1024**3)
    monitor_devices = []

    class FakePowerMonitor:
        def __init__(self, device_indices):
            self.device_indices = tuple(device_indices)
            monitor_devices.append(self.device_indices)

        def __call__(self):
            return {0: 100.0, 1: 200.0}[self.device_indices[0]]

    monkeypatch.setattr("drift.cli.run_node.NvidiaPowerMonitor", FakePowerMonitor)
    config = NodeConfig.from_dict(
        _config_dict(
            models=[{"manifest": str(manifest_path), "initial_peers": ["peer-one"]}],
            workers=[
                {
                    "id": f"gpu-worker-{index}",
                    "model": manifest.name,
                    "identity_path": f"worker-{index}.key",
                    "num_blocks": 2,
                    "device": f"cuda:{index}",
                }
                for index in range(2)
            ],
            contribution_policy={
                "sharing_enabled": True,
                "max_disk_space": "1GiB",
                "max_vram": "8GiB",
                "max_power_watts": 250,
            },
        ),
        base_dir=tmp_path,
    )
    manager, _, _ = _build_model_manager(config, token=None)
    supervisor = _build_worker_supervisor(config, manager)

    snapshots = {snapshot["id"]: snapshot for snapshot in supervisor.snapshots()}
    assert monitor_devices == [(0,), (1,)]
    assert snapshots["gpu-worker-0"]["current_power_watts"] == 100.0
    assert snapshots["gpu-worker-1"]["current_power_watts"] == 200.0
    assert all(snapshot["resource_admitted"] is True for snapshot in snapshots.values())

    supervisor.shutdown()
    manager.shutdown()


def test_accelerator_worker_requires_node_wide_vram_pool(monkeypatch, tmp_path):
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.canonical_json(), encoding="utf-8")
    monkeypatch.setattr(
        "drift.cli.run_node.make_manifest_loader",
        lambda *args, **kwargs: lambda: ModelRuntime(object(), object()),
    )
    config = NodeConfig.from_dict(
        _config_dict(
            models=[{"manifest": str(manifest_path), "initial_peers": ["peer-one"]}],
            workers=[
                {
                    "id": "gpu-worker",
                    "model": manifest.name,
                    "identity_path": "worker.key",
                    "num_blocks": 2,
                    "device": "cuda:0",
                }
            ],
            contribution_policy={"sharing_enabled": True, "max_disk_space": "1GiB"},
        ),
        base_dir=tmp_path,
    )
    manager, _, _ = _build_model_manager(config, token=None)

    with pytest.raises(NodeConfigError, match="requires a finite contribution max_vram"):
        _build_worker_supervisor(config, manager)

    manager.shutdown()


def test_router_identity_is_never_generated_and_must_match_a_catalog_authority(tmp_path):
    registered = []

    class Discovery:
        def register_local_route_demand_key(self, key_id):
            registered.append(key_id)

    path = tmp_path / "route-demand.key"
    roots = ("sha256:" + "a" * 64, "sha256:" + "b" * 64)
    assert _prepare_route_identity(Discovery(), path, roots) is None
    assert not path.exists()

    NodeIdentity.create(path)
    assert _prepare_route_identity(Discovery(), path, roots) is None
    assert registered == []


@pytest.mark.parametrize("error", [OSError("unwritable"), ProtocolSecurityError("corrupt")])
def test_router_identity_failure_disables_only_publication(monkeypatch, tmp_path, error):
    registered = []

    class Discovery:
        def register_local_route_demand_key(self, key_id):
            registered.append(key_id)

    path = tmp_path / "route-demand.key"
    path.write_bytes(b"present")
    monkeypatch.setattr(NodeIdentity, "load", lambda path: (_ for _ in ()).throw(error))

    roots = ("sha256:" + "a" * 64, "sha256:" + "b" * 64)
    assert _prepare_route_identity(Discovery(), path, roots) is None
    assert registered == []


def _automatic_worker(index=0, *, num_blocks=1):
    return {
        "id": f"automatic-{index}",
        "model": "auto",
        "identity_path": f"automatic-{index}.key",
        "num_blocks": num_blocks,
    }


def _placement_model(index):
    return {
        "manifest": f"manifests/model-{index}.json",
        "initial_peers": [f"peer-{index}"],
    }


def test_node_config_bounds_the_public_alpha_automatic_placement_surface(tmp_path):
    models = [_placement_model(index) for index in range(MAX_AUTOMATIC_PLACEMENT_CANDIDATES)]
    source = _config_dict(models=models, workers=[_automatic_worker()])
    accepted = NodeConfig.from_dict(source, base_dir=tmp_path)
    assert len(accepted.models) == MAX_AUTOMATIC_PLACEMENT_CANDIDATES

    source["models"] = models + [_placement_model(MAX_AUTOMATIC_PLACEMENT_CANDIDATES)]
    with pytest.raises(NodeConfigError, match="at most 32 configured models"):
        NodeConfig.from_dict(source, base_dir=tmp_path)

    source = _config_dict(
        models=models + [_placement_model(MAX_AUTOMATIC_PLACEMENT_CANDIDATES)],
        workers=[],
    )
    assert len(NodeConfig.from_dict(source, base_dir=tmp_path).models) == 33

    source = _config_dict(
        workers=[
            _automatic_worker(),
            _automatic_worker(1),
        ]
    )
    with pytest.raises(NodeConfigError, match="at most one auto worker"):
        NodeConfig.from_dict(source, base_dir=tmp_path)

    source = _config_dict(
        workers=[
            _automatic_worker(num_blocks=MAX_AUTOMATIC_PLACEMENT_BLOCKS),
        ]
    )
    assert NodeConfig.from_dict(source, base_dir=tmp_path).workers[0].num_blocks == 512
    source["workers"][0]["num_blocks"] = MAX_AUTOMATIC_PLACEMENT_BLOCKS + 1
    with pytest.raises(NodeConfigError, match="at most 512 blocks"):
        NodeConfig.from_dict(source, base_dir=tmp_path)


def test_automatic_placement_reconciliation_has_a_one_second_floor(tmp_path):
    config = NodeConfig.from_dict(
        _config_dict(
            discovery_update_period=0.001,
            workers=[_automatic_worker()],
        ),
        base_dir=tmp_path,
    )

    service = _build_automatic_placement_service(
        config,
        object(),
        object(),
        object(),
        PlacementRegistry(),
        token=None,
        config_path=None,
        peer_cache=PeerCache(tmp_path / "peers.json"),
    )

    assert service._period == 1.0
    service.close()
