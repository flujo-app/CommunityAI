"""Exercise Run backup/restore without touching a registry or launching an app."""

import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from qualify_login_startup_windows_state import RunStateConflict, RunStateVerificationError, RunValueGuard

ORIGINAL = ("original executable --started-at-login", 2)
QUALIFICATION = ("qualified executable --started-at-login", 1)


class Registry:
    def __init__(self, value):
        self.value = deepcopy(value)
        self.calls = []

    def read(self):
        self.calls.append("read")
        return deepcopy(self.value)

    def write(self, value):
        self.calls.append(("write", deepcopy(value)))
        self.value = deepcopy(value)

    def delete(self):
        self.calls.append("delete")
        self.value = None


def guard_for(registry):
    return RunValueGuard(registry.read, registry.write, registry.delete)


def test_original_absence_is_restored_by_deleting_only_the_expected_value():
    registry = Registry(None)
    guard = guard_for(registry)
    guard.expect(QUALIFICATION)
    registry.value = QUALIFICATION
    assert guard.restore() is True
    assert registry.value is None
    assert registry.calls == ["read", "read", "delete", "read"]


@pytest.mark.parametrize("current", [QUALIFICATION, None])
def test_exact_original_data_and_type_are_restored_from_enabled_or_disabled_state(current):
    registry = Registry(ORIGINAL)
    guard = guard_for(registry)
    guard.expect(QUALIFICATION)
    guard.expect(None)
    registry.value = current
    assert guard.restore() is True
    assert registry.value == ORIGINAL
    assert registry.calls[-2:] == [("write", ORIGINAL), "read"]


@pytest.mark.parametrize("original", [None, ORIGINAL])
def test_unchanged_original_is_verified_without_any_write(original):
    registry = Registry(original)
    guard = guard_for(registry)
    assert guard.restore() is False
    assert registry.calls == ["read", "read", "read"]


@pytest.mark.parametrize("unrelated", [("another executable", 1), (QUALIFICATION[0], 2), None])
def test_unknown_concurrent_data_type_or_unregistered_absence_is_not_overwritten(unrelated):
    registry = Registry(ORIGINAL)
    guard = guard_for(registry)
    guard.expect(QUALIFICATION)
    registry.value = unrelated
    with pytest.raises(RunStateConflict):
        guard.restore()
    assert registry.value == unrelated
    assert registry.calls == ["read", "read"]


def test_snapshot_and_expectations_are_not_changed_through_mutable_value_aliases():
    registry = Registry((["original"], 7))
    guard = guard_for(registry)
    expected = (["qualification"], 7)
    guard.expect(expected)
    expected[0].append("unrelated")
    guard.original[0].append("unrelated")
    registry.value = (["qualification"], 7)
    guard.restore()
    assert registry.value == (["original"], 7)


def test_verification_is_read_only_and_detects_registry_type_mismatch():
    registry = Registry(QUALIFICATION)
    guard = guard_for(registry)
    guard.verify(QUALIFICATION)
    with pytest.raises(RunStateVerificationError):
        guard.verify((QUALIFICATION[0], 2))
    assert registry.calls == ["read", "read", "read"]


def test_restore_does_not_claim_success_when_delete_silently_fails():
    registry = Registry(None)
    guard = RunValueGuard(registry.read, registry.write, lambda: registry.calls.append("delete"))
    guard.expect(QUALIFICATION)
    registry.value = QUALIFICATION
    with pytest.raises(RunStateVerificationError):
        guard.restore()
    assert registry.calls[-2:] == ["delete", "read"]


def test_restore_verifies_final_state_even_when_write_reports_an_error():
    registry = Registry(ORIGINAL)

    def write_then_fail(value):
        registry.write(value)
        raise OSError("qualification write error")

    guard = RunValueGuard(registry.read, write_then_fail, registry.delete)
    guard.expect(QUALIFICATION)
    registry.value = QUALIFICATION
    with pytest.raises(OSError, match="qualification write error"):
        guard.restore()
    assert registry.value == ORIGINAL
    assert registry.calls[-2:] == [("write", ORIGINAL), "read"]


def test_failed_read_cannot_trigger_a_restore_write():
    registry = Registry(ORIGINAL)
    available = True

    def read():
        if not available:
            raise PermissionError("qualification read error")
        return registry.read()

    guard = RunValueGuard(read, registry.write, registry.delete)
    guard.expect(QUALIFICATION)
    registry.value = QUALIFICATION
    available = False
    with pytest.raises(PermissionError):
        guard.restore()
    assert registry.value == QUALIFICATION
    assert registry.calls == ["read"]


def test_concurrent_change_during_final_verification_is_reported_without_second_write():
    registry = Registry(None)
    reads = 0

    def read():
        nonlocal reads
        reads += 1
        if reads == 3:
            registry.value = ("concurrent executable", 1)
        return registry.read()

    guard = RunValueGuard(read, registry.write, registry.delete)
    guard.expect(QUALIFICATION)
    registry.value = QUALIFICATION
    with pytest.raises(RunStateVerificationError):
        guard.restore()
    assert registry.value == ("concurrent executable", 1)
    assert registry.calls.count("delete") == 1
