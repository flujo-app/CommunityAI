"""Controlled polling/cached-view tests, not native containment qualification."""

import os
import threading
from types import SimpleNamespace

import pytest

from drift.node import linux_anchor_node as node
from drift.node.resource_recovery import RecoverableStateError


@pytest.mark.parametrize("never_empty", [False, True])
def test_tree_poll_does_not_repeat_expensive_identity_probes(tmp_path, monkeypatch, never_empty):
    owner = node.AnchorNode.__new__(node.AnchorNode)
    counts = dict(layout=0, lease=0, identity=0, reads=0)
    clock = [0.0]

    def count(name):
        counts[name] += 1

    owner.layout = SimpleNamespace(validate=lambda: count("layout"))
    owner._lease = SimpleNamespace(validate=lambda: count("lease"))
    profile = SimpleNamespace(root=str(tmp_path))
    control = tmp_path / "control"
    control.write_bytes(b"\0\0")

    def observe(*args):
        count("identity")
        return profile

    def read(*args):
        count("reads")
        return never_empty or counts["reads"] <= 3

    def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(node, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    monkeypatch.setattr(node.anchor.cg, "_open_root", lambda *args: os.open(control, os.O_RDONLY))
    monkeypatch.setattr(node.anchor.cg, "_control", lambda *args, **kwargs: os.open(control, os.O_WRONLY))
    monkeypatch.setattr(node.anchor.cg, "_observe_root", observe)
    monkeypatch.setattr(node.anchor.cg, "_read_control", read)
    monkeypatch.setattr(node.anchor.cg, "_events", lambda value: value)
    if never_empty:
        with pytest.raises(RecoverableStateError):
            owner._kill_profile(profile)
        assert 5 <= clock[0] <= 5.1 and counts["reads"] <= 102
        assert counts["layout"] == counts["identity"] == 1
    else:
        owner._kill_profile(profile)
        assert counts["reads"] == 4 and clock[0] < 0.2
        assert counts["layout"] == counts["identity"] == 2
    assert counts["lease"] == 1


def test_cached_completion_is_no_io_and_masked_by_active_pending_or_closed():
    owner = node.AnchorNode.__new__(node.AnchorNode)
    owner._lock = threading.Lock()
    owner._closed = False
    owner._active = owner._pending = None
    owner._cached = dict(drain_complete=True, pending_request_id=None)
    assert owner.snapshot()["drain_complete"]
    owner._active = ("start", 1, "a" * 32)
    assert owner.snapshot() == dict(drain_complete=False, pending_request_id="a" * 32)
    owner._pending = ("drain", 1, "b" * 32)
    assert owner.snapshot() == dict(drain_complete=False, pending_request_id="b" * 32)
    owner._active = owner._pending = None
    owner._closed = True
    assert not owner.snapshot()["drain_complete"]
