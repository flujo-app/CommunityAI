"""Small real-p2pd security integration tests (kept outside the cheap CI list)."""

from hivemind import DHT
from hivemind.utils import get_dht_time

from drift.data_structures import ServerInfo, ServerState
from drift.protocol_identity import NodeIdentity, ReplayGuard, RevocationStore, create_worker_announcement
from drift.utils.dht import declare_active_modules, get_remote_module_infos


def test_signed_announcement_round_trip_over_real_dht(tmp_path):
    digest = "a" * 64
    prefix = f"drift-m1-{digest}"
    profile = {
        "implementation": "drift",
        "minimum_version": "2.3.0.dev0",
        "maximum_version_exclusive": "2.4.0",
        "protocol_version": 1,
        "tensor_schema": "hidden-states-v1",
        "attention_implementation": "eager",
        "dtype": "float32",
        "quantization": "none",
        "adapter_profile": "none",
    }
    identity_path = tmp_path / "worker.key"
    identity = NodeIdentity.create(identity_path)
    dht = DHT(
        initial_peers=[],
        start=True,
        client_mode=False,
        use_relay=False,
        host_maddrs=["/ip4/127.0.0.1/tcp/0"],
        identity_path=str(identity_path),
        tls=True,
    )
    try:
        assert dht.peer_id == identity.peer_id
        info = ServerInfo(
            state=ServerState.ONLINE,
            throughput=1.0,
            start_block=0,
            end_block=2,
            manifest_digest=digest,
            torch_dtype="float32",
            quant_type="none",
            using_relay=False,
        )
        issued_at = get_dht_time()
        expires_at = issued_at + 60
        info.signed_announcement = create_worker_announcement(
            identity,
            dht_prefix=prefix,
            manifest_digest=digest,
            execution_profile=profile,
            server_info=info.signed_payload(),
            issued_at=issued_at,
            expires_at=expires_at,
            sequence=1,
        ).to_dict()
        uids = [f"{prefix}.0", f"{prefix}.1"]
        assert all(declare_active_modules(dht, uids, info, expires_at).values())

        modules = get_remote_module_infos(
            dht,
            uids,
            manifest_digest=digest,
            manifest_execution_profile=profile,
            revocations=RevocationStore(),
            replay_guard=ReplayGuard(),
            latest=True,
        )
        assert [list(module.servers) for module in modules] == [[identity.peer_id], [identity.peer_id]]
    finally:
        dht.shutdown()
        dht.join()
