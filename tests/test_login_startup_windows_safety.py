"""Qualification cleanup checks without launching Qt or touching user state."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop" / "src"))
import qualify_login_startup_windows as replay


class Store:
    def __init__(self, *, delete_error=None, read_error=None, remains=False):
        self.delete_error, self.read_error, self.remains = delete_error, read_error, remains
        self.calls = []

    def delete(self):
        self.calls.append("delete")
        if self.delete_error:
            raise self.delete_error
        return True

    def get(self):
        self.calls.append("get")
        if self.read_error:
            raise self.read_error
        if self.remains:
            return "test-only-sentinel"
        raise replay.CredentialMissingError("test account absent")


def finish(tmp_path, store, *, stopped=True, created=True, original=None, read_run=lambda: None):
    result = {"result": "passed", "owned_processes_stopped": stopped}
    replay.finalize_evidence(result, tmp_path, store, created, original, [(12, 120)], read_run=read_run)
    assert json.loads((tmp_path / "result.json").read_text()) == json.loads(json.dumps(result))
    return result


def test_success_requires_credential_absence_after_owned_process_cleanup(tmp_path):
    store = Store()
    result = finish(tmp_path, store)
    assert result["result"] == "passed"
    assert result["run_entry_unchanged"] is True
    assert result["private_credential_removed"] is True
    assert store.calls == ["delete", "get"]


@pytest.mark.parametrize("stopped", [False, None])
def test_live_or_unverified_runtime_preserves_private_credential(tmp_path, stopped):
    store = Store()
    result = finish(tmp_path, store, stopped=stopped)
    assert store.calls == []
    assert not result.get("private_credential_removed", False)
    assert result["result"] == "failed"


def test_uncreated_credential_is_never_deleted(tmp_path):
    store = Store()
    finish(tmp_path, store, created=False)
    assert store.calls == []


@pytest.mark.parametrize("proof", [None, "False", "unknown"])
def test_missing_job_cleanup_proof_retains_credential_despite_recorded_pid_absence(tmp_path, proof):
    if proof is not None:
        (tmp_path / "helper-result.txt").write_text(f"job_cleanup_verified={proof}\n")
    result = {"result": "passed", "owned_processes_stopped": True, "helper_containment_required": True}
    store = Store()
    replay.finalize_evidence(result, tmp_path, store, True, None, [], read_run=lambda: None)
    saved = json.loads((tmp_path / "result.json").read_text())
    assert saved["result"] == "failed"
    assert saved["owned_processes_stopped"] is False
    assert saved["private_credential_retained_for_live_processes"] is True
    assert store.calls == []


def test_verified_emergency_job_cleanup_allows_credential_removal_but_cannot_pass(tmp_path):
    (tmp_path / "helper-result.txt").write_text("result=failed\njob_cleanup_verified=True\n")
    result = {"result": "failed", "owned_processes_stopped": True, "helper_containment_required": True}
    store = Store()
    replay.finalize_evidence(result, tmp_path, store, True, None, [], read_run=lambda: None)
    assert result["result"] == "failed"
    assert result["private_credential_removed"] is True
    assert store.calls == ["delete", "get"]


@pytest.mark.parametrize(
    "store",
    [
        Store(delete_error=RuntimeError("delete failed")),
        Store(read_error=RuntimeError("read failed")),
        Store(remains=True),
    ],
)
def test_credential_cleanup_failure_cannot_pass_or_skip_evidence(tmp_path, store):
    result = finish(tmp_path, store)
    assert result["result"] == "failed"
    assert not result.get("private_credential_removed", False)


def test_registry_read_failure_keeps_evidence_and_still_cleans_owned_credential(tmp_path):
    def denied():
        raise PermissionError("registry unreadable")

    store = Store()
    result = finish(tmp_path, store, read_run=denied)
    assert result["result"] == "failed"
    assert result["private_credential_removed"] is True
    assert store.calls == ["delete", "get"]


@pytest.mark.parametrize("current", [None, ("original", 2), ("changed", 1)])
def test_registry_check_preserves_exact_original_value_and_type(tmp_path, current):
    result = finish(tmp_path, Store(), original=("original", 1), read_run=lambda: current)
    assert result["result"] == "failed"
    assert result["run_entry_unchanged"] is False


class Process:
    def __init__(self, pid, created, children=()):
        self.pid, self.created, self.descendants = pid, created, children
        self.children_read = False

    def create_time(self):
        return self.created

    def is_running(self):
        return True

    def status(self):
        return replay.psutil.STATUS_RUNNING

    def children(self, recursive=False):
        assert recursive
        self.children_read = True
        return self.descendants


def processes(monkeypatch, values):
    lookup = {value.pid: value for value in values}

    def find(pid):
        try:
            return lookup[pid]
        except KeyError:
            raise replay.psutil.NoSuchProcess(pid) from None

    monkeypatch.setattr(replay.psutil, "Process", find)


def test_missing_or_reused_gui_pid_is_not_treated_as_owned(monkeypatch):
    processes(monkeypatch, [Process(12, 999)])
    assert not replay.owned_gui_is_live(None)
    assert not replay.owned_gui_is_live((10, 100))
    assert not replay.owned_gui_is_live((12, 120))
    assert replay.owned_gui_is_live((12, 999))


def test_descendant_discovery_continues_when_initial_helper_has_exited(monkeypatch):
    grandchild = Process(13, 130)
    child = Process(12, 120, [grandchild])
    processes(monkeypatch, [child, grandchild])
    identities = [(10, 100), (12, 120)]
    replay.refresh_owned_identities(identities)
    assert (13, 130) in identities
    assert child.children_read


def test_pid_reuse_does_not_adopt_unrelated_descendants(monkeypatch):
    unrelated = Process(13, 130)
    reused = Process(12, 999, [unrelated])
    processes(monkeypatch, [reused, unrelated])
    identities = [(12, 120)]
    replay.refresh_owned_identities(identities)
    assert set(identities) == {(12, 120)}
    assert not reused.children_read


def test_unreadable_process_ownership_is_not_reported_as_absent(monkeypatch):
    def denied(_):
        raise replay.psutil.AccessDenied()

    monkeypatch.setattr(replay.psutil, "Process", denied)
    with pytest.raises(replay.psutil.AccessDenied):
        replay.owned_gui_is_live((12, 120))
    with pytest.raises(replay.psutil.AccessDenied):
        replay.refresh_owned_identities([(12, 120)])
