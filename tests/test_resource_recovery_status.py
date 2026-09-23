"""Cached recovery API, strict desktop codec and honest sharing controls."""

import copy
import io
import json
import threading
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from communityai_desktop.acceptance import _FakeNodeState
from communityai_desktop.client import NodeClient, NodeClientError, _normalize_contribution_status
from communityai_desktop.controller import DesktopController
from communityai_desktop.presentation import recovery_reason, sharing_summary
from fastapi.testclient import TestClient

from drift.node.model_manager import ModelManager
from drift.node.server import _contribution_status, create_node_app

CHECKING = {"state": "checking", "reason": "checking", "retryable": True}
READY = {"state": "ready", "reason": "none", "retryable": False}
BLOCKED = {"state": "blocked", "reason": "unverifiable_state", "retryable": False}


def contribution(recovery=None, *, enabled=False, workers=True):
    fixture = _FakeNodeState()
    fixture.policy["sharing_enabled"] = enabled
    result = _contribution_status(
        [
            {
                "id": "worker",
                "model": "model",
                "state": "paused",
                "desired_running": False,
                "operator_paused": True,
                "automatic": True,
                "policy_admitted": True,
                "schedule_admitted": True,
                "resource_admitted": True,
            }
        ]
        if workers
        else [],
        configured=True,
        editable=True,
        policy_snapshot=fixture.policy_response(),
    )
    if recovery is not None:
        result["recovery"] = copy.deepcopy(recovery)
    return result


@pytest.mark.parametrize(
    "reason", ["active_owner", "cleanup_pending", "legacy_state", "unverifiable_state", "unsupported_platform"]
)
def test_blocked_recovery_remains_visible_while_off_without_workers(reason):
    recovery = {**BLOCKED, "reason": reason}
    decoded = _normalize_contribution_status(contribution(recovery, workers=False))
    view = DesktopController._contribution_view(decoded, [])
    title, detail, state = sharing_summary({"contribution": view, "workers": []})
    assert (title, state) == ("Sharing is off", "off")
    assert detail == recovery_reason(recovery) and detail
    assert not view["intent_enabled"] and not view["enabled"]
    assert not view["can_start"] and not view["can_pause"] and view["editable"]
    assert not any(word in detail.casefold() for word in ("delete", "increase", "choose pause", "restart to"))
    if reason == "unsupported_platform":
        assert "current system session" in detail
        assert "start or recover sharing" in detail
        assert "earlier sharing" not in detail
        assert "not supported on this system" not in detail
    if reason == "unverifiable_state":
        assert "cannot verify that sharing can start or recover safely" in detail
        assert "current system session" in detail and "earlier sharing" not in detail


def test_checking_and_ready_do_not_change_saved_pause_or_create_start_intent():
    source = contribution(CHECKING, enabled=True)
    workers = [DesktopController._worker_view(w) for w in _normalize_contribution_status(source)["workers"]]
    checking = DesktopController._contribution_view(_normalize_contribution_status(source), workers)
    assert sharing_summary({"contribution": checking, "workers": workers}) == (
        "Sharing is paused",
        recovery_reason(CHECKING),
        "paused",
    )
    assert checking["can_pause"] and not checking["can_start"]
    source["recovery"] = READY
    ready = DesktopController._contribution_view(_normalize_contribution_status(source), workers)
    assert ready["can_start"] and not ready["enabled"] and ready["selected_models"] == []
    assert sharing_summary({"contribution": ready, "workers": workers}) == ("Sharing is paused", "", "paused")


@pytest.mark.parametrize("recovery", [CHECKING, BLOCKED])
@pytest.mark.parametrize("action", ["master", "start", "restart", "selected"])
def test_start_is_rejected_before_any_policy_or_worker_mutation(recovery, action):
    class Client:
        def status(self):
            return {"contribution": contribution(recovery)}

        def worker_action(self, *args, **kwargs):
            raise AssertionError("worker mutation before recovery ready")

        def update_contribution_policy(self, *args, **kwargs):
            raise AssertionError("policy mutation before recovery ready")

    controller = DesktopController(Client())
    with pytest.raises(NodeClientError) as error:
        if action == "master":
            controller.set_sharing_enabled(True)
        elif action == "selected":
            controller.set_workers_enabled(["worker"], True)
        else:
            controller.worker_action("worker", action)
    assert str(error.value) == recovery_reason(recovery)


def test_recovery_does_not_prevent_explicit_pause_and_persisted_off():
    saved = contribution(BLOCKED, enabled=True)
    actions = []

    def pause(worker, action):
        actions.append(action)
        return {"worker": {"state": "paused"}}

    def persist(policy, *, expected_revision):
        assert expected_revision == saved["policy"]["config_revision"]
        actions.append("save")
        assert policy["sharing_enabled"] is False
        return {"policy": policy, "config_revision": expected_revision}

    controller = DesktopController(
        SimpleNamespace(status=lambda: {"contribution": saved}, worker_action=pause, update_contribution_policy=persist)
    )
    result = controller.set_sharing_enabled(False)
    assert actions == ["pause", "save"] and result["policy"]["sharing_enabled"] is False
    assert "recovered" not in result["message"].casefold()


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {**READY, "state": []},
        {**READY, "reason": "legacy_state"},
        {**CHECKING, "reason": "none"},
        {**BLOCKED, "reason": "private path"},
        {**READY, "retryable": 1},
        {**READY, "token": "private token"},
        {"state": "ready", "reason": "none"},
    ],
)
def test_client_rejects_malformed_or_incoherent_optional_recovery(value):
    source = contribution()
    source["recovery"] = value
    with pytest.raises(NodeClientError, match="recovery status is invalid"):
        _normalize_contribution_status(source)


def test_legacy_without_recovery_is_unchanged():
    decoded = _normalize_contribution_status(contribution())
    assert "recovery" not in decoded
    workers = [DesktopController._worker_view(w) for w in decoded["workers"]]
    view = DesktopController._contribution_view(decoded, workers)
    assert "recovery" not in view and view["can_start"]


def test_live_control_api_and_real_codec_use_cached_recovery_during_background_work():
    cached, calls = [dict(CHECKING)], []
    release, entered = threading.Event(), threading.Event()

    def recovery_work():
        entered.set()
        assert release.wait(10)

    background = threading.Thread(target=recovery_work, daemon=True)
    background.start()
    assert entered.wait(2)

    def provider():
        calls.append(1)
        return dict(cached[0])

    manager = ModelManager()
    app = create_node_app(manager, api_keys=["client"], control_keys=["control"], resource_recovery_status=provider)
    try:
        with TestClient(app) as api:
            node = NodeClient("http://127.0.0.1:8080", "control")

            def request(request, timeout):
                response = api.request(
                    request.get_method(), urlsplit(request.full_url).path, headers=dict(request.header_items())
                )
                assert response.status_code == 200
                assert "private" not in response.text
                return io.BytesIO(response.content)

            node._opener = SimpleNamespace(open=request)
            first = node.status()
            assert first["status"] == "running" and first["contribution"]["recovery"] == CHECKING
            assert background.is_alive() and not release.is_set()
            cached[0] = READY
            assert node.status()["contribution"]["recovery"] == READY
            assert len(calls) == 2
    finally:
        release.set()
        background.join(2)
        manager.shutdown()


@pytest.mark.parametrize("malformed", [True, False])
def test_provider_failure_keeps_api_healthy_and_exposes_only_fixed_blocked_status(malformed):
    def provider():
        if malformed:
            return {**READY, "private_path": "private token"}
        raise RuntimeError("private path private token")

    manager = ModelManager()
    try:
        app = create_node_app(manager, api_keys=["client"], control_keys=["control"], resource_recovery_status=provider)
        with TestClient(app) as api:
            response = api.get("/control/v1/status", headers={"Authorization": "Bearer control"})
            assert response.status_code == 200 and response.json()["status"] == "running"
            assert response.json()["contribution"]["recovery"] == BLOCKED
            assert "private" not in json.dumps(response.json())
    finally:
        manager.shutdown()
