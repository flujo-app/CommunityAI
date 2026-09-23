"""Strict orchestration intent and containment contract, not native acceptance."""

import base64
import copy
from types import SimpleNamespace

import pytest
from test_linux_anchor_state import valid_state

from communityai_anchor.resource_recovery import RecoverableStateError, RecoveryIdentity
from drift.node import linux_anchor_replacement as replacement


@pytest.fixture
def context(monkeypatch):
    origin = valid_state()["binding"]
    monkeypatch.setattr(replacement, "current_recovery_identity", lambda: RecoveryIdentity.from_json(origin["machine"]))
    return dict(
        schema_version=1,
        origin=origin,
        mode="retained",
        attempt=dict(service=dict(origin["service"], pid=101, invocation="c" * 32), root=origin["layout"][0]),
        children=[],
        pending_child=None,
        migrated=False,
        receipt=None,
        receipt_identity=None,
        receipt_consuming=False,
        endpoint=dict(
            schema_version=1,
            binding=replacement._digest(origin),
            nonce="d" * 32,
            directory=[1, 7],
            lease=[1, 8],
            phase="bound",
            socket=[1, 9],
        ),
        endpoint_target=None,
        quiescent_source=False,
        no_spawn=None,
    )


def test_exact_context_is_bounded_and_preserves_inputs(context):
    before = copy.deepcopy(context)
    assert replacement._context(context) == before
    assert len(replacement.private._encode(context)) < 8192
    assert context == before


@pytest.mark.parametrize(
    "path,value",
    [
        (("schema_version",), True),
        (("mode",), []),
        (("migrated",), 1),
        (("attempt", "service", "pid"), True),
        (("attempt", "service", "uid"), True),
        (("attempt", "service", "start_ticks"), 0),
        (("attempt", "service", "invocation"), "0" * 32),
        (("attempt", "service", "control_group"), "/another"),
        (("receipt_identity",), {}),
        (("receipt_consuming",), True),
        (("pending_child",), "workers"),
        (("endpoint_target",), "A" * 64),
        (("endpoint_target",), "f" * 64),
        (("quiescent_source",), 1),
        (("no_spawn",), {}),
    ],
)
def test_context_rejects_ambiguous_or_unowned_intent(context, path, value):
    target = context
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with pytest.raises(RecoverableStateError):
        replacement._context(context)


def test_manager_pruned_context_requires_exact_clean_receipt(context):
    context["mode"] = "retired"
    with pytest.raises(RecoverableStateError):
        replacement._context(context)
    evidence = dict(fingerprint=[1, 2, 33152, 50, 1000, 1000, 1], digest="e" * 64)
    receipt = dict(
        schema_version=1,
        binding=replacement._digest(context["origin"]),
        records={name: copy.deepcopy(evidence) for name in replacement._FILES},
        endpoint_source=copy.deepcopy(context["endpoint"]),
        endpoint=replacement._digest(dict(context["endpoint"], phase="retired", socket=None)),
    )
    context.update(receipt=receipt, receipt_identity=dict(evidence, digest=replacement._digest(receipt)))
    assert replacement._context(context) == context
    context["children"] = [context["origin"]["layout"][1]]
    context["pending_child"] = "nodes"
    assert replacement._context(context) == context
    context["pending_child"] = "workers"
    with pytest.raises(RecoverableStateError):
        replacement._context(context)


def _snapshot(context):
    state = valid_state()
    state["binding"] = copy.deepcopy(context["origin"])
    values = {
        "state.json": state,
        "resources.json": {"binding": state["binding"]},
        "bootstrap.json": {"binding": replacement._digest(state["binding"])},
    }
    return dict(
        files={
            name: dict(
                source=dict(
                    raw_b64=base64.b64encode(replacement.bootstrap._json(value)).decode(),
                    fingerprint=[1, 2, 33152, 50, 1000, 1000, 1],
                ),
                prepared=None,
                published_fingerprint=None,
            )
            for name, value in values.items()
        }
    )


@pytest.mark.parametrize(
    "field", ["root", "root_identity", "mount_id", "mount_root", "mount_point", "namespaces", "uid"]
)
def test_retirement_does_not_authorize_an_unrelated_cgroup_view(context, field):
    context.update(mode="retired", quiescent_source=True)
    root = copy.deepcopy(context["attempt"]["root"])
    context["attempt"]["root"] = root
    if field == "root_identity":
        root[field][0] += 1
    elif field == "namespaces":
        root[field][0][1] += 1
    elif type(root[field]) is int:
        root[field] += 1
    else:
        root[field] += "/different"
    with pytest.raises(RecoverableStateError):
        replacement._context(context)


def test_retirement_can_change_only_root_inode(context):
    context.update(mode="retired", quiescent_source=True)
    context["attempt"]["root"] = copy.deepcopy(context["attempt"]["root"])
    context["attempt"]["root"]["root_identity"][1] += 100
    assert replacement._context(context) == context
    context["mode"] = "retained"
    with pytest.raises(RecoverableStateError):
        replacement._context(context)


def test_full_coexistent_retired_context_fits_its_fixed_eight_kib_bound(context):
    minimum = len(replacement.private._encode(context))
    context.update(mode="retired", children=copy.deepcopy(context["origin"]["layout"][1:]), migrated=True)
    evidence = dict(fingerprint=[2**64 - 1, 2**64 - 1, 33152, 8192, 2**64 - 1, 2**64 - 1, 1], digest="e" * 64)
    receipt = dict(
        schema_version=1,
        binding=replacement._digest(context["origin"]),
        records={name: copy.deepcopy(evidence) for name in replacement._FILES},
        endpoint_source=copy.deepcopy(context["endpoint"]),
        endpoint=replacement._digest(dict(context["endpoint"], phase="retired", socket=None)),
    )
    context.update(
        receipt=receipt,
        receipt_identity=dict(evidence, digest=replacement._digest(receipt)),
        receipt_consuming=True,
        endpoint_target="f" * 64,
    )
    context["no_spawn"] = dict(
        attempt=replacement._digest(context["attempt"]),
        binding="f" * 64,
        records={name: copy.deepcopy(evidence) for name in replacement._FILES},
    )
    assert replacement._context(context) == context
    encoded = replacement.private._encode(context)
    assert minimum + 1500 < len(encoded) <= 8192


@pytest.mark.parametrize("index,bad", [(0, True), (0, 2**64), (2, 16832), (2, 41471), (6, 2)])
def test_context_fingerprints_require_single_native_regular_files(index, bad):
    evidence = dict(fingerprint=[1, 2, 33152, 50, 1000, 1000, 1], digest="e" * 64)
    evidence["fingerprint"][index] = bad
    with pytest.raises(RecoverableStateError):
        replacement._record_evidence(evidence)


def test_no_spawn_marker_is_bound_to_exact_ledger_sources_and_attempt(context):
    snapshot = _snapshot(context)
    context["no_spawn"] = dict(
        attempt=replacement._digest(context["attempt"]), records=replacement._ledger_sources(snapshot), binding=None
    )
    assert replacement._context(context) == context
    assert replacement._check_no_spawn(snapshot, context)
    snapshot["files"]["state.json"]["source"]["fingerprint"][5] += 1
    with pytest.raises(RecoverableStateError):
        replacement._check_no_spawn(snapshot, context)


def test_retargeted_marker_cannot_authorize_effects_without_fresh_target_binding(context):
    snapshot = _snapshot(context)
    context["no_spawn"] = dict(
        attempt=replacement._digest(context["attempt"]), records=replacement._ledger_sources(snapshot), binding=None
    )
    snapshot["files"]["bootstrap.json"]["prepared"] = snapshot["files"]["bootstrap.json"]["source"]
    with pytest.raises(RecoverableStateError):
        replacement._check_no_spawn(snapshot, context)


def test_no_spawn_partial_prefix_accepts_source_state_but_rejects_wrong_target(context):
    snapshot = _snapshot(context)
    target = dict(context["origin"], service=context["attempt"]["service"])
    binding = replacement._digest(target)
    context["no_spawn"] = dict(
        attempt=replacement._digest(context["attempt"]), records=replacement._ledger_sources(snapshot), binding=binding
    )
    prepared = {"binding": binding}
    snapshot["files"]["bootstrap.json"]["prepared"] = dict(
        raw_b64=base64.b64encode(replacement.bootstrap._json(prepared)).decode()
    )
    assert replacement._check_no_spawn(snapshot, context)
    prepared["binding"] = "f" * 64
    snapshot["files"]["bootstrap.json"]["prepared"]["raw_b64"] = base64.b64encode(
        replacement.bootstrap._json(prepared)
    ).decode()
    with pytest.raises(RecoverableStateError):
        replacement._check_no_spawn(snapshot, context)


def test_old_process_identity_requires_proven_death(context, monkeypatch):
    service = context["origin"]["service"]
    monkeypatch.setattr(replacement.anchor, "_process", lambda _pid: (service["start_ticks"], "/service"))
    with pytest.raises(RecoverableStateError):
        replacement._dead(service)
    monkeypatch.setattr(replacement.anchor, "_process", lambda _pid: (service["start_ticks"] + 1, "/service"))
    replacement._dead(service)  # Stable reused identity, never a numeric-PID kill.
    monkeypatch.setattr(replacement.anchor, "_process", lambda _pid: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(PermissionError):
        replacement._dead(service)


@pytest.mark.parametrize("failure", ["first", "cancel"])
def test_containment_attempts_both_exact_roots_despite_first_failure_or_stop(monkeypatch, failure):
    profiles = tuple(SimpleNamespace(root=name) for name in ("root", "control", "nodes", "workers"))
    calls = []
    stopped = False
    monkeypatch.setattr(replacement.anchor.cg, "_open_root", lambda path: path)
    monkeypatch.setattr(
        replacement.anchor.cg, "_observe_root", lambda path, fd: next(p for p in profiles if p.root == path)
    )
    monkeypatch.setattr(replacement.anchor.cg, "_control", lambda fd, *args, **kwargs: fd)
    monkeypatch.setattr(replacement.anchor.cg, "_read_control", lambda *args: "populated 0\nfrozen 0\n")
    monkeypatch.setattr(replacement.os, "close", lambda _fd: None)

    def write(fd, payload):
        nonlocal stopped
        calls.append((fd, payload))
        if fd == "nodes":
            stopped = True
            if failure == "first":
                raise OSError("fixture first-root kill failure")
        return len(payload)

    monkeypatch.setattr(replacement.os, "write", write)
    with pytest.raises(RecoverableStateError):
        replacement._kill_old_trees(profiles, lambda: None, lambda: stopped)
    assert calls == [("nodes", b"1\n"), ("workers", b"1\n")]
