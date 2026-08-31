from types import SimpleNamespace

import pytest
import torch

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from drift.cli.run_node import _validate_args, build_parser
from drift.model_manifest import ModelManifest
from drift.node.keys import ApiKeyStore, load_or_create_api_key, load_or_create_control_key
from drift.node.loading import make_manifest_loader
from drift.node.model_manager import ModelDescriptor, ModelManager, ModelRuntime, ModelState
from drift.node.server import create_node_app
from drift.node.worker_supervisor import WorkerPolicyError


class FakeTokenizer:
    def apply_chat_template(self, messages, **kwargs):
        return {"input_ids": torch.tensor([[1, 2]])}

    def __call__(self, text, **kwargs):
        return SimpleNamespace(input_ids=torch.tensor([[1, 2]]))

    def decode(self, ids, **kwargs):
        return "done"


class FakeModel:
    def generate(self, input_ids, **kwargs):
        return torch.cat([input_ids, torch.tensor([[3]])], dim=1)


def test_local_api_key_is_stable_and_not_overwritten(tmp_path):
    key_path = tmp_path / "secrets" / "local-api.key"
    first, created = load_or_create_api_key(key_path)
    second, created_again = load_or_create_api_key(key_path)

    assert created is True
    assert created_again is False
    assert first == second
    assert first.startswith("drift_")
    assert key_path.read_text(encoding="utf-8").strip() == first


def test_local_api_key_rejects_non_file_path(tmp_path):
    key_path = tmp_path / "local-api.key"
    key_path.mkdir()

    with pytest.raises(ValueError, match="not a regular file"):
        load_or_create_api_key(key_path)


def test_local_control_key_is_stable_and_uses_a_distinct_key_class(tmp_path):
    key_path = tmp_path / "secrets" / "control-api.key"
    first, created = load_or_create_control_key(key_path)
    second, created_again = load_or_create_control_key(key_path)

    assert created is True
    assert created_again is False
    assert first == second
    assert first.startswith("drift_control_")
    assert key_path.read_text(encoding="utf-8").strip() == first

    invalid_path = tmp_path / "secrets" / "invalid-control.key"
    invalid_path.write_text("drift_client-key", encoding="utf-8")
    with pytest.raises(ValueError, match="drift_control_ key class"):
        load_or_create_control_key(invalid_path)


def test_node_status_requires_auth_and_reports_lazy_model():
    manager = ModelManager(max_loaded_models=1)
    loads = []
    manager.register(
        ModelDescriptor(
            "Tiny Test",
            aliases=("tiny",),
            manifest_digest="sha256:" + "a" * 64,
            repository="org/tiny",
            selected_whole_shard_bytes=1_234_567,
        ),
        lambda: loads.append(True) or ModelRuntime(FakeModel(), FakeTokenizer()),
    )
    app = create_node_app(
        manager,
        api_keys=["client-secret"],
        control_keys=["control-secret"],
        host="127.0.0.1",
        port=8080,
    )

    with TestClient(app) as client:
        assert client.get("/control/v1/status").status_code == 401
        client_headers = {"Authorization": "Bearer client-secret"}
        control_headers = {"Authorization": "Bearer control-secret"}
        assert client.get("/control/v1/status", headers=client_headers).status_code == 401
        assert client.get("/v1/models", headers=control_headers).status_code == 401
        status = client.get("/control/v1/status", headers=control_headers)
        assert status.status_code == 200
        body = status.json()
        assert body["api_version"] == 1
        assert body["openai_base_url"] == "http://127.0.0.1:8080/v1"
        assert body["runtime_budget"] == {"max_loaded_models": 1, "resident_models": 0}
        assert body["models"][0]["state"] == "known"
        assert body["models"][0]["aliases"] == ["tiny"]
        assert body["models"][0]["download"] == {
            "schema_version": 1,
            "selected_whole_shard_bytes": 1_234_567,
        }
        assert body["contribution"] == {
            "schema_version": 3,
            "configured": False,
            "editable": False,
            "policy": {
                "schema_version": 1,
                "config_revision": None,
                "policy": {
                    "sharing_enabled": False,
                    "allowed_models": [],
                    "preferred_models": [],
                    "denied_models": [],
                    "max_disk_space": None,
                    "max_vram": None,
                    "max_bandwidth_mbps": None,
                    "max_power_watts": None,
                    "pause_timeout": 10.0,
                    "schedule": None,
                },
            },
            "workers": [],
        }
        assert loads == []

        response = client.post(
            "/v1/completions",
            headers=client_headers,
            json={"model": "tiny", "prompt": "hello", "temperature": 0},
        )
        assert response.status_code == 200
        assert response.json()["model"] == "Tiny Test"
        assert loads == [True]
        assert manager.snapshots()[0].state is ModelState.READY
        assert manager.snapshots()[0].active_requests == 0

        unloaded = client.post("/control/v1/models/unload", headers=control_headers, json={"model": "tiny"})
        assert unloaded.status_code == 200
        assert unloaded.json() == {"model": "Tiny Test", "unloaded": True}
        assert manager.snapshots()[0].state is ModelState.KNOWN

    assert manager.closed


def test_contribution_policy_endpoint_requires_control_auth_and_strict_versioned_json():
    revision = "sha256:" + "a" * 64
    policy = {
        "sharing_enabled": False,
        "allowed_models": [],
        "preferred_models": [],
        "denied_models": [],
        "max_disk_space": "20GiB",
        "max_vram": None,
        "max_bandwidth_mbps": None,
        "max_power_watts": None,
        "pause_timeout": 10.0,
        "schedule": None,
    }

    class FakeSupervisor:
        def snapshots(self):
            return ()

        def shutdown(self):
            pass

    class FakePolicyStore:
        def __init__(self):
            self.calls = []

        def snapshot(self):
            return {"schema_version": 1, "config_revision": revision, "policy": policy}

        def update(self, source, *, expected_revision):
            self.calls.append((source, expected_revision))
            return self.snapshot()

    manager = ModelManager()
    manager.register(ModelDescriptor("model"), lambda: ModelRuntime(FakeModel(), FakeTokenizer()))
    store = FakePolicyStore()
    app = create_node_app(
        manager,
        api_keys=["client-secret"],
        control_keys=["control-secret"],
        worker_supervisor=FakeSupervisor(),
        contribution_policy_store=store,
    )
    control = {"Authorization": "Bearer control-secret"}
    json_control = {**control, "Content-Type": "application/json"}
    client_key = {"Authorization": "Bearer client-secret"}
    request = {
        "schema_version": 1,
        "expected_config_revision": revision,
        "policy": policy,
    }

    with TestClient(app) as client:
        assert client.get("/control/v1/contribution-policy").status_code == 401
        assert client.get("/control/v1/contribution-policy", headers=client_key).status_code == 401
        assert client.get("/control/v1/contribution-policy", headers=control).json()["policy"] == policy
        response = client.put("/control/v1/contribution-policy", headers=control, json=request)
        assert response.status_code == 200
        assert store.calls == [(policy, revision)]

        assert client.put("/control/v1/contribution-policy", headers=control, content="{}").status_code == 415
        duplicate = '{"schema_version":1,"schema_version":1,"expected_config_revision":"' + revision + '","policy":{}}'
        assert client.put("/control/v1/contribution-policy", headers=json_control, content=duplicate).status_code == 422
        non_finite = duplicate.replace('"schema_version":1,"schema_version":1', '"schema_version":NaN')
        assert (
            client.put("/control/v1/contribution-policy", headers=json_control, content=non_finite).status_code == 422
        )
        assert (
            client.put(
                "/control/v1/contribution-policy", headers=json_control, content=b"x" * (256 * 1024 + 1)
            ).status_code
            == 413
        )


def test_node_refuses_to_unload_a_model_with_an_active_lease():
    manager = ModelManager(max_loaded_models=1)
    manager.register(ModelDescriptor("model"), lambda: ModelRuntime(FakeModel(), FakeTokenizer()))
    app = create_node_app(manager, api_keys=["client-secret"], control_keys=["control-secret"])

    with TestClient(app) as client:
        lease = manager.load("model")
        response = client.post(
            "/control/v1/models/unload",
            headers={"Authorization": "Bearer control-secret"},
            json={"model": "model"},
        )
        assert response.status_code == 409
        lease.release()


def test_authenticated_worker_controls_are_routed_through_supervisor():
    class FakeSupervisor:
        def __init__(self):
            self.state = "paused"
            self.calls = []
            self.closed = False

        def snapshots(self):
            return (
                {
                    "id": "worker",
                    "model": "model",
                    "state": self.state,
                    "desired_running": self.state == "running",
                    "policy_admitted": True,
                    "policy_reason": None,
                    "preferred": True,
                    "schedule_admitted": True,
                    "schedule_reason": None,
                    "schedule_suspended": False,
                    "resource_admitted": True,
                    "resource_reason": None,
                    "resource_suspended": False,
                    "max_disk_bytes": 100 * 1024**3,
                    "max_vram_bytes": 4 * 1024**3,
                    "vram_pool_bytes": 8 * 1024**3,
                    "max_bandwidth_mbps": 100.0,
                    "current_bandwidth_mbps": 12.5,
                    "max_power_watts": 250.0,
                    "current_power_watts": 125.0,
                    "pid": 1234,
                    "last_error": "private failure detail",
                    "recent_logs": ["private worker log"],
                },
            )

        def snapshot(self, worker_id):
            assert worker_id == "worker"
            return self.snapshots()[0]

        def start_worker(self, worker_id):
            self.calls.append(("start", worker_id))
            self.state = "running"
            return True

        def pause_worker(self, worker_id):
            self.calls.append(("pause", worker_id))
            self.state = "paused"
            return True

        def restart_worker(self, worker_id):
            self.calls.append(("restart", worker_id))
            self.state = "running"
            return True

        def shutdown(self):
            self.closed = True

    manager = ModelManager()
    manager.register(ModelDescriptor("model"), lambda: ModelRuntime(FakeModel(), FakeTokenizer()))
    supervisor = FakeSupervisor()
    app = create_node_app(
        manager,
        api_keys=["client-secret"],
        control_keys=["control-secret"],
        worker_supervisor=supervisor,
    )
    headers = {"Authorization": "Bearer control-secret"}

    with TestClient(app) as client:
        assert client.get("/control/v1/workers").status_code == 401
        assert client.get("/control/v1/workers", headers={"Authorization": "Bearer client-secret"}).status_code == 401
        assert client.get("/control/v1/workers", headers=headers).json()["workers"][0]["state"] == "paused"
        status = client.get("/control/v1/status", headers=headers).json()
        assert status["workers"] == [{"id": "worker", "model": "model", "state": "paused", "desired_running": False}]
        status_worker = status["contribution"]["workers"][0]
        assert status_worker["placement"] == {
            "automatic": False,
            "block_indices": None,
            "reason": None,
        }
        assert status_worker["policy"] == {"admitted": True, "reason": None, "preferred": True}
        assert status_worker["resources"]["limits"]["vram_bytes"] == 4 * 1024**3
        assert status_worker["resources"]["measurements"]["power_watts"] == 125.0
        assert "pid" not in status_worker
        assert "recent_logs" not in status_worker
        assert "last_error" not in status_worker
        for action in ("start", "pause", "restart"):
            response = client.post(f"/control/v1/workers/worker/{action}", headers=headers)
            assert response.status_code == 200
            assert response.json()["changed"] is True

    assert supervisor.calls == [("start", "worker"), ("pause", "worker"), ("restart", "worker")]
    assert supervisor.closed


def test_worker_policy_rejection_is_reported_as_a_control_conflict():
    class PolicyBlockedSupervisor:
        def snapshots(self):
            return ({"id": "worker", "policy_admitted": False},)

        def start_worker(self, worker_id):
            raise WorkerPolicyError("sharing is disabled by contribution policy")

        def pause_worker(self, worker_id):
            raise AssertionError("pause was not requested")

        def restart_worker(self, worker_id):
            raise AssertionError("restart was not requested")

        def shutdown(self):
            pass

    manager = ModelManager()
    manager.register(ModelDescriptor("model"), lambda: ModelRuntime(FakeModel(), FakeTokenizer()))
    app = create_node_app(
        manager,
        api_keys=["client-secret"],
        control_keys=["control-secret"],
        worker_supervisor=PolicyBlockedSupervisor(),
    )

    with TestClient(app) as client:
        response = client.post(
            "/control/v1/workers/worker/start",
            headers={"Authorization": "Bearer control-secret"},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "sharing is disabled by contribution policy"


def test_persistent_key_crud_updates_authentication_immediately(tmp_path):
    store = ApiKeyStore(tmp_path / "api-keys.json")
    first_secret = "drift_first-secret"
    first = store.ensure_key(first_secret, label="bootstrap")
    manager = ModelManager()
    manager.register(ModelDescriptor("model"), lambda: ModelRuntime(FakeModel(), FakeTokenizer()))
    app = create_node_app(manager, api_key_store=store, control_keys=["drift_control-secret"])
    first_headers = {"Authorization": f"Bearer {first_secret}"}
    control_headers = {"Authorization": "Bearer drift_control-secret"}

    with TestClient(app) as client:
        assert client.get("/control/v1/keys", headers=first_headers).status_code == 401
        assert client.get("/v1/models", headers=control_headers).status_code == 401
        listed = client.get("/control/v1/keys", headers=control_headers)
        assert listed.status_code == 200
        assert listed.json()["keys"] == [first]
        assert "secret_hash" not in listed.text

        created = client.post("/control/v1/keys", headers=control_headers, json={"label": "second client"})
        assert created.status_code == 201
        second_secret = created.json()["secret"]
        second = created.json()["key"]
        second_headers = {"Authorization": f"Bearer {second_secret}"}
        assert client.get("/v1/models", headers=second_headers).status_code == 200
        assert client.get("/control/v1/status", headers=second_headers).status_code == 401
        renamed = client.patch(
            f"/control/v1/keys/{second['id']}", headers=control_headers, json={"label": "renamed client"}
        )
        assert renamed.status_code == 200
        assert renamed.json()["key"]["label"] == "renamed client"

        revoked = client.delete(f"/control/v1/keys/{first['id']}", headers=control_headers)
        assert revoked.status_code == 200
        assert revoked.json()["key"]["revoked_at"] is not None
        assert client.get("/v1/models", headers=first_headers).status_code == 401
        assert client.delete(f"/control/v1/keys/{second['id']}", headers=control_headers).status_code == 409


def test_node_requires_control_credentials_distinct_from_openai_keys(tmp_path):
    manager = ModelManager()
    manager.register(ModelDescriptor("model"), lambda: ModelRuntime(FakeModel(), FakeTokenizer()))

    with pytest.raises(ValueError, match="requires at least one non-empty control key"):
        create_node_app(manager, api_keys=["client-secret"])
    with pytest.raises(ValueError, match="distinct from OpenAI API keys"):
        create_node_app(manager, api_keys=["same-secret"], control_keys=["same-secret"])
    with pytest.raises(ValueError, match="must not contain duplicates"):
        create_node_app(manager, api_keys=["client-secret"], control_keys=["control-secret", "control-secret"])

    store = ApiKeyStore(tmp_path / "api-keys.json")
    store.ensure_key("stored-secret", label="client")
    with pytest.raises(ValueError, match="distinct from OpenAI API keys"):
        create_node_app(manager, api_key_store=store, control_keys=["stored-secret"])

    store.ensure_key("second-secret", label="second client")
    stored_id = next(item["id"] for item in store.list() if item["label"] == "client")
    store.revoke(stored_id)
    with pytest.raises(ValueError, match="distinct from OpenAI API keys"):
        create_node_app(manager, api_key_store=store, control_keys=["stored-secret"])


def test_node_parser_requires_explicit_network_opt_in():
    parser = build_parser()
    args = parser.parse_args(["manifest.json", "--initial_peers", "/ip4/127.0.0.1/tcp/1/p2p/fake", "--host", "0.0.0.0"])
    with pytest.raises(SystemExit):
        _validate_args(parser, args)

    args.allow_network = True
    _validate_args(parser, args)


def test_node_parser_accepts_config_mode_and_rejects_ambiguous_model_options():
    parser = build_parser()
    config_args = parser.parse_args(["--config", "node.json", "--control_key_path", "private/control.key"])
    _validate_args(parser, config_args)
    assert config_args.control_key_path.as_posix() == "private/control.key"

    native_args = parser.parse_args(["--config", "node.json", "--control_key_source", "native"])
    _validate_args(parser, native_args)
    conflicting_native_args = parser.parse_args(
        [
            "--config",
            "node.json",
            "--control_key_source",
            "native",
            "--control_key_path",
            "private/control.key",
        ]
    )
    with pytest.raises(SystemExit):
        _validate_args(parser, conflicting_native_args)

    both = parser.parse_args(
        [
            "manifest.json",
            "--config",
            "node.json",
            "--initial_peers",
            "/ip4/127.0.0.1/tcp/1/p2p/fake",
        ]
    )
    with pytest.raises(SystemExit):
        _validate_args(parser, both)

    scoped_override = parser.parse_args(["--config", "node.json", "--initial_peers", "/ip4/127.0.0.1/tcp/1/p2p/fake"])
    with pytest.raises(SystemExit):
        _validate_args(parser, scoped_override)

    non_finite_timeout = parser.parse_args(
        [
            "manifest.json",
            "--initial_peers",
            "/ip4/127.0.0.1/tcp/1/p2p/fake",
            "--request_timeout",
            "NaN",
        ]
    )
    with pytest.raises(SystemExit):
        _validate_args(parser, non_finite_timeout)


def test_manifest_loader_pins_identity_and_closes_client_dht(monkeypatch, tmp_path):
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    calls = SimpleNamespace(metadata=[], tokenizer=None, model=None, shutdown=0, dht_shutdown=0)

    class FakeVerifier:
        def __init__(self, checked_manifest, **kwargs):
            assert checked_manifest is manifest
            assert kwargs["repository"] == manifest.source.repository
            assert kwargs["revision"] == manifest.source.revision
            self.snapshot_root = tmp_path

        def ensure_startup_metadata(self, **kwargs):
            calls.metadata.append(kwargs)

    class FakeAutoTokenizer:
        @classmethod
        def from_pretrained(cls, source, **kwargs):
            calls.tokenizer = (source, kwargs)
            return FakeTokenizer()

    class FakeDHT:
        alive = True

        def is_alive(self):
            return self.alive

        def shutdown(self):
            calls.dht_shutdown += 1
            self.alive = False

    class FakeUpdateThread:
        alive = True

        def is_alive(self):
            return self.alive

    class FakeSequenceManager:
        dht = FakeDHT()
        _thread = FakeUpdateThread()

        def shutdown(self):
            calls.shutdown += 1
            self._thread.alive = False

    fake_model = FakeModel()
    fake_model.transformer = SimpleNamespace(h=SimpleNamespace(sequence_manager=FakeSequenceManager()))

    class FakeAutoModel:
        @classmethod
        def from_pretrained(cls, repository, **kwargs):
            calls.model = (repository, kwargs)
            return fake_model

    monkeypatch.setattr("drift.node.loading.ManifestArtifactVerifier", FakeVerifier)
    monkeypatch.setattr("transformers.AutoTokenizer", FakeAutoTokenizer)
    monkeypatch.setattr("drift.AutoDistributedModelForCausalLM", FakeAutoModel)

    runtime = make_manifest_loader(
        manifest,
        initial_peers=["/ip4/127.0.0.1/tcp/1/p2p/fake"],
        revocation_files=["revoked.json"],
        request_timeout=7,
        max_retries=2,
    )()

    assert calls.metadata == [{"include_tokenizer": True}]
    assert calls.tokenizer == (tmp_path, {"local_files_only": True})
    repository, kwargs = calls.model
    assert repository == manifest.source.repository
    assert kwargs["revision"] == manifest.source.revision
    assert kwargs["dht_prefix"] == manifest.dht_prefix
    assert kwargs["manifest_digest"] == manifest.digest
    assert kwargs["manifest_execution_profile"] == manifest.runtime.to_dict()
    assert kwargs["request_timeout"] == 7
    assert kwargs["max_retries"] == 2
    assert kwargs["artifact_verifier"].snapshot_root == tmp_path

    runtime.close()
    runtime.close()
    assert calls.shutdown == 1
    assert calls.dht_shutdown == 1
    assert runtime.cleanup_health() == {
        "observed": True,
        "sequence_manager_update_thread_alive": False,
        "dht_process_alive": False,
        "clean": True,
    }
