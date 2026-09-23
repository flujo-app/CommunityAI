"""Codec fixtures only; native persistence and lock tests live separately."""

import copy
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
        ("schema_version", 2),
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
