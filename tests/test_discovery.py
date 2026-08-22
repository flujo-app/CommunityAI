import threading
import time
from types import SimpleNamespace

from drift.data_structures import RemoteModuleInfo, ServerState
from drift.model_manifest import ModelManifest
from drift.node.discovery import CoverageTarget, ModelCoverageDiscovery


class FakeDHT:
    def __init__(self):
        self.alive = True
        self.shutdown_calls = 0

    def is_alive(self):
        return self.alive

    def shutdown(self):
        self.alive = False
        self.shutdown_calls += 1


class StrictFakeDHT(FakeDHT):
    def is_alive(self):
        # Mimic Hivemind's short shutdown race: process liveness may remain true
        # after its control pipe has already closed.
        return True

    def shutdown(self):
        if self.shutdown_calls:
            raise OSError("handle is closed")
        self.shutdown_calls += 1


def test_unloaded_discovery_reports_verified_complete_coverage():
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    dht = FakeDHT()
    refreshed = threading.Event()
    calls = []

    def lookup(selected_dht, uids, **kwargs):
        calls.append((selected_dht, uids, kwargs))
        peer = object()
        refreshed.set()
        return [RemoteModuleInfo(uid, {peer: SimpleNamespace(state=ServerState.ONLINE)}) for uid in uids]

    discovery = ModelCoverageDiscovery(
        [CoverageTarget(manifest, ("peer-one",))],
        update_period=60,
        startup_timeout=2,
        dht_factory=lambda **kwargs: dht,
        lookup=lookup,
    )
    discovery.start()
    assert refreshed.wait(timeout=1)

    health = discovery.snapshot(manifest.digest_id)
    assert health["source"] == "discovery"
    assert health["status"] == "complete"
    assert health["covered_blocks"] == manifest.model.num_blocks
    assert health["minimum_replicas"] == 1
    assert health["last_error"] is None
    assert calls[0][0] is dht
    assert calls[0][2]["manifest_digest"] == manifest.digest
    assert calls[0][2]["manifest_execution_profile"] == manifest.runtime.to_dict()
    assert calls[0][2]["latest"] is True

    discovery.close()
    discovery.close()
    assert dht.shutdown_calls == 1


def test_models_with_the_same_peers_share_one_dht_but_keep_independent_guards():
    first = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    second_source = first.to_dict()
    second_source["name"] = "Second discovery model"
    second_source["aliases"] = ["second-discovery"]
    second = ModelManifest.from_dict(second_source)
    dht = FakeDHT()
    refreshed = threading.Event()
    factory_calls = []
    lookups = []

    def factory(**kwargs):
        factory_calls.append(kwargs)
        return dht

    def lookup(selected_dht, uids, **kwargs):
        lookups.append(kwargs)
        if len(lookups) == 2:
            refreshed.set()
        peer = object()
        return [RemoteModuleInfo(uid, {peer: SimpleNamespace(state=ServerState.ONLINE)}) for uid in uids]

    discovery = ModelCoverageDiscovery(
        [CoverageTarget(first, ("shared-peer",)), CoverageTarget(second, ("shared-peer",))],
        update_period=60,
        startup_timeout=1,
        dht_factory=factory,
        lookup=lookup,
    )
    discovery.start()
    assert refreshed.wait(timeout=1)

    assert len(factory_calls) == 1
    assert {call["manifest_digest"] for call in lookups} == {first.digest, second.digest}
    assert lookups[0]["replay_guard"] is not lookups[1]["replay_guard"]
    assert discovery.snapshot(first.digest_id)["status"] == "complete"
    assert discovery.snapshot(second.digest_id)["status"] == "complete"
    discovery.close()


def test_discovery_failure_is_observable_without_blocking_startup():
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    attempted = threading.Event()

    def fail_factory(**kwargs):
        attempted.set()
        raise RuntimeError("seed unavailable")

    discovery = ModelCoverageDiscovery(
        [CoverageTarget(manifest, ("missing-peer",))],
        update_period=60,
        startup_timeout=0.1,
        dht_factory=fail_factory,
    )
    discovery.start()
    assert attempted.wait(timeout=1)
    deadline = time.monotonic() + 1
    while discovery.snapshot(manifest.digest_id)["last_error"] is None and time.monotonic() < deadline:
        time.sleep(0.01)

    health = discovery.snapshot(manifest.digest_id)
    assert health["status"] == "unknown"
    assert health["covered_blocks"] is None
    assert health["last_error"] == "RuntimeError: seed unavailable"
    discovery.close()


def test_discovery_shutdown_calls_each_dht_only_once_across_thread_race():
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    dht = StrictFakeDHT()
    refreshed = threading.Event()

    def lookup(selected_dht, uids, **kwargs):
        refreshed.set()
        return [RemoteModuleInfo(uid, {}) for uid in uids]

    discovery = ModelCoverageDiscovery(
        [CoverageTarget(manifest, ("peer-one",))],
        update_period=60,
        startup_timeout=1,
        dht_factory=lambda **kwargs: dht,
        lookup=lookup,
    )
    discovery.start()
    assert refreshed.wait(timeout=1)
    discovery.close()

    assert dht.shutdown_calls == 1
