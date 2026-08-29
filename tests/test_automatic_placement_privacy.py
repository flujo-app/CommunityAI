import json
import logging

from drift.cli.run_node import _prepare_route_identity
from drift.node.route_metrics import RouteOutcomeTracker
from drift.protocol_identity import (
    NodeIdentity,
    ProtocolSecurityError,
    ReplayGuard,
    create_intent_lease,
    create_route_demand,
)

MANIFEST_DIGEST = "a" * 64
MANIFEST_DIGEST_ID = f"sha256:{MANIFEST_DIGEST}"
OBSERVATION_FIELDS = {
    "schema_version",
    "manifest_digest",
    "window_seconds",
    "attempts_bucket",
    "successes_bucket",
    "useful_tokens_per_second_milli",
    "reliability_milli",
    "age_seconds_bucket",
}
ENVELOPE_FIELDS = {
    "schema_version",
    "kind",
    "algorithm",
    "key_id",
    "public_key",
    "payload",
    "signature",
}


def route_observation():
    return {
        "schema_version": 1,
        "manifest_digest": MANIFEST_DIGEST_ID,
        "window_seconds": 300,
        "attempts_bucket": 4,
        "successes_bucket": 2,
        "useful_tokens_per_second_milli": 2_000,
        "reliability_milli": 500,
        "age_seconds_bucket": 15,
    }


def test_route_outcomes_retain_only_one_bounded_aggregate_window():
    now = [600.0]
    tracker = RouteOutcomeTracker(clock=lambda: now[0])
    for _ in range(4):
        tracker.record(
            manifest_digest=MANIFEST_DIGEST_ID,
            succeeded=True,
            completion_tokens=8,
            duration_seconds=2,
        )

    snapshot = tracker.snapshot(MANIFEST_DIGEST_ID)

    assert set(snapshot) == OBSERVATION_FIELDS
    assert snapshot["attempts_bucket"] == 4
    assert snapshot["successes_bucket"] == 4
    assert set(tracker._windows) == {MANIFEST_DIGEST_ID}
    aggregate = tracker._windows[MANIFEST_DIGEST_ID][0]
    assert set(aggregate.__dict__) == {
        "window_number",
        "attempts",
        "successes",
        "useful_tokens",
        "useful_duration_seconds",
        "last_observed_at",
    }


def test_public_intent_demand_and_replay_documents_have_exact_low_cardinality_schemas(tmp_path):
    identity = NodeIdentity.create(tmp_path / "observer.key")
    demand = create_route_demand(
        identity,
        manifest_digest=MANIFEST_DIGEST,
        observation=route_observation(),
        issued_at=2_000_000_000,
        expires_at=2_000_000_090,
        sequence=7,
    )
    intent = create_intent_lease(
        identity,
        manifest_digest=MANIFEST_DIGEST,
        start_block=1,
        end_block=2,
        resource_claims={
            "schema_version": 1,
            "artifact_bytes": 1024,
            "block_count": 1,
            "throughput_milli_rps": 500,
        },
        issued_at=2_000_000_000,
        expires_at=2_000_000_600,
        sequence=8,
        nonce="0" * 32,
    )

    assert set(demand.to_dict()) == ENVELOPE_FIELDS
    assert set(demand.payload) == {
        "manifest_digest",
        "observation",
        "issued_at_ms",
        "expires_at_ms",
        "sequence",
    }
    assert set(demand.payload["observation"]) == OBSERVATION_FIELDS
    assert set(intent.to_dict()) == ENVELOPE_FIELDS
    assert set(intent.payload) == {
        "peer_id",
        "manifest_digest",
        "start_block",
        "end_block",
        "resource_claims",
        "issued_at_ms",
        "expires_at_ms",
        "sequence",
        "nonce",
    }
    assert set(intent.payload["resource_claims"]) == {
        "schema_version",
        "artifact_bytes",
        "block_count",
        "throughput_milli_rps",
    }

    history_path = tmp_path / "replay.json"
    ReplayGuard(history_path, clock=lambda: 2_000_000_000).check(demand)
    history = json.loads(history_path.read_text(encoding="utf-8"))
    assert set(history) == {"schema_version", "entries"}
    assert len(history["entries"]) == 1
    assert set(history["entries"][0]) == {
        "kind",
        "key_id",
        "issued_at_ms",
        "sequence",
        "record_digest",
        "retain_until_ms",
    }

    rendered = json.dumps({"demand": demand.to_dict(), "intent": intent.to_dict(), "history": history})
    for forbidden in (
        "prompt",
        "generated_text",
        "token_ids",
        "api_key",
        "request_id",
        "client_id",
        "remote_address",
        "identity_path",
        "private_key",
        "error_detail",
    ):
        assert forbidden not in rendered


def test_route_identity_failure_log_omits_private_path_and_exception_detail(monkeypatch, tmp_path, caplog):
    path = tmp_path / "private-operator-name" / "route-demand.key"
    path.parent.mkdir()
    path.write_bytes(b"invalid")
    sensitive_detail = f"corrupt credential at {path}"
    monkeypatch.setattr(
        NodeIdentity,
        "load",
        lambda _: (_ for _ in ()).throw(ProtocolSecurityError(sensitive_detail)),
    )

    class Discovery:
        def register_local_route_demand_key(self, key_id):
            raise AssertionError("a corrupt key must not be registered")

    roots = ("sha256:" + "b" * 64, "sha256:" + "c" * 64)
    with caplog.at_level(logging.WARNING):
        assert _prepare_route_identity(Discovery(), path, roots) is None

    assert sensitive_detail not in caplog.text
    assert str(path) not in caplog.text
    assert "could not be loaded safely" in caplog.text
