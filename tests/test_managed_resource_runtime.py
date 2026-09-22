"""Managed launch -> durable admission -> real contained child -> verified release.

GPU inventory/config geometry and resource availability are synthetic. Children
sleep instead of loading a model; this does not qualify accelerator execution.
"""

import json
import subprocess
import sys
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_joint_placement_runtime import joint_runtime
from test_managed_placement_sizing import _candidates, managed_candidates

from drift.cli import run_node
from drift.node import device_binding
from drift.node.contribution_planner import AutomaticContributionPlanner
from drift.node.discovery import PeerCache
from drift.node.placement_resources import CacheSnapshot, ResourceSnapshot, VolumeSnapshot
from drift.node.resource_reservations import ResourceReservationManager


def eventually(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("asynchronous resource operation did not complete")
        time.sleep(0.01)


@pytest.fixture
def managed_launch(managed_candidates, monkeypatch):
    fixture = managed_candidates
    plan = AutomaticContributionPlanner(num_blocks=1, jitter_seed="host-integration").propose(
        _candidates(fixture), sharing_enabled=True
    )
    plan = replace(plan, intent_published=True, remote_acknowledged=True)
    card = SimpleNamespace(uuid="GPU-00000000-0000-0000-0000-000000000001", total_memory=1024**3)
    monkeypatch.setattr(run_node.torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(run_node.torch.cuda, "device_count", lambda: 1)
    monkeypatch.setattr(run_node.torch.cuda, "get_device_properties", lambda index: card)
    monkeypatch.setattr(run_node, "get_device_total_memory", lambda device: card.total_memory)
    monkeypatch.setattr(device_binding, "_DEFAULT_LIVENESS_PROBE", lambda identity: identity == card.uuid)
    fixture.plan = plan
    return fixture


def settings(fixture, **kwargs):
    return run_node._prepare_worker_supervisor_settings(
        fixture.config, fixture.manager, automatic_placements={"gpu-0": fixture.plan}, **kwargs
    )


def test_explicit_host_allowance_is_required_and_gpu_allowance_cannot_supply_it(managed_launch):
    fixture = managed_launch
    fixture.config = replace(
        fixture.config,
        contribution_policy=replace(
            fixture.config.contribution_policy, max_host_memory=None, max_host_memory_bytes=None
        ),
    )
    before = fixture.loader.call_count
    launch = settings(fixture).launches[0]
    assert not launch.policy_admitted and "shared host RAM allowance" in launch.policy_reason
    assert launch.resource_claim is None and fixture.loader.call_count == before


def test_exact_artifact_and_memory_claim_is_bound_before_resource_admission(managed_launch):
    fixture = managed_launch
    launch = settings(fixture).launches[0]
    assert launch.policy_admitted
    assert launch.resource_claim.persistent_host_bytes > 1024**3
    assert launch.resource_claim.staging_host_bytes > 1024**2
    assert sum(a.size_bytes for a in launch.resource_claim.artifacts) == launch.placement_artifact_bytes
    assert all(a.relative_path.startswith("manifest-artifacts/") for a in launch.resource_claim.artifacts)
    fixture.plan = replace(fixture.plan, decision=replace(fixture.plan.decision, artifact_bytes=1))
    launch = settings(fixture).launches[0]
    assert not launch.policy_admitted
    assert launch.resource_claim is None


def test_cached_claim_does_not_churn_unchanged_launches(managed_launch):
    cache = {}
    first = settings(managed_launch, resource_claim_cache=cache).launches[0]
    before = managed_launch.loader.call_count
    second = settings(managed_launch, resource_claim_cache=cache).launches[0]
    assert first == second and first.resource_claim is second.resource_claim
    assert managed_launch.loader.call_count == before


def test_policy_prepare_uses_prepared_claim_without_metadata_io_or_inventing_a_claim(managed_launch):
    fixture = managed_launch
    cache = {}
    before = fixture.loader.call_count
    pending = settings(fixture, resource_claim_cache=cache, allow_resource_metadata_io=False).launches[0]
    assert not pending.policy_admitted and pending.resource_claim is None
    assert fixture.loader.call_count == before
    original = settings(fixture, resource_claim_cache=cache).launches[0]
    before = fixture.loader.call_count
    fixture.loader.side_effect = AssertionError("policy must never reload metadata")
    fixture.config = replace(
        fixture.config,
        contribution_policy=replace(
            fixture.config.contribution_policy, max_host_memory="5GiB", max_host_memory_bytes=5 * 1024**3
        ),
    )
    updated = settings(fixture, resource_claim_cache=cache, allow_resource_metadata_io=False).launches[0]
    assert updated.policy_admitted and updated.resource_claim == original.resource_claim
    assert fixture.loader.call_count == before


@pytest.mark.parametrize("loader_fails", [False, True])
def test_metadata_loader_requires_prior_durable_budget_even_before_worker_admission(
    managed_candidates, tmp_path, loader_fails
):
    fixture = managed_candidates
    directory = tmp_path / "metadata-reservations"
    host = [1]

    def snapshot(claims, *, host_limit_bytes, cache_limits, now, **kwargs):
        return ResourceSnapshot(
            now,
            host_limit_bytes,
            host[0],
            tuple(CacheSnapshot(root, "disk", 0, limit) for root, limit in cache_limits.items()),
            (VolumeSnapshot("disk", 100 * 1024**3),),
        )

    resources = ResourceReservationManager(directory, snapshot_provider=snapshot)
    before = fixture.loader.call_count
    denied = _candidates(fixture, resource_manager=resources)[0]
    assert denied.policy_reason is not None and fixture.loader.call_count == before
    assert json.loads((directory / "generations.json").read_text(encoding="utf-8"))["cache_roots"]

    def load(*args, **kwargs):
        current = json.loads((directory / "generations.json").read_text(encoding="utf-8"))["reservations"]
        assert len(current) == 1
        artifacts = current[0]["claim"]["artifacts"]
        assert {item["relative_path"].split("/")[-1] for item in artifacts} == {
            "config.json",
            "model.safetensors.index.json",
        }
        assert current[0]["claim"]["staging_host_bytes"] > 0
        assert kwargs["artifact_root"] is None  # Network-capable load is inside admission.
        if loader_fails:
            raise RuntimeError("private metadata failure")
        return fixture.metadata

    fixture.loader.side_effect = load
    host[0] = 100 * 1024**3
    candidate = _candidates(fixture, resource_manager=resources)[0]
    assert (candidate.policy_reason is not None) == loader_fails
    assert json.loads((directory / "generations.json").read_text(encoding="utf-8"))["reservations"] == []
    if loader_fails:
        assert "private metadata failure" not in candidate.policy_reason


def test_candidate_without_coordinator_is_read_only_and_missing_host_consent_skips_metadata(managed_candidates):
    fixture = managed_candidates
    _candidates(fixture)
    assert fixture.loader.call_args.kwargs["artifact_root"].name == "snapshot"
    fixture.config = replace(
        fixture.config,
        contribution_policy=replace(
            fixture.config.contribution_policy, max_host_memory=None, max_host_memory_bytes=None
        ),
    )
    before = fixture.loader.call_count
    candidate = _candidates(fixture)[0]
    assert "shared host RAM allowance" in candidate.policy_reason
    assert fixture.loader.call_count == before


@pytest.mark.parametrize("managed_marker", [None, "desktop_gpu"])
def test_host_allowance_cannot_silently_leave_manual_worker_unaccounted(managed_launch, managed_marker):
    fixture = managed_launch
    manual = replace(
        fixture.config.workers[0],
        model=fixture.manager.resolve.return_value.model_id,
        managed_by=managed_marker,
        max_processing_percent=100 if managed_marker else None,
    )
    fixture.config = replace(
        fixture.config,
        workers=(manual,),
        contribution_policy=replace(
            fixture.config.contribution_policy, processing_scope="per_device" if managed_marker else "node"
        ),
    )
    launch = settings(fixture).launches[0]
    assert not launch.policy_admitted
    assert "shared host RAM admission" in launch.policy_reason


def test_real_child_cannot_spawn_before_journal_and_releases_only_after_pause(managed_launch, tmp_path):
    fixture = managed_launch
    directory = tmp_path / "reservations"
    host = [100 * 1024**3]

    def snapshot(claims, *, host_limit_bytes, cache_limits, now, **kwargs):
        return ResourceSnapshot(
            now,
            host_limit_bytes,
            host[0],
            tuple(CacheSnapshot(root, "disk", 0, limit) for root, limit in cache_limits.items()),
            (VolumeSnapshot("disk", 100 * 1024**3),),
        )

    resources = ResourceReservationManager(directory, snapshot_provider=snapshot)
    supervisor = run_node._build_worker_supervisor(
        fixture.config, fixture.manager, automatic_placements={"gpu-0": fixture.plan}, resource_manager=resources
    )
    children = []

    def spawn(command, **kwargs):
        reservations = json.loads((directory / "generations.json").read_text(encoding="utf-8"))["reservations"]
        assert len(reservations) == 1
        assert reservations[0]["claim"]["persistent_host_bytes"] > 0
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(child)
        return child

    supervisor._popen = spawn
    try:
        host[0] = 1
        assert supervisor.start_worker("gpu-0") is False
        assert children == []
        eventually(
            lambda: "shared host memory or cache storage" in (supervisor.snapshot("gpu-0")["resource_reason"] or "")
        )
        host[0] = 100 * 1024**3
        supervisor.start_worker("gpu-0")
        eventually(lambda: len(children) == 1 and supervisor.snapshot("gpu-0")["pid"] is not None)
        assert len(children) == 1 and children[0].poll() is None
        token = json.loads((directory / "generations.json").read_text(encoding="utf-8"))["reservations"][0]["claim"][
            "reservation_id"
        ]
        supervisor.pause_worker("gpu-0")
        assert children[0].poll() is not None
        eventually(lambda: supervisor.snapshot("gpu-0")["resource_operation"] is None)
        assert json.loads((directory / "generations.json").read_text(encoding="utf-8"))["reservations"] == []
        supervisor.start_worker("gpu-0")
        eventually(lambda: len(children) == 2 and supervisor.snapshot("gpu-0")["pid"] is not None)
        newer = json.loads((directory / "generations.json").read_text(encoding="utf-8"))["reservations"][0]["claim"][
            "reservation_id"
        ]
        assert token != newer and len(children) == 2
    finally:
        supervisor.shutdown()
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
        eventually(lambda: supervisor.snapshot("gpu-0")["resource_operation"] is None)
    assert json.loads((directory / "generations.json").read_text(encoding="utf-8"))["reservations"] == []


@pytest.mark.parametrize("disabled", [True, False])
def test_disabled_or_paused_candidate_does_not_materialize_metadata(managed_candidates, tmp_path, disabled):
    fixture = managed_candidates
    fixture.config = replace(
        fixture.config,
        contribution_policy=replace(fixture.config.contribution_policy, sharing_enabled=not disabled),
    )
    directory = tmp_path / "never-reserved"
    resources = ResourceReservationManager(directory)
    before = fixture.loader.call_count
    candidate = _candidates(fixture, resource_manager=resources, resource_cancelled=lambda: not disabled)[0]
    assert candidate.policy_reason is not None
    assert fixture.loader.call_count == before
    assert not directory.exists()


@pytest.mark.parametrize("during_loader", [False, True])
def test_metadata_cancellation_does_not_publish_ready_candidate(managed_candidates, tmp_path, during_loader):
    fixture = managed_candidates
    cancel = threading.Event()
    directory = tmp_path / "metadata-cancel"
    before = fixture.loader.call_count

    def snapshot(claims, *, host_limit_bytes, cache_limits, now, **kwargs):
        if kwargs["maximum_scan_seconds"] == 2.0 and not during_loader:
            cancel.set()
        return ResourceSnapshot(
            now,
            host_limit_bytes,
            100 * 1024**3,
            tuple(CacheSnapshot(root, "disk", 0, limit) for root, limit in cache_limits.items()),
            (VolumeSnapshot("disk", 100 * 1024**3),),
        )

    resources = ResourceReservationManager(directory, snapshot_provider=snapshot)

    def load(*args, **kwargs):
        assert len(json.loads((directory / "generations.json").read_text())["reservations"]) == 1
        cancel.set()
        return fixture.metadata

    fixture.loader.side_effect = load
    metadata_cache = {}
    candidate = _candidates(
        fixture, resource_manager=resources, resource_cancelled=cancel.is_set, placement_metadata_cache=metadata_cache
    )[0]
    assert candidate.policy_reason is not None and not metadata_cache
    assert fixture.loader.call_count == before + int(during_loader)
    assert json.loads((directory / "generations.json").read_text())["reservations"] == []


def test_paused_managed_reconcile_keeps_map_without_metadata_or_publication(joint_runtime, monkeypatch, tmp_path):
    runtime = joint_runtime(1)
    runtime.service.reconcile_once()
    before = runtime.registry.snapshot()
    published = runtime.publication.call_count
    runtime.supervisor.pause_worker("gpu-0")
    # Only the planner's managed-worker classification changes in this fixture;
    # no GPU probe, admission, model or child is authorized by this paused path.
    config = replace(runtime.config, workers=(replace(runtime.config.workers[0], managed_by="desktop_gpu"),))
    service = run_node._build_automatic_placement_service(
        config,
        runtime.manager,
        runtime.discovery,
        runtime.supervisor,
        runtime.registry,
        token=None,
        config_path=None,
        peer_cache=PeerCache(tmp_path / "paused-peers.json"),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("paused placement must not prepare new candidates")

    monkeypatch.setattr(run_node, "_automatic_placement_candidates", forbidden)
    runtime.clock[0] += 700  # Even an expired lease does not authorize work while paused.
    service.reconcile_once()
    assert runtime.registry.snapshot() == before
    assert runtime.publication.call_count == published
    assert not runtime.children
