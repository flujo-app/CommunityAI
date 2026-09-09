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

    def status(self):
        return replay.psutil.STATUS_RUNNING if self.alive else replay.psutil.STATUS_DEAD

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


def test_zombie_is_not_signaled_or_treated_as_an_executing_runtime(monkeypatch):
    zombie = Process(10, 100)
    monkeypatch.setattr(zombie, "status", lambda: replay.psutil.STATUS_ZOMBIE)
    monkeypatch.setattr(replay.psutil, "Process", lambda _: zombie)
    replay.wait_tree_gone([(10, 100)])
    replay.force_stop_owned_tree([(10, 100)])
    assert zombie.signals == []


def test_status_permission_failure_is_not_misreported_as_gone(monkeypatch):
    owned = Process(10, 100)

    def denied():
        raise replay.psutil.AccessDenied()

    monkeypatch.setattr(owned, "status", denied)
    monkeypatch.setattr(replay.psutil, "Process", lambda _: owned)
    with pytest.raises(replay.psutil.AccessDenied):
        replay.wait_tree_gone([(10, 100)])


def test_preflight_allows_zombie_but_refuses_live_desktop(monkeypatch):
    process = Process(10, 100)
    process.info = {"name": "CommunityAI"}
    monkeypatch.setattr(replay.psutil, "process_iter", lambda _: [process])
    with pytest.raises(RuntimeError, match="existing CommunityAI"):
        replay.require_desktop_stopped()
    monkeypatch.setattr(process, "status", lambda: replay.psutil.STATUS_ZOMBIE)
    replay.require_desktop_stopped()


def test_preflight_does_not_treat_unreadable_desktop_as_stopped(monkeypatch):
    process = Process(10, 100)
    process.info = {"name": "CommunityAI.exe"}
    monkeypatch.setattr(replay.psutil, "process_iter", lambda _: [process])

    def denied():
        raise replay.psutil.AccessDenied()

    monkeypatch.setattr(process, "status", denied)
    with pytest.raises(replay.psutil.AccessDenied):
        replay.require_desktop_stopped()
