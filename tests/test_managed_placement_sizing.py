"""Managed sizing uses real planner/artifact/profile code with synthetic metadata.

Device inventory, signed metadata loading and coverage are controlled fixtures;
these tests do not load model weights or qualify accelerator execution.
"""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import torch
from test_joint_placement_runtime import joint_runtime

from drift.cli import run_node
from drift.model_manifest import ManifestError, ModelManifest, select_manifest_block_artifacts
from drift.node import device_binding
from drift.node.config import NodeConfig, NodeConfigError
from drift.node.contribution_planner import (
    AutomaticContributionPlanner,
    PlacementResourcePlan,
    propose_joint_placements,
)
from drift.node.placement_sizing import PlacementSpanResolver
from drift.server.memory_budget import ModelMemoryProfile
from drift.utils.convert_block import QuantType


def _metadata(tmp_path, *, sharded=True):
    document = ModelManifest.load("tests/data/model_manifest_v1_vector.json").to_dict()
    document["artifacts"] = [
        dict(path="config.json", role="config", sha256="1" * 64, size=100),
        dict(path="tokenizer.json", role="tokenizer", sha256="3" * 64, size=1),
    ]
    if sharded:
        document["artifacts"] += [
            dict(path="model.safetensors.index.json", role="weight_index", sha256="2" * 64, size=200),
            dict(path="a.safetensors", role="weight", sha256="a" * 64, size=400),
            dict(path="b.safetensors", role="weight", sha256="b" * 64, size=600),
        ]
        weight_map = {
            f"model.layers.{index}.weight": "a.safetensors" if index < 4 else "b.safetensors" for index in range(8)
        }
    else:
        document["artifacts"] += [dict(path="model.safetensors", role="weight", sha256="a" * 64, size=1000)]
        weight_map = None
    manifest = ModelManifest.from_dict(document)
    profile = ModelMemoryProfile(
        hidden_size=1,
        num_devices=1,
        dtype=torch.float32,
        quant_type=QuantType.NONE,
        attn_cache_tokens=1,
        block_weight_bytes=tuple(100 * (index + 1) for index in range(8)),
        block_cache_bytes=(10,) * 8,
    )
    return manifest, SimpleNamespace(
        manifest_digest=manifest.digest_id,
        cache_root=tmp_path / "cache",
        block_prefix="model.layers",
        weight_map=weight_map,
        memory_profile=profile,
    )


@pytest.mark.parametrize("sharded", [True, False])
def test_every_exact_span_matches_child_artifact_binding_and_shared_memory_profile(tmp_path, sharded):
    manifest, metadata = _metadata(tmp_path, sharded=sharded)
    resolver = PlacementSpanResolver(manifest, metadata, max_device_memory_bytes=10**8, max_artifact_bytes=10**8)
    for start in range(8):
        for end in range(start + 1, 9):
            expected = select_manifest_block_artifacts(
                manifest,
                block_prefix=metadata.block_prefix,
                start_block=start,
                end_block=end,
                weight_map=metadata.weight_map,
            )
            result = resolver(start, end)
            assert (result.start_block, result.end_block) == (start, end)
            assert result.artifact_bytes == expected.artifact_bytes
            assert result.artifact_set_digest == expected.artifact_set_digest
            assert result.device_memory_bytes == metadata.memory_profile.estimate_span(start, end)
            assert resolver.artifact_plan(start, end) == expected
    if not sharded:
        assert resolver(0, 1).artifact_bytes == resolver(0, 8).artifact_bytes == 1100


def test_resource_feasibility_respects_heterogeneous_layers_and_whole_shard_storage(tmp_path):
    manifest, metadata = _metadata(tmp_path)
    memory_limit = metadata.memory_profile.estimate_span(0, 4)
    resolver = PlacementSpanResolver(manifest, metadata, max_device_memory_bytes=memory_limit, max_artifact_bytes=700)
    assert resolver(0, 4) is not None
    assert resolver(0, 5) is None
    assert resolver(4, 5) is None  # its single block still needs the complete larger shard
    assert resolver(4, 8) is None
    assert resolver(0, 4).device_memory_bytes > resolver(0, 4).artifact_bytes


@pytest.mark.parametrize("span", [(True, 2), (0, False), (-1, 1), (0, 9), (1, 1), ("0", 1)])
def test_resolver_rejects_noncanonical_or_outside_spans(tmp_path, span):
    manifest, metadata = _metadata(tmp_path)
    resolver = PlacementSpanResolver(manifest, metadata, max_device_memory_bytes=10**8, max_artifact_bytes=10**8)
    with pytest.raises(ValueError):
        resolver(*span)


def test_resolver_rejects_mismatched_metadata_and_missing_layer_binding(tmp_path):
    manifest, metadata = _metadata(tmp_path)
    metadata.manifest_digest = "wrong"
    with pytest.raises(ManifestError):
        PlacementSpanResolver(manifest, metadata, max_device_memory_bytes=10**8, max_artifact_bytes=10**8)
    metadata.manifest_digest = manifest.digest_id
    metadata.weight_map.pop("model.layers.3.weight")
    with pytest.raises(ManifestError):
        PlacementSpanResolver(manifest, metadata, max_device_memory_bytes=10**8, max_artifact_bytes=10**8)


@pytest.fixture
def managed_candidates(tmp_path, monkeypatch):
    manifest, metadata = _metadata(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.canonical_json(), encoding="utf-8")
    config = NodeConfig.from_dict(
        {
            "schema_version": 1,
            "models": [{"manifest": str(manifest_path), "initial_peers": ["fixture-peer"]}],
            "workers": [
                {
                    "id": "gpu-0",
                    "model": "auto",
                    "identity_path": "worker.key",
                    "num_blocks": 1,
                    "enabled": False,
                    "device": "cuda:0",
                    "managed_by": "desktop_gpu",
                    "max_vram": "1GiB",
                    "max_processing_percent": 100,
                }
            ],
            "contribution_policy": {
                "sharing_enabled": True,
                "max_disk_space": "1GiB",
                "max_vram": "1GiB",
                "processing_scope": "per_device",
            },
        },
        base_dir=tmp_path,
    )
    manager = Mock()
    manager.catalog_allows_contribution.return_value = True
    manager.resolve.return_value = SimpleNamespace(model_id=manifest.name, manifest_digest=manifest.digest_id)
    discovery = Mock()
    discovery.snapshot.return_value = {"status": "incomplete", "last_updated_age": 0, "replica_counts": [0] * 8}
    loader = Mock(return_value=metadata)
    monkeypatch.setattr(run_node, "load_placement_memory", loader)
    capacity = [10**8]
    monkeypatch.setattr(
        run_node, "_managed_placement_device_budget", lambda worker, policy: (torch.device(worker.device), capacity[0])
    )
    monkeypatch.setattr(
        run_node, "_manifest_artifact_plans", Mock(side_effect=AssertionError("fixed placeholder planning"))
    )
    return SimpleNamespace(
        config=config, manager=manager, discovery=discovery, loader=loader, metadata=metadata, capacity=capacity
    )


def _candidates(fixture, worker=None, **kwargs):
    return run_node._automatic_placement_candidates(
        fixture.config,
        fixture.manager,
        fixture.discovery,
        worker or fixture.config.workers[0],
        token=None,
        **kwargs,
    )


def test_managed_candidate_uses_verified_geometry_instead_of_one_block_placeholder(managed_candidates):
    fixture = managed_candidates
    candidates = _candidates(fixture)
    planner = AutomaticContributionPlanner(num_blocks=1, jitter_seed="managed")
    plan = propose_joint_placements({"gpu-0": planner}, {"gpu-0": candidates}, sharing_enabled=True)["gpu-0"]
    assert plan.decision.block_indices == "0:8"
    assert plan.decision.device_memory_bytes == fixture.metadata.memory_profile.estimate_span(0, 8)
    assert plan.decision.artifact_bytes == 1300
    assert planner.current_decision is None


def test_cards_share_verified_metadata_but_refresh_each_card_budget(managed_candidates):
    fixture = managed_candidates
    metadata_cache, artifact_cache = {}, {}
    first = _candidates(fixture, placement_metadata_cache=metadata_cache, artifact_plan_cache=artifact_cache)
    second_worker = replace(fixture.config.workers[0], worker_id="gpu-1", device="cuda:1")
    fixture.capacity[0] = fixture.metadata.memory_profile.estimate_span(0, 4)
    second = _candidates(
        fixture, second_worker, placement_metadata_cache=metadata_cache, artifact_plan_cache=artifact_cache
    )
    assert fixture.loader.call_count == 1
    assert first[0].max_device_memory_bytes > second[0].max_device_memory_bytes
    assert second[0].resource_plan(0, 8) is None
    planners = {name: AutomaticContributionPlanner(num_blocks=1, jitter_seed=name) for name in ("gpu-0", "gpu-1")}
    plans = propose_joint_placements(planners, {"gpu-0": first, "gpu-1": second}, sharing_enabled=True)
    spans = [tuple(map(int, plan.decision.block_indices.split(":"))) for plan in plans.values()]
    assert sum(end - start for start, end in spans) == 8
    assert all(end > start for start, end in spans)
    assert spans[0][1] <= spans[1][0] or spans[1][1] <= spans[0][0]
    assert plans["gpu-1"].decision.device_memory_bytes <= fixture.capacity[0]


def test_missing_capacity_or_metadata_fails_closed_before_publication(managed_candidates, monkeypatch):
    fixture = managed_candidates
    monkeypatch.setattr(
        run_node, "_managed_placement_device_budget", Mock(side_effect=NodeConfigError("private-device-id"))
    )
    candidate = _candidates(fixture)[0]
    assert candidate.policy_reason and "private-device-id" not in candidate.policy_reason
    assert fixture.loader.call_count == 0
    planner = AutomaticContributionPlanner(num_blocks=1, jitter_seed="failure")
    assert planner.propose((candidate,), sharing_enabled=True).decision is None


def test_cached_resolver_does_not_reload_evicted_metadata(managed_candidates):
    fixture = managed_candidates
    metadata_cache, artifact_cache = {}, {}
    first = _candidates(fixture, placement_metadata_cache=metadata_cache, artifact_plan_cache=artifact_cache)[0]
    assert first.policy_reason is None and fixture.loader.call_count == 1
    metadata_cache.clear()  # Simulate the bounded metadata cache evicting this model.
    fixture.loader.side_effect = AssertionError("metadata must not reopen for a cached exact resolver")
    second = _candidates(fixture, placement_metadata_cache=metadata_cache, artifact_plan_cache=artifact_cache)[0]
    assert second.resource_plan is first.resource_plan
    assert second.policy_reason is None and fixture.loader.call_count == 1


@pytest.mark.parametrize("estimate", [None, True, 0, 2 * 1024**3, "valid"])
def test_managed_launch_rechecks_estimate_against_current_physical_ceiling(managed_candidates, monkeypatch, estimate):
    fixture = managed_candidates
    candidate = _candidates(fixture)[0]
    plan = AutomaticContributionPlanner(num_blocks=1, jitter_seed="launch").propose((candidate,), sharing_enabled=True)
    valid = estimate == "valid"
    decision = plan.decision if valid else replace(plan.decision, device_memory_bytes=estimate)
    plan = replace(plan, decision=decision, intent_published=True, remote_acknowledged=True)
    card = SimpleNamespace(uuid="GPU-00000000-0000-0000-0000-000000000001", total_memory=1024**3)
    monkeypatch.setattr(run_node.torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(run_node.torch.cuda, "device_count", lambda: 1)
    monkeypatch.setattr(run_node.torch.cuda, "get_device_properties", lambda index: card)
    monkeypatch.setattr(run_node, "get_device_total_memory", lambda device: card.total_memory)
    monkeypatch.setattr(device_binding, "_DEFAULT_LIVENESS_PROBE", lambda identity: identity == card.uuid)
    settings = run_node._prepare_worker_supervisor_settings(
        fixture.config,
        fixture.manager,
        automatic_placements={"gpu-0": plan},
    )
    launch = settings.launches[0]
    assert launch.policy_admitted is valid
    if valid:
        assert launch.block_indices == "0:8"
        assert launch.command[launch.command.index("--max_device_memory") + 1] == str(1024**3)
    else:
        assert launch.policy_reason == "automatic placement is waiting for a fitting verified memory estimate"


def test_recent_gap_retains_variable_span_only_while_exact_memory_and_artifacts_fit(managed_candidates):
    fixture = managed_candidates
    candidate = _candidates(fixture)[0]
    planner = AutomaticContributionPlanner(num_blocks=1, jitter_seed="gap")
    decision = planner.propose((candidate,), sharing_enabled=True).decision
    assert decision.block_indices == "0:8"
    gap = replace(candidate, health={"status": "unknown", "last_known_status": "incomplete", "last_updated_age": 1})
    assert run_node._recent_gap_preserves_artifact_claim(gap, decision, 1, maximum_age=5)
    too_small = replace(gap, max_device_memory_bytes=decision.device_memory_bytes - 1)
    assert not run_node._recent_gap_preserves_artifact_claim(too_small, decision, 1, maximum_age=5)


@pytest.mark.parametrize("policy_size,worker_fraction,expected", [(800, 0.9, 800), (900, 0.5, 500), (2000, 1.0, 1000)])
def test_physical_budget_uses_minimum_of_fresh_capacity_policy_and_card(
    tmp_path, monkeypatch, policy_size, worker_fraction, expected
):
    worker = SimpleNamespace(
        worker_id="gpu-0",
        device="cuda:0",
        identity_path=tmp_path / "key",
        max_vram_bytes=None,
        max_vram_fraction=worker_fraction,
    )
    policy = SimpleNamespace(max_vram_bytes=policy_size, max_vram_fraction=None)
    monkeypatch.setattr(run_node, "_resolve_worker_device", lambda *args: torch.device("cuda:0"))
    monkeypatch.setattr(run_node, "get_device_total_memory", lambda device: 1000)
    store = Mock()
    monkeypatch.setattr(run_node, "DeviceBindingStore", Mock(return_value=store))
    assert run_node._managed_placement_device_budget(worker, policy) == (torch.device("cuda:0"), expected)
    store.load_existing.assert_called_once_with("gpu-0", "cuda:0")
    store.bind.assert_not_called()


def test_service_preserves_sized_spans_and_children_during_residency(joint_runtime, monkeypatch):
    runtime = joint_runtime(2, active=True)

    def resource(start, end):
        if end - start > 3:
            return None
        return PlacementResourcePlan(start, end, 4242 + start, f"{start + 1:064x}", 100 + 20 * (end - start))

    for worker, candidate in runtime.candidates.items():
        runtime.candidates[worker] = replace(
            candidate, artifact_plans=(), resource_plan=resource, max_device_memory_bytes=160
        )
        runtime.planners[worker]._minimum_residency = 900
        runtime.planners[worker]._cooldown = 300
    batch = Mock(wraps=runtime.supervisor.replace_launches)
    monkeypatch.setattr(runtime.supervisor, "replace_launches", batch)
    runtime.service.reconcile_once()
    original = {worker: plan.decision.block_indices for worker, plan in runtime.registry.snapshot().items()}
    assert all(int(span.split(":")[1]) - int(span.split(":")[0]) == 3 for span in original.values())
    children = tuple(runtime.children)
    assert len(children) == 2 and all(child.poll() is None for child in children)
    counts = [0] * 8
    for span in original.values():
        start, end = map(int, span.split(":"))
        counts[start:end] = [5] * (end - start)
    for worker, candidate in runtime.candidates.items():
        runtime.candidates[worker] = replace(
            candidate, health={"status": "incomplete", "last_updated_age": 0, "replica_counts": counts}
        )
    runtime.clock[0] += 1
    runtime.service.reconcile_once()
    assert {worker: plan.decision.block_indices for worker, plan in runtime.registry.snapshot().items()} == original
    assert batch.call_count == 1
    assert tuple(runtime.children) == children and all(child.poll() is None for child in children)
