"""Shutdown ownership tests for the fixed replacement session."""

from types import SimpleNamespace

import pytest

from communityai_anchor.resource_recovery import RecoverableStateError
from drift.node import linux_anchor_replacement as replacement
from drift.node.linux_anchor_replacement import FixedAnchorSession


class _Handle:
    def __init__(self, events, name):
        self.events, self.name = events, name

    def close(self, *args, **kwargs):
        self.events.append((self.name, args, kwargs))
        return True


class _Channel(_Handle):
    def abandon(self):
        self.events.append(("channel-abandon", (), {}))

    def close(self, *args, **kwargs):
        pytest.fail("session shutdown must not directly unlink the fenced socket")


class _Controller:
    def __init__(self, events, result):
        self.events, self.result = events, result

    def close(self, *args, **kwargs):
        self.events.append(("controller-close", args, kwargs))
        return self.result


def _session(tmp_path, *, controller_result, transferred=True):
    events = []
    session = FixedAnchorSession(SimpleNamespace(root=tmp_path), None, None, None, cancelled=lambda: False)
    session.controller = _Controller(events, controller_result)
    session.channel = _Channel(events, "channel-close")
    session.journal = _Handle(events, "journal-close")
    session.manager = _Handle(events, "manager-close")
    session.lifetime = _Handle(events, "lifetime-close")
    session.state_lease = _Handle(events, "state-close")
    session.layout = _Handle(events, "layout-close")
    session.lease = _Handle(events, "lease-close")
    session.transferred = transferred
    return session, events


def test_failed_controller_close_retains_every_session_authority(tmp_path):
    session, events = _session(tmp_path, controller_result=False)

    assert session.close() is False
    assert events == [("controller-close", (), {"timeout": 2.0})]


@pytest.mark.parametrize("transferred", [False, True])
def test_successful_controller_close_abandons_listener_then_releases_session_handles(tmp_path, transferred):
    session, events = _session(tmp_path, controller_result=True, transferred=transferred)

    assert session.close() is True
    assert events[:3] == [
        ("controller-close", (), {"timeout": 2.0}),
        ("channel-abandon", (), {}),
        ("journal-close", (), {}),
    ]
    authority_closes = [name for name, _args, _kwargs in events[3:]]
    if transferred:
        assert authority_closes == ["layout-close", "lease-close"]
    else:
        assert authority_closes == [
            "manager-close",
            "lifetime-close",
            "state-close",
            "layout-close",
            "lease-close",
        ]


def _retirement_receipt():
    binding = {"fixture": "binding"}
    binding_digest = replacement._digest(binding)
    source = dict(
        schema_version=1,
        binding=binding_digest,
        nonce="a" * 32,
        directory=[1, 2],
        lease=[3, 4],
        phase="bound",
        socket=[5, 6],
    )
    record = {"fingerprint": [1, 2, 33152, 4, 5, 6, 1], "digest": "b" * 64}
    records = {name: dict(record) for name in replacement._FILES}
    receipt = dict(
        schema_version=1,
        binding=binding_digest,
        records=records,
        endpoint_source=source,
        endpoint=replacement._digest(dict(source, phase="retired", socket=None)),
    )
    return binding, source, records, receipt


@pytest.mark.parametrize("phase", ["bound", "clearing", "retired"])
def test_retirement_receipt_accepts_only_its_exact_endpoint_transition(tmp_path, monkeypatch, phase):
    binding, source, records, receipt = _retirement_receipt()
    current = dict(source, phase=phase, socket=None if phase == "retired" else source["socket"])
    fence = SimpleNamespace(value=current, validate=lambda: replacement.validate_endpoint(current))
    monkeypatch.setattr(
        replacement,
        "_read_record",
        lambda _root, name: ({}, records[name]),
    )

    replacement._check_receipt(tmp_path, binding, fence, receipt)


@pytest.mark.parametrize(
    "change",
    [
        {"phase": "pending", "socket": None},
        {"phase": "clearing", "socket": None},
        {"nonce": "c" * 32},
        {"socket": [7, 8]},
    ],
)
def test_retirement_receipt_refuses_endpoint_transition_mismatch(tmp_path, monkeypatch, change):
    binding, source, records, receipt = _retirement_receipt()
    current = dict(source, **change)
    fence = SimpleNamespace(value=current, validate=lambda: replacement.validate_endpoint(current))
    monkeypatch.setattr(
        replacement,
        "_read_record",
        lambda _root, name: ({}, records[name]),
    )

    with pytest.raises(RecoverableStateError):
        replacement._check_receipt(tmp_path, binding, fence, receipt)
