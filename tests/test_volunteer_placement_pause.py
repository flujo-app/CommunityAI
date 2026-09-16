"""Volunteer placement must stay passive until the operator explicitly starts it."""

import time
from unittest.mock import Mock

import pytest

from drift.cli import run_node
from drift.model_manifest import ModelManifest
from drift.node.config import NodeConfig
from drift.node.contribution_planner import PlacementArtifactPlan, PlacementRegistry
from drift.node.discovery import PeerCache
from drift.node.model_manager import ModelRuntime
from drift.node.route_metrics import RouteOutcomeTracker
from drift.protocol_identity import NodeIdentity


@pytest.fixture
def placement_fixture(tmp_path, monkeypatch):
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.canonical_json(), encoding="utf-8")
    route_path = tmp_path / "route-demand.key"
    route_identity = NodeIdentity.create(route_path)
    second_authority = NodeIdentity.create(tmp_path / "second-authority.key")
    config = NodeConfig.from_dict(
        {
            "schema_version": 1,
            "models": [{"manifest": str(manifest_path), "initial_peers": ["peer-one"]}],
            "route_demand_authority_roots": sorted((route_identity.key_id, second_authority.key_id)),
            "workers": [
                {
                    "id": "automatic",
                    "model": "auto",
                    "identity_path": "worker.key",
                    "num_blocks": 1,
                    "enabled": True,
                    "device": "cpu",
                }
            ],
            "contribution_policy": {"sharing_enabled": True, "max_disk_space": "1GiB"},
        },
        base_dir=tmp_path,
    )
    monkeypatch.setattr(
        run_node, "make_text_peer_loader", lambda *args, **kwargs: lambda: ModelRuntime(object(), object())
    )
    monkeypatch.setattr(run_node, "get_dht_time", lambda: 2_000.0)
    manager, _, discovery = run_node._build_model_manager(config, token=None)
    state = discovery._states[manifest.digest_id]
    state.last_health = {
        "status": "incomplete",
        "total_blocks": manifest.model.num_blocks,
        "covered_blocks": manifest.model.num_blocks - 1,
        "missing_blocks": [1],
        "minimum_replicas": 0,
        "replica_counts": [0 if index == 1 else 1 for index in range(manifest.model.num_blocks)],
        "peer_count": 1,
        "last_updated_age": 0.0,
    }
    state.last_updated = time.monotonic()
    artifacts = Mock(
        return_value=tuple(
            PlacementArtifactPlan(index, index + 1, 4_242 + index, f"{index + 1:064x}")
            for index in range(manifest.model.num_blocks)
        )
    )
    candidates = Mock(wraps=run_node._automatic_placement_candidates)
    monkeypatch.setattr(run_node, "_manifest_artifact_plans", artifacts)
    monkeypatch.setattr(run_node, "_automatic_placement_candidates", candidates)
    intent = Mock(return_value=True)
    demand = Mock(return_value=True)
    monkeypatch.setattr(discovery, "publish_intent", intent)
    monkeypatch.setattr(discovery, "publish_route_demand", demand)
    outcomes = RouteOutcomeTracker()
    monkeypatch.setattr(
        outcomes,
        "closed_snapshot",
        lambda digest: {
            "schema_version": 1,
            "manifest_digest": digest,
            "window_seconds": 300,
            "attempts_bucket": 4,
            "successes_bucket": 2,
            "useful_tokens_per_second_milli": 2_000,
            "reliability_milli": 500,
            "age_seconds_bucket": 15,
        },
    )
    registry = PlacementRegistry()
    supervisor = run_node._build_worker_supervisor(config, manager, automatic_placements=registry.snapshot())
    supervisor.pause_worker("automatic")
    services = []

    def build_service(*, require_explicit_start):
        service = run_node._build_automatic_placement_service(
            config,
            manager,
            discovery,
            supervisor,
            registry,
            token=None,
            config_path=None,
            peer_cache=PeerCache(tmp_path / "peers.json"),
            route_outcomes=outcomes,
            route_identity_path=route_path,
            require_explicit_start=require_explicit_start,
        )
        services.append(service)
        return service

    yield build_service, supervisor, registry, candidates, artifacts, intent, demand
    for service in services:
        service.close()
    supervisor.shutdown()
    manager.shutdown()


def test_volunteer_initial_pause_blocks_artifact_planning_and_publication_until_explicit_start(placement_fixture):
    build_service, supervisor, registry, candidates, artifacts, intent, demand = placement_fixture
    service = build_service(require_explicit_start=True)

    service.reconcile_once()
    assert supervisor.snapshot("automatic")["operator_paused"]
    assert supervisor.snapshot("automatic")["pid"] is None
    assert (candidates.call_count, artifacts.call_count, intent.call_count, demand.call_count) == (0, 0, 0, 0)

    # Start clears the paused placeholder even before an exact placement exists.
    supervisor.start_worker("automatic")
    service.reconcile_once()
    assert not supervisor.snapshot("automatic")["operator_paused"]
    assert (candidates.call_count, artifacts.call_count, intent.call_count, demand.call_count) == (1, 1, 1, 1)
    assert registry.snapshot()["automatic"].decision is not None
    # No monitor/service was started: this test must not spawn an inference process.
    assert supervisor.snapshot("automatic")["pid"] is None

    supervisor.pause_worker("automatic")
    for callback in (candidates, artifacts, intent, demand):
        callback.reset_mock()
    service.reconcile_once()
    # The startup gate is now open. Preserve ordinary placement maintenance
    # while paused so a future Start cannot adopt an unmaintained old lease.
    assert (candidates.call_count, artifacts.call_count, intent.call_count, demand.call_count) == (1, 0, 0, 0)
    assert registry.snapshot()["automatic"].decision is not None
    assert supervisor.snapshot("automatic")["operator_paused"]
    assert supervisor.snapshot("automatic")["pid"] is None


def test_standard_profile_keeps_existing_passive_placement_planning(placement_fixture):
    build_service, supervisor, registry, candidates, artifacts, intent, demand = placement_fixture
    service = build_service(require_explicit_start=False)
    service.reconcile_once()
    assert (candidates.call_count, artifacts.call_count, intent.call_count, demand.call_count) == (1, 1, 1, 1)
    assert registry.snapshot()["automatic"].decision is not None
    assert supervisor.snapshot("automatic")["operator_paused"]
    assert supervisor.snapshot("automatic")["pid"] is None
