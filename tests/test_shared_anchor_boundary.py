"""Shared implementation identity and distinct observation/admission contracts."""

import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from communityai_anchor import linux_cgroup_recovery as cg, linux_node_channel as channel
from communityai_anchor.linux_node_identity import make_identity


@pytest.mark.parametrize(
    "name",
    [
        "worker_loading",
        "resource_recovery_config",
        "resource_recovery",
        "linux_cgroup_recovery",
        "linux_anchor",
        "linux_anchor_control",
        "linux_anchor_state",
        "linux_anchor_resources",
        "linux_anchor_entry",
        "linux_node_channel",
        "linux_node_identity",
    ],
)
def test_runtime_compatibility_path_is_the_same_module_and_mutable_state(name):
    shared = importlib.import_module("communityai_anchor." + name)
    runtime = importlib.import_module("drift.node." + name)
    assert shared is runtime
    assert vars(shared) is vars(runtime)


def test_read_only_observation_never_probes_birth_but_admission_still_requires_it(monkeypatch):
    events = []
    profile = object()
    monkeypatch.setattr(cg, "_platform", lambda: None)
    monkeypatch.setattr(cg, "_open_root", lambda path: 12)
    monkeypatch.setattr(cg, "_observe_root", lambda *args: events.append("identity") or profile)
    monkeypatch.setattr(cg, "_require_unfrozen", lambda fd: events.append("unfrozen"))
    monkeypatch.setattr(cg, "_close_descriptors", lambda *args: events.append("closed"))

    def backend():
        events.append("backend")
        raise cg.RecoverableStateError("unsupported_platform")

    monkeypatch.setattr(cg, "_validate_backend", backend)
    assert cg.observe_cgroup_profile("/fixture") is profile
    assert events == ["identity", "unfrozen", "identity", "unfrozen", "closed"]
    events.clear()
    with pytest.raises(cg.RecoverableStateError):
        cg.validate_cgroup_profile("/fixture")
    assert events == ["identity", "unfrozen", "backend", "closed"]


def test_desktop_peer_verification_uses_observation_not_process_birth(monkeypatch):
    identity = make_identity({}, dict(id="a" * 32, pid=42, start_ticks=123, cgroup={"fixture": "native"}))
    receipt = dict(
        service={}, layout_digest="fixture", node=dict(phase="running", pending_request_id=None, api_identity=identity)
    )
    transport = channel.NodeControlTransport(receipt)
    observe = Mock(return_value=SimpleNamespace(to_json=lambda: {"fixture": "native"}))
    birth = Mock(side_effect=AssertionError("Desktop must not require the process birth runtime"))
    monkeypatch.setattr(channel.anchor, "ServiceIdentity", lambda **kwargs: SimpleNamespace(control_group="/service"))
    monkeypatch.setattr(channel.anchor, "_peer", lambda connection: 42)
    monkeypatch.setattr(channel.anchor, "_process", lambda pid: (123, "/service/nodes/node-" + "a" * 32))
    monkeypatch.setattr(channel.anchor, "_delegated_path", lambda service: "/delegated")
    monkeypatch.setattr(cg, "observe_cgroup_profile", observe)
    monkeypatch.setattr(cg, "validate_cgroup_profile", birth)
    monkeypatch.setattr(channel.anchor, "_private_directory", lambda directory: (1, 2))
    monkeypatch.setattr(cg, "_identity", lambda descriptor: (1, 2))
    monkeypatch.setattr(channel.anchor, "_socket_identity", lambda path: (3, 4))
    transport._verify_peer(object(), "directory", (1, 2), 12, "path", (3, 4))
    observe.assert_called_once_with("/delegated/nodes/node-" + "a" * 32)
    birth.assert_not_called()


@pytest.mark.parametrize("fault", [None, "response", "http", "connection", "descriptor", "interrupt"])
def test_transport_close_attempts_every_owned_resource_after_any_failure(monkeypatch, fault):
    events = []

    def close(name):
        events.append(name)
        if name == fault:
            raise RuntimeError("private close detail")
        if fault == "interrupt" and name == "response":
            raise KeyboardInterrupt()

    monkeypatch.setattr(channel.os, "close", lambda fd: close("descriptor"))
    resources = [SimpleNamespace(close=lambda name=name: close(name)) for name in ("response", "http", "connection")]
    if fault is None:
        channel._close_transport(*resources, 77)
    elif fault == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            channel._close_transport(*resources, 77)
    else:
        with pytest.raises(OSError, match="could not be verified") as error:
            channel._close_transport(*resources, 77)
        assert "private" not in str(error.value)
    assert events == ["response", "http", "connection", "descriptor"]
