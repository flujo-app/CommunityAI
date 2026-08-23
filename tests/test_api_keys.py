import json

import pytest

from drift.cli.run_node import _prepare_api_key_store, _prepare_control_key
from drift.node.keys import ApiKeyStore, ApiKeyStoreError, LastActiveKeyError


def test_labeled_api_keys_persist_as_hashes_and_revoke(tmp_path):
    path = tmp_path / "api-keys.json"
    store = ApiKeyStore(path)
    bootstrap = "drift_bootstrap-secret"
    bootstrap_metadata = store.ensure_key(bootstrap, label="bootstrap")
    created_metadata, created_secret = store.create(label="desktop client")

    assert store.verify(bootstrap)
    assert store.verify(created_secret)
    assert {item["label"] for item in store.list()} == {"bootstrap", "desktop client"}
    contents = path.read_text(encoding="utf-8")
    assert bootstrap not in contents
    assert created_secret not in contents
    assert "secret_hash" in contents

    reopened = ApiKeyStore(path)
    reopened.revoke(bootstrap_metadata["id"])
    renamed = reopened.update_label(bootstrap_metadata["id"], label="retired bootstrap")
    assert not reopened.verify(bootstrap)
    assert reopened.contains(bootstrap)
    assert reopened.verify(created_secret)
    assert renamed["label"] == "retired bootstrap"
    assert reopened.list()[0]["fingerprint"]
    with pytest.raises(LastActiveKeyError):
        reopened.revoke(created_metadata["id"])


def test_restart_does_not_reimport_a_revoked_bootstrap_key(tmp_path):
    store, key_path, _ = _prepare_api_key_store(tmp_path, [])
    bootstrap_secret = key_path.read_text(encoding="utf-8").strip()
    bootstrap = store.list()[0]
    replacement, replacement_secret = store.create(label="replacement")
    store.revoke(bootstrap["id"])

    reopened, reopened_key_path, created = _prepare_api_key_store(tmp_path, [])

    assert reopened_key_path is None
    assert created is False
    assert not reopened.verify(bootstrap_secret)
    assert reopened.verify(replacement_secret)
    assert next(item for item in reopened.list() if item["id"] == bootstrap["id"])["revoked_at"] is not None
    assert next(item for item in reopened.list() if item["id"] == replacement["id"])["revoked_at"] is None


def test_control_key_is_stable_separate_and_supports_an_explicit_path(tmp_path):
    api_store, api_path, _ = _prepare_api_key_store(tmp_path, [])
    control_key, control_path, created = _prepare_control_key(tmp_path, None)
    reopened, reopened_path, created_again = _prepare_control_key(tmp_path, None)

    assert created is True
    assert created_again is False
    assert control_path == reopened_path == tmp_path / "control-api.key"
    assert control_key == reopened
    assert control_key.startswith("drift_control_")
    assert not api_store.verify(control_key)
    assert api_path.read_text(encoding="utf-8").strip() != control_key

    custom_path = tmp_path / "native-bridge" / "control.key"
    custom, selected_path, custom_created = _prepare_control_key(tmp_path, custom_path)
    assert custom_created is True
    assert selected_path == custom_path
    assert custom_path.read_text(encoding="utf-8").strip() == custom


def test_control_key_can_come_from_the_native_store_without_a_file(monkeypatch, tmp_path):
    native_key = "drift_control_" + "K" * 43
    monkeypatch.setattr("drift.cli.run_node.load_native_control_key", lambda location: native_key)

    loaded, selected_path, created = _prepare_control_key(
        tmp_path,
        None,
        source="native",
        credential_service="service",
        credential_account="account",
    )

    assert loaded == native_key
    assert selected_path is None
    assert created is False
    assert not (tmp_path / "control-api.key").exists()


def test_key_store_rejects_duplicate_json_keys(tmp_path):
    path = tmp_path / "api-keys.json"
    path.write_text('{"schema_version":1,"schema_version":1,"keys":[]}', encoding="utf-8")

    with pytest.raises(ApiKeyStoreError, match="duplicate object key"):
        ApiKeyStore(path)


def test_key_store_rejects_unknown_fields(tmp_path):
    path = tmp_path / "api-keys.json"
    path.write_text(json.dumps({"schema_version": 1, "keys": [], "secret": "no"}), encoding="utf-8")

    with pytest.raises(ApiKeyStoreError, match="exactly"):
        ApiKeyStore(path)


def test_failed_mutation_does_not_change_in_memory_key_state(monkeypatch, tmp_path):
    store = ApiKeyStore(tmp_path / "api-keys.json")
    first = store.ensure_key("first-secret", label="first")
    store.ensure_key("second-secret", label="second")
    before = store.list()
    monkeypatch.setattr(store, "_write_locked", lambda: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        store.update_label(first["id"], label="changed")
    assert store.list() == before

    with pytest.raises(OSError, match="disk full"):
        store.revoke(first["id"])
    assert store.list() == before

    with pytest.raises(OSError, match="disk full"):
        store.create(label="third")
    assert store.list() == before


@pytest.mark.parametrize("schema_version", [True, "1", 2])
def test_key_store_rejects_non_exact_schema_versions(tmp_path, schema_version):
    path = tmp_path / "api-keys.json"
    path.write_text(json.dumps({"schema_version": schema_version, "keys": []}), encoding="utf-8")

    with pytest.raises(ApiKeyStoreError, match="schema_version"):
        ApiKeyStore(path)
