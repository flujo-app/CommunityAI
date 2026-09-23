"""Codec fixtures only; native persistence and lock tests live separately."""

import copy
import threading
from dataclasses import replace

import pytest

from drift.node import linux_anchor as anchor, linux_anchor_state as state
from drift.node.resource_recovery import RecoverableStateError


def valid_state():
    root = anchor.cg.LinuxCgroupProfile("/mount/service", (2, 3), 10, "/", "/mount", ((5, 6), (5, 7), (5, 8)), 1000)
    layout = [root] + [
        replace(root, root=root.root + "/" + name, root_identity=(2, index + 4))
        for index, name in enumerate(anchor._CHILDREN)
    ]
    return dict(
        schema_version=1,
        profile=anchor.PROFILE,
        revision=0,
        phase="checking",
        request_id=None,
        operation=None,
        generation=None,
        binding=dict(
            service=dict(pid=100, uid=1000, start_ticks=10, invocation="a" * 32, control_group="/service"),
            machine=dict(
                platform="linux", host_id="sha256:" + "b" * 64, boot_id="11111111-1111-4111-8111-111111111111"
            ),
            layout=[profile.to_json() for profile in layout],
            storage=dict(profile=[1, 2], directory=[1, 3], lease=[1, 4]),
        ),
    )


def generation(value):
    profile = anchor.cg.LinuxCgroupProfile.from_json(value["binding"]["layout"][2])
    return dict(
        id="c" * 32,
        token="d" * 32,
        pid=101,
        start_ticks=11,
        cgroup=replace(profile, root=profile.root + "/node-" + "c" * 32, root_identity=(2, 9)).to_json(),
    )


def quiescence(value):
    record = dict(fingerprint=[1, 2, 33152, 50, 1000, 1000, 1], digest="e" * 64)
    return dict(
        epoch="f" * 32,
        binding=state._sha256(value["binding"]),
        generation=state._sha256(value["generation"]),
        records={name: copy.deepcopy(record) for name in state._QUIESCENCE_RECORDS},
    )


def test_codec_preserves_intent_but_is_not_cleanup_authority():
    value = valid_state()
    assert state.validate_state(value) == value
    value.update(phase="starting", generation=generation(value), operation="start", request_id="e" * 32)
    assert state.validate_state(value) == value
    value["phase"] = "running"
    assert state.validate_state(value) == value
    # Recording idle is intent only; the store deliberately supplies no clean,
    # admission, maintenance or API-readiness receipt.
    value["phase"] = "idle"
    assert state.validate_state(value) == value
    assert not {"clean", "admission", "maintenance", "api_ready"} & value.keys()


def test_v2_codec_binds_idle_quiescence_to_full_binding_generation_and_records():
    value = valid_state()
    value.update(schema_version=2, phase="idle", quiescence=quiescence(value))
    assert state.validate_state(value) == value
    for mutate in (
        lambda item: item.update(phase="draining"),
        lambda item: item["quiescence"].update(binding="0" * 64),
        lambda item: item["quiescence"].update(generation="0" * 64),
        lambda item: item["quiescence"]["records"].pop("endpoint.json"),
        lambda item: item["quiescence"]["records"]["endpoint.json"].update(fingerprint=[1, 2, 3]),
    ):
        malformed = copy.deepcopy(value)
        mutate(malformed)
        with pytest.raises(RecoverableStateError):
            state.validate_state(malformed)


@pytest.mark.parametrize("index,bad", [(0, True), (0, 2**64), (2, 16832), (2, 41471), (6, 2)])
def test_quiescence_rejects_non_native_or_non_regular_file_fingerprints(index, bad):
    value = valid_state()
    value.update(schema_version=2, phase="idle", quiescence=quiescence(value))
    value["quiescence"]["records"]["endpoint.json"]["fingerprint"][index] = bad
    with pytest.raises(RecoverableStateError):
        state.validate_state(value)


def _owner(value):
    owner = object.__new__(state.AnchorState)
    owner._mutex = threading.RLock()
    owner._value = copy.deepcopy(value)
    owner.validate = lambda: None
    owner._commit = lambda proposed: copy.deepcopy(proposed)
    return owner


def test_generic_write_cannot_set_and_always_clears_v2_quiescence():
    value = valid_state()
    value.update(schema_version=2, phase="idle", quiescence=quiescence(value))
    owner = _owner(value)
    updated = owner.write(0, request_id="a" * 32, operation="drain")
    assert updated["schema_version"] == 2 and updated["quiescence"] is None
    with pytest.raises(RecoverableStateError):
        owner.write(0, quiescence=quiescence(value))


def test_dedicated_quiescent_write_upgrades_v1_and_only_accepts_drain_metadata():
    value = valid_state()
    value["phase"] = "draining"
    owner = _owner(value)
    proof = quiescence(value)
    updated = owner.write_quiescent(0, proof, request_id="a" * 32, operation="drain")
    assert updated == {
        **value,
        "schema_version": 2,
        "revision": 1,
        "phase": "idle",
        "request_id": "a" * 32,
        "operation": "drain",
        "quiescence": proof,
    }
    assert updated["binding"] == value["binding"] and updated["generation"] == value["generation"]
    with pytest.raises(RecoverableStateError):
        owner.write_quiescent(0, proof, request_id="b" * 32, operation="start")


def test_quiescence_digest_is_canonical_persisted_json_including_newline():
    assert state._sha256({"x": 1}) == "bb157861a164e35cdde9d726b0af9ce2765a8f530c35d9e45732b94ee65e9557"


def test_v2_payload_bound_counts_the_persisted_trailing_newline(monkeypatch):
    value = valid_state()
    value.update(schema_version=2, phase="idle", quiescence=quiescence(value))
    persisted_size = len(state.private._encode(value)) + 1
    monkeypatch.setattr(state, "_MAX_STATE_V2_BYTES", persisted_size)
    assert state.validate_state(value) == value
    monkeypatch.setattr(state, "_MAX_STATE_V2_BYTES", persisted_size - 1)
    with pytest.raises(RecoverableStateError):
        state.validate_state(value)


@pytest.mark.parametrize("operation", ["start", "drain"])
def test_dedicated_quiescent_write_preserves_existing_metadata_when_unspecified(operation):
    value = valid_state()
    value.update(
        phase="draining",
        request_id="c" * 32,
        operation=operation,
    )
    owner = _owner(value)

    updated = owner.write_quiescent(0, quiescence(value))

    assert updated["request_id"] == value["request_id"]
    assert updated["operation"] == operation


@pytest.mark.parametrize("native_id", [[2**63, 2**63 + 1], [2**64 - 1, 2**64 - 1]])
def test_storage_native_identity_allows_unsigned_high_bits(native_id):
    value = valid_state()
    value["binding"]["storage"]["lease"] = native_id
    assert state.validate_state(value) == value


@pytest.mark.parametrize("native_id", [[2**64, 1], [1, 2**64], [-1, 1], [1, True]])
def test_storage_identity_rejects_overflow_negative_and_boolean(native_id):
    value = valid_state()
    value["binding"]["storage"]["lease"] = native_id
    with pytest.raises(RecoverableStateError):
        state.validate_state(value)


@pytest.mark.parametrize(
    "key,bad",
    [
        ("schema_version", True),
        ("schema_version", 3),
        ("profile", []),
        ("profile", "ordinary"),
        ("revision", True),
        ("revision", -1),
        ("revision", 2**63),
        ("phase", []),
        ("phase", "ready"),
        ("operation", {}),
        ("operation", "exec"),
        ("request_id", True),
        ("request_id", "A" * 32),
        ("phase", "starting"),
        ("phase", "running"),
        ("generation", {}),
        ("binding", None),
    ],
)
def test_malformed_state_fails_with_fixed_error(key, bad):
    value = valid_state()
    value[key] = bad
    with pytest.raises(RecoverableStateError):
        state.validate_state(value)


@pytest.mark.parametrize(
    "section,key,bad",
    [
        ("service", "pid", True),
        ("service", "pid", 1),
        ("service", "uid", -1),
        ("service", "start_ticks", 0),
        ("service", "invocation", "0" * 32),
        ("service", "control_group", "/../private"),
        ("machine", "boot_id", "private"),
        ("storage", "lease", [1, 0]),
        ("storage", "directory", [True, 1]),
    ],
)
def test_malformed_binding(section, key, bad):
    value = valid_state()
    value["binding"][section][key] = bad
    with pytest.raises(RecoverableStateError) as error:
        state.validate_state(value)
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    "key,bad",
    [
        ("id", None),
        ("token", "A" * 32),
        ("pid", True),
        ("start_ticks", -1),
        ("cgroup", None),
        ("cgroup", {}),
        ("pid", None),
    ],
)
def test_running_generation_requires_exact_native_shape(key, bad):
    value = valid_state()
    value.update(phase="running", generation=generation(value))
    value["generation"][key] = bad
    with pytest.raises(RecoverableStateError):
        state.validate_state(value)


@pytest.mark.parametrize("section", ["layout", "generation"])
@pytest.mark.parametrize("field", ["root", "mount_id", "namespaces", "uid", "root_identity", "device"])
def test_child_profiles_cannot_change_namespace_mount_owner_or_path(section, field):
    value = valid_state()
    value.update(phase="running", generation=generation(value))
    target = value["binding"]["layout"][2] if section == "layout" else value["generation"]["cgroup"]
    target["root_identity" if field == "device" else field] = {
        "root": "/mount/other",
        "mount_id": 11,
        "namespaces": [[5, 6], [5, 7], [5, 99]],
        "uid": 1001,
        "root_identity": [2, 3],
        "device": [99, 50],
    }[field]
    with pytest.raises(RecoverableStateError):
        state.validate_state(value)


def test_unknown_duplicate_semantics_and_oversized_intent_are_refused():
    for mutate in (
        lambda v: v.update(clean=True),
        lambda v: v["binding"]["service"].update(password="private"),
        lambda v: v.update(operation="start"),
        lambda v: v.update(request_id="a" * 32),
        lambda v: v["binding"]["service"].update(control_group="/" + "x" * 9000),
    ):
        value = copy.deepcopy(valid_state())
        mutate(value)
        with pytest.raises(RecoverableStateError):
            state.validate_state(value)


def test_held_lease_rejects_untrusted_type_without_calling_it():
    class UntrustedLease:
        closed = False

        def close(self):
            self.closed = True

    lease = UntrustedLease()
    with pytest.raises(RecoverableStateError):
        state.AnchorState("unused", None, held_lease=lease)
    assert not lease.closed
