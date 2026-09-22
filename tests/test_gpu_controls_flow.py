"""Real desktop-to-node control flow with in-process transport and fake hardware.

The controller, client JSON/auth/error handling, FastAPI routes, policy persistence,
device pins, supervisor and model manager are real. Inventory/UUIDs and the urllib
transport are test doubles; no network socket, model, GPU or eight-worker runtime
is qualified. A fresh application context exercises paused config reconstruction.
"""

import copy
import io
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop" / "src"))

from communityai_desktop.client import NodeApiError, NodeClient
from communityai_desktop.controller import DesktopController, GpuSelectionDraft

from drift.node import device_binding
from drift.node.config import NodeConfig
from drift.node.gpu_selection_tokens import GpuSelectionTokens
from drift.node.keys import ApiKeyStore
from drift.node.model_manager import ModelDescriptor, ModelManager, ModelManagerClosedError, ModelRuntime
from drift.node.policy_store import ContributionPolicyStore
from drift.node.server import create_node_app
from drift.node.worker_supervisor import WorkerLaunch, WorkerSupervisor, WorkerSupervisorSettings

GPU_URL = "/control/v1/contribution-gpu-selection"
CONTROL_KEY = "flow-control-fixture"
GIB = 1024**3


class TestClientOpener:
    """Only replace transport; every response comes from an actual FastAPI route."""

    __test__ = False

    def __init__(self, client):
        self.client = client
        self.calls = []

    def open(self, request, *, timeout):
        path = urlsplit(request.full_url).path
        headers = dict(request.header_items())
        response = self.client.request(request.get_method(), path, headers=headers, content=request.data)
        self.calls.append(
            {
                "method": request.get_method(),
                "path": path,
                "headers": headers,
                "body": None if request.data is None else json.loads(request.data),
                "status": response.status_code,
            }
        )
        if response.status_code >= 400:
            raise HTTPError(
                request.full_url,
                response.status_code,
                response.reason_phrase,
                response.headers,
                io.BytesIO(response.content),
            )
        return io.BytesIO(response.content)


def hardware_view(policy):
    return {
        "cpu_name": "CPU fixture",
        "device": "cpu",
        "selected_device": "cpu",
        "device_status": "available",
        "gpu_name": None,
        "gpu_device": None,
        "gpu_total_bytes": None,
        "sharing_vram_bytes": None,
        "sharing_vram_available_bytes": None,
        "sharing_vram_scope": "per_device",
        "sharing_vram_kind": "capacity",
        "processing_percent": policy["max_processing_percent"],
        "gpus": [
            {
                "device": f"cuda:{index}",
                "name": "H100 fixture",
                "total_bytes": 80 * GIB,
                "status": "available",
                "sharing_vram_bytes": 32 * GIB,
                "sharing_vram_available_bytes": 80 * GIB,
            }
            for index in range(8)
        ],
        "gpu_backends": {
            "cuda": {"status": "available", "visible_count": 8},
            "xpu": {"status": "unsupported", "visible_count": None},
            "mps": {"status": "unsupported", "visible_count": None},
        },
        "gpu_inventory_limit": 16,
        "gpu_visible_count": 8,
        "gpu_inventory_status": "available",
    }


@pytest.fixture
def open_flow(tmp_path, monkeypatch):
    """Reopen this private configuration without importing another test's fixtures."""
    path = tmp_path / "node.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "models": [{"manifest": "manifest.json", "initial_peers": ["/ip4/127.0.0.1/tcp/31337/p2p/example"]}],
                "contribution_policy": {
                    "sharing_enabled": False,
                    "processing_scope": "per_device",
                    "max_processing_percent": 100,
                    "max_disk_space": "20GiB",
                    "max_vram": "40%",
                },
                "workers": [],
            }
        ),
        encoding="utf-8",
    )
    identities = {f"cuda:{index}": f"GPU-00000000-0000-0000-0000-{index + 1:012x}" for index in range(8)}
    monkeypatch.setattr(device_binding, "_cuda_identity", identities.get)
    monkeypatch.setattr(device_binding, "_DEFAULT_LIVENESS_PROBE", lambda physical: physical in identities.values())

    @contextmanager
    def open_runtime():
        def prepare(config):
            return WorkerSupervisorSettings(
                launches=tuple(
                    WorkerLaunch(
                        item.worker_id,
                        item.model,
                        (sys.executable, "-c", "pass"),
                        auto_start=item.enabled,
                        auto_restart=False,
                        device=item.device,
                    )
                    for item in config.workers
                ),
                stop_timeout=1,
            )

        configured = NodeConfig.load(path)
        settings = prepare(configured)
        supervisor = WorkerSupervisor(settings.launches)
        initial_workers = copy.deepcopy(supervisor.snapshots())
        for item in configured.workers:
            supervisor.pause_worker(item.worker_id)
        manager = ModelManager()
        manager.register(ModelDescriptor("flow-model"), lambda: ModelRuntime(object(), object()))
        store = ContributionPolicyStore(path, supervisor, prepare, expected_config=configured)
        store._gpu_selection_tokens = GpuSelectionTokens(
            devices=lambda: tuple(identities),
            identity=identities.get,
            live=lambda physical: physical in identities.values(),
        )
        restarts = []
        app = create_node_app(
            manager,
            api_key_store=ApiKeyStore(tmp_path / "api-keys.json"),
            control_keys=[CONTROL_KEY],
            worker_supervisor=supervisor,
            contribution_policy_store=store,
            hardware_status=hardware_view,
            request_restart=lambda: restarts.append(json.loads(path.read_bytes())),
        )
        try:
            with TestClient(app) as transport:
                bridge = TestClientOpener(transport)
                client = NodeClient("http://127.0.0.1:8080", CONTROL_KEY)
                client._opener = bridge
                yield SimpleNamespace(
                    path=path,
                    identities=identities,
                    manager=manager,
                    supervisor=supervisor,
                    store=store,
                    client=client,
                    controller=DesktopController(client),
                    bridge=bridge,
                    restarts=restarts,
                    settings=settings,
                    initial_workers=initial_workers,
                )
        finally:
            supervisor.shutdown()
            manager.shutdown()

    return open_runtime


def draft_rows(snapshot, selected):
    context = GpuSelectionDraft()
    context.observe(snapshot)
    rows = [{**row, "selected": row["device"] in selected} for row in snapshot["rows"]]
    context.set_dirty(rows != snapshot["rows"])
    return context.request_rows(rows, snapshot["config_revision"])


def assert_no_start(flow):
    assert not any(
        call["method"] == "POST" and call["path"].endswith(("/start", "/restart")) for call in flow.bridge.calls
    )
    assert all(not item["desired_running"] and item["pid"] is None for item in flow.supervisor.snapshots())


def test_controller_snapshot_renders_eight_distinct_tokenized_cards_without_private_ids(open_flow):
    with open_flow() as flow:
        view = flow.controller.snapshot()
        selection = view["gpu_selection"]
        assert selection["editable"] and not selection["restart_required"]
        assert len(selection["inventory"]) == len(selection["rows"]) == 8
        assert len({row["selection_token"] for row in selection["inventory"]}) == 8
        assert {row["device"] for row in selection["inventory"]} == set(flow.identities)
        assert {row["name"] for row in selection["inventory"]} == {"H100 fixture"}
        assert all(row["status"] == "available" for row in selection["inventory"])
        assert not any(row["selected"] for row in selection["rows"])
        encoded = json.dumps(view)
        assert all(identity not in encoded for identity in flow.identities.values())
        assert "identity_path" not in encoded and str(flow.path.parent) not in encoded
        assert all(call["headers"].get("Authorization") == f"Bearer {CONTROL_KEY}" for call in flow.bridge.calls)
        invalid_client = NodeClient("http://127.0.0.1:8080", "wrong-key")
        invalid_client._opener = flow.bridge
        with pytest.raises(NodeApiError) as error:
            invalid_client.get_gpu_selection()
        assert error.value.status_code == 401
        assert flow.restarts == []


def test_one_card_save_is_durable_before_reload_and_reopens_paused(open_flow):
    with open_flow() as flow:
        selection = flow.controller.snapshot()["gpu_selection"]
        rows = draft_rows(selection, {"cuda:3"})
        chosen = next(row for row in rows if row["selected"])
        chosen.update(max_vram="75%", max_processing_percent=37.5)
        result = flow.controller.update_gpu_selection(rows, expected_revision=selection["config_revision"])
        saved = json.loads(flow.path.read_bytes())
        assert flow.bridge.calls[-1]["status"] == 202
        assert result["restart_required"] and not result["runtime_ready"]
        assert flow.restarts == [saved]
        assert saved["contribution_policy"]["sharing_enabled"] is False
        assert saved["contribution_policy"]["max_vram"] == "40%"
        assert len(saved["workers"]) == 1
        worker = saved["workers"][0]
        worker_id = worker["id"]
        assert worker["device"] == "cuda:3" and worker["enabled"] is False
        assert worker["max_vram"] == "75%" and worker["max_processing_percent"] == 37.5
        assert flow.supervisor.configuration_restart_pending
        with pytest.raises(ModelManagerClosedError):
            flow.manager.load("flow-model")
        assert_no_start(flow)
    with open_flow() as reloaded:
        assert all(not launch.auto_start for launch in reloaded.settings.launches)
        assert all(not item["desired_running"] and item["pid"] is None for item in reloaded.initial_workers)
        assert all(item["state"] == "paused" for item in reloaded.supervisor.snapshots())
        view = reloaded.controller.snapshot()
        selection = view["gpu_selection"]
        assert not selection["restart_required"] and selection["editable"]
        assert [row["device"] for row in selection["rows"] if row["selected"]] == ["cuda:3"]
        assert view["workers"][0]["id"] == worker_id
        assert_no_start(reloaded)


@pytest.mark.parametrize("count", [2, 8])
def test_controller_multi_card_save_rejects_without_disk_change_or_start(open_flow, count):
    with open_flow() as flow:
        selection = flow.controller.snapshot()["gpu_selection"]
        before = flow.path.read_bytes()
        rows = draft_rows(selection, {f"cuda:{index}" for index in range(count)})
        with pytest.raises(NodeApiError) as error:
            flow.controller.update_gpu_selection(rows, expected_revision=selection["config_revision"])
        assert error.value.status_code == 422
        assert "joint automatic GPU runtime" in error.value.detail
        assert flow.path.read_bytes() == before and flow.restarts == []
        assert not flow.supervisor.configuration_restart_pending and not flow.manager.closed
        assert not (flow.path.parent / "worker-identities").exists()
        assert_no_start(flow)


@pytest.mark.parametrize("change,expected_status", [("revision", 412), ("physical_card", 409)])
def test_controller_keeps_original_token_context_and_rejects_stale_save(open_flow, change, expected_status):
    with open_flow() as flow:
        selection = flow.controller.snapshot()["gpu_selection"]
        rows = draft_rows(selection, {"cuda:0"})
        original_token = next(row["selection_token"] for row in rows if row["selected"])
        if change == "revision":
            policy = flow.client.get_contribution_policy()
            flow.client.update_contribution_policy(
                {**policy["policy"], "pause_timeout": 11}, expected_revision=policy["config_revision"]
            )
        else:
            flow.identities["cuda:0"] = "GPU-00000000-0000-0000-0000-000000000099"
        before = flow.path.read_bytes()
        calls_before = len(flow.bridge.calls)
        with pytest.raises(NodeApiError) as error:
            flow.controller.update_gpu_selection(rows, expected_revision=selection["config_revision"])
        assert error.value.status_code == expected_status
        attempted = flow.bridge.calls[calls_before:]
        assert [(call["method"], call["path"]) for call in attempted] == [("PUT", GPU_URL)]
        assert next(row["selection_token"] for row in attempted[0]["body"]["rows"] if row["selected"]) == original_token
        assert flow.path.read_bytes() == before and flow.restarts == []
        assert not flow.supervisor.configuration_restart_pending and not flow.manager.closed
        assert_no_start(flow)


def test_unchanged_retained_card_returns_200_without_reload(open_flow):
    with open_flow() as flow:
        selection = flow.controller.snapshot()["gpu_selection"]
        flow.controller.update_gpu_selection(
            draft_rows(selection, {"cuda:0"}), expected_revision=selection["config_revision"]
        )
    with open_flow() as flow:
        selection = flow.controller.snapshot()["gpu_selection"]
        before = flow.path.read_bytes()
        result = flow.controller.update_gpu_selection(
            draft_rows(selection, {"cuda:0"}), expected_revision=selection["config_revision"]
        )
        assert flow.bridge.calls[-1]["status"] == 200
        assert not result["restart_required"] and result["config_revision"] == selection["config_revision"]
        assert flow.path.read_bytes() == before and flow.restarts == []
        assert not flow.supervisor.configuration_restart_pending and not flow.manager.closed
        assert_no_start(flow)
