"""Bounded control API and desktop readiness, without models or physical devices."""

import copy
import io
import json
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from communityai_desktop.acceptance import _FakeNodeState
from communityai_desktop.client import NodeClient, NodeClientError, _normalize_contribution_status
from communityai_desktop.controller import DesktopController
from communityai_desktop.presentation import sharing_reason, sharing_summary
from fastapi.testclient import TestClient

from drift.node.model_manager import ModelManager
from drift.node.server import _contribution_status, create_node_app

FAILED = "worker loading acknowledgement failed; choose Start to retry after cleanup"


def raw(**changes):
    return {
        "id": "worker",
        "model": "model",
        "state": "running",
        "desired_running": True,
        "operator_paused": False,
        "policy_admitted": True,
        "schedule_admitted": True,
        "resource_admitted": True,
        "resource_suspended": False,
        "automatic": False,
        "load_state": None,
        "model_ready": False,
        **changes,
    }


def projected(snapshot):
    fixture = _FakeNodeState()
    fixture.policy["sharing_enabled"] = True
    return _contribution_status((snapshot,), configured=True, editable=True, policy_snapshot=fixture.policy_response())


@pytest.mark.parametrize(
    "state,ready,active",
    [
        ("waiting", False, False),
        ("loading", False, False),
        ("ready", False, False),
        ("ready", True, True),
        ("failed", False, False),
    ],
)
def test_live_process_only_becomes_sharing_after_current_ready_ack(state, ready, active):
    source = projected(raw(load_state=state, model_ready=ready))
    decoded = _normalize_contribution_status(source)
    worker = DesktopController._worker_view(decoded["workers"][0])
    contribution = DesktopController._contribution_view(decoded, [worker])
    summary = sharing_summary({"workers": [worker], "contribution": contribution})
    assert worker["sharing_active"] is active
    assert contribution["enabled"] is active
    assert bool(contribution["active_models"]) is active
    assert (worker["display_status"] == "Sharing") is active
    assert (summary[0] == "Sharing is on") is active
    assert worker["preparing"] is (not active and state != "failed")


def test_legacy_without_protocol_fields_preserves_existing_display():
    decoded = _normalize_contribution_status(projected(raw()))
    worker = decoded["workers"][0]
    assert "load_state" not in worker and "model_ready" not in worker
    assert DesktopController._worker_view(worker)["sharing_active"] is True


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "stopping"},
        {"operator_paused": True},
        {"desired_running": False},
        {"resource_admitted": False},
        {"schedule_admitted": False},
        {"policy_admitted": False},
    ],
)
def test_projection_withholds_late_or_inadmissible_ready(changes):
    source = projected(raw(load_state="ready", model_ready=True, **changes))
    assert source["workers"][0]["model_ready"] is False
    worker = DesktopController._worker_view(_normalize_contribution_status(source)["workers"][0])
    assert worker["sharing_active"] is False


@pytest.mark.parametrize(
    "fields",
    [
        {"load_state": "ready"},
        {"model_ready": True},
        {"load_state": None, "model_ready": False},
        {"load_state": [], "model_ready": False},
        {"load_state": "private-invalid", "model_ready": False},
        {"load_state": "ready", "model_ready": 1},
        {"load_state": "loading", "model_ready": True},
        {"load_state": "failed", "model_ready": True},
    ],
)
def test_client_rejects_partial_malformed_or_inconsistent_readiness(fields):
    source = projected(raw())
    source["workers"][0].update(fields)
    with pytest.raises(NodeClientError, match="readiness is invalid"):
        _normalize_contribution_status(source)


def test_client_rejects_ready_true_for_paused_or_inadmissible_worker():
    original = projected(raw(load_state="ready", model_ready=True))
    for field, value in [("state", "paused"), ("desired_running", False), ("operator_paused", True)]:
        source = copy.deepcopy(original)
        source["workers"][0][field] = value
        with pytest.raises(NodeClientError, match="readiness is invalid"):
            _normalize_contribution_status(source)
    source = copy.deepcopy(original)
    source["workers"][0]["resources"].update(admitted=False, reason="temporarily unavailable")
    with pytest.raises(NodeClientError, match="readiness is invalid"):
        _normalize_contribution_status(source)


def test_failed_loading_offers_explicit_retry_with_fixed_copy_and_respects_policy():
    source = projected(
        raw(
            load_state="failed",
            model_ready=False,
            state="paused",
            desired_running=False,
            resource_admitted=False,
            resource_reason=FAILED,
        )
    )
    decoded = _normalize_contribution_status(source)
    worker = DesktopController._worker_view(decoded["workers"][0])
    contribution = DesktopController._contribution_view(decoded, [worker])
    assert worker["can_start"] is True
    assert worker["sharing_active"] is False
    title, help_text, state = sharing_summary({"workers": [worker], "contribution": contribution})
    assert title == "Sharing stopped" and state == "error"
    assert "Pause" in help_text and "Start" in help_text
    assert "acknowledgement" not in help_text
    assert sharing_reason(FAILED + " private-path private-token") == help_text
    decoded["workers"][0]["policy"].update(admitted=False, reason="sharing is disabled by contribution policy")
    assert DesktopController._worker_view(decoded["workers"][0])["can_start"] is False


def test_control_status_and_real_desktop_codec_expose_only_bounded_readiness_pair():
    snapshot = raw(
        load_state="waiting",
        model_ready=False,
        loading_binding={"token": "private-token", "directory": "private-path"},
        loading_status={"nonce": "private-nonce"},
    )
    supervisor = SimpleNamespace(snapshots=lambda: (dict(snapshot),), shutdown=lambda: None)
    manager = ModelManager()
    app = create_node_app(manager, api_keys=["client"], control_keys=["control"], worker_supervisor=supervisor)
    try:
        with TestClient(app) as api:
            node = NodeClient("http://127.0.0.1:8080", "control")

            def request(request, timeout):
                response = api.request(
                    request.get_method(), urlsplit(request.full_url).path, headers=dict(request.header_items())
                )
                assert response.status_code == 200
                assert (
                    "private-token" not in response.text
                    and "private-path" not in response.text
                    and "private-nonce" not in response.text
                )
                return io.BytesIO(response.content)

            node._opener = SimpleNamespace(open=request)
            first = node.status()["contribution"]["workers"][0]
            assert first["load_state"] == "waiting" and first["model_ready"] is False
            assert DesktopController._worker_view(first)["sharing_active"] is False
            snapshot.update(load_state="ready", model_ready=True)
            ready = node.status()["contribution"]["workers"][0]
            assert ready["model_ready"] is True
            assert DesktopController._worker_view(ready)["sharing_active"] is True
    finally:
        manager.shutdown()


def test_projection_never_turns_invalid_private_state_into_legacy_sharing():
    worker = projected(raw(load_state="private-state", model_ready="private-token"))["workers"][0]
    assert worker["load_state"] == "failed" and worker["model_ready"] is False
    assert "private-" not in json.dumps(worker)
