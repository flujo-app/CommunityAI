import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_catalog_bootstrap import _release_documents

from drift.model_catalog import CatalogRung
from drift.node.model_manager import ModelDescriptor, ModelManager, ModelManagerClosedError, ModelRuntime
from drift.node.model_selection import MeasuredModelSelector, RouteProbeService
from drift.node.route_health import _coverage_health


def test_peer_disjoint_routes_and_largest_peer_loss():
    complete = _coverage_health([{1, 3}, {1, 3}, {2, 4}, {2, 4}], updated_age=0)
    assert complete["minimum_replicas"] == complete["independent_routes"] == 2
    assert complete["replicas_after_largest_peer_loss"] == 1
    # Two replicas per block do not imply two routes with independent peers.
    overlapping = _coverage_health([{1, 2}, {2, 3}, {1, 3}], updated_age=0)
    assert overlapping["minimum_replicas"] == 2
    assert overlapping["independent_routes"] == 1
    assert _coverage_health([{1}, {2}], updated_age=0)["replicas_after_largest_peer_loss"] == 0
    assert complete["coverage_fingerprint"] != overlapping["coverage_fingerprint"]


def test_probe_soak_staleness_latency_and_peer_change_gate_promotion():
    _, envelope, _ = _release_documents()
    catalog = envelope.signed
    digest = catalog.models[0].manifest_digest
    health = _coverage_health([{1, 2}, {1, 2}], updated_age=0.5)
    now = [2_000_000_000.0]
    selector = MeasuredModelSelector(catalog, lambda _: health, clock=lambda: now[0])
    assert selector.selection()[0] is None
    target = selector.probe_target()
    assert target == (digest, health["coverage_fingerprint"])
    assert selector.record_probe(digest, target[1], first_token_seconds=0.2, completion_tokens=3, duration_seconds=1)
    assert selector.selection()[0] is None  # Coverage has not soaked yet.
    for _ in range(6):
        now[0] += 10
        selector.selection()
    assert selector.selection()[0].manifest_digest == digest
    health["last_updated_age"] = 31
    assert selector.selection()[0] is None
    health["last_updated_age"] = 0
    target = selector.probe_target()
    selector.record_probe(digest, target[1], first_token_seconds=3, completion_tokens=3, duration_seconds=4)
    for _ in range(6):
        now[0] += 10
        selector.selection()
    assert selector.selection()[0] is None  # Real latency misses signed budget.
    health.update(_coverage_health([{3, 4}, {3, 4}], updated_age=0))
    assert not selector.record_probe(
        digest, target[1], first_token_seconds=0.1, completion_tokens=3, duration_seconds=1
    )
    assert selector.selection()[0] is None


def test_best_effort_is_an_explicit_catalog_policy_and_local_needs_no_peer_probe():
    _, envelope, _ = _release_documents()
    rung = replace(
        envelope.signed.rungs[0],
        minimum_replicas=1,
        minimum_independent_routes=1,
        minimum_surviving_replicas=0,
        minimum_soak_seconds=0,
    )
    assert CatalogRung.from_dict(rung.to_dict(), index=0).minimum_surviving_replicas == 0
    local, remote = envelope.signed.models
    catalog = replace(envelope.signed, rungs=(rung,), models=(replace(local, execution="local"), remote))
    health = _coverage_health([{1}, {2}], updated_age=0)

    def read(digest):
        assert digest == remote.manifest_digest
        return health

    selector = MeasuredModelSelector(catalog, read, clock=lambda: 2_000_000_000)
    digest, fingerprint = selector.probe_target()
    selector.record_probe(digest, fingerprint, first_token_seconds=0.1, completion_tokens=3, duration_seconds=1)
    assert selector.selection()[0] == remote


def test_catalog_restart_waits_for_lease_and_atomically_rejects_new_loads():
    manager = ModelManager()
    closed = []
    manager.register(
        ModelDescriptor("local"), lambda: ModelRuntime(object(), object(), close=lambda: closed.append(True))
    )
    lease = manager.load("local")
    assert not manager.begin_idle_restart()
    assert lease.runtime.model is not None
    lease.release()
    assert manager.begin_idle_restart()
    with pytest.raises(ModelManagerClosedError):
        manager.load("local")
    manager.shutdown()
    assert closed == [True]


def test_busy_generation_keeps_observing_fresh_coverage_and_still_rejects_real_gaps():
    _, envelope, _ = _release_documents()
    now = [2_000_000_000.0]
    health = _coverage_health([{1, 2}, {1, 2}], updated_age=0)
    selector = MeasuredModelSelector(envelope.signed, lambda _: health, clock=lambda: now[0])
    digest, fingerprint = selector.probe_target()
    selector.record_probe(digest, fingerprint, first_token_seconds=0.1, completion_tokens=3, duration_seconds=1)
    observed, gap_seen = threading.Event(), threading.Event()
    reads = []

    def read(_):
        now[0] += 5  # Fresh observations throughout a generation longer than 30 seconds.
        reads.append(now[0])
        if len(reads) >= 20:
            observed.set()
        if health["status"] == "unknown":
            gap_seen.set()
        return dict(health)

    selector._health_reader = read

    class BusyManager:
        inference_mode = "auto"

        def snapshots(self):
            return [SimpleNamespace(active_requests=1)]

        def load(self, _):
            raise AssertionError("A busy generation must not start another probe")

    service = RouteProbeService(BusyManager(), selector, period=0.005)
    service.start()
    try:
        assert observed.wait(2), "Route observations stopped while generation was busy"
        assert selector.selection()[0] is not None
        assert selector._measurements[digest].samples == 1
        health["status"] = "unknown"
        assert gap_seen.wait(2)
        assert selector.selection()[0] is None
    finally:
        service.close()
