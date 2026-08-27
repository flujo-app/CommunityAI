import json

import pytest

from drift.model_catalog import CATALOG_SCHEMA_VERSION, CatalogSigningKey, ModelCatalog, SignedModelCatalog
from drift.model_manifest import ModelManifest
from drift.node.catalog_bootstrap import (
    MAX_CATALOG_BYTES,
    MAX_MANIFEST_BYTES,
    CatalogBootstrapConfig,
    CatalogBootstrapError,
    bootstrap_node_from_catalog,
)
from drift.node.config import NodeConfig

NOW = 2_000_000_000.0
PEER = "/dns4/bootstrap.communityai.example/tcp/31337/p2p/QmBootstrap"


def _manifest(name: str, alias: str) -> ModelManifest:
    source = ModelManifest.load("tests/data/model_manifest_v1_vector.json").to_dict()
    source["name"] = name
    source["aliases"] = [alias]
    return ModelManifest.from_dict(source)


def _rung() -> dict:
    return {
        "id": "1-2b",
        "order": 1,
        "minimum_replicas": 2,
        "minimum_independent_routes": 2,
        "minimum_surviving_replicas": 1,
        "minimum_soak_seconds": 60,
        "maximum_observation_age_seconds": 30,
        "maximum_p95_first_token_ms": 2_000,
        "minimum_tokens_per_minute": 60,
    }


def _release_documents(*, sequence: int = 1):
    primary = _manifest("Primary Test", "primary-test")
    standby = _manifest("Standby Test", "standby-test")
    models = []
    manifest_documents = {}
    for role, manifest in (("primary", primary), ("standby", standby)):
        url = f"https://models.example/{manifest.digest}.json"
        manifest_documents[url] = manifest.canonical_json()
        models.append(
            {
                "manifest_digest": manifest.digest_id,
                "manifest_urls": [url],
                "rung": "1-2b",
                "role": role,
                "total_parameters": 1_000_000_000,
                "active_parameters": 1_000_000_000,
                "weight_bytes": 2_000_000_000,
            }
        )
    catalog = ModelCatalog.from_dict(
        {
            "catalog_id": "communityai-test",
            "sequence": sequence,
            "issued_at_ms": int((NOW - 60) * 1000),
            "expires_at_ms": int((NOW + 3600) * 1000),
            "rungs": [_rung()],
            "models": models,
        }
    )
    key = CatalogSigningKey.generate()
    envelope = SignedModelCatalog(CATALOG_SCHEMA_VERSION, catalog, ()).add_signature(key)
    bootstrap = {
        "schema_version": 1,
        "trust_root": {
            "schema_version": 1,
            "catalog_id": catalog.catalog_id,
            "threshold": 1,
            "keys": [key.trusted_key.to_dict()],
        },
        "catalog_mirrors": ["https://catalog-one.example/catalog.json", "https://catalog-two.example/catalog.json"],
        "initial_peers": [PEER],
        "max_loaded_models": 1,
    }
    return bootstrap, envelope, manifest_documents


def _write_bootstrap(tmp_path, source) -> object:
    path = tmp_path / "bootstrap.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    return path


def test_bootstrap_installs_verified_manifests_and_atomic_node_config(tmp_path):
    bootstrap, envelope, manifests = _release_documents()
    bootstrap_path = _write_bootstrap(tmp_path, bootstrap)
    catalog_text = json.dumps(envelope.to_dict())
    calls = []

    def fetch(url, maximum_bytes):
        calls.append((url, maximum_bytes))
        if url == bootstrap["catalog_mirrors"][0]:
            raise CatalogBootstrapError("first mirror unavailable")
        if url == bootstrap["catalog_mirrors"][1]:
            return catalog_text
        return manifests[url]

    data_dir = tmp_path / "data"
    config_path = data_dir / "node-config.json"
    result = bootstrap_node_from_catalog(
        bootstrap_path,
        data_dir=data_dir,
        config_path=config_path,
        fetch_text=fetch,
        now=NOW,
    )

    assert result.created is True
    assert result.catalog_sequence == 1
    assert result.model_count == 2
    assert result.source == bootstrap["catalog_mirrors"][1]
    config = NodeConfig.load(config_path)
    assert len(config.models) == 2
    assert config.auto_model_priority == tuple(model.manifest_digest for model in envelope.signed.models)
    assert all(model.initial_peers == (PEER,) for model in config.models)
    assert all(model.manifest_path.parent == (data_dir / "manifests").resolve() for model in config.models)
    assert (data_dir / "catalogs" / "communityai-test" / "catalog.signed.json").is_file()
    assert (data_dir / "catalogs" / "communityai-test" / "rollback-state.json").is_file()
    assert calls[0] == (bootstrap["catalog_mirrors"][0], MAX_CATALOG_BYTES)
    assert any(limit == MAX_MANIFEST_BYTES for _, limit in calls)
    assert not (data_dir / ".catalog-bootstrap.lock").exists()


def test_existing_config_is_preserved_without_network_access(tmp_path):
    bootstrap, _, _ = _release_documents()
    bootstrap_path = _write_bootstrap(tmp_path, bootstrap)
    config_path = tmp_path / "node-config.json"
    existing = {
        "schema_version": 1,
        "models": [{"manifest": "existing.json", "initial_peers": [PEER]}],
    }
    rendered = json.dumps(existing) + "\n"
    config_path.write_text(rendered, encoding="utf-8")

    result = bootstrap_node_from_catalog(
        bootstrap_path,
        data_dir=tmp_path / "data",
        config_path=config_path,
        fetch_text=lambda *_: pytest.fail("existing config must not fetch"),
        now=NOW,
    )

    assert result.created is False
    assert result.source == "existing-config"
    assert config_path.read_text(encoding="utf-8") == rendered


def test_bootstrap_refuses_symbolic_link_inputs_and_activation_targets(tmp_path):
    bootstrap, _, _ = _release_documents()
    real_bootstrap = _write_bootstrap(tmp_path, bootstrap)
    linked_bootstrap = tmp_path / "linked-bootstrap.json"
    try:
        linked_bootstrap.symlink_to(real_bootstrap)
    except OSError:
        pytest.skip("symbolic links are unavailable on this test host")

    with pytest.raises(CatalogBootstrapError, match="missing or unsafe"):
        CatalogBootstrapConfig.load(linked_bootstrap)

    real_config = tmp_path / "real-node-config.json"
    real_config.write_text("{}\n", encoding="utf-8")
    linked_config = tmp_path / "linked-node-config.json"
    linked_config.symlink_to(real_config)
    with pytest.raises(CatalogBootstrapError, match="configuration symlink"):
        bootstrap_node_from_catalog(
            real_bootstrap,
            data_dir=tmp_path / "data",
            config_path=linked_config,
            fetch_text=lambda *_: pytest.fail("unsafe target must not fetch"),
            now=NOW,
        )


def test_last_known_good_catalog_and_manifests_support_offline_reprovision(tmp_path):
    bootstrap, envelope, manifests = _release_documents()
    bootstrap_path = _write_bootstrap(tmp_path, bootstrap)
    catalog_text = json.dumps(envelope.to_dict())

    def online_fetch(url, _maximum_bytes):
        if url in bootstrap["catalog_mirrors"]:
            return catalog_text
        return manifests[url]

    data_dir = tmp_path / "data"
    config_path = data_dir / "node-config.json"
    bootstrap_node_from_catalog(
        bootstrap_path, data_dir=data_dir, config_path=config_path, fetch_text=online_fetch, now=NOW
    )
    config_path.unlink()

    result = bootstrap_node_from_catalog(
        bootstrap_path,
        data_dir=data_dir,
        config_path=config_path,
        fetch_text=lambda *_: (_ for _ in ()).throw(CatalogBootstrapError("offline")),
        now=NOW,
    )

    assert result.created is True
    assert result.source == "last-known-good cache"
    assert NodeConfig.load(config_path).models


def test_tampered_catalog_and_wrong_manifest_digest_fail_without_activation(tmp_path):
    bootstrap, envelope, manifests = _release_documents()
    bootstrap["catalog_mirrors"] = [bootstrap["catalog_mirrors"][0]]
    bootstrap_path = _write_bootstrap(tmp_path, bootstrap)
    tampered = envelope.to_dict()
    tampered["signed"]["models"][0]["weight_bytes"] += 1
    config_path = tmp_path / "data" / "node-config.json"

    with pytest.raises(CatalogBootstrapError, match="No trusted usable.*signature"):
        bootstrap_node_from_catalog(
            bootstrap_path,
            data_dir=tmp_path / "data",
            config_path=config_path,
            fetch_text=lambda url, _: json.dumps(tampered) if "catalog" in url else manifests[url],
            now=NOW,
        )
    assert not config_path.exists()

    wrong_manifest = _manifest("Wrong Model", "wrong-model").canonical_json()
    with pytest.raises(CatalogBootstrapError, match="does not match catalog digest"):
        bootstrap_node_from_catalog(
            bootstrap_path,
            data_dir=tmp_path / "other-data",
            config_path=tmp_path / "other-data" / "node-config.json",
            fetch_text=lambda url, _: json.dumps(envelope.to_dict()) if "catalog" in url else wrong_manifest,
            now=NOW,
        )


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda value: value.update({"unknown": True}), "unknown field"),
        (lambda value: value.update({"catalog_mirrors": ["http://unsafe.example/catalog"]}), "HTTPS"),
        (lambda value: value.update({"initial_peers": ["not-a-multiaddr"]}), "multiaddress"),
    ],
)
def test_bootstrap_config_is_strict(mutation, message):
    bootstrap, _, _ = _release_documents()
    mutation(bootstrap)
    with pytest.raises(CatalogBootstrapError, match=message):
        CatalogBootstrapConfig.from_dict(bootstrap)
