"""Whole-set control API and durable store; physical inventory is a bounded test double."""

import asyncio
import copy
import json
import sys
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import drift.node.device_binding as binding_module
import drift.node.policy_store as store_module
from drift.node.config import NodeConfig
from drift.node.device_binding import DeviceBindingStore
from drift.node.gpu_selection_tokens import GpuSelectionTokens
from drift.node.model_manager import ModelDescriptor, ModelManager, ModelManagerClosedError, ModelRuntime
from drift.node.policy_store import ContributionPolicyConflictError, ContributionPolicyStore
from drift.node.server import _commit_gpu_selection, create_node_app
from drift.node.worker_supervisor import WorkerLaunch, WorkerSupervisor, WorkerSupervisorSettings

URL = "/control/v1/contribution-gpu-selection"
CONTROL = {"Authorization": "Bearer control-key"}
IDENTITIES = {f"cuda:{index}": f"GPU-00000000-0000-0000-0000-{index + 1:012d}" for index in range(8)}


def managed_worker(**updates):
    return {
        "id": "card-zero",
        "model": "auto",
        "num_blocks": 1,
        "identity_path": "saved.key",
        "device": "cuda:0",
        "managed_by": "desktop_gpu",
        "max_vram": ".5%",
        "max_processing_percent": 25,
        "enabled": False,
        **updates,
    }


def manual_worker(**updates):
    return {
        "id": "worker-looks-managed",
        "model": "model",
        "block_indices": "2:4",
        "identity_path": "manual.key",
        "device": "cuda:0",
        "max_processing_percent": 25,
        "enabled": False,
        **updates,
    }


def prepare(config, *, device_overrides=None):
    return WorkerSupervisorSettings(
        launches=tuple(
            WorkerLaunch(
                worker.worker_id,
                worker.model,
                (sys.executable, "-c", "pass"),
                auto_start=worker.enabled,
                auto_restart=False,
                device=(device_overrides or {}).get(
                    worker.worker_id, "cuda:0" if worker.device == "cuda" else worker.device
                ),
            )
            for worker in config.workers
        ),
        stop_timeout=1,
    )


@pytest.fixture
def runtime_factory(tmp_path, monkeypatch):
    identities = dict(IDENTITIES)
    monkeypatch.setattr(binding_module, "_cuda_identity", identities.get)
    monkeypatch.setattr(binding_module, "_DEFAULT_LIVENESS_PROBE", lambda identity: identity in identities.values())
    runtimes = []

    def build(*, workers=(), policy=None, paused=True, pinned=True, device_overrides=None):
        directory = tmp_path / str(len(runtimes))
        directory.mkdir()
        document = {
            "schema_version": 1,
            "models": [{"manifest": "manifest.json", "initial_peers": ["/ip4/127.0.0.1/tcp/31337/p2p/example"]}],
            "contribution_policy": {"sharing_enabled": False, "processing_scope": "per_device"}
            if policy is None
            else policy,
            "workers": copy.deepcopy(list(workers)),
        }
        path = directory / "node.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        config = NodeConfig.load(path)
        supervisor = WorkerSupervisor(prepare(config, device_overrides=device_overrides).launches)
        for worker in config.workers:
            if paused:
                supervisor.pause_worker(worker.worker_id)
            if pinned and worker.device and worker.device.startswith("cuda:"):
                DeviceBindingStore(worker.identity_path.with_name(f".{worker.identity_path.name}.device-binding")).bind(
                    worker.worker_id, worker.device
                )
        manager = ModelManager()
        manager.register(ModelDescriptor("model"), lambda: ModelRuntime(object(), object()))
        store = ContributionPolicyStore(path, supervisor, prepare)
        store._gpu_selection_tokens = GpuSelectionTokens(
            devices=lambda: tuple(identities),
            identity=identities.get,
            live=lambda identity: identity in identities.values(),
        )
        inventory = [
            {"device": device, "name": "Identical test GPU", "total_bytes": 8 * 1024**3, "status": "available"}
            for device in identities
        ]
        runtime = SimpleNamespace(
            path=path,
            supervisor=supervisor,
            manager=manager,
            store=store,
            identities=identities,
            inventory=inventory,
            document=document,
        )
        runtime.hardware = lambda policy: {"gpus": copy.deepcopy(inventory)}
        runtimes.append(runtime)
        return runtime

    yield build
    for runtime in runtimes:
        runtime.supervisor.shutdown()
        runtime.manager.shutdown()


def snapshot(runtime):
    return runtime.store.gpu_selection_state(hardware_status=runtime.hardware, manager=runtime.manager)


def draft(state, selected=None):
    tokens = {row["device"]: row["selection_token"] for row in state["inventory"] if "selection_token" in row}
    rows = copy.deepcopy(state["rows"])
    for row in rows:
        if selected is not None:
            row["selected"] = row["device"] in selected
        if row["selected"]:
            row["selection_token"] = tokens[row["device"]]
    return {"schema_version": 1, "expected_config_revision": state["config_revision"], "rows": rows}


def app_for(runtime, restart):
    return create_node_app(
        runtime.manager,
        api_keys=["client-key"],
        control_keys=["control-key"],
        worker_supervisor=runtime.supervisor,
        contribution_policy_store=runtime.store,
        hardware_status=runtime.hardware,
        request_restart=restart,
    )


def assert_unchanged(runtime, payload, revision):
    assert runtime.path.read_bytes() == payload
    assert runtime.store.snapshot()["config_revision"] == revision
    assert runtime.supervisor.configuration_restart_pending is False
    with runtime.manager.load("model"):
        pass


@pytest.mark.parametrize("operation", ["add", "remove", "reselect"])
def test_legacy_api_rejects_managed_gpu_mutations_without_side_effects(runtime_factory, operation):
    runtime = runtime_factory(workers=[managed_worker()])
    original = runtime.path.read_bytes()
    revision = runtime.store.snapshot()["config_revision"]
    private_files = {str(path): path.read_bytes() for path in runtime.path.parent.rglob("*") if path.is_file()}
    request = {"schema_version": 1, "expected_config_revision": revision, "operation": operation}
    if operation != "add":
        request["worker_id"] = "card-zero"
    if operation != "remove":
        request["device"] = "cuda:1"
    restarts = []
    with TestClient(app_for(runtime, lambda: restarts.append(True))) as client:
        response = client.put("/control/v1/contribution-worker-selection", headers=CONTROL, json=request)
        assert response.status_code == 409
        assert response.json() == {"detail": "Use the GPU selection batch control for managed GPU configurations."}
        assert restarts == []
        assert_unchanged(runtime, original, revision)
        assert {
            str(path): path.read_bytes() for path in runtime.path.parent.rglob("*") if path.is_file()
        } == private_files


def test_snapshot_lists_eight_cards_without_claiming_joint_runtime_or_exposing_identity(runtime_factory):
    runtime = runtime_factory()
    state = snapshot(runtime)
    assert state["editable"] is True and state["runtime_ready"] is True
    assert state["restart_required"] is False
    assert "not supported" in state["reason"]
    assert len(state["inventory"]) == len(state["rows"]) == 8
    assert all(not row["selected"] for row in state["rows"])
    assert len({item["selection_token"] for item in state["inventory"]}) == 8
    assert all(identity not in json.dumps(state) for identity in IDENTITIES.values())
    assert str(runtime.path.parent) not in json.dumps(state)


def test_snapshot_uses_one_current_policy_and_detects_external_revision_change(runtime_factory):
    runtime = runtime_factory()
    policies = []
    original = runtime.hardware

    def changing_hardware(policy):
        policies.append(policy)
        runtime.path.write_bytes(runtime.path.read_bytes() + b"\n")
        return original(policy)

    runtime.hardware = changing_hardware
    with pytest.raises(ContributionPolicyConflictError):
        snapshot(runtime)
    assert policies == [NodeConfig.load(runtime.path).contribution_policy.to_dict()]
    assert runtime.supervisor.configuration_restart_pending is False


def test_snapshot_sanitizes_hardware_fields_and_honors_capacity_availability(runtime_factory):
    runtime = runtime_factory()
    runtime.inventory[0].update(name=IDENTITIES["cuda:0"], total_bytes=True, private_uuid=IDENTITIES["cuda:0"])
    runtime.inventory[1].update(name=None)
    state = snapshot(runtime)
    assert state["inventory"][0] == {"device": "cuda:0", "name": "GPU", "total_bytes": None, "status": "unavailable"}
    assert state["inventory"][1]["name"] == "GPU"
    assert "GPU-" not in json.dumps(state) and "private_uuid" not in json.dumps(state)


@pytest.mark.parametrize("problem", ["missing-pin", "deleted-pin", "changed-card"])
def test_selected_bad_pin_is_deselectable_but_never_reenrolled_by_snapshot(runtime_factory, problem):
    runtime = runtime_factory(workers=[managed_worker()], pinned=problem != "missing-pin")
    worker = NodeConfig.load(runtime.path).workers[0]
    directory = worker.identity_path.with_name(f".{worker.identity_path.name}.device-binding")
    if problem == "deleted-pin":
        next(directory.glob("*.json")).unlink()
    elif problem == "changed-card":
        runtime.identities["cuda:0"] = IDENTITIES["cuda:1"]
    before = {str(path): path.read_bytes() for path in directory.glob("*.json")}
    state = snapshot(runtime)
    row = next(item for item in state["inventory"] if item["device"] == "cuda:0")
    assert row["status"] == "unavailable" and "selection_token" not in row
    assert state["rows"][0]["selected"] is True and state["editable"] is True
    assert state["runtime_ready"] is False
    assert {str(path): path.read_bytes() for path in directory.glob("*.json")} == before
    if problem == "missing-pin":
        assert not directory.exists()
    result = runtime.store.update_gpu_selection(
        draft(state, selected=set()), manager=runtime.manager, hardware_status=runtime.hardware
    )
    assert result["restart_required"] is True
    assert NodeConfig.load(runtime.path).workers == ()


def test_api_auth_content_type_size_and_strict_parse_never_mutate(runtime_factory):
    runtime = runtime_factory()
    restarts = []
    state = snapshot(runtime)
    request = draft(state, {"cuda:0"})
    original = runtime.path.read_bytes()
    with TestClient(app_for(runtime, lambda: restarts.append(True))) as client:
        for headers in ({}, {"Authorization": "Bearer client-key"}):
            assert client.get(URL, headers=headers).status_code == 401
            assert client.put(URL, json=request, headers=headers).status_code == 401
        assert client.put(URL, content="{}", headers=CONTROL).status_code == 415
        assert (
            client.put(URL, content=b"x" * 16385, headers={**CONTROL, "Content-Type": "application/json"}).status_code
            == 413
        )
        assert client.put(URL, json={**request, "identity_path": "private"}, headers=CONTROL).status_code == 422
        assert client.get(URL, headers=CONTROL).status_code == 200
        assert_unchanged(runtime, original, state["config_revision"])
    assert restarts == []


def test_one_card_save_pins_before_reload_and_reports_saved_pending_state(runtime_factory):
    runtime = runtime_factory()
    state = snapshot(runtime)
    request = draft(state, {"cuda:0"})
    request["rows"][0].update(max_vram="6GiB", max_processing_percent=40)
    restarts = []
    with TestClient(app_for(runtime, lambda: restarts.append(NodeConfig.load(runtime.path)))) as client:
        response = client.put(URL, json=request, headers=CONTROL)
        assert response.status_code == 202, response.text
        result = response.json()
        assert set(result) == {"schema_version", "config_revision", "restart_required", "runtime_ready"}
        assert result["restart_required"] is True and result["runtime_ready"] is False
        assert len(restarts) == 1
        worker = restarts[0].workers[0]
        assert worker.managed_by == "desktop_gpu" and worker.model == "auto" and worker.enabled is False
        assert worker.max_vram == "6GiB" and worker.max_processing_percent == 40
        pin = DeviceBindingStore(worker.identity_path.with_name(f".{worker.identity_path.name}.device-binding"))
        assert pin.load_existing(worker.worker_id, worker.device).cuda_visible_devices == IDENTITIES["cuda:0"]
        pending = client.get(URL, headers=CONTROL).json()
        assert pending["restart_required"] is True and pending["editable"] is False
        assert pending["config_revision"] == result["config_revision"]
        assert client.put(URL, json=request, headers=CONTROL).status_code == 409
        assert len(restarts) == 1


def test_retained_card_edit_preserves_identity_pin_and_exact_memory_spelling(runtime_factory):
    runtime = runtime_factory(workers=[managed_worker()])
    before = NodeConfig.load(runtime.path).workers[0]
    pin = next(before.identity_path.with_name(f".{before.identity_path.name}.device-binding").glob("*.json"))
    pin_bytes = pin.read_bytes()
    request = draft(snapshot(runtime))
    request["rows"][0]["max_processing_percent"] = 75
    result = runtime.store.update_gpu_selection(request, manager=runtime.manager, hardware_status=runtime.hardware)
    saved = NodeConfig.load(runtime.path).workers[0]
    assert result["restart_required"] is True
    assert saved.worker_id == before.worker_id and saved.identity_path == before.identity_path
    assert saved.max_vram == ".5%" and saved.max_processing_percent == 75
    assert pin.read_bytes() == pin_bytes


@pytest.mark.parametrize("workers", [[], [managed_worker()]])
def test_exact_noop_returns_200_without_persistence_or_closing_admission(runtime_factory, workers):
    runtime = runtime_factory(workers=workers)
    state = snapshot(runtime)
    request = draft(state)
    if not workers:
        request["rows"][0].update(max_vram="40%", max_processing_percent=15)
    original = runtime.path.read_bytes()
    restarts = []
    with TestClient(app_for(runtime, lambda: restarts.append(True))) as client:
        result = client.put(URL, json=request, headers=CONTROL)
        assert result.status_code == 200, result.text
        assert result.json() == {
            "schema_version": 1,
            "config_revision": state["config_revision"],
            "restart_required": False,
            "runtime_ready": True,
        }
        assert_unchanged(runtime, original, state["config_revision"])
    assert restarts == []


@pytest.mark.parametrize("count", [2, 8])
def test_joint_runtime_guard_rejects_complete_multi_card_save_without_pins(runtime_factory, count):
    runtime = runtime_factory()
    state = snapshot(runtime)
    original = runtime.path.read_bytes()
    restarts = []
    with TestClient(app_for(runtime, lambda: restarts.append(True))) as client:
        result = client.put(URL, json=draft(state, {f"cuda:{index}" for index in range(count)}), headers=CONTROL)
        assert result.status_code == 422
        assert "joint automatic GPU runtime" in result.json()["detail"]
        assert_unchanged(runtime, original, state["config_revision"])
    assert not (runtime.path.parent / "worker-identities").exists()
    assert restarts == []


@pytest.mark.parametrize(
    "problem,expected", [("stale-revision", 412), ("bad-token", 409), ("card-swap", 409), ("lost-card", 409)]
)
def test_stale_context_rejects_without_rebinding_or_restart(runtime_factory, problem, expected):
    runtime = runtime_factory()
    state = snapshot(runtime)
    request = draft(state, {"cuda:0"})
    original = runtime.path.read_bytes()
    if problem == "stale-revision":
        request["expected_config_revision"] = "sha256:" + "f" * 64
    elif problem == "bad-token":
        request["rows"][0]["selection_token"] = "sha256:" + "f" * 64
    elif problem == "card-swap":
        runtime.identities["cuda:0"], runtime.identities["cuda:1"] = (
            runtime.identities["cuda:1"],
            runtime.identities["cuda:0"],
        )
    else:
        runtime.identities.pop("cuda:0")
    restarts = []
    with TestClient(app_for(runtime, lambda: restarts.append(True))) as client:
        result = client.put(URL, json=request, headers=CONTROL)
        assert result.status_code == expected, result.text
        assert "GPU-" not in result.text and str(runtime.path.parent) not in result.text
        assert_unchanged(runtime, original, state["config_revision"])
    assert not (runtime.path.parent / "worker-identities").exists() and restarts == []


def test_mapping_change_during_enrollment_cannot_commit(runtime_factory, monkeypatch):
    runtime = runtime_factory()
    state = snapshot(runtime)
    original = runtime.path.read_bytes()
    enroll = store_module.enroll_selection

    def change_then_enroll(config, worker_id):
        runtime.identities["cuda:0"] = IDENTITIES["cuda:1"]
        return enroll(config, worker_id)

    monkeypatch.setattr(store_module, "enroll_selection", change_then_enroll)
    with TestClient(app_for(runtime, lambda: pytest.fail("rejected enrollment requested reload"))) as client:
        result = client.put(URL, json=draft(state, {"cuda:0"}), headers=CONTROL)
        assert result.status_code == 409
        assert_unchanged(runtime, original, state["config_revision"])


def test_manual_card_and_physical_alias_are_unavailable_and_unrelated_worker_is_preserved(runtime_factory):
    runtime = runtime_factory(workers=[manual_worker()])
    state = snapshot(runtime)
    assert state["inventory"][0]["status"] == "unsupported"
    assert "selection_token" not in state["inventory"][0]
    runtime.identities["cuda:2"] = IDENTITIES["cuda:0"]
    alias = next(row for row in snapshot(runtime)["inventory"] if row["device"] == "cuda:2")
    assert alias["status"] == "unsupported" and "selection_token" not in alias
    runtime.store.update_gpu_selection(
        draft(state, {"cuda:1"}), manager=runtime.manager, hardware_status=runtime.hardware
    )
    saved = json.loads(runtime.path.read_bytes())
    assert saved["workers"][0] == runtime.document["workers"][0]
    assert saved["workers"][1]["managed_by"] == "desktop_gpu"


@pytest.mark.parametrize("problem", ["deleted-pin", "changed-card"])
def test_removing_final_managed_card_validates_retained_manual_pin_before_preparation(
    runtime_factory, monkeypatch, problem
):
    runtime = runtime_factory(workers=[managed_worker(), manual_worker(device="cuda:1")])
    manual = NodeConfig.load(runtime.path).workers[1]
    directory = manual.identity_path.with_name(f".{manual.identity_path.name}.device-binding")
    if problem == "deleted-pin":
        next(directory.glob("*.json")).unlink()
    runtime.identities["cuda:1"] = IDENTITIES["cuda:2"]
    pins_before = {str(path): path.read_bytes() for path in directory.glob("*.json")}
    state = snapshot(runtime)
    original = runtime.path.read_bytes()
    preparations = []

    def prepare_with_real_bind(config):
        preparations.append(config)
        for worker in config.workers:
            DeviceBindingStore(worker.identity_path.with_name(f".{worker.identity_path.name}.device-binding")).bind(
                worker.worker_id, worker.device
            )
        return prepare(config)

    monkeypatch.setattr(runtime.store, "_prepare", prepare_with_real_bind)
    with TestClient(app_for(runtime, lambda: pytest.fail("invalid manual pin requested restart"))) as client:
        result = client.put(URL, json=draft(state, set()), headers=CONTROL)
        assert result.status_code == 409, result.text
        assert preparations == []
        assert {str(path): path.read_bytes() for path in directory.glob("*.json")} == pins_before
        assert_unchanged(runtime, original, state["config_revision"])


@pytest.mark.parametrize("device", [None, "cuda:00", "cpu:0", "cuda:16", "xpu:0"])
def test_manual_ambiguous_device_disables_editing_and_rejects_before_preparation(runtime_factory, monkeypatch, device):
    worker = manual_worker(device=device)
    policy = None
    if device is None:
        worker.pop("max_processing_percent")
        policy = {"sharing_enabled": False}
    # NodeConfig accepts these legacy strings. Give the inert supervisor a
    # canonical launch while testing whether the saved document is editable.
    runtime = runtime_factory(workers=[worker], policy=policy, pinned=False, device_overrides={worker["id"]: "cpu"})
    state = snapshot(runtime)
    original = runtime.path.read_bytes()
    assert state["editable"] is False
    assert "explicit supported device" in state["reason"]
    monkeypatch.setattr(runtime.store, "_prepare", lambda config: pytest.fail("ambiguous device reached preparation"))
    with TestClient(app_for(runtime, lambda: pytest.fail("ambiguous device requested restart"))) as client:
        result = client.put(URL, json=draft(state, {"cuda:1"}), headers=CONTROL)
        assert result.status_code == 422, result.text
        assert_unchanged(runtime, original, state["config_revision"])
    assert not list(runtime.path.parent.rglob("*.device-binding"))


@pytest.mark.parametrize("device", ["cpu", "cuda"])
def test_explicit_cpu_and_cuda_alias_manual_workers_remain_editable(runtime_factory, device):
    runtime = runtime_factory(workers=[manual_worker(device=device)], pinned=False)
    worker = NodeConfig.load(runtime.path).workers[0]
    if device == "cuda":
        DeviceBindingStore(worker.identity_path.with_name(f".{worker.identity_path.name}.device-binding")).bind(
            worker.worker_id, "cuda:0"
        )
    state = snapshot(runtime)
    assert state["editable"] is True
    result = runtime.store.update_gpu_selection(
        draft(state, {"cuda:1"}), manager=runtime.manager, hardware_status=runtime.hardware
    )
    assert result["restart_required"] is True
    assert json.loads(runtime.path.read_bytes())["workers"][0] == runtime.document["workers"][0]


@pytest.mark.parametrize(
    "workers,policy",
    [
        (
            [{key: value for key, value in managed_worker().items() if key != "managed_by"}],
            {"sharing_enabled": False, "processing_scope": "per_device"},
        ),
        (
            [{key: value for key, value in manual_worker().items() if key != "max_processing_percent"}],
            {"sharing_enabled": False},
        ),
    ],
)
def test_ambiguous_ownership_snapshot_and_save_fail_closed(runtime_factory, workers, policy):
    runtime = runtime_factory(workers=workers, policy=policy)
    state = snapshot(runtime)
    original = runtime.path.read_bytes()
    assert state["editable"] is False
    with TestClient(app_for(runtime, lambda: pytest.fail("ambiguous ownership requested restart"))) as client:
        result = client.put(URL, json=draft(state, set()), headers=CONTROL)
        assert result.status_code == 422
        assert_unchanged(runtime, original, state["config_revision"])


def test_worker_status_marker_comes_from_saved_provenance_not_id(runtime_factory):
    runtime = runtime_factory(workers=[managed_worker(), manual_worker(device="cuda:1", identity_path="manual.key")])
    with TestClient(app_for(runtime, lambda: None)) as client:
        workers = client.get("/control/v1/status", headers=CONTROL).json()["contribution"]["workers"]
    by_id = {worker["id"]: worker for worker in workers}
    assert by_id["card-zero"]["managed_by"] == "desktop_gpu"
    assert "managed_by" not in by_id["worker-looks-managed"]


@pytest.mark.parametrize("busy", ["unpaused", "inference"])
def test_busy_runtime_is_not_interrupted_or_reconfigured(runtime_factory, busy):
    runtime = runtime_factory(workers=[managed_worker()], paused=busy != "unpaused")
    state = snapshot(runtime)
    original = runtime.path.read_bytes()
    request = draft(state)
    request["rows"][0]["max_processing_percent"] = 50
    lease = runtime.manager.load("model") if busy == "inference" else None
    try:
        assert snapshot(runtime)["editable"] is False
        with TestClient(app_for(runtime, lambda: pytest.fail("busy runtime requested reload"))) as client:
            result = client.put(URL, json=request, headers=CONTROL)
            assert result.status_code == 409
        assert runtime.path.read_bytes() == original
        assert runtime.supervisor.configuration_restart_pending is False
        if lease is not None:
            assert lease.runtime is not None and runtime.manager.snapshots()[0].active_requests == 1
    finally:
        if lease is not None:
            lease.release()


@pytest.mark.parametrize("failure", ["enrollment", "preparation", "persistence"])
def test_failed_commit_preserves_disk_and_admission_and_sanitizes_error(runtime_factory, monkeypatch, failure):
    runtime = runtime_factory()
    state = snapshot(runtime)
    original = runtime.path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError(f"private {runtime.path} {IDENTITIES['cuda:0']}")

    if failure == "enrollment":
        monkeypatch.setattr(store_module, "enroll_selection", fail)
    elif failure == "preparation":
        monkeypatch.setattr(runtime.store, "_prepare", fail)
    else:
        monkeypatch.setattr(store_module, "_exchange_paths", fail)
    with TestClient(app_for(runtime, lambda: pytest.fail("failed commit requested restart"))) as client:
        result = client.put(URL, json=draft(state, {"cuda:0"}), headers=CONTROL)
        assert result.status_code == 503
        assert "GPU-" not in result.text and str(runtime.path.parent) not in result.text
        assert_unchanged(runtime, original, state["config_revision"])


def test_failed_reload_signal_leaves_truthful_saved_pending_state(runtime_factory):
    runtime = runtime_factory()

    def fail():
        raise RuntimeError("reload signal failed")

    with TestClient(app_for(runtime, fail)) as client:
        result = client.put(URL, json=draft(snapshot(runtime), {"cuda:0"}), headers=CONTROL)
        assert result.status_code == 503 and "was saved" in result.json()["detail"]
        state = client.get(URL, headers=CONTROL).json()
        assert state["restart_required"] is True and state["runtime_ready"] is False and state["editable"] is False
    assert len(NodeConfig.load(runtime.path).workers) == 1
    with pytest.raises(ModelManagerClosedError):
        runtime.manager.load("model")


def test_cancelled_http_await_still_signals_the_durable_save(runtime_factory, monkeypatch):
    runtime = runtime_factory()
    request = draft(snapshot(runtime), {"cuda:0"})
    entered, finish, restarted = threading.Event(), threading.Event(), threading.Event()
    original = runtime.store._atomic_replace

    def blocked(*args, **kwargs):
        entered.set()
        assert finish.wait(5)
        original(*args, **kwargs)

    monkeypatch.setattr(runtime.store, "_atomic_replace", blocked)

    async def scenario():
        task = asyncio.create_task(
            _commit_gpu_selection(runtime.store, request, runtime.manager, runtime.hardware, restarted.set)
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
    assert runtime.supervisor.configuration_restart_pending is True
    assert NodeConfig.load(runtime.path).workers[0].enabled is False
