"""Cycle finalization without launching a GUI or changing native user state."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop" / "src"))
from qualify_login_startup_windows_cycle import CredentialMissingError, finalize_cycle
from qualify_login_startup_windows_state import RunStateConflict, RunValueGuard


class Guard:
    def __init__(self, calls, error=None):
        self.calls, self.error = calls, error

    def restore(self):
        self.calls.append("restore")
        if self.error:
            raise self.error
        return False


class Store:
    def __init__(self, calls, *, delete_error=None, read_error=None, remains=False):
        self.calls, self.delete_error, self.read_error, self.remains = calls, delete_error, read_error, remains

    def delete(self):
        self.calls.append("delete")
        if self.delete_error:
            raise self.delete_error

    def get(self):
        self.calls.append("get")
        if self.read_error:
            raise self.read_error
        if self.remains:
            return "qualification-sentinel"
        raise CredentialMissingError("qualification account absent")


def result_with(stopped=True):
    return {"result": "passed", "phases": [{"owned_processes_stopped": stopped}]}


def finish(tmp_path, result, guard, store, created=True):
    finalize_cycle(result, tmp_path, guard, store, created)
    assert json.loads((tmp_path / "result.json").read_text()) == result


def test_original_is_restored_before_private_credential_is_removed(tmp_path):
    calls = []
    result = result_with()
    finish(tmp_path, result, Guard(calls), Store(calls))
    assert result["result"] == "passed"
    assert result["original_run_state_restored"] is True
    assert result["private_credential_removed"] is True
    assert calls == ["restore", "delete", "get"]


@pytest.mark.parametrize("stopped", [False, None, 1])
def test_failed_or_unknown_shutdown_prevents_restoration_and_credential_deletion(tmp_path, stopped):
    calls = []
    result = result_with(stopped)
    finish(tmp_path, result, Guard(calls), Store(calls))
    assert result["result"] == "failed"
    assert result["original_run_state_restored"] is False
    assert result["private_credential_retained_for_live_processes"] is True
    assert calls == []


def test_one_unverified_phase_prevents_restore_even_when_other_phase_stopped(tmp_path):
    calls = []
    result = result_with()
    result["phases"].append({})
    finish(tmp_path, result, Guard(calls), Store(calls))
    assert result["result"] == "failed"
    assert result["owned_processes_stopped"] is False
    assert calls == []


def test_prelaunch_failure_still_restores_and_cleans_without_claiming_acceptance(tmp_path):
    calls = []
    result = {"result": "failed", "phases": []}
    finish(tmp_path, result, Guard(calls), Store(calls))
    assert result["result"] == "failed"
    assert result["original_run_state_restored"] is True
    assert result["private_credential_removed"] is True
    assert calls == ["restore", "delete", "get"]


@pytest.mark.parametrize("error", [RunStateConflict("qualification conflict"), OSError("qualification restore error")])
def test_restoration_failure_retains_evidence_and_still_cleans_stopped_runtime_credential(tmp_path, error):
    calls = []
    result = result_with()
    finish(tmp_path, result, Guard(calls, error), Store(calls))
    assert result["result"] == "failed"
    assert result["original_run_state_restored"] is False
    assert result["restoration_error_type"] == type(error).__name__
    assert result["private_credential_removed"] is True
    assert calls == ["restore", "delete", "get"]


@pytest.mark.parametrize("problem", ["delete", "read", "remains"])
def test_credential_cleanup_error_downgrades_pass_and_preserves_result(tmp_path, problem):
    calls = []
    result = result_with()
    store = Store(
        calls,
        delete_error=OSError("qualification delete error") if problem == "delete" else None,
        read_error=OSError("qualification read error") if problem == "read" else None,
        remains=problem == "remains",
    )
    finish(tmp_path, result, Guard(calls), store)
    assert result["result"] == "failed"
    assert result["original_run_state_restored"] is True
    assert result["private_credential_removed"] is False


def test_uncreated_credential_is_never_touched(tmp_path):
    calls = []
    result = result_with()
    finish(tmp_path, result, Guard(calls), Store(calls), created=False)
    assert calls == ["restore"]


def test_actual_state_guard_conflict_leaves_unrelated_run_value_untouched(tmp_path):
    calls = []
    values = [None]

    def write(value):
        calls.append("registry write")
        values[0] = value

    def delete():
        calls.append("registry delete")
        values[0] = None

    guard = RunValueGuard(lambda: values[0], write, delete)
    guard.expect(("qualification executable", 1))
    values[0] = ("concurrent executable", 1)
    result = result_with()
    finish(tmp_path, result, guard, Store(calls))
    assert result["result"] == "failed"
    assert result["restoration_error_type"] == "RunStateConflict"
    assert values[0] == ("concurrent executable", 1)
    assert calls == ["delete", "get"]
