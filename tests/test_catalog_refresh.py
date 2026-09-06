import json
from dataclasses import replace

import pytest
from test_catalog_bootstrap import NOW, _release_documents

from drift.model_catalog import CatalogSigningKey, SignedModelCatalog
from drift.node.catalog_bootstrap import CatalogBootstrapConfig, CatalogBootstrapError, CatalogBootstrapInstaller
from drift.node.catalog_refresh import load_configured_catalog
from drift.node.config import NodeConfig


def installation(tmp_path):
    bootstrap, envelope, manifests = _release_documents()
    key = CatalogSigningKey.generate()
    bootstrap["trust_root"]["keys"] = [key.trusted_key.to_dict()]
    released = [replace(envelope, signatures=()).add_signature(key)]

    def fetch(url, _maximum):
        return json.dumps(released[0].to_dict()) if url in bootstrap["catalog_mirrors"] else manifests[url]

    config_path = tmp_path / "node.json"
    installer = CatalogBootstrapInstaller(
        CatalogBootstrapConfig.from_dict(bootstrap),
        data_dir=tmp_path,
        config_path=config_path,
        fetch_text=fetch,
        now=NOW,
    )
    installer.install()
    return installer, config_path, released, key


def test_signed_refresh_preserves_user_policy_and_supports_existing_installations(tmp_path):
    installer, path, released, key = installation(tmp_path)
    original = json.loads(path.read_text())
    original["inference_mode"] = "local_only"
    original["max_loaded_models"] = 2
    original["contribution_policy"] = {"sharing_enabled": False}
    original["models"][0]["request_timeout"] = 47
    path.write_text(json.dumps(original))
    before = NodeConfig.load(path)
    released[0] = SignedModelCatalog(1, replace(released[0].signed, sequence=2), ()).add_signature(key)
    assert installer.refresh().created
    after = NodeConfig.load(path)
    assert after.inference_mode == "local_only"
    assert after.max_loaded_models == 2
    assert after.contribution_policy == before.contribution_policy
    assert after.workers == before.workers
    assert after.models[0].request_timeout == 47
    assert after.catalog_path != before.catalog_path
    assert before.catalog_path.is_file()
    assert not installer.refresh().created


def test_refresh_rejects_tamper_rollback_and_equivocation_without_changing_active_config(tmp_path):
    installer, path, released, key = installation(tmp_path)
    old = released[0]
    released[0] = SignedModelCatalog(1, replace(old.signed, sequence=2), ()).add_signature(key)
    installer.refresh()
    accepted = path.read_bytes()
    for rejected in (
        old,
        SignedModelCatalog(
            1, replace(old.signed, sequence=2, expires_at_ms=old.signed.expires_at_ms + 1000), ()
        ).add_signature(key),
        replace(released[0], signed=replace(released[0].signed, sequence=3)),
    ):
        released[0] = rejected
        with pytest.raises(CatalogBootstrapError):
            installer.refresh()
        assert path.read_bytes() == accepted


def test_expired_installed_catalog_can_be_renewed(tmp_path):
    installer, path, released, key = installation(tmp_path)
    installer.now = NOW + 4000
    released[0] = SignedModelCatalog(
        1,
        replace(
            released[0].signed,
            sequence=2,
            issued_at_ms=int((NOW + 3900) * 1000),
            expires_at_ms=int((NOW + 7600) * 1000),
        ),
        (),
    ).add_signature(key)
    assert installer.refresh().created
    assert NodeConfig.load(path).catalog_path.is_file()


def test_replacement_root_requires_application_authorization_and_preserves_rollback(tmp_path):
    installer, path, released, old_key = installation(tmp_path)
    old_config = NodeConfig.load(path)
    old_bootstrap = installer.bootstrap
    new_key = CatalogSigningKey.generate()
    new_root = replace(old_bootstrap.trust_root, keys=(new_key.trusted_key,))
    old_catalog = released[0].signed
    released[0] = SignedModelCatalog(1, replace(old_catalog, sequence=2), ()).add_signature(new_key)
    with pytest.raises(CatalogBootstrapError, match="No trusted"):
        installer.refresh()  # A network response alone cannot install a root.
    installer.bootstrap = replace(old_bootstrap, trust_root=new_root)
    with pytest.raises(CatalogBootstrapError, match="does not authorize"):
        installer.refresh()
    authorized = replace(installer.bootstrap, replaces_trust_roots=(old_bootstrap.trust_root_digest,))
    installer = CatalogBootstrapInstaller(
        authorized, data_dir=tmp_path, config_path=path, fetch_text=installer.fetch_text, now=NOW
    )
    assert installer.refresh().created
    assert load_configured_catalog(NodeConfig.load(path)).sequence == 2
    # The prior configuration remains self-verifying, including after a crash
    # between staging the new trust file and committing the new configuration.
    assert load_configured_catalog(old_config).sequence == 1
    assert old_config.catalog_bootstrap_path != NodeConfig.load(path).catalog_bootstrap_path
    accepted = path.read_bytes()
    released[0] = SignedModelCatalog(1, old_catalog, ()).add_signature(new_key)
    with pytest.raises(CatalogBootstrapError):
        installer.refresh()
    assert path.read_bytes() == accepted


def test_catalog_architecture_rejection_does_not_advance_rollback_or_activate(tmp_path, monkeypatch):
    import drift.node.loading as loading
    from drift.model_manifest import ManifestError

    installer, path, released, key = installation(tmp_path)
    accepted = path.read_bytes()
    guard = installer.rollback_path.read_bytes()
    released[0] = SignedModelCatalog(1, replace(released[0].signed, sequence=2), ()).add_signature(key)

    def reject(*_args):
        raise ManifestError("unsupported architecture")

    monkeypatch.setattr(loading, "validate_manifest_execution", reject)
    with pytest.raises(CatalogBootstrapError, match="unsupported architecture"):
        installer.refresh()
    assert installer.rollback_path.read_bytes() == guard
    assert path.read_bytes() == accepted
