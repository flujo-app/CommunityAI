"""Explicit shared host RAM consent through config and the real local policy API."""

import json

import pytest
from fastapi.testclient import TestClient
from test_policy_store import _config_document, _store

from drift.node.config import ContributionPolicyConfig, NodeConfig, NodeConfigError
from drift.node.model_manager import ModelManager
from drift.node.server import create_node_app

URL = "/control/v1/contribution-policy"
CONTROL = {"Authorization": "Bearer host-memory-test-control"}


@pytest.mark.parametrize("source", [{}, {"max_host_memory": None}])
def test_legacy_policy_has_no_invented_allowance_or_new_serialized_key(source):
    policy = ContributionPolicyConfig.from_dict({"sharing_enabled": False, **source})
    assert policy.max_host_memory is None
    assert policy.max_host_memory_bytes is None
    assert "max_host_memory" not in policy.to_dict()
    assert "max_host_memory_bytes" not in policy.to_dict()


@pytest.mark.parametrize("text,expected", [("16GiB", 16 * 1024**3), ("1.5 GiB", 3 * 1024**3 // 2), ("1 byte", 1)])
def test_explicit_allowance_round_trips_and_is_shared_not_per_worker(tmp_path, text, expected):
    document = _config_document()
    document["contribution_policy"]["max_host_memory"] = text
    config = NodeConfig.from_dict(document, base_dir=tmp_path)
    assert config.contribution_policy.max_host_memory == text
    assert config.contribution_policy.max_host_memory_bytes == expected
    wire = dict(config.contribution_policy.to_dict())
    assert wire["max_host_memory"] == text
    assert "max_host_memory_bytes" not in wire
    assert ContributionPolicyConfig.from_dict(wire) == config.contribution_policy
    assert all(not hasattr(worker, "max_host_memory") for worker in config.workers)


@pytest.mark.parametrize("value", [True, 16, 1.5, "", " ", "0", "0GiB", "-1GiB", "50%", "NaN", "inf", "unknown"])
def test_host_memory_requires_positive_byte_size_text(value):
    with pytest.raises(NodeConfigError, match="max_host_memory"):
        ContributionPolicyConfig.from_dict({"sharing_enabled": False, "max_host_memory": value})


def test_host_memory_cannot_be_set_as_a_worker_budget(tmp_path):
    document = _config_document()
    document["workers"][0]["max_host_memory"] = "16GiB"
    with pytest.raises(NodeConfigError, match="unknown field.*max_host_memory"):
        NodeConfig.from_dict(document, base_dir=tmp_path)


@pytest.fixture
def policy_api(tmp_path):
    path, supervisor, store = _store(tmp_path)
    manager = ModelManager()
    app = create_node_app(
        manager,
        api_keys=["host-memory-test-client"],
        control_keys=["host-memory-test-control"],
        worker_supervisor=supervisor,
        contribution_policy_store=store,
    )
    try:
        with TestClient(app) as client:
            yield client, path, supervisor, store
    finally:
        supervisor.shutdown()
        manager.shutdown()


def save(client, snapshot, value):
    return client.put(
        URL,
        headers=CONTROL,
        json={
            "schema_version": 1,
            "expected_config_revision": snapshot["config_revision"],
            "policy": {**snapshot["policy"], "max_host_memory": value},
        },
    )


def test_api_persists_explicit_allowance_and_clear_restores_legacy_shape(policy_api):
    client, path, supervisor, store = policy_api
    original = json.loads(path.read_text(encoding="utf-8"))
    initial = client.get(URL, headers=CONTROL).json()
    assert "max_host_memory" not in initial["policy"]

    response = save(client, initial, "24GiB")
    assert response.status_code == 200
    saved = response.json()
    assert saved["policy"]["max_host_memory"] == "24GiB"
    assert saved["config_revision"] != initial["config_revision"]
    assert client.get(URL, headers=CONTROL).json() == saved == store.snapshot()
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["contribution_policy"] == saved["policy"]
    assert persisted["workers"] == original["workers"]
    assert NodeConfig.load(path).contribution_policy.max_host_memory_bytes == 24 * 1024**3
    assert all(worker["state"] != "running" for worker in supervisor.snapshots())

    response = save(client, saved, None)
    assert response.status_code == 200
    cleared = response.json()
    assert "max_host_memory" not in cleared["policy"]
    assert "max_host_memory_bytes" not in cleared["policy"]
    assert "max_host_memory" not in json.loads(path.read_text(encoding="utf-8"))["contribution_policy"]
    assert NodeConfig.load(path).contribution_policy.max_host_memory_bytes is None


def test_stale_host_allowance_save_cannot_overwrite_new_consent(policy_api):
    client, path, supervisor, store = policy_api
    initial = client.get(URL, headers=CONTROL).json()
    response = save(client, initial, "12GiB")
    assert response.status_code == 200
    before = path.read_bytes(), supervisor.launches, store.snapshot()
    stale = save(client, initial, "120GiB")
    assert stale.status_code == 412
    assert (path.read_bytes(), supervisor.launches, store.snapshot()) == before


@pytest.mark.parametrize("value", ["0GiB", "50%", True])
def test_invalid_api_allowance_has_no_persistence_or_runtime_side_effects(policy_api, value):
    client, path, supervisor, store = policy_api
    initial = store.snapshot()
    before = path.read_bytes(), supervisor.launches, initial
    response = save(client, initial, value)
    assert response.status_code == 422
    assert "max_host_memory" in response.json()["detail"]
    assert (path.read_bytes(), supervisor.launches, store.snapshot()) == before
