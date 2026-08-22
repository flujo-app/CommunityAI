import asyncio
import copy
import time
from types import SimpleNamespace

import pytest
from hivemind.p2p import PeerID

from drift.data_structures import ServerInfo, ServerState
from drift.protocol_identity import (
    NodeIdentity,
    ProtocolSecurityError,
    ReplayGuard,
    RevocationStore,
    SignedRecord,
    create_intent_lease,
    create_revocation_record,
    create_rotation_record,
    create_worker_announcement,
    verify_intent_lease,
    verify_rotation_record,
    verify_worker_announcement,
)
from drift.utils.dht import _get_remote_module_infos, get_remote_module_infos

MANIFEST_DIGEST = "a" * 64
DHT_PREFIX = f"drift-m1-{MANIFEST_DIGEST}"
EXECUTION_PROFILE = {
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


def make_identity(tmp_path, name="identity.key"):
    return NodeIdentity.create(tmp_path / name)


def make_server_info():
    return ServerInfo(
        state=ServerState.ONLINE,
        throughput=2.5,
        start_block=0,
        end_block=2,
        manifest_digest=MANIFEST_DIGEST,
        torch_dtype="float32",
        quant_type="none",
        using_relay=False,
    )


def sign_server_info(identity, server_info, *, now=None, sequence=1):
    now = time.time() if now is None else now
    record = create_worker_announcement(
        identity,
        dht_prefix=DHT_PREFIX,
        manifest_digest=MANIFEST_DIGEST,
        execution_profile=EXECUTION_PROFILE,
        server_info=server_info.signed_payload(),
        issued_at=now,
        expires_at=now + 60,
        sequence=sequence,
    )
    server_info.signed_announcement = record.to_dict()
    return record


def verify_server_info(identity, server_info, *, replay_guard=None, revocations=None, now=None):
    return verify_worker_announcement(
        server_info.signed_announcement,
        expected_peer_id=identity.peer_id,
        expected_dht_prefix=DHT_PREFIX,
        expected_manifest_digest=MANIFEST_DIGEST,
        expected_server_info=server_info.signed_payload(),
        expected_execution_profile=EXECUTION_PROFILE,
        replay_guard=replay_guard,
        revocations=revocations,
        now=now,
    )


def test_node_identity_matches_hivemind_peer_id_and_permissions(tmp_path):
    path = tmp_path / "worker.key"
    identity = NodeIdentity.create(path)
    loaded = NodeIdentity.load(path)

    assert loaded.peer_id == PeerID.from_identity(path.read_bytes())
    assert loaded.peer_id == identity.peer_id
    assert loaded.key_id == identity.key_id
    assert "PRIVATE" not in identity.public_key_b64


def test_worker_announcement_covers_identity_manifest_profile_and_metadata(tmp_path):
    identity = make_identity(tmp_path)
    server_info = make_server_info()
    record = sign_server_info(identity, server_info)

    verified = verify_server_info(identity, server_info)
    assert verified.key_id == identity.key_id
    assert verified.peer_id == identity.peer_id

    tampered = copy.deepcopy(server_info.signed_announcement)
    tampered["payload"]["manifest_digest"] = "b" * 64
    server_info.signed_announcement = tampered
    with pytest.raises(ProtocolSecurityError, match="invalid signature"):
        verify_server_info(identity, server_info)

    server_info.signed_announcement = record.to_dict()
    server_info.throughput = 99.0
    with pytest.raises(ProtocolSecurityError, match="does not cover"):
        verify_server_info(identity, server_info)


def test_worker_announcement_rejects_wrong_peer_expiry_and_replay(tmp_path):
    identity = make_identity(tmp_path, "first.key")
    other = make_identity(tmp_path, "other.key")
    server_info = make_server_info()
    now = time.time()
    first = sign_server_info(identity, server_info, now=now, sequence=10)

    with pytest.raises(ProtocolSecurityError, match="PeerID"):
        verify_worker_announcement(
            first.to_dict(),
            expected_peer_id=other.peer_id,
            expected_dht_prefix=DHT_PREFIX,
            expected_manifest_digest=MANIFEST_DIGEST,
            expected_server_info=server_info.signed_payload(),
            now=now,
        )
    with pytest.raises(ProtocolSecurityError, match="expired"):
        verify_server_info(identity, server_info, now=now + 61)

    guard = ReplayGuard()
    verify_server_info(identity, server_info, replay_guard=guard, now=now)
    verify_server_info(identity, server_info, replay_guard=guard, now=now)  # same record covers another block

    older_info = make_server_info()
    sign_server_info(identity, older_info, now=now - 1, sequence=9)
    with pytest.raises(ProtocolSecurityError, match="older"):
        verify_server_info(identity, older_info, replay_guard=guard, now=now)

    equivocation = make_server_info()
    equivocation.throughput = 3.0
    sign_server_info(identity, equivocation, now=now, sequence=10)
    with pytest.raises(ProtocolSecurityError, match="equivocated"):
        verify_server_info(identity, equivocation, replay_guard=guard, now=now)

    server_info.signed_announcement = first.to_dict()
    wrong_profile = {**EXECUTION_PROFILE, "dtype": "float16"}
    with pytest.raises(ProtocolSecurityError, match="execution profile"):
        verify_worker_announcement(
            first.to_dict(),
            expected_peer_id=identity.peer_id,
            expected_dht_prefix=DHT_PREFIX,
            expected_manifest_digest=MANIFEST_DIGEST,
            expected_server_info=server_info.signed_payload(),
            expected_execution_profile=wrong_profile,
            now=now,
        )


def test_intent_lease_is_signed_bounded_and_replay_checked(tmp_path):
    identity = make_identity(tmp_path)
    now = time.time()
    lease = create_intent_lease(
        identity,
        manifest_digest=MANIFEST_DIGEST,
        start_block=2,
        end_block=6,
        resource_claims={"vram_bytes": 8_000_000_000, "throughput": 4.5},
        issued_at=now,
        expires_at=now + 30,
        sequence=4,
        nonce="00112233445566778899aabbccddeeff",
    )
    assert verify_intent_lease(lease.to_dict(), expected_manifest_digest=MANIFEST_DIGEST, now=now).peer_id

    with pytest.raises(ProtocolSecurityError, match="different manifest"):
        verify_intent_lease(lease.to_dict(), expected_manifest_digest="b" * 64, now=now)

    tampered = lease.to_dict()
    tampered["payload"]["end_block"] = 7
    with pytest.raises(ProtocolSecurityError, match="invalid signature"):
        verify_intent_lease(tampered, now=now)


def test_dual_signed_rotation_and_successor_revocation(tmp_path):
    old = make_identity(tmp_path, "old.key")
    new = make_identity(tmp_path, "new.key")
    rotation = create_rotation_record(old, new, sequence=1)
    assert verify_rotation_record(rotation) == (old.key_id, new.key_id)

    successor_revocation = create_revocation_record(
        new,
        revoked_key_id=old.key_id,
        revoked_peer_id=old.peer_id.to_base58(),
        reason="old key retired",
        sequence=2,
    )
    store = RevocationStore.from_records([successor_revocation, rotation])
    assert old.key_id in store.revoked_key_ids

    server_info = make_server_info()
    sign_server_info(old, server_info)
    with pytest.raises(ProtocolSecurityError, match="revoked"):
        verify_server_info(old, server_info, revocations=store)

    forged_rotation = copy.deepcopy(rotation)
    forged_rotation["payload"]["new_peer_id"] = old.peer_id.to_base58()
    with pytest.raises(ProtocolSecurityError, match="same payload"):
        verify_rotation_record(forged_rotation)


def test_rotation_forks_cycles_and_unauthorized_revocations_fail_closed(tmp_path):
    old = make_identity(tmp_path, "old.key")
    first = make_identity(tmp_path, "first.key")
    second = make_identity(tmp_path, "second.key")
    unrelated = make_identity(tmp_path, "unrelated.key")

    with pytest.raises(ProtocolSecurityError, match="forks"):
        RevocationStore.from_records([create_rotation_record(old, first), create_rotation_record(old, second)])
    with pytest.raises(ProtocolSecurityError, match="cycle"):
        RevocationStore.from_records([create_rotation_record(old, first), create_rotation_record(first, old)])

    unauthorized = create_revocation_record(
        unrelated,
        revoked_key_id=old.key_id,
        revoked_peer_id=old.peer_id.to_base58(),
        reason="forged authority",
    )
    with pytest.raises(ProtocolSecurityError, match="not signed"):
        RevocationStore.from_records([unauthorized])


def test_dht_reader_filters_unsigned_and_tampered_manifested_records(tmp_path):
    identity = make_identity(tmp_path)
    uid = f"{DHT_PREFIX}.0"
    server_info = make_server_info()
    sign_server_info(identity, server_info)

    class FakeNode:
        def __init__(self, value):
            self.value = value

        async def get_many(self, uids, expiration_time, num_workers):
            return {uid: SimpleNamespace(value={identity.peer_id.to_base58(): SimpleNamespace(value=self.value)})}

    dht = SimpleNamespace(num_workers=None)
    accepted = asyncio.run(
        _get_remote_module_infos(
            dht,
            FakeNode(server_info.to_tuple()),
            [uid],
            None,
            MANIFEST_DIGEST,
            EXECUTION_PROFILE,
            RevocationStore(),
            ReplayGuard(),
            None,
            True,
        )
    )
    assert list(accepted[0].servers) == [identity.peer_id]

    unsigned = make_server_info()
    rejected = asyncio.run(
        _get_remote_module_infos(
            dht,
            FakeNode(unsigned.to_tuple()),
            [uid],
            None,
            MANIFEST_DIGEST,
            EXECUTION_PROFILE,
            RevocationStore(),
            ReplayGuard(),
            None,
            True,
        )
    )
    assert not rejected[0].servers

    copied_outside_range = f"{DHT_PREFIX}.7"

    class CopiedNode:
        async def get_many(self, uids, expiration_time, num_workers):
            return {
                copied_outside_range: SimpleNamespace(
                    value={identity.peer_id.to_base58(): SimpleNamespace(value=server_info.to_tuple())}
                )
            }

    rejected_copy = asyncio.run(
        _get_remote_module_infos(
            dht,
            CopiedNode(),
            [copied_outside_range],
            None,
            MANIFEST_DIGEST,
            EXECUTION_PROFILE,
            RevocationStore(),
            ReplayGuard(),
            None,
            True,
        )
    )
    assert not rejected_copy[0].servers

    with pytest.raises(ValueError, match="execution profile"):
        get_remote_module_infos(
            SimpleNamespace(),
            [uid],
            manifest_digest=MANIFEST_DIGEST,
        )


def test_signed_record_parser_rejects_unknown_fields_and_noncanonical_keys(tmp_path):
    identity = make_identity(tmp_path)
    record = SignedRecord.create("test", {"message": "hello"}, identity).to_dict()
    record["extra"] = True
    with pytest.raises(ProtocolSecurityError, match="unknown fields"):
        SignedRecord.from_dict(record)


def test_signed_payload_rejects_non_finite_network_measurements(tmp_path):
    identity = make_identity(tmp_path)
    server_info = make_server_info()
    server_info.next_pings = {"unreachable-peer": float("inf")}
    with pytest.raises(ProtocolSecurityError, match="non-finite"):
        sign_server_info(identity, server_info)


def test_trust_bundle_rejects_duplicate_json_keys(tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text('{"kind":"identity_revocation","kind":"identity_rotation"}', encoding="utf-8")
    with pytest.raises(ProtocolSecurityError, match="duplicate object key"):
        RevocationStore.from_files([path])


def test_identity_cli_never_overwrites_private_keys_with_trust_records(tmp_path, monkeypatch):
    from drift.cli import run_identity

    identity_path = tmp_path / "worker.key"
    identity = NodeIdentity.create(identity_path)
    original_bytes = identity_path.read_bytes()
    monkeypatch.setattr(
        "sys.argv",
        ["drift identity", "revoke", str(identity_path), "--output", str(identity_path), "--force"],
    )
    with pytest.raises(SystemExit):
        run_identity.main()
    assert identity_path.read_bytes() == original_bytes
    assert NodeIdentity.load(identity_path).key_id == identity.key_id

    occupied_output = tmp_path / "rotation.json"
    occupied_output.write_text("keep", encoding="utf-8")
    new_identity_path = tmp_path / "next.key"
    monkeypatch.setattr(
        "sys.argv",
        [
            "drift identity",
            "rotate",
            str(identity_path),
            str(new_identity_path),
            "--output",
            str(occupied_output),
        ],
    )
    with pytest.raises(SystemExit):
        run_identity.main()
    assert not new_identity_path.exists()
    assert occupied_output.read_text(encoding="utf-8") == "keep"
