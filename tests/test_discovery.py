import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest
from hivemind.p2p import PeerID

from drift.data_structures import RemoteModuleInfo, ServerState
from drift.model_manifest import ModelManifest
from drift.node.discovery import (
    MAX_PEER_CACHE_AGE_SECONDS,
    CoverageTarget,
    ModelCoverageDiscovery,
    PeerCache,
    _connected_peer_addresses,
)
from drift.protocol_identity import NodeIdentity, create_intent_lease

PUBLIC_PEER = "/ip4/8.8.8.8/tcp/31337/p2p/Qm" + "A" * 44
SECOND_PUBLIC_PEER = "/ip6/2606:4700:4700::1111/tcp/31337/p2p/Qm" + "B" * 44
DNS_PEER = "/dns4/seed.example.com/tcp/31337/p2p/Qm" + "B" * 44
PRIVATE_PEER = "/ip4/10.0.0.4/tcp/31337/p2p/Qm" + "C" * 44
INVALID_PEER_ID = "/ip4/8.8.4.4/tcp/31337/p2p/" + "0" * 20
CACHE_SCOPE = ("shipped-seed",)


class FakeDHT:
    def __init__(self):
        self.alive = True
        self.shutdown_calls = 0

    def is_alive(self):
        return self.alive

    def shutdown(self):
        self.alive = False
        self.shutdown_calls += 1


class IntentFakeDHT(FakeDHT):
    def __init__(self, *, store_result=True):
        super().__init__()
        self.store_result = store_result
        self.store_calls = []

    def store(self, **kwargs):
        self.store_calls.append(kwargs)
        return self.store_result


class StrictFakeDHT(FakeDHT):
    def is_alive(self):
        # Mimic Hivemind's short shutdown race: process liveness may remain true
        # after its control pipe has already closed.
        return True

    def shutdown(self):
        if self.shutdown_calls:
            raise OSError("handle is closed")
        self.shutdown_calls += 1


def test_intent_publication_requires_a_remote_dht_store(tmp_path):
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    initial_peers = ("peer-one",)
    dht = IntentFakeDHT()
    discovery = ModelCoverageDiscovery([CoverageTarget(manifest, initial_peers)])
    discovery._dhts[initial_peers] = dht
    now = time.time()
    identity = NodeIdentity.create(tmp_path / "intent.key")
    record = create_intent_lease(
        identity,
        manifest_digest=manifest.digest,
        start_block=1,
        end_block=2,
        resource_claims={
            "schema_version": 1,
            "artifact_bytes": 100,
            "block_count": 1,
            "throughput_milli_rps": None,
        },
        issued_at=now,
        expires_at=now + 60,
        sequence=1,
    )

    assert discovery.publish_intent(manifest.digest_id, record.to_dict()) is True
    call = dht.store_calls[0]
    assert call["key"] == f"{manifest.dht_prefix}.intent-v1"
    assert call["subkey"] == record.key_id
    assert call["exclude_self"] is True
    assert call["expiration_time"] == record.payload["expires_at_ms"] / 1000
    assert call["value"] == record.to_dict()

    dht.store_result = False
    record = create_intent_lease(
        identity,
        manifest_digest=manifest.digest,
        start_block=2,
        end_block=3,
        resource_claims={
            "schema_version": 1,
            "artifact_bytes": 100,
            "block_count": 1,
            "throughput_milli_rps": None,
        },
        issued_at=now,
        expires_at=now + 60,
        sequence=2,
    )
    assert discovery.publish_intent(manifest.digest_id, record.to_dict()) is False

    def unavailable_store(**kwargs):
        raise RuntimeError("remote store unavailable")

    dht.store = unavailable_store
    assert discovery.publish_intent(manifest.digest_id, record.to_dict()) is False


def test_peer_cache_retains_only_fresh_unique_public_peers(tmp_path):
    now = [2_000_000_000.0]
    path = tmp_path / "discovery-peers.json"
    cache = PeerCache(path, clock=lambda: now[0])

    assert (
        cache.store(
            CACHE_SCOPE,
            (PRIVATE_PEER, PUBLIC_PEER, PUBLIC_PEER, SECOND_PUBLIC_PEER, DNS_PEER, INVALID_PEER_ID),
        )
        is True
    )
    assert cache.load(CACHE_SCOPE) == (PUBLIC_PEER, SECOND_PUBLIC_PEER)
    assert cache.load(("different-seed",)) == ()
    assert cache.store(CACHE_SCOPE, (PUBLIC_PEER, SECOND_PUBLIC_PEER)) is False
    rendered = json.loads(path.read_text(encoding="utf-8"))
    entry = next(iter(rendered["scopes"].values()))
    assert entry["peers"] == [PUBLIC_PEER, SECOND_PUBLIC_PEER]
    assert all(
        rejected not in path.read_text(encoding="utf-8") for rejected in (PRIVATE_PEER, DNS_PEER, INVALID_PEER_ID)
    )

    now[0] += MAX_PEER_CACHE_AGE_SECONDS + 1
    assert PeerCache(path, clock=lambda: now[0]).load(CACHE_SCOPE) == ()


def test_peer_cache_ignores_malformed_or_symbolic_link_files(tmp_path):
    path = tmp_path / "discovery-peers.json"
    path.write_text('{"schema_version":1,"schema_version":1,"scopes":{}}', encoding="utf-8")
    cache = PeerCache(path, clock=lambda: 1.0)
    assert cache.load(CACHE_SCOPE) == ()
    path.write_text('{"schema_version":true,"scopes":{}}', encoding="utf-8")
    assert cache.load(CACHE_SCOPE) == ()
    assert cache.store(CACHE_SCOPE, (PUBLIC_PEER,)) is True
    assert cache.load(CACHE_SCOPE) == (PUBLIC_PEER,)

    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    path.unlink()
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable on this host")
    assert PeerCache(path, clock=lambda: 1.0).load(CACHE_SCOPE) == ()
    with pytest.raises(OSError, match="symbolic-link"):
        PeerCache(path, clock=lambda: 1.0).store(CACHE_SCOPE, (PUBLIC_PEER,))


def test_successful_discovery_persists_connected_public_peers(tmp_path):
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    dht = FakeDHT()
    refreshed = threading.Event()
    cache = PeerCache(tmp_path / "discovery-peers.json", clock=lambda: 2_000_000_000.0)

    def lookup(selected_dht, uids, **kwargs):
        refreshed.set()
        return [RemoteModuleInfo(uid, {}) for uid in uids]

    runtime_peers = ("shipped-seed", SECOND_PUBLIC_PEER)
    discovery = ModelCoverageDiscovery(
        [CoverageTarget(manifest, runtime_peers, cache_scope=CACHE_SCOPE)],
        update_period=60,
        startup_timeout=1,
        dht_factory=lambda **kwargs: dht,
        lookup=lookup,
        peer_cache=cache,
        peer_snapshot=lambda selected_dht: (PRIVATE_PEER, PUBLIC_PEER),
    )
    discovery.start()
    assert refreshed.wait(timeout=1)
    deadline = time.monotonic() + 1
    while not cache.path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)

    assert cache.load(CACHE_SCOPE) == (PUBLIC_PEER,)
    assert cache.load(runtime_peers) == ()
    discovery.close()


def test_connected_peer_snapshot_keeps_only_public_routing_table_addresses():
    routing_peer = PeerID.from_base58("Qm" + "A" * 44)
    unrelated_peer = PeerID.from_base58("Qm" + "B" * 44)

    class FakeP2P:
        async def list_peers(self):
            return [
                SimpleNamespace(
                    peer_id=routing_peer,
                    addrs=("/ip4/8.8.8.8/tcp/31337", "/ip4/10.0.0.4/tcp/31337"),
                ),
                SimpleNamespace(peer_id=unrelated_peer, addrs=("/ip6/2606:4700:4700::1111/tcp/31337",)),
            ]

    routing_table = SimpleNamespace(peer_id_to_uid={routing_peer: object()})
    protocol = SimpleNamespace(p2p=FakeP2P(), routing_table=routing_table)
    node = SimpleNamespace(protocol=protocol)

    assert asyncio.run(_connected_peer_addresses(None, node)) == (PUBLIC_PEER,)


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
