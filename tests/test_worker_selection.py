import asyncio
import hashlib
import json
import sys
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import drift.node.device_binding as binding_module
import drift.node.policy_store as store_module
import drift.node.worker_selection as selection_module
from drift.node.config import NodeConfig, NodeConfigError
from drift.node.config_lock import node_config_write_lock
from drift.node.device_binding import DeviceBindingError, DeviceBindingStore
from drift.node.gpu_selection_tokens import GpuSelectionChangedError, GpuSelectionTokens
from drift.node.model_manager import ModelDescriptor, ModelManager, ModelManagerClosedError, ModelRuntime
from drift.node.policy_store import (
    ContributionPolicyConflictError,
    ContributionPolicyPersistenceError,
    ContributionPolicyStore,
)
from drift.node.server import _commit_worker_selection, create_node_app
from drift.node.worker_selection import candidate_selection, parse_selection_request
from drift.node.worker_supervisor import (
    WorkerLaunch,
    WorkerNotFoundError,
    WorkerReconfigurationBusyError,
    WorkerSupervisor,
    WorkerSupervisorSettings,
)

CUDA_UUID = "GPU-00000000-0000-0000-0000-000000000001"
OTHER_UUID = "GPU-00000000-0000-0000-0000-000000000002"
URL = "/control/v1/contribution-worker-selection"
CONTROL = {"Authorization": "Bearer control-key"}


def request(revision="sha256:" + "a" * 64, operation="reselect", **values):
    result = {"schema_version": 1, "expected_config_revision": revision, "operation": operation}
    if operation != "add":
        result["worker_id"] = "worker"
    if operation != "remove":
        result["device"] = "cuda:1"
    result.update(values)
    return result


def settings(config):
    return WorkerSupervisorSettings(
        launches=tuple(
            WorkerLaunch(
                worker.worker_id,
                worker.model,
                (sys.executable, "-c", "pass"),
                auto_start=worker.enabled,
                auto_restart=False,
                device=worker.device,
            )
            for worker in config.workers
        ),
        stop_timeout=1,
    )


@pytest.fixture
def runtime(tmp_path, monkeypatch, request):
    monkeypatch.setattr(binding_module, "_cuda_identity", lambda device: CUDA_UUID)
    monkeypatch.setattr(binding_module, "_DEFAULT_LIVENESS_PROBE", lambda identity: identity == CUDA_UUID)
    document = {
        "schema_version": 1,
        "models": [{"manifest": "manifest.json", "initial_peers": ["/ip4/127.0.0.1/tcp/31337/p2p/example"]}],
        "contribution_policy": {"sharing_enabled": False},
        "workers": [
            {"id": "worker", "model": "model", "identity_path": "old.key", "block_indices": "2:4", "device": "cuda:0"}
        ],
    }
    if hasattr(request, "param"):
        document["workers"] = request.param
    path = tmp_path / "node.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    supervisor = WorkerSupervisor(settings(NodeConfig.load(path)).launches)
    for worker in document["workers"]:
        supervisor.pause_worker(worker["id"])
    manager = ModelManager()
    manager.register(ModelDescriptor("model"), lambda: ModelRuntime(object(), object()))
    store = ContributionPolicyStore(path, supervisor, settings)
    value = SimpleNamespace(path=path, supervisor=supervisor, manager=manager, store=store, document=document)
    yield value
    supervisor.shutdown()
    manager.shutdown()


def revision(runtime):
    return runtime.store.snapshot()["config_revision"]


def apply(runtime, operation="reselect", **values):
    return runtime.store.update_worker_selection(
        request(revision(runtime), operation, **values), manager=runtime.manager
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"schema_version": True},
        {"schema_version": 1.0},
        {"schema_version": 2},
        {"operation": []},
        {"operation": "replace"},
        {"expected_config_revision": "sha256:" + "A" * 64},
        {"expected_config_revision": "sha256:" + "+" + "a" * 63},
        {"worker_id": "../key"},
        {"worker_id": "x" * 65},
        {"device": "cpu"},
        {"device": "cuda"},
        {"device": "cuda:01"},
        {"device": "cuda:16"},
        {"device": "xpu:0"},
        {"device": CUDA_UUID},
        {"device": None},
        {"identity_path": "private.key"},
        {"command": ["run"]},
        {"model": "arbitrary"},
        {"environment": {"HF_TOKEN": "not-a-real-token"}},
    ],
)
def test_strict_request_rejects_untrusted_or_ambiguous_fields(updates):
    with pytest.raises(NodeConfigError):
        parse_selection_request(json.dumps(request(**updates)).encode())


@pytest.mark.parametrize("payload", [b"{}", b"[]", b"\xff", b"x" * 8193, b'{"operation":NaN}', b"[" * 1500])
def test_invalid_payload_is_bounded(payload):
    with pytest.raises(NodeConfigError):
        parse_selection_request(payload)


def test_duplicate_fields_rejected():
    payload = json.dumps(request()).replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')
    with pytest.raises(NodeConfigError):
        parse_selection_request(payload.encode())


def test_managed_identity_parent_cannot_redirect_outside_profile(runtime, monkeypatch):
    directory = runtime.path.parent / "worker-identities"
    monkeypatch.setattr(selection_module.os.path, "isjunction", lambda path: path == directory, raising=False)
    with pytest.raises(NodeConfigError, match="unlinked directory"):
        apply(runtime)
    assert not directory.exists() and not runtime.supervisor.configuration_restart_pending


def test_fresh_identity_collision_retries_and_never_replaces_existing_pin(runtime, monkeypatch):
    duplicate, fresh = "a" * 32, "b" * 32
    directory = runtime.path.parent / "worker-identities"
    directory.mkdir()
    identity = directory / f"worker-{duplicate}.key"
    identity.write_bytes(b"must remain")
    values = iter((duplicate, fresh))
    monkeypatch.setattr(selection_module.secrets, "token_hex", lambda count: next(values))
    result = apply(runtime)
    assert result["worker_id"] == "worker-" + fresh
    assert identity.read_bytes() == b"must remain"


def test_rotation_pins_before_commit_preserves_manual_assignment_and_pauses(runtime, monkeypatch):
    old_private = runtime.path.parent / "old.key"
    old_private.write_bytes(b"old identity stays private")
    old_binding = DeviceBindingStore(old_private.with_name(".old.key.device-binding"))
    old_binding.bind("worker", "cuda:0")
    original_prepare = runtime.store._prepare

    def prepare(config):
        worker = config.workers[0]
        directory = worker.identity_path.with_name(f".{worker.identity_path.name}.device-binding")
        assert len(list(directory.glob("*.json"))) == 1
        assert runtime.path.read_text() == json.dumps(runtime.document)
        return original_prepare(config)

    monkeypatch.setattr(runtime.store, "_prepare", prepare)
    result = apply(runtime, worker_id="WORKER")
    saved = json.loads(runtime.path.read_bytes())
    worker = saved["workers"][0]
    assert worker["id"] != "worker" and worker["id"] == result["worker_id"]
    assert worker["device"] == "cuda:1" and worker["block_indices"] == "2:4"
    assert worker["enabled"] is False
    assert worker["model"] == "model"
    assert worker["identity_path"].startswith("worker-identities/worker-")
    assert result == {
        "schema_version": 1,
        "config_revision": "sha256:" + hashlib.sha256(runtime.path.read_bytes()).hexdigest(),
        "restart_required": True,
        "worker_id": worker["id"],
        "device": "cuda:1",
    }
    assert old_private.read_bytes() == b"old identity stays private"
    assert old_binding.bind("worker", "cuda:0").check() is None
    assert CUDA_UUID not in json.dumps(result) and "identity_path" not in json.dumps(result)
    with pytest.raises(WorkerReconfigurationBusyError):
        runtime.supervisor.start_worker("worker")
    with pytest.raises(ModelManagerClosedError):
        runtime.manager.load("model")
    for update in (
        lambda: runtime.store.update({"sharing_enabled": False}, expected_revision=revision(runtime)),
        lambda: runtime.store.update_inference_mode("local_only", expected_revision=revision(runtime)),
        lambda: apply(runtime),
    ):
        with pytest.raises(WorkerReconfigurationBusyError):
            update()
    # Restart observes the persisted pin; reordering cannot enroll a new card.
    monkeypatch.setattr(binding_module, "_cuda_identity", lambda device: OTHER_UUID)
    monkeypatch.setattr(binding_module, "_DEFAULT_LIVENESS_PROBE", lambda identity: True)
    selected = NodeConfig.load(runtime.path).workers[0]
    with pytest.raises(DeviceBindingError):
        DeviceBindingStore(selected.identity_path.with_name(f".{selected.identity_path.name}.device-binding")).bind(
            selected.worker_id, selected.device
        )


def test_removal_never_deletes_retired_private_identity(runtime):
    identity = runtime.path.parent / "old.key"
    identity.write_bytes(b"old")
    result = apply(runtime, "remove")
    assert json.loads(runtime.path.read_bytes())["workers"] == []
    assert identity.read_bytes() == b"old"
    assert result["device"] is None and result["worker_id"] == "worker"


def test_add_only_first_worker_and_reselection_does_not_invent_spans(runtime):
    with pytest.raises(NodeConfigError, match="additional GPU allocation"):
        apply(runtime, "add")
    original = runtime.path.read_bytes()
    empty = {**runtime.document, "workers": []}
    candidate, worker_id, device = candidate_selection(empty, request(operation="add"), base_dir=runtime.path.parent)
    assert candidate["workers"] == [
        {
            "model": "auto",
            "num_blocks": 1,
            "managed_by": "desktop_gpu",
            "id": worker_id,
            "identity_path": f"worker-identities/{worker_id}.key",
            "device": device,
            "enabled": False,
        }
    ]
    assert runtime.path.read_bytes() == original


@pytest.mark.parametrize("runtime", [[]], indirect=True)
def test_first_card_creation_is_durable_pinned_and_paused(runtime):
    result = apply(runtime, "add")
    worker = NodeConfig.load(runtime.path).workers[0]
    assert worker.worker_id == result["worker_id"] and worker.model == "auto"
    assert worker.num_blocks == 1 and worker.enabled is False and worker.device == "cuda:1"
    assert runtime.supervisor.configuration_restart_pending
    assert len(list(worker.identity_path.parent.glob(".*.device-binding/*.json"))) == 1


def test_remove_last_per_device_worker_then_reload_and_add_first_card(runtime):
    document = runtime.document
    document["contribution_policy"].update(processing_scope="per_device", max_processing_percent=17)
    document["workers"][0]["max_processing_percent"] = 25
    runtime.path.write_text(json.dumps(document), encoding="utf-8")
    runtime.store = ContributionPolicyStore(runtime.path, runtime.supervisor, settings)
    apply(runtime, "remove")
    assert NodeConfig.load(runtime.path).workers == ()
    manager = ModelManager()
    supervisor = WorkerSupervisor(())
    try:
        store = ContributionPolicyStore(runtime.path, supervisor, settings)
        store.update_worker_selection(request(store.snapshot()["config_revision"], "add"), manager=manager)
        saved = NodeConfig.load(runtime.path)
        assert saved.workers[0].max_processing_percent == 100
        assert saved.workers[0].managed_by == "desktop_gpu"
        assert saved.workers[0].enabled is False
        assert saved.contribution_policy.processing_scope == "per_device"
        assert saved.contribution_policy.max_processing_percent == 17
    finally:
        supervisor.shutdown()
        manager.shutdown()


@pytest.mark.parametrize("operation", ["reselect", "remove"])
def test_missing_worker_leaves_runtime_and_disk_unchanged(runtime, operation):
    original = runtime.path.read_bytes()
    with pytest.raises(WorkerNotFoundError):
        apply(runtime, operation, worker_id="missing")
    assert runtime.path.read_bytes() == original
    with runtime.manager.load("model"):
        pass


@pytest.mark.parametrize("conflict", ["stale", "external", "lock"])
def test_revision_and_external_writer_conflicts(runtime, conflict):
    old_revision = revision(runtime)
    if conflict == "external":
        runtime.path.write_bytes(runtime.path.read_bytes() + b"\n")
    original = runtime.path.read_bytes()
    value = request(old_revision if conflict != "stale" else "sha256:" + "b" * 64)
    if conflict == "lock":
        with node_config_write_lock(runtime.path):
            with pytest.raises(ContributionPolicyConflictError):
                runtime.store.update_worker_selection(value, manager=runtime.manager)
    else:
        with pytest.raises(ContributionPolicyConflictError):
            runtime.store.update_worker_selection(value, manager=runtime.manager)
    assert runtime.path.read_bytes() == original and revision(runtime) == old_revision
    assert not runtime.supervisor.configuration_restart_pending
    with runtime.manager.load("model"):
        pass


@pytest.mark.parametrize("failure", ["enroll", "prepare", "exchange"])
def test_failure_keeps_admission_and_config_and_never_reuses_orphan_pin(runtime, monkeypatch, failure):
    original = runtime.path.read_bytes()
    old_revision = revision(runtime)

    def fail(*args, **kwargs):
        raise OSError("private path or hardware UUID must never reach the API")

    if failure == "enroll":
        monkeypatch.setattr(store_module, "enroll_selection", fail)
    elif failure == "prepare":
        monkeypatch.setattr(runtime.store, "_prepare", fail)
    else:
        monkeypatch.setattr(store_module, "_exchange_paths", fail)
    with pytest.raises((OSError, ContributionPolicyPersistenceError)):
        apply(runtime)
    assert runtime.path.read_bytes() == original and revision(runtime) == old_revision
    assert not runtime.supervisor.configuration_restart_pending
    with runtime.manager.load("model"):
        pass
    pins = list((runtime.path.parent / "worker-identities").glob(".*.device-binding/*.json"))
    assert len(pins) == (0 if failure == "enroll" else 1)
    if pins:
        retired = json.loads(pins[0].read_bytes())["worker_id"]
        monkeypatch.setattr(selection_module.secrets, "token_hex", lambda count: retired.removeprefix("worker-"))
        with pytest.raises(NodeConfigError, match="fresh worker identity"):
            candidate_selection(runtime.document, request(), base_dir=runtime.path.parent)


def test_active_local_inference_blocks_save_without_interruption_or_pin(runtime):
    original = runtime.path.read_bytes()
    with runtime.manager.load("model") as lease:
        with pytest.raises(WorkerReconfigurationBusyError):
            apply(runtime)
        assert lease.runtime is not None and runtime.manager.snapshots()[0].active_requests == 1
    assert runtime.path.read_bytes() == original
    assert not (runtime.path.parent / "worker-identities").exists()


def app_for(runtime, restart):
    return create_node_app(
        runtime.manager,
        api_keys=["client-key"],
        control_keys=["control-key"],
        worker_supervisor=runtime.supervisor,
        contribution_policy_store=runtime.store,
        request_restart=restart,
    )


def test_endpoint_auth_bounded_parsing_and_exactly_one_graceful_reload(runtime):
    restarts = []
    app = app_for(runtime, lambda: restarts.append(json.loads(runtime.path.read_bytes())))
    with TestClient(app) as client:
        body = request(revision(runtime))
        original = runtime.path.read_bytes()
        for headers in ({}, {"Authorization": "Bearer client-key"}):
            assert client.put(URL, json=body, headers=headers).status_code == 401
        assert client.put(URL, content="{}", headers=CONTROL).status_code == 415
        assert (
            client.put(URL, content=b"x" * 8193, headers={**CONTROL, "Content-Type": "application/json"}).status_code
            == 413
        )
        assert client.put(URL, json={**body, "device": CUDA_UUID}, headers=CONTROL).status_code == 422
        assert runtime.path.read_bytes() == original and restarts == []
        response = client.put(URL, json=body, headers=CONTROL)
        assert response.status_code == 202, response.text
        assert len(restarts) == 1 and restarts[0]["workers"][0]["enabled"] is False
        assert response.json()["config_revision"] == revision(runtime)
        assert CUDA_UUID not in response.text and str(runtime.path.parent) not in response.text
        assert client.put(URL, json=body, headers=CONTROL).status_code == 409
        assert client.post("/control/v1/workers/worker/start", headers=CONTROL).status_code == 409
        assert len(restarts) == 1
        status = client.get("/control/v1/status", headers=CONTROL).json()
        assert status["status"] == "stopping" and status["configuration_restart_pending"] is True
        assert status["contribution"]["editable"] is False and status["inference_mode_editable"] is False


@pytest.mark.parametrize(
    "error,expected", [(NodeConfigError, 422), (DeviceBindingError, 409), (ContributionPolicyPersistenceError, 503)]
)
def test_endpoint_errors_are_sanitized_and_do_not_request_restart(runtime, monkeypatch, error, expected):
    restarts = []

    def fail(*args, **kwargs):
        raise error(f"private {runtime.path} {CUDA_UUID}")

    monkeypatch.setattr(runtime.store, "_prepare", fail)
    with TestClient(app_for(runtime, lambda: restarts.append(True))) as client:
        response = client.put(URL, json=request(revision(runtime)), headers=CONTROL)
        assert response.status_code == expected
        assert CUDA_UUID not in response.text and str(runtime.path) not in response.text
        assert restarts == []


def test_restart_callback_failure_keeps_paused_durable_config(runtime):
    def fail():
        raise RuntimeError("reload signal failed")

    with TestClient(app_for(runtime, fail)) as client:
        response = client.put(URL, json=request(revision(runtime)), headers=CONTROL)
        assert response.status_code == 503 and "was saved" in response.json()["detail"]
        assert runtime.supervisor.configuration_restart_pending
        assert json.loads(runtime.path.read_bytes())["workers"][0]["enabled"] is False
        with pytest.raises(ModelManagerClosedError):
            runtime.manager.load("model")


def test_cancelled_http_await_still_signals_completed_transaction(runtime, monkeypatch):
    entered, finish, restarted = threading.Event(), threading.Event(), threading.Event()
    original = runtime.store._atomic_replace

    def blocked(*args, **kwargs):
        entered.set()
        assert finish.wait(5)
        original(*args, **kwargs)

    monkeypatch.setattr(runtime.store, "_atomic_replace", blocked)

    async def scenario():
        task = asyncio.create_task(
            _commit_worker_selection(runtime.store, request(revision(runtime)), runtime.manager, restarted.set)
        )
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            finish.set()
        assert await asyncio.to_thread(restarted.wait, 5)

    asyncio.run(scenario())
    assert runtime.supervisor.configuration_restart_pending
    assert json.loads(runtime.path.read_bytes())["workers"][0]["device"] == "cuda:1"


def test_gpu_tokens_require_control_key_and_apply_the_same_displayed_card(runtime):
    runtime.store._gpu_selection_tokens = GpuSelectionTokens(
        devices=lambda: ("cuda:0", "cuda:1"), identity=lambda device: CUDA_UUID, live=lambda identity: True
    )
    restarts = []
    with TestClient(app_for(runtime, lambda: restarts.append(True))) as client:
        url = "/control/v1/contribution-gpu-devices"
        assert client.get(url).status_code == 401
        assert client.get(url, headers={"Authorization": "Bearer client-key"}).status_code == 401
        snapshot = client.get(url, headers=CONTROL).json()
        assert snapshot["config_revision"] == revision(runtime)
        row = snapshot["devices"][1]
        response = client.put(URL, json=request(revision(runtime), **row), headers=CONTROL)
        assert response.status_code == 202, response.text
        assert restarts == [True]
        assert client.get(url, headers=CONTROL).status_code == 409


def test_card_change_between_token_check_and_enrollment_never_commits(runtime, monkeypatch):
    original = runtime.path.read_bytes()
    provider = GpuSelectionTokens(
        devices=lambda: ("cuda:1",), identity=lambda device: CUDA_UUID, live=lambda identity: True
    )
    runtime.store._gpu_selection_tokens = provider
    token = provider.snapshot(revision(runtime))["devices"][0]["selection_token"]
    monkeypatch.setattr(binding_module, "_cuda_identity", lambda device: OTHER_UUID)
    monkeypatch.setattr(binding_module, "_DEFAULT_LIVENESS_PROBE", lambda identity: True)
    with pytest.raises(GpuSelectionChangedError):
        apply(runtime, selection_token=token)
    assert runtime.path.read_bytes() == original and not runtime.supervisor.configuration_restart_pending
    with runtime.manager.load("model"):
        pass
