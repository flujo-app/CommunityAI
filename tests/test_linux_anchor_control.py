"""Strict protocol fixtures; no kernel, installed service or model qualification."""

import copy

import pytest

from drift.node import linux_anchor_control as control
from drift.node.linux_node_identity import make_identity
from drift.node.resource_recovery import RecoverableStateError


def snapshot():
    return dict(
        revision=1,
        phase="idle",
        generation=None,
        request_id=None,
        operation=None,
        drain_complete=True,
        api_ready=False,
        api_identity=None,
        maintenance=False,
        pending_request_id=None,
    )


def test_control_commands_have_fixed_bounded_contract():
    assert control.request("observe", "a" * 64)["revision"] is None
    condition = dict(generation=None, pending_request_id=None)
    value = control.request("start", "b" * 64, 1, "c" * 32, condition)
    assert value == dict(
        version=2,
        profile="multigpu-volunteer",
        operation="start",
        nonce="b" * 64,
        revision=1,
        request_id="c" * 32,
        condition=condition,
    )
    assert control.validate_snapshot(snapshot()) == snapshot()


@pytest.mark.parametrize(
    "operation,revision,request_id",
    [
        ("exec", 1, "a" * 32),
        ([], 1, "a" * 32),
        ("start", True, "a" * 32),
        ("start", -1, "a" * 32),
        ("start", 2**63, "a" * 32),
        ("start", 1, "A" * 32),
        ("start", 1, None),
        ("observe", 1, None),
        ("observe", None, "a" * 32),
    ],
)
def test_invalid_commands_never_grant_launch(operation, revision, request_id):
    with pytest.raises(RecoverableStateError):
        control.request(operation, "b" * 64, revision, request_id, dict(generation=None, pending_request_id=None))


@pytest.mark.parametrize(
    "condition",
    [None, {}, [], {"generation": None, "pending_request_id": True}, {"generation": "a", "pending_request_id": None}],
)
def test_mutation_requires_exact_scoped_condition(condition):
    for operation in ("start", "drain"):
        with pytest.raises(RecoverableStateError):
            control.request(operation, "b" * 64, 1, "c" * 32, condition)


@pytest.mark.parametrize(
    "key,value",
    [
        ("revision", True),
        ("revision", -1),
        ("phase", []),
        ("phase", "ready"),
        ("generation", {}),
        ("request_id", True),
        ("request_id", "c" * 32),
        ("operation", "start"),
        ("pending_request_id", True),
        ("pending_request_id", "c" * 32),
        ("drain_complete", 1),
        ("api_ready", True),
        ("api_ready", 0),
        ("maintenance", True),
    ],
)
def test_unsafe_receipts_refused(key, value):
    result = snapshot()
    result[key] = value
    with pytest.raises(RecoverableStateError):
        control.validate_snapshot(result)


def test_receipt_running_requires_exact_process_but_is_not_api_readiness():
    value = snapshot()
    value.update(drain_complete=False, phase="running", generation=dict(id="a" * 32, pid=2, start_ticks=10))
    value["api_identity"] = make_identity({}, dict(**value["generation"], cgroup={}))
    assert control.validate_snapshot(value) == value
    for key, bad in (("pid", True), ("start_ticks", None), ("id", "private")):
        forged = copy.deepcopy(value)
        forged["generation"][key] = bad
        with pytest.raises(RecoverableStateError):
            control.validate_snapshot(forged)
    value["maintenance"] = True
    with pytest.raises(RecoverableStateError):
        control.validate_snapshot(value)


def test_no_extra_argv_paths_or_token_fields_can_hide_in_receipt():
    value = snapshot()
    value["command"] = ["private"]
    with pytest.raises(RecoverableStateError) as error:
        control.validate_snapshot(value)
    assert "private" not in str(error.value)


def test_starting_receipt_requires_generation_even_without_completion():
    value = snapshot()
    value.update(phase="starting", drain_complete=False)
    with pytest.raises(RecoverableStateError):
        control.validate_snapshot(value)


def test_checking_receipt_cannot_claim_an_existing_generation():
    value = snapshot()
    value.update(phase="checking", drain_complete=False, generation=dict(id="a" * 32, pid=None, start_ticks=None))
    with pytest.raises(RecoverableStateError):
        control.validate_snapshot(value)
