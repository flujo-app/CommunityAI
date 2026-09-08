import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from test_catalog_bootstrap import NOW, _release_documents

from drift.model_catalog import CatalogSigningKey, SignedModelCatalog
from drift.model_manifest import ModelManifest
from drift.node.catalog_bootstrap import CatalogBootstrapConfig, CatalogBootstrapError, CatalogBootstrapInstaller
from drift.node.catalog_refresh import CatalogRefreshService, load_configured_catalog
from drift.node.config import NodeConfig
from drift.node.model_manager import ModelDescriptor, ModelManager, ModelManagerClosedError, ModelRuntime


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


@pytest.mark.parametrize("same_identity", [True, False])
def test_catalog_migration_preserves_preferences_by_exact_manifest_identity(tmp_path, same_identity):
    installer, path, released, key = installation(tmp_path)
    original = json.loads(path.read_text())
    manifest = ModelManifest.load(original["models"][0]["manifest"])
    if not same_identity:
        source = manifest.to_dict()
        source["source"]["revision"] = "f" * 40
        manifest = ModelManifest.from_dict(source)
    old_path = tmp_path / "custom-manifest.json"
    old_path.write_text(manifest.canonical_json())
    cache = tmp_path / "retained-cache"
    cache.mkdir()
    (cache / "sentinel").write_bytes(b"retained")
    original["models"][0].update(manifest=str(old_path), cache_dir=str(cache), request_timeout=47)
    original["auto_model_priority"][0] = manifest.digest_id
    original.pop("catalog_path")
    original.pop("catalog_bootstrap_path")
    path.write_text(json.dumps(original))

    assert installer.refresh().created
    migrated = NodeConfig.load(path)
    assert migrated.models[0].manifest_path != old_path
    assert (migrated.models[0].cache_dir == cache) is same_identity
    assert (migrated.models[0].request_timeout == 47) is same_identity
    assert (cache / "sentinel").read_bytes() == b"retained"
    old_path.unlink()
    assert (
        ModelManifest.load(migrated.models[0].manifest_path).digest_id == released[0].signed.models[0].manifest_digest
    )


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


def periodic_service(installer, path, manager, restart):
    """Use production refresh/admission code; substitute only transport and time."""
    config = replace(NodeConfig.load(path), catalog_refresh_seconds=0.01)
    service = CatalogRefreshService(config, path, installer.data_dir, manager, restart)
    service.installer.fetch_text = installer.fetch_text
    service.installer.now = installer.now
    attempted, staged, busy = threading.Event(), threading.Event(), threading.Event()
    real_refresh = service.installer.refresh
    real_begin_restart = manager.begin_idle_restart

    def observe_refresh():
        try:
            result = real_refresh()
            if result.created:
                staged.set()
            return result
        finally:
            attempted.set()

    def observe_admission():
        claimed = real_begin_restart()
        if not claimed:
            busy.set()
        return claimed

    service.installer.refresh = observe_refresh
    manager.begin_idle_restart = observe_admission
    return service, attempted, staged, busy


def test_periodic_signed_withdrawal_waits_for_last_lease_before_claiming_restart(tmp_path):
    installer, path, released, key = installation(tmp_path)
    old_config = NodeConfig.load(path)
    old_catalog = load_configured_catalog(old_config)
    withdrawn = old_catalog.models[1].manifest_digest
    closed, callbacks = [], []
    manager = ModelManager()
    manager.set_catalog_models(model.manifest_digest for model in old_catalog.models)
    manager.register_manifest(
        ModelManifest.load(old_config.models[1].manifest_path),
        lambda: ModelRuntime(object(), object(), lambda: closed.append(True)),
    )
    first, last = manager.load(withdrawn), manager.load(withdrawn)
    restarted = threading.Event()

    def restart():
        # This callback is the server-restart boundary, after atomic admission
        # closure. The old runtime must not admit a request in this gap.
        try:
            manager.load(withdrawn)
        except ModelManagerClosedError:
            callbacks.append("admission-closed")
        else:
            callbacks.append("admission-open")
        restarted.set()

    released[0] = SignedModelCatalog(
        1, replace(old_catalog, sequence=2, models=(old_catalog.models[0],)), ()
    ).add_signature(key)
    service, _, staged, busy = periodic_service(installer, path, manager, restart)
    service.start()
    try:
        assert staged.wait(3) and busy.wait(3), "The signed update was not staged while leases were active"
        assert load_configured_catalog(NodeConfig.load(path)).sequence == 2
        assert load_configured_catalog(old_config).sequence == 1
        assert manager.catalog_allows_contribution(withdrawn)
        assert first.runtime is last.runtime and first.runtime.model is not None
        assert not restarted.is_set() and not closed
        first.release()
        busy.clear()
        assert busy.wait(3), "Restart did not observe the remaining lease"
        assert manager.snapshots()[0].active_requests == 1
        assert not restarted.is_set() and not closed
        last.release()
        assert restarted.wait(3), "Restart did not follow the last lease release"
        assert callbacks == ["admission-closed"]
        updated = load_configured_catalog(NodeConfig.load(path))
        next_manager = ModelManager()
        next_manager.set_catalog_models(model.manifest_digest for model in updated.models)
        assert not next_manager.catalog_allows_contribution(withdrawn)
        next_manager.shutdown()
    finally:
        first.release()
        last.release()
        service.close()
        manager.shutdown()
    assert not service._thread.is_alive()
    assert closed == [True]


def test_periodic_refresh_waits_for_loading_then_its_returned_lease(tmp_path):
    installer, path, released, key = installation(tmp_path)
    manager = ModelManager()
    loader_entered, allow_loader, restarted = threading.Event(), threading.Event(), threading.Event()
    runtime = ModelRuntime(object(), object())

    def loader():
        loader_entered.set()
        assert allow_loader.wait(5), "Test did not release its controlled loader"
        return runtime

    manager.register(ModelDescriptor("loading"), loader)
    released[0] = SignedModelCatalog(1, replace(released[0].signed, sequence=2), ()).add_signature(key)
    service, _, staged, busy = periodic_service(installer, path, manager, restarted.set)
    lease = None
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending_load = executor.submit(manager.load, "loading")
        try:
            assert loader_entered.wait(3)
            service.start()
            assert staged.wait(3) and busy.wait(3)
            assert not restarted.is_set()
            allow_loader.set()
            lease = pending_load.result(timeout=3)
            assert lease.runtime is runtime
            busy.clear()
            assert busy.wait(3), "The completed loader's lease did not hold the restart"
            assert not restarted.is_set()
            lease.release()
            assert restarted.wait(3)
            with pytest.raises(ModelManagerClosedError):
                manager.load("loading")
        finally:
            allow_loader.set()
            if lease is None:
                lease = pending_load.result(timeout=3)
            lease.release()
            service.close()
            manager.shutdown()
    assert not service._thread.is_alive()


@pytest.mark.parametrize("rejection", ["tamper", "rollback", "equivocation"])
def test_periodic_rejection_keeps_catalog_guard_and_request_admission(tmp_path, rejection):
    installer, path, released, key = installation(tmp_path)
    previous = released[0]
    current = SignedModelCatalog(1, replace(previous.signed, sequence=2), ()).add_signature(key)
    released[0] = current
    installer.refresh()
    before, guard = path.read_bytes(), installer.rollback_path.read_bytes()
    if rejection == "tamper":
        released[0] = replace(current, signed=replace(current.signed, sequence=3))
    elif rejection == "rollback":
        released[0] = previous
    else:
        released[0] = SignedModelCatalog(
            1, replace(current.signed, expires_at_ms=current.signed.expires_at_ms + 1000), ()
        ).add_signature(key)
    manager = ModelManager()
    manager.register(ModelDescriptor("retained"), lambda: ModelRuntime(object(), object()))
    restarted = threading.Event()
    service, attempted, staged, _ = periodic_service(installer, path, manager, restarted.set)
    service.start()
    try:
        assert attempted.wait(3), "The periodic refresh did not run"
        service.close()
        assert path.read_bytes() == before and installer.rollback_path.read_bytes() == guard
        assert not staged.is_set() and not restarted.is_set()
        with manager.load("retained") as lease:
            assert lease.runtime.model is not None
    finally:
        service.close()
        manager.shutdown()
    assert not service._thread.is_alive()


def test_closing_periodic_service_during_drain_wait_preserves_active_lease(tmp_path):
    installer, path, released, key = installation(tmp_path)
    manager = ModelManager()
    manager.register(ModelDescriptor("active"), lambda: ModelRuntime(object(), object()))
    lease = manager.load("active")
    restarted = threading.Event()
    released[0] = SignedModelCatalog(1, replace(released[0].signed, sequence=2), ()).add_signature(key)
    service, _, staged, busy = periodic_service(installer, path, manager, restarted.set)
    service.start()
    try:
        assert staged.wait(3) and busy.wait(3)
        service.close()
        assert not service._thread.is_alive() and not restarted.is_set()
        assert lease.runtime.model is not None
        with manager.load("active") as another:
            assert another.runtime is lease.runtime
        # The accepted immutable update remains available for the next node
        # start, without erasing the configuration or rollback guard.
        assert load_configured_catalog(NodeConfig.load(path)).sequence == 2
    finally:
        lease.release()
        service.close()
        manager.shutdown()
