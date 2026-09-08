"""Offline signed withdrawal and forward-restore drill for the real alpha catalog.

No publication key, HTTPS server, DHT, or model weights are used. A real packaged
consumer still needs to repeat this sequence in the monitored public canary.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from drift.model_catalog import CatalogSigningKey, SignedModelCatalog
from drift.node.catalog_bootstrap import CatalogBootstrapConfig, CatalogBootstrapError, CatalogBootstrapInstaller
from drift.node.catalog_refresh import load_configured_catalog
from drift.node.config import NodeConfig


def test_qwen_catalog_withdrawal_and_forward_restore_preserve_local_settings_and_cache(tmp_path):
    release = Path(__file__).resolve().parents[1] / "public-alpha" / "catalog-qwen-v2"
    original = SignedModelCatalog.from_json((release / "catalog.signed.json").read_text()).signed
    now = original.issued_at_ms / 1000 + 1
    local = next(model for model in original.models if model.execution == "local")
    remote = next(model for model in original.models if model.execution == "distributed")
    key = CatalogSigningKey.generate()
    bootstrap_source = json.loads((release / "catalog-bootstrap.json").read_text())
    bootstrap_source["trust_root"]["keys"] = [key.trusted_key.to_dict()]
    bootstrap_source["trust_root"]["threshold"] = 1
    bootstrap_source.pop("replaces_trust_roots", None)
    bootstrap = CatalogBootstrapConfig.from_dict(bootstrap_source)
    active = [SignedModelCatalog(1, original, ()).add_signature(key)]
    manifests = {
        url: (release / "manifests" / (model.manifest_digest.removeprefix("sha256:") + ".json")).read_text()
        for model in original.models
        for url in model.manifest_urls
    }

    def fetch(url, _maximum_bytes):
        return json.dumps(active[0].to_dict()) if url in bootstrap.catalog_mirrors else manifests[url]

    path = tmp_path / "node-config.json"
    installer = CatalogBootstrapInstaller(bootstrap, data_dir=tmp_path, config_path=path, fetch_text=fetch, now=now)
    installer.install()
    document = json.loads(path.read_text())
    cache = tmp_path / "custom-local-cache"
    cache.mkdir()
    (cache / "retained-sentinel").write_bytes(b"cache stays across withdrawal")
    local_config = next(item for item in document["models"] if item.get("execution") == "local")
    local_config.update(cache_dir=str(cache), local_device="cpu", request_timeout=17)
    document["contribution_policy"].update(sharing_enabled=False, max_vram="75%", max_processing_percent=50)
    path.write_text(json.dumps(document))
    retained_policy = NodeConfig.load(path).contribution_policy

    withdrawal = replace(
        original,
        sequence=original.sequence + 1,
        models=(local,),
        rungs=tuple(rung for rung in original.rungs if rung.rung_id == local.rung_id),
    )
    active[0] = SignedModelCatalog(1, withdrawal, ()).add_signature(key)
    assert installer.refresh().created
    withdrawn = NodeConfig.load(path)
    # Older exact models remain manual choices by design. Withdrawal removes
    # catalog approval and automatic routing, not operator-owned model history.
    assert len(withdrawn.models) == 2
    withdrawn_local = next(model for model in withdrawn.models if model.execution == "local")
    assert withdrawn.auto_model_priority == (local.manifest_digest,)
    assert remote.manifest_digest not in {model.manifest_digest for model in load_configured_catalog(withdrawn).models}
    assert withdrawn_local.cache_dir == cache
    assert withdrawn_local.local_device == "cpu"
    assert withdrawn_local.request_timeout == 17
    assert withdrawn.contribution_policy == retained_policy
    accepted_bytes = path.read_bytes()
    accepted_rollback = installer.rollback_path.read_bytes()

    # An operator rollback must be a newer signed release of known-good content,
    # never a lower sequence or removal of the persistent rollback guard.
    active[0] = SignedModelCatalog(1, original, ()).add_signature(key)
    with pytest.raises(CatalogBootstrapError):
        installer.refresh()
    assert path.read_bytes() == accepted_bytes
    assert installer.rollback_path.read_bytes() == accepted_rollback

    restored = replace(original, sequence=original.sequence + 2)
    active[0] = SignedModelCatalog(1, restored, ()).add_signature(key)
    assert installer.refresh().created
    resumed = NodeConfig.load(path)
    assert {model.manifest_digest for model in load_configured_catalog(resumed).models} == {
        local.manifest_digest,
        remote.manifest_digest,
    }
    resumed_local = next(model for model in resumed.models if model.execution == "local")
    assert resumed_local.cache_dir == cache
    assert resumed_local.local_device == "cpu"
    assert resumed_local.request_timeout == 17
    assert resumed.contribution_policy == retained_policy
    assert (cache / "retained-sentinel").read_bytes() == b"cache stays across withdrawal"
