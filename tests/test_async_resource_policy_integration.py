"""Real policy persistence/API with controlled async callbacks; no GPU or model.

The claim-bearing CPU launch is a supervisor lifecycle seam. Production managed
CUDA launch eligibility, physical resources, and journal I/O are tested elsewhere.
"""

import io
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest
from communityai_desktop.client import NodeClient
from communityai_desktop.controller import DesktopController
from fastapi.testclient import TestClient
from test_policy_store import _config_document

from drift.node.config import NodeConfig
from drift.node.model_manager import ModelManager
from drift.node.placement_resources import WorkerResourceClaim
from drift.node.policy_store import ContributionPolicyPersistenceError, ContributionPolicyStore
from drift.node.server import create_node_app
from drift.node.worker_supervisor import WorkerLaunch, WorkerPolicyError, WorkerSupervisor, WorkerSupervisorSettings

CONTROL = {"Authorization": "Bearer async-policy-control"}
POLICY_URL = "/control/v1/contribution-policy"


def _wait(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.01)
    raise AssertionError("asynchronous resource operation did not drain")


@pytest.fixture
def runtime(tmp_path):
    document = _config_document(sharing_enabled=True)
    document["contribution_policy"]["max_host_memory"] = "1GiB"
    path = tmp_path / "node.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    acquired, released, spawned, prepared = [], [], [], []
    acquiring, allow_acquire = threading.Event(), threading.Event()
    releasing, allow_release = threading.Event(), threading.Event()

    def prepare(config):
        prepared.append(config)
        policy = config.contribution_policy
        return WorkerSupervisorSettings(
            launches=(
                WorkerLaunch(
                    "worker",
                    "model",
                    (sys.executable, "-c", "raise AssertionError('unexpected child')"),
                    auto_restart=True,
                    restart_backoff=0.01,
                    policy_admitted=policy.sharing_enabled,
                    policy_reason=None if policy.sharing_enabled else "sharing is disabled by contribution policy",
                    resource_claim=WorkerResourceClaim("template", "worker", 10, 20),
                    max_host_memory_bytes=policy.max_host_memory_bytes,
                    max_disk_bytes=policy.max_disk_bytes,
                ),
            ),
            stop_timeout=0.1,
        )

    def acquire(launch, cancel):
        acquired.append((launch, cancel))
        acquiring.set()
        assert allow_acquire.wait(10), "fixture must release acquisition"
        # Deliberately return a token after cancellation: ownership must drain.
        return "private-async-generation"

    def release(token):
        releasing.set()
        assert allow_release.wait(10), "fixture must release cleanup"
        released.append(token)

    def popen(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("cancelled policy operation must never create a child")

    initial = prepare(NodeConfig.load(path))
    supervisor = WorkerSupervisor(
        initial.launches,
        acquire_resources_cancellable=acquire,
        release_resources=release,
        popen=popen,
        stop_timeout=0.1,
        poll_period=0.01,
    )
    store = ContributionPolicyStore(path, supervisor, prepare)
    manager = ModelManager()
    app = create_node_app(
        manager,
        api_keys=["async-policy-client"],
        control_keys=["async-policy-control"],
        worker_supervisor=supervisor,
        contribution_policy_store=store,
    )
    pool = ThreadPoolExecutor(max_workers=3)
    supervisor.start_service()
    try:
        with TestClient(app) as client:
            value = SimpleNamespace(**locals())
            value.call = lambda fn, *args, **kwargs: pool.submit(fn, *args, **kwargs).result(timeout=2)
            try:
                yield value
            finally:
                # Open gates before waiting on client lifespan or failed futures.
                allow_acquire.set()
                allow_release.set()
                supervisor.pause_worker("worker")
                _wait(lambda: supervisor.snapshot("worker")["resource_operation"] is None)
    finally:
        supervisor.shutdown()
        manager.shutdown()
        pool.shutdown(wait=True)


def _action(runtime, action):
    return runtime.call(runtime.client.post, f"/control/v1/workers/worker/{action}", headers=CONTROL)


def _save(runtime, snapshot, **changes):
    return runtime.call(
        runtime.client.put,
        POLICY_URL,
        headers=CONTROL,
        json={
            "schema_version": 1,
            "expected_config_revision": snapshot["config_revision"],
            "policy": {**snapshot["policy"], **changes},
        },
    )


def _blocked(runtime, phase, *, pause=True):
    assert _action(runtime, "start").status_code == 200
    assert runtime.acquiring.wait(2)
    if pause:
        response = _action(runtime, "pause")
        assert response.status_code == 200
        assert response.json()["worker"]["resource_cancel_requested"] is True
        assert runtime.acquired[0][1].is_set()
    if phase == "release":
        runtime.allow_acquire.set()
        assert runtime.releasing.wait(2)
    assert runtime.supervisor.snapshot("worker")["resource_operation"] == phase


@pytest.mark.parametrize("phase", ["acquire", "release"])
def test_master_pause_persists_while_cancelled_work_drains_and_reboot_stays_disabled(runtime, phase):
    before = runtime.store.snapshot()
    _blocked(runtime, phase)
    old_launch = runtime.supervisor.launches[0]
    preparations = len(runtime.prepared)

    response = _save(runtime, before, sharing_enabled=False)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["config_revision"] != before["config_revision"]
    assert saved["policy"]["sharing_enabled"] is False
    assert runtime.call(runtime.store.snapshot) == saved
    assert runtime.call(runtime.client.get, "/control/v1/status", headers=CONTROL).status_code == 200
    assert len(runtime.prepared) == preparations
    assert runtime.supervisor.launches[0] is old_launch
    pending = runtime.supervisor.snapshot("worker")
    assert pending["resource_operation"] == phase
    assert pending["operator_paused"] and not pending["desired_running"]
    assert pending["policy_admitted"] is False
    assert pending["cleanup_pending"] and pending["pid"] is None
    assert "private-async-generation" not in json.dumps(pending)
    assert _action(runtime, "start").status_code == 409
    assert _action(runtime, "restart").status_code == 409

    loaded = NodeConfig.load(runtime.path)
    assert loaded.contribution_policy.sharing_enabled is False
    rebooted = WorkerSupervisor(runtime.prepare(loaded).launches)
    try:
        rebooted.start_service()
        assert rebooted.snapshot("worker")["policy_admitted"] is False
        with pytest.raises(WorkerPolicyError, match="sharing is disabled"):
            rebooted.start_worker("worker")
    finally:
        rebooted.shutdown()

    runtime.allow_acquire.set()
    runtime.allow_release.set()
    _wait(lambda: runtime.supervisor.snapshot("worker")["resource_operation"] is None)
    assert runtime.released == ["private-async-generation"]
    assert len(runtime.acquired) == 1 and not runtime.spawned
    assert runtime.supervisor.snapshot("worker")["policy_admitted"] is False
    assert _action(runtime, "start").status_code == 409

    # Re-enabling uses ordinary preparation/reconfigure only once cleanup is done.
    response = _save(runtime, saved, sharing_enabled=True)
    assert response.status_code == 200, response.text
    assert runtime.supervisor.snapshot("worker")["policy_admitted"] is True
    assert runtime.supervisor.launches[0] is not old_launch
    assert not runtime.supervisor.snapshot("worker")["desired_running"]
    assert not runtime.spawned


@pytest.mark.parametrize("phase", ["acquire", "release"])
@pytest.mark.parametrize("changes", [{"max_host_memory": "2GiB"}, {"max_disk_space": "21GiB"}])
def test_mixed_disable_and_resource_edit_is_busy_and_changes_nothing(runtime, phase, changes):
    before = runtime.store.snapshot()
    original = runtime.path.read_bytes()
    _blocked(runtime, phase)
    launch = runtime.supervisor.launches[0]
    response = _save(runtime, before, sharing_enabled=False, **changes)
    assert response.status_code == 409, response.text
    assert runtime.path.read_bytes() == original
    assert runtime.store.snapshot() == before
    assert runtime.supervisor.launches[0] is launch
    assert runtime.supervisor.snapshot("worker")["policy_admitted"] is True
    assert runtime.supervisor.snapshot("worker")["resource_operation"] == phase
    assert not runtime.spawned


def test_persistent_pause_requires_actual_operator_pause_before_writing(runtime):
    before = runtime.store.snapshot()
    original = runtime.path.read_bytes()
    _blocked(runtime, "acquire", pause=False)
    response = _save(runtime, before, sharing_enabled=False)
    assert response.status_code == 409
    assert runtime.path.read_bytes() == original
    assert runtime.store.snapshot() == before
    assert not runtime.acquired[0][1].is_set()
    assert runtime.supervisor.snapshot("worker")["desired_running"] is True


@pytest.mark.parametrize("external_change", [False, True])
def test_stale_persistent_pause_does_not_latch_or_publish_policy(runtime, external_change):
    before = runtime.store.snapshot()
    _blocked(runtime, "acquire")
    expected = dict(before)
    if external_change:
        runtime.path.write_bytes(runtime.path.read_bytes() + b"\n")
    else:
        expected["config_revision"] = "sha256:" + "0" * 64
    original = runtime.path.read_bytes()
    response = _save(runtime, expected, sharing_enabled=False)
    assert response.status_code == 412
    assert runtime.path.read_bytes() == original
    assert runtime.store.snapshot() == before
    assert runtime.supervisor.snapshot("worker")["policy_admitted"] is True
    assert runtime.supervisor.snapshot("worker")["resource_cancel_requested"] is True


def test_persistence_error_retains_cancelled_operation_and_allows_truthful_retry(runtime, monkeypatch):
    before = runtime.store.snapshot()
    original = runtime.path.read_bytes()
    _blocked(runtime, "release")
    persist = runtime.store._atomic_replace

    def fail(*args, **kwargs):
        raise ContributionPolicyPersistenceError("policy persistence failed")

    monkeypatch.setattr(runtime.store, "_atomic_replace", fail)
    with pytest.raises(ContributionPolicyPersistenceError, match="persistence failed"):
        runtime.call(
            runtime.store.update,
            {**before["policy"], "sharing_enabled": False},
            expected_revision=before["config_revision"],
        )
    assert runtime.path.read_bytes() == original
    assert runtime.store.snapshot() == before
    assert runtime.supervisor.snapshot("worker")["policy_admitted"] is True
    assert runtime.supervisor.snapshot("worker")["resource_operation"] == "release"
    monkeypatch.setattr(runtime.store, "_atomic_replace", persist)
    assert _save(runtime, before, sharing_enabled=False).status_code == 200
    assert runtime.supervisor.snapshot("worker")["policy_admitted"] is False


def _desktop(runtime):
    """Real desktop codec/controller over the real local ASGI control API."""

    def open_request(request, timeout):
        response = runtime.client.request(
            request.get_method(),
            urlsplit(request.full_url).path,
            headers=dict(request.header_items()),
            content=request.data,
        )
        if response.status_code >= 400:
            raise HTTPError(request.full_url, response.status_code, "API error", {}, io.BytesIO(response.content))
        return io.BytesIO(response.content)

    client = NodeClient("http://127.0.0.1:8080", "async-policy-control")
    client._opener = SimpleNamespace(open=open_request)
    return DesktopController(client)


def _sharing_view(desktop):
    contribution = desktop.client.status()["contribution"]
    return desktop._contribution_view(
        contribution, [desktop._worker_view(worker) for worker in contribution["workers"]]
    )


@pytest.mark.parametrize("phase", ["acquire", "release"])
def test_real_desktop_pause_saves_off_and_keeps_retry_available_during_drain(runtime, phase):
    _blocked(runtime, phase)
    desktop = _desktop(runtime)
    response = runtime.call(desktop.set_sharing_enabled, False)
    assert response["policy"]["sharing_enabled"] is False
    assert response["message"] == "Sharing is saved off. Cleanup is still pending; use Pause to retry."
    state = runtime.call(_sharing_view, desktop)
    assert state["can_pause"] is True
    assert state["intent_enabled"] is False
    # A retry does not require false-to-false policy reconfiguration during drain.
    assert runtime.call(desktop.set_sharing_enabled, False)["config_revision"] == response["config_revision"]
    assert runtime.supervisor.snapshot("worker")["resource_operation"] == phase
    assert not runtime.spawned


def test_real_desktop_persists_off_after_cleanup_error_but_keeps_error_visible(runtime, monkeypatch):
    _blocked(runtime, "acquire")
    original_pause = runtime.supervisor.pause_worker

    def failed_cleanup(worker):
        original_pause(worker)
        raise RuntimeError("worker resource release is incomplete; retry cleanup")

    # Exercise the actual API's503 branch after stopped intent has been recorded.
    # The callback remains blocked, so no test claims cleanup was completed.
    monkeypatch.setattr(runtime.supervisor, "pause_worker", failed_cleanup)
    try:
        desktop = _desktop(runtime)
        result = runtime.call(desktop.set_sharing_enabled, False)
        assert result["policy"]["sharing_enabled"] is False
        assert "Cleanup is still pending" in result["message"]
        assert NodeConfig.load(runtime.path).contribution_policy.sharing_enabled is False
        assert runtime.call(_sharing_view, desktop)["can_pause"] is True
        assert not runtime.spawned
    finally:
        monkeypatch.setattr(runtime.supervisor, "pause_worker", original_pause)
