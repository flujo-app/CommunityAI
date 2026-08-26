import base64
import copy
import json
import sys
import time

import pytest

from drift.cli import run_catalog
from drift.model_catalog import (
    CATALOG_ROOT_SCHEMA_VERSION,
    CATALOG_SCHEMA_VERSION,
    CapacityObservation,
    CatalogRollbackGuard,
    CatalogSigningKey,
    CatalogTrustRoot,
    ModelCatalog,
    ModelCatalogError,
    SignedModelCatalog,
    select_highest_eligible_model,
)
from drift.node.catalog_bootstrap import CatalogBootstrapConfig

NOW = 2_000_000_000.0


def digest(character: str) -> str:
    return f"sha256:{character * 64}"


def rung(rung_id: str, order: int) -> dict:
    return {
        "id": rung_id,
        "order": order,
        "minimum_replicas": 2,
        "minimum_independent_routes": 2,
        "minimum_surviving_replicas": 1,
        "minimum_soak_seconds": 60,
        "maximum_observation_age_seconds": 30,
        "maximum_p95_first_token_ms": 2_000,
        "minimum_tokens_per_minute": 60,
    }


def model(character: str, rung_id: str, role: str, parameters: int) -> dict:
    return {
        "manifest_digest": digest(character),
        "manifest_urls": [f"https://catalog.example/manifests/{character}.json"],
        "rung": rung_id,
        "role": role,
        "total_parameters": parameters,
        "active_parameters": parameters,
        "weight_bytes": parameters,
    }


def catalog_dict(*, sequence: int = 1) -> dict:
    return {
        "catalog_id": "communityai-test",
        "sequence": sequence,
        "issued_at_ms": int((NOW - 60) * 1000),
        "expires_at_ms": int((NOW + 3600) * 1000),
        "rungs": [rung("small", 1), rung("large", 2)],
        "models": [
            model("a", "small", "primary", 1_700_000_000),
            model("b", "small", "standby", 1_000_000_000),
            model("c", "large", "primary", 8_000_000_000),
            model("d", "large", "standby", 8_000_000_000),
        ],
    }


def envelope(source: dict | None = None) -> SignedModelCatalog:
    return SignedModelCatalog(
        schema_version=CATALOG_SCHEMA_VERSION,
        signed=ModelCatalog.from_dict(catalog_dict() if source is None else source),
        signatures=(),
    )


def root(*keys: CatalogSigningKey, threshold: int = 1) -> CatalogTrustRoot:
    return CatalogTrustRoot.from_dict(
        {
            "schema_version": CATALOG_ROOT_SCHEMA_VERSION,
            "catalog_id": "communityai-test",
            "threshold": threshold,
            "keys": [key.trusted_key.to_dict() for key in keys],
        }
    )


def observation(
    character: str,
    *,
    replicas: float = 2,
    routes: int = 2,
    surviving: float = 1,
    stable_seconds: int = 120,
    age_seconds: int = 0,
    p95_ms: int = 1_000,
    tokens_per_minute: int = 120,
) -> CapacityObservation:
    observed_at_ms = int((NOW - age_seconds) * 1000)
    return CapacityObservation(
        manifest_digest=digest(character),
        observed_at_ms=observed_at_ms,
        stable_since_ms=int((NOW - stable_seconds) * 1000),
        bottleneck_replicas=replicas,
        independent_routes=routes,
        replicas_after_largest_peer_loss=surviving,
        p95_first_token_ms=p95_ms,
        tokens_per_minute=tokens_per_minute,
    )


def test_catalog_requires_two_options_and_one_primary_per_rung():
    source = catalog_dict()
    source["models"] = source["models"][:1] + source["models"][2:]
    with pytest.raises(ModelCatalogError, match="at least two model options"):
        ModelCatalog.from_dict(source)

    source = catalog_dict()
    source["models"][1]["role"] = "primary"
    with pytest.raises(ModelCatalogError, match="exactly one primary"):
        ModelCatalog.from_dict(source)


@pytest.mark.parametrize(
    "mutation, error",
    [
        (lambda source: source.update({"unknown": True}), "unknown fields"),
        (lambda source: source["rungs"].reverse(), "ascending order"),
        (lambda source: source["models"][0].update({"rung": "missing"}), "declared rung"),
        (lambda source: source["models"][0].update({"manifest_urls": ["http://unsafe.example/a"]}), "HTTPS"),
        (lambda source: source["models"][0].update({"active_parameters": 2_000_000_000}), "cannot exceed"),
    ],
)
def test_catalog_strict_validation(mutation, error):
    source = catalog_dict()
    mutation(source)
    with pytest.raises(ModelCatalogError, match=error):
        ModelCatalog.from_dict(source)


def test_catalog_json_rejects_duplicate_keys_and_nonfinite_numbers():
    key = CatalogSigningKey.generate()
    signed = envelope().add_signature(key).to_dict()
    rendered = json.dumps(signed)
    duplicate = rendered.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1', 1)
    with pytest.raises(ModelCatalogError, match="duplicate object key"):
        SignedModelCatalog.from_json(duplicate)
    with pytest.raises(ModelCatalogError, match="non-finite"):
        SignedModelCatalog.from_json(rendered.replace("1700000000", "NaN", 1))


def test_one_of_one_signature_and_private_key_round_trip(tmp_path):
    key = CatalogSigningKey.generate()
    private_path = tmp_path / "catalog.pem"
    key.save(private_path)
    loaded = CatalogSigningKey.load(private_path)
    assert loaded.key_id == key.key_id
    assert b"PRIVATE KEY" in private_path.read_bytes()

    signed = envelope().add_signature(loaded)
    assert signed.verify(root(key), now=NOW).digest == signed.signed.digest


def test_threshold_signatures_require_distinct_trusted_keys():
    keys = [CatalogSigningKey.generate() for _ in range(3)]
    trust_root = root(*keys, threshold=2)
    once = envelope().add_signature(keys[0])
    with pytest.raises(ModelCatalogError, match="2 required"):
        once.verify(trust_root, now=NOW)

    twice = once.add_signature(keys[1])
    assert twice.verify(trust_root, now=NOW).sequence == 1
    assert len(twice.add_signature(keys[1]).signatures) == 2


def test_tampering_unknown_signers_wrong_roots_and_expiry_fail_closed():
    trusted, attacker = CatalogSigningKey.generate(), CatalogSigningKey.generate()
    signed = envelope().add_signature(trusted)

    tampered = signed.to_dict()
    tampered["signed"]["models"][0]["total_parameters"] += 1
    with pytest.raises(ModelCatalogError, match="invalid"):
        SignedModelCatalog.from_dict(tampered).verify(root(trusted), now=NOW)

    with pytest.raises(ModelCatalogError, match="untrusted signature"):
        signed.add_signature(attacker).verify(root(trusted), now=NOW)

    wrong_root = CatalogTrustRoot.from_dict(
        {
            "schema_version": CATALOG_ROOT_SCHEMA_VERSION,
            "catalog_id": "different-community",
            "threshold": 1,
            "keys": [trusted.trusted_key.to_dict()],
        }
    )
    with pytest.raises(ModelCatalogError, match="does not match"):
        signed.verify(wrong_root, now=NOW)

    with pytest.raises(ModelCatalogError, match="expired"):
        signed.verify(root(trusted), now=NOW + 7200)


def test_trusted_key_rejects_mismatched_id_and_non_ed25519_material():
    key = CatalogSigningKey.generate().trusted_key.to_dict()
    key["key_id"] = digest("f")
    with pytest.raises(ModelCatalogError, match="does not match"):
        CatalogTrustRoot.from_dict(
            {
                "schema_version": CATALOG_ROOT_SCHEMA_VERSION,
                "catalog_id": "communityai-test",
                "threshold": 1,
                "keys": [key],
            }
        )

    key["key_id"] = digest("f")
    key["public_key"] = base64.b64encode(b"not DER").decode()
    with pytest.raises(ModelCatalogError, match="valid DER"):
        CatalogTrustRoot.from_dict(
            {
                "schema_version": CATALOG_ROOT_SCHEMA_VERSION,
                "catalog_id": "communityai-test",
                "threshold": 1,
                "keys": [key],
            }
        )


def test_rollback_guard_rejects_older_and_equivocating_catalogs(tmp_path):
    key = CatalogSigningKey.generate()
    trust_root = root(key)
    guard = CatalogRollbackGuard()
    sequence_two = catalog_dict(sequence=2)
    envelope(sequence_two).add_signature(key).verify(trust_root, now=NOW, rollback_guard=guard)

    with pytest.raises(ModelCatalogError, match="older"):
        envelope(catalog_dict(sequence=1)).add_signature(key).verify(trust_root, now=NOW, rollback_guard=guard)

    equivocation = copy.deepcopy(sequence_two)
    equivocation["models"][0]["weight_bytes"] += 1
    with pytest.raises(ModelCatalogError, match="equivocated"):
        envelope(equivocation).add_signature(key).verify(trust_root, now=NOW, rollback_guard=guard)

    state_path = tmp_path / "state.json"
    guard.save(state_path)
    reopened = CatalogRollbackGuard.load(state_path)
    assert reopened.latest == guard.latest


def test_highest_eligible_rung_prefers_primary_then_standby():
    catalog = ModelCatalog.from_dict(catalog_dict())

    selected, evaluations = select_highest_eligible_model(
        catalog,
        [observation("a"), observation("b"), observation("c"), observation("d")],
        now=NOW,
    )
    assert selected.manifest_digest == digest("c")
    assert all(item.eligible for item in evaluations)

    selected, _ = select_highest_eligible_model(
        catalog,
        [observation("a"), observation("b"), observation("c", replicas=1), observation("d")],
        now=NOW,
    )
    assert selected.manifest_digest == digest("d")


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"replicas": 1}, "bottleneck replica"),
        ({"routes": 1}, "independent routes"),
        ({"surviving": 0}, "largest-peer loss"),
        ({"stable_seconds": 10}, "soak"),
        ({"age_seconds": 31}, "stale"),
        ({"p95_ms": 2_001}, "first-token latency"),
        ({"tokens_per_minute": 59}, "throughput"),
    ],
)
def test_promotion_requires_coverage_failure_survival_soak_and_slos(overrides, reason):
    catalog = ModelCatalog.from_dict(catalog_dict())
    observations = [observation("a", **overrides)]
    selected, evaluations = select_highest_eligible_model(catalog, observations, now=NOW)
    assert selected is None
    evaluation = next(item for item in evaluations if item.model.manifest_digest == digest("a"))
    assert not evaluation.eligible
    assert any(reason in item for item in evaluation.reasons)


def test_selection_rejects_duplicate_observations_and_never_switches_without_evidence():
    catalog = ModelCatalog.from_dict(catalog_dict())
    selected, evaluations = select_highest_eligible_model(catalog, [], now=NOW)
    assert selected is None
    assert all(item.reasons == ("no capacity observation",) for item in evaluations)

    duplicate = observation("a")
    with pytest.raises(ModelCatalogError, match="at most one"):
        select_highest_eligible_model(catalog, [duplicate, duplicate], now=NOW)


def test_catalog_cli_keygen_root_sign_verify_and_rollback_state(tmp_path, monkeypatch, capsys):
    payload = tmp_path / "catalog.json"
    private_key = tmp_path / "catalog.pem"
    public_key = tmp_path / "catalog.pub.json"
    trust_root = tmp_path / "root.json"
    signed = tmp_path / "catalog.signed.json"
    bootstrap = tmp_path / "catalog-bootstrap.json"
    state = tmp_path / "state.json"
    source = catalog_dict()
    current = time.time()
    source["issued_at_ms"] = int((current - 60) * 1000)
    source["expires_at_ms"] = int((current + 3600) * 1000)
    payload.write_text(json.dumps(source), encoding="utf-8")

    def run(*args: str) -> str:
        monkeypatch.setattr(sys, "argv", ["drift catalog", *args])
        run_catalog.main()
        return capsys.readouterr().out

    run("keygen", str(private_key), "--public-output", str(public_key))
    run(
        "root",
        "--catalog-id",
        "communityai-test",
        "--threshold",
        "1",
        "--key",
        str(public_key),
        "--output",
        str(trust_root),
    )
    run("sign", str(payload), "--key", str(private_key), "--output", str(signed))
    run(
        "bootstrap-config",
        "--root",
        str(trust_root),
        "--catalog-mirror",
        "https://catalog.example.com/catalog.signed.json",
        "--initial-peer",
        "/dns4/bootstrap.example.com/tcp/31337/p2p/QmAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "--output",
        str(bootstrap),
    )
    output = run("verify", str(signed), "--root", str(trust_root), "--state", str(state))

    assert "valid catalog communityai-test sequence 1" in output
    assert CatalogRollbackGuard.load(state).latest["communityai-test"][0] == 1
    assert CatalogBootstrapConfig.load(bootstrap).trust_root.catalog_id == "communityai-test"


def test_catalog_cli_never_overwrites_a_signing_input(tmp_path, monkeypatch):
    private_key = tmp_path / "catalog.pem"
    CatalogSigningKey.generate().save(private_key)
    payload = tmp_path / "catalog.json"
    payload.write_text(json.dumps(catalog_dict()), encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "drift catalog",
            "sign",
            str(payload),
            "--key",
            str(private_key),
            "--output",
            str(private_key),
            "--force",
        ],
    )
    with pytest.raises(SystemExit) as raised:
        run_catalog.main()
    assert raised.value.code == 2
    assert b"PRIVATE KEY" in private_key.read_bytes()
