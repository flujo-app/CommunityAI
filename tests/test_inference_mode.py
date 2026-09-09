import json

from fastapi.testclient import TestClient
from test_policy_store import _store

from drift.node.model_manager import ModelDescriptor, ModelManager, ModelRuntime
from drift.node.server import create_node_app


def test_control_mode_change_is_authenticated_persistent_and_preserves_active_answer(tmp_path):
    path, supervisor, store = _store(tmp_path)
    manager = ModelManager()
    remote = {"status": "complete", "covered_blocks": 64, "total_blocks": 64, "peer_count": 4, "source": "discovery"}
    local = {"status": "complete", "covered_blocks": 24, "total_blocks": 24, "peer_count": 0, "source": "local"}
    manager.register(ModelDescriptor("community"), lambda: ModelRuntime(object(), None), route_health=lambda: remote)
    manager.register(
        ModelDescriptor("local", execution="local"), lambda: ModelRuntime(object(), None), route_health=lambda: local
    )
    manager.configure_auto_selection(["community", "local"])
    app = create_node_app(
        manager,
        api_keys=["client-secret"],
        control_keys=["control-secret"],
        worker_supervisor=supervisor,
        contribution_policy_store=store,
    )
    with TestClient(app) as client:
        active = manager.load("auto")
        prior = path.read_bytes()
        body = {"inference_mode": "local_only", "expected_config_revision": store.snapshot()["config_revision"]}
        assert (
            client.put(
                "/control/v1/inference-mode", json=body, headers={"Authorization": "Bearer client-secret"}
            ).status_code
            == 401
        )
        assert path.read_bytes() == prior
        headers = {"Authorization": "Bearer control-secret"}
        assert client.put("/control/v1/inference-mode", json=body, headers=headers).status_code == 200
        assert json.loads(path.read_text())["inference_mode"] == "local_only"
        assert manager.resolve("auto").model_id == "local"
        assert active.descriptor.model_id == "community"
        assert active.runtime.model is not None
        assert client.put("/control/v1/inference-mode", json=body, headers=headers).status_code == 412
        active.release()
        assert (
            client.post(
                "/v1/completions",
                json={"model": "community", "prompt": "hello"},
                headers={"Authorization": "Bearer client-secret"},
            ).status_code
            == 503
        )
