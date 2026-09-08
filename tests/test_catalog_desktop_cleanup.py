"""Owned-process fallback checks without launching Qt or a model runtime."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop" / "src"))
import qualify_catalog_desktop as replay


class Process:
    def __init__(self, pid, created, children=()):
        self.pid, self.created, self.descendants = pid, created, children
        self.alive = True
        self.signals = []

    def create_time(self):
        return self.created

    def is_running(self):
        return self.alive

    def children(self, recursive=False):
        assert recursive
        return self.descendants

    def terminate(self):
        self.signals.append("terminate")
        self.alive = False

    def kill(self):
        self.signals.append("kill")
        self.alive = False


def test_fallback_stops_owned_descendants_without_signaling_reused_pid(monkeypatch):
    child = Process(12, 120)
    owned = Process(10, 100, (child,))
    reused = Process(11, 999)
    processes = {process.pid: process for process in (owned, reused, child)}
    monkeypatch.setattr(replay.psutil, "Process", processes.__getitem__)
    monkeypatch.setattr(replay.time, "sleep", lambda _: None)
    identities = [(10, 100), (11, 110)]

    replay.force_stop_owned_tree(identities)

    assert owned.signals == child.signals == ["terminate"]
    assert reused.signals == [] and reused.alive
    assert (12, 120) in identities


def test_fallback_reports_failed_cleanup_when_ownership_cannot_be_inspected(monkeypatch):
    def denied(_):
        raise replay.psutil.AccessDenied()

    monkeypatch.setattr(replay.psutil, "Process", denied)
    with pytest.raises(replay.psutil.AccessDenied):
        replay.force_stop_owned_tree([(10, 100)])


def test_fallback_has_a_finite_deadline_without_signaling_after_expiry(monkeypatch):
    owned = Process(10, 100)
    clock = iter((100, 116))
    monkeypatch.setattr(replay.psutil, "Process", lambda _: owned)
    monkeypatch.setattr(replay.time, "monotonic", lambda: next(clock))
    with pytest.raises(TimeoutError, match="deadline"):
        replay.force_stop_owned_tree([(10, 100)], timeout=15)
    assert owned.signals == []
