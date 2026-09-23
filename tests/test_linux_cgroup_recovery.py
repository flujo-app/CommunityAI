"""Strict identity codecs and authority boundaries; native checks are opt-in."""

import copy
import json
import os
import select
import stat
import sys
import threading
import time
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from drift.node import linux_cgroup_recovery as cgroups, resource_recovery as recovery

DIGEST = "sha256:" + "a" * 64
HOST = "sha256:" + "b" * 64
BOOT = "11111111-1111-4111-8111-111111111111"
NEXT_BOOT = "22222222-2222-4222-8222-222222222222"


def profile():
    return cgroups.LinuxCgroupProfile(
        "/sys/fs/cgroup/communityai", (5, 20), 123, "/", "/sys/fs/cgroup", ((1, 10), (1, 11), (1, 12)), 1000
    )


@pytest.fixture
def generation(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, "current_recovery_identity", lambda: recovery.RecoveryIdentity("linux", HOST, BOOT))
    directory = tmp_path / "owners"
    owner = recovery.open_owner_lease(directory, uuid4().hex)
    reservation = uuid4().hex
    identity = cgroups.LinuxCgroupIdentity(
        profile(), cgroups.generation_name(owner.owner_binding.owner_id, reservation), (5, 30)
    )
    binding = recovery.make_generation_binding(
        owner.owner_binding, reservation, kind="worker", claim_digest=DIGEST, linux_cgroup=identity
    )
    yield owner, directory, binding
    owner.close()


def guarded(directory, binding, **kwargs):
    return recovery.acquire_recovery_guard(directory, binding, expected_claim_digest=DIGEST, **kwargs)


def test_schema2_roundtrip_is_pure_and_cannot_modify_generation_identity(generation, monkeypatch):
    owner, _, binding = generation
    value = binding.to_json()
    assert value["schema_version"] == 2 and binding.contract == "linux_cgroup_v1"
    monkeypatch.setattr(cgroups, "_platform", lambda: pytest.fail("codec performed platform I/O"))
    assert recovery.GenerationRecoveryBinding.from_json(json.loads(json.dumps(value))) == binding
    assert owner.owner_binding.owner_id not in repr(binding.linux_cgroup)
    with pytest.raises(FrozenInstanceError):
        binding.linux_cgroup.name = "changed"
    with pytest.raises(recovery.RecoverableStateError):
        replace(binding, reservation_id=uuid4().hex)
    with pytest.raises(recovery.RecoverableStateError):
        replace(binding, kind="metadata")


@pytest.mark.parametrize(
    "field,value",
    [
        ("root", "/sys/fs/cgroup/../elsewhere"),
        ("root", "/sys/fs/cgroup"),
        ("root", "//sys/fs/cgroup/communityai"),
        ("root", "/sys/fs/cgroup/communityai\x00"),
        ("root", "/sys/fs/cgroup/communityai\x7f"),
        ("root", "/sys/fs/cgroup/communityai\\child"),
        ("mount_id", True),
        ("mount_id", 0),
        ("root_identity", (5, 0)),
        ("root_identity", (True, 10)),
        ("namespaces", ((1, 2),)),
        ("uid", -1),
        ("mount_point", "/unrelated"),
    ],
)
def test_profile_rejects_ambiguous_or_unbounded_identity(field, value):
    with pytest.raises(recovery.RecoverableStateError):
        replace(profile(), **{field: value})


@pytest.mark.parametrize(
    "mutation", ["extra", "version", "missing", "traversal", "other_generation", "cross_device", "wrong_schema"]
)
def test_binding_codec_rejects_changed_schema_and_native_identity(generation, mutation):
    _, _, binding = generation
    value = copy.deepcopy(binding.to_json())
    if mutation == "extra":
        value["linux_cgroup"]["surprise"] = "ignored authority"
    elif mutation == "version":
        value["linux_cgroup"]["schema_version"] = True
    elif mutation == "missing":
        del value["linux_cgroup"]
    elif mutation == "traversal":
        value["linux_cgroup"]["name"] = "../other"
    elif mutation == "other_generation":
        value["linux_cgroup"]["name"] = cgroups.generation_name(uuid4().hex, uuid4().hex)
    elif mutation == "cross_device":
        value["linux_cgroup"]["directory_identity"][0] += 1
    else:
        value["schema_version"] = 1
    with pytest.raises(recovery.RecoverableStateError):
        recovery.GenerationRecoveryBinding.from_json(value)


def test_legacy_linux_codec_and_same_boot_rejection_are_preserved(generation):
    owner, directory, _ = generation
    old = recovery.make_generation_binding(owner.owner_binding, uuid4().hex, kind="worker", claim_digest=DIGEST)
    encoded = old.to_json()
    assert encoded["schema_version"] == 1 and "linux_cgroup" not in encoded and old.contract == "linux_boot_v1"
    assert recovery.GenerationRecoveryBinding.from_json(encoded) == old
    owner.close()
    with guarded(directory, old) as guard:
        with pytest.raises(recovery.RecoverableStateError) as error:
            guard.prove_empty(linux_probe=lambda *args: pytest.fail("legacy had no same-boot authority"))
        assert error.value.reason == "unsupported_platform"


def test_live_owner_excludes_new_cgroup_proof_and_empty_proof_is_guard_bound(generation):
    owner, directory, binding = generation
    with pytest.raises(recovery.RecoverableStateError) as error:
        with guarded(directory, binding):
            pytest.fail("live owner acquired")
    assert error.value.reason == "active_owner"
    owner.close()
    with guarded(directory, binding) as guard:
        with pytest.raises(recovery.RecoverableStateError):
            guard.prove_empty()
        with pytest.raises(recovery.RecoverableStateError):
            guard.prove_empty(linux_probe=lambda *args: 1)
        proof = guard.prove_empty(linux_probe=lambda binding, authority: authority.require_binding(binding) or True)
        assert proof.reason == "linux_cgroup_empty"
        guard.require_proof(proof)
    with pytest.raises(recovery.RecoverableStateError):
        guard.require_proof(proof)


def test_new_boot_does_not_open_obsolete_cgroup_path(generation, monkeypatch):
    owner, directory, binding = generation
    owner.close()
    monkeypatch.setattr(
        recovery, "current_recovery_identity", lambda: recovery.RecoveryIdentity("linux", HOST, NEXT_BOOT)
    )
    with guarded(directory, binding) as guard:
        proof = guard.prove_empty(linux_probe=lambda *args: pytest.fail("previous boot has no live cgroup"))
        assert proof.reason == "linux_previous_boot"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "populated 2\n",
        "populated 0\npopulated 0\n",
        "populated 0\nprivate secret\n",
        "populated\n",
        "populated 0 " + "x" * 4096,
    ],
)
def test_malformed_population_never_means_empty(text):
    with pytest.raises(recovery.RecoverableStateError):
        cgroups._events(text)


def test_subtree_population_is_exact_and_independent_of_freeze():
    assert cgroups._events("populated 1\nfrozen 1\n")
    assert not cgroups._events("populated 0\nfrozen 0\n")


@pytest.mark.parametrize("requested,frozen", [("1", "0"), ("0", "1"), ("1", "1"), ("0", None)])
def test_frozen_root_denies_admission_but_keeps_structural_recovery_preflight(monkeypatch, requested, frozen):
    closed, probes = [], []
    monkeypatch.setattr(cgroups, "_platform", lambda: None)
    monkeypatch.setattr(cgroups, "_open_root", lambda path: 44)
    monkeypatch.setattr(cgroups, "_observe_root", lambda path, descriptor: profile())
    monkeypatch.setattr(cgroups, "_validate_backend", lambda: probes.append(True))
    monkeypatch.setattr(os, "close", closed.append)
    events = "populated 1\n" + (f"frozen {frozen}\n" if frozen is not None else "")
    monkeypatch.setattr(
        cgroups, "_read_control", lambda descriptor, name: requested + "\n" if name == "cgroup.freeze" else events
    )
    with pytest.raises(recovery.RecoverableStateError) as error:
        cgroups.validate_cgroup_profile(profile().root)
    assert error.value.reason == "unsupported_platform" and probes == []
    assert cgroups.validate_cgroup_profile(profile().root, require_unfrozen=False) == profile()
    assert probes == [True] and closed == [44, 44]


@pytest.mark.parametrize("frozen_descriptor", [44, 45])
@pytest.mark.parametrize("requested,frozen", [("1", "0"), ("0", "1")])
def test_root_or_leaf_freeze_blocks_birth_without_blocking_cleanup(
    generation, monkeypatch, frozen_descriptor, requested, frozen
):
    from drift.node import linux_cgroup_process

    owner, _, binding = generation
    native = cgroups.LinuxCgroupContainment(
        binding.linux_cgroup, owner.owner_binding, binding.reservation_id, 44, 45, cgroups._KEY
    )
    native._binding = binding
    monkeypatch.setattr(native, "_validate", lambda: None)

    def control(descriptor, name):
        if name == "cgroup.freeze":
            return (requested if descriptor == frozen_descriptor else "0") + "\n"
        return "populated 1\nfrozen " + (frozen if descriptor == frozen_descriptor else "0") + "\n"

    monkeypatch.setattr(cgroups, "_read_control", control)
    monkeypatch.setattr(linux_cgroup_process, "spawn", lambda *args, **kwargs: pytest.fail("frozen child created"))
    with pytest.raises(recovery.RecoverableStateError) as error:
        native.spawn([sys.executable])
    assert error.value.reason == "unsupported_platform" and not native._spawned
    assert native.has_members()
    writes, closed = [], []
    monkeypatch.setattr(cgroups, "_control", lambda *args, **kwargs: 46)
    monkeypatch.setattr(os, "write", lambda descriptor, data: writes.append((descriptor, data)) or len(data))
    monkeypatch.setattr(os, "close", closed.append)
    native.terminate()
    assert writes == [(46, b"1\n")] and closed == [46]


def test_mount_record_requires_exact_cgroup2_mount_and_device(monkeypatch):
    monkeypatch.setattr(os, "major", lambda device: 0, raising=False)
    monkeypatch.setattr(os, "minor", lambda device: 26, raising=False)
    text = "123 45 0:26 / /sys/fs/cgroup rw,nosuid,nodev - cgroup2 cgroup rw\n"
    assert cgroups._mount_record(text, 123, 26) == ("/", "/sys/fs/cgroup")
    for changed in (
        text.replace("cgroup2", "tmpfs"),
        text.replace("0:26", "0:27"),
        text + text,
        text.replace("123", "124", 1),
    ):
        with pytest.raises(recovery.RecoverableStateError):
            cgroups._mount_record(changed, 123, 26)


@pytest.mark.parametrize("name", ["\u97f3\u697d", "with\u00a0space", "with\u2028separator", "with\u0085separator"])
def test_mountinfo_accepts_unrelated_unicode_names_without_retokenizing(tmp_path, monkeypatch, name):
    monkeypatch.setattr(os, "major", lambda device: 0, raising=False)
    monkeypatch.setattr(os, "minor", lambda device: 26, raising=False)
    path = tmp_path / "mountinfo"
    path.write_bytes(
        (
            f"456 45 0:50 / /mnt/{name} rw - ext4 /dev/sdb rw\n"
            f"123 45 0:26 / /sys/fs/{name} rw,nosuid,nodev - cgroup2 cgroup rw\n"
        ).encode("utf-8")
    )
    text = cgroups._read_path(path, cgroups._MAX_MOUNTINFO, filesystem_text=True)
    assert cgroups._mount_record(text, 123, 26) == ("/", f"/sys/fs/{name}")
    with pytest.raises(UnicodeDecodeError):
        cgroups._read_path(path, cgroups._MAX_MOUNTINFO)
    with pytest.raises(recovery.RecoverableStateError):
        cgroups._read_path(path, 8, filesystem_text=True)


@pytest.mark.parametrize("mode,uid", [(0o555, 1000), (0o777, 1000), (0o775, 1000), (0o700, 1001)])
def test_native_directory_requires_exclusive_owner_control(monkeypatch, mode, uid):
    with monkeypatch.context() as patch:
        patch.setattr(os, "geteuid", lambda: 1000, raising=False)
        patch.setattr(
            os, "fstat", lambda fd: SimpleNamespace(st_mode=stat.S_IFDIR | mode, st_uid=uid, st_dev=5, st_ino=30)
        )
        with pytest.raises(recovery.RecoverableStateError):
            cgroups._identity(44)


class ObservedCgroup:
    def __init__(self, *, remains=False, after_kill=None):
        self.populated, self.remains, self.after_kill = True, remains, after_kill
        self.kills = self.closed = 0

    def has_members(self):
        return self.populated

    def terminate(self):
        self.kills += 1
        self.populated = self.remains
        if self.after_kill:
            self.after_kill()

    def close(self):
        self.closed += 1


def test_native_probe_requires_guard_terminates_subtree_then_checks_empty(generation, monkeypatch):
    owner, directory, binding = generation
    owner.close()
    native = ObservedCgroup()
    monkeypatch.setattr(cgroups, "_reopen_generation", lambda value: native)
    with pytest.raises(recovery.RecoverableStateError):
        cgroups.recover_linux_cgroup(binding, object())
    assert native.kills == 0
    with guarded(directory, binding) as guard:
        proof = guard.prove_empty(linux_probe=cgroups.recover_linux_cgroup)
        guard.require_proof(proof)
    assert native.kills == native.closed == 1


def test_native_probe_timeout_retains_retryable_authority(generation, monkeypatch):
    owner, directory, binding = generation
    owner.close()
    native = ObservedCgroup(remains=True)
    monkeypatch.setattr(cgroups, "_reopen_generation", lambda value: native)
    with guarded(directory, binding) as guard:
        with pytest.raises(recovery.RecoverableStateError) as error:
            cgroups.recover_linux_cgroup(binding, guard, timeout=0)
        assert error.value.reason == "cleanup_pending"
    assert native.kills == native.closed == 1


def test_cancel_after_kill_never_yields_death_proof(generation, monkeypatch):
    owner, directory, binding = generation
    owner.close()
    cancel = threading.Event()
    native = ObservedCgroup(after_kill=cancel.set)
    monkeypatch.setattr(cgroups, "_reopen_generation", lambda value: native)
    with guarded(directory, binding, cancelled=cancel.is_set) as guard:
        with pytest.raises(recovery.RecoverableStateError) as error:
            cgroups.recover_linux_cgroup(binding, guard)
        assert error.value.reason == "cleanup_pending"
    assert native.closed == 1


@pytest.mark.parametrize(
    "failure", [FileNotFoundError("private cgroup"), OSError("private mount"), PermissionError("private owner")]
)
def test_missing_replaced_inaccessible_native_state_is_never_absence_proof(generation, monkeypatch, failure):
    owner, directory, binding = generation
    owner.close()
    monkeypatch.setattr(cgroups, "_reopen_generation", lambda value: (_ for _ in ()).throw(failure))
    with guarded(directory, binding) as guard:
        with pytest.raises(recovery.RecoverableStateError) as error:
            cgroups.recover_linux_cgroup(binding, guard)
        assert error.value.reason == "unverifiable_state" and "private" not in str(error.value)


def test_closed_containment_does_not_remove_generation_or_accept_empty(monkeypatch):
    identity = cgroups.LinuxCgroupIdentity(profile(), cgroups.generation_name("a" * 32, "b" * 32), (5, 30))
    native = cgroups.LinuxCgroupContainment(identity, None, "b" * 32, 44, 45, cgroups._KEY)
    closed = []
    monkeypatch.setattr(os, "close", closed.append)
    native.close()
    native.close()
    assert closed == [45, 44]
    with pytest.raises(recovery.RecoverableStateError):
        native.has_members()


def test_other_platform_cannot_validate_explicit_profile(monkeypatch):
    monkeypatch.setattr(cgroups.sys, "platform", "win32")
    with pytest.raises(recovery.RecoverableStateError) as error:
        cgroups.validate_cgroup_profile("/sys/fs/cgroup/communityai")
    assert error.value.reason == "unsupported_platform"


def test_descriptor_cleanup_attempts_every_handle_and_sanitizes_failure(monkeypatch):
    closed = []

    def close(descriptor):
        closed.append(descriptor)
        if descriptor == 10:
            raise OSError("private native cleanup detail")

    monkeypatch.setattr(os, "close", close)
    with pytest.raises(recovery.RecoverableStateError) as error:
        cgroups._close_descriptors(10, None, 11)
    assert closed == [10, 11]
    assert "private" not in str(error.value)


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or not os.environ.get("COMMUNITYAI_TEST_CGROUP_ROOT"),
    reason="requires an explicitly delegated native cgroup2 test root and compiled backend",
)
def test_native_generation_survives_close_and_recovery_requires_identical_leaf(tmp_path):
    root = os.environ["COMMUNITYAI_TEST_CGROUP_ROOT"]
    owner = recovery.open_owner_lease(tmp_path / "owners", uuid4().hex)
    reservation = uuid4().hex
    containment = None
    try:
        containment = cgroups.prepare_generation(root, owner.owner_binding, reservation)
        binding = recovery.make_generation_binding(
            owner.owner_binding, reservation, kind="worker", claim_digest=DIGEST, linux_cgroup=containment.identity
        )
        containment.bind(binding)
        assert not os.get_inheritable(containment.cgroup_fd) and not containment.has_members()
        containment.close()
        assert os.path.isdir(root + "/" + binding.containment_name)
        owner.close()
        with guarded(tmp_path / "owners", binding) as guard:
            proof = guard.prove_empty(linux_probe=cgroups.recover_linux_cgroup)
            guard.require_proof(proof)
        assert os.path.isdir(root + "/" + binding.containment_name)
    finally:
        if containment is not None:
            containment.close()
        owner.close()


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or not os.environ.get("COMMUNITYAI_TEST_CGROUP_ROOT"),
    reason="requires an explicitly delegated native cgroup2 test root and compiled backend",
)
def test_native_missing_then_recreated_empty_generation_is_not_death_proof(tmp_path):
    root = os.environ["COMMUNITYAI_TEST_CGROUP_ROOT"]
    owner = recovery.open_owner_lease(tmp_path / "owners", uuid4().hex)
    containment = None
    try:
        reservation = uuid4().hex
        containment = cgroups.prepare_generation(root, owner.owner_binding, reservation)
        binding = recovery.make_generation_binding(
            owner.owner_binding, reservation, kind="worker", claim_digest=DIGEST, linux_cgroup=containment.identity
        )
        containment.bind(binding)
        assert not containment.has_members()
        path = root + "/" + binding.containment_name
        containment.close()
        owner.close()
        # Only this test's fresh, empty generation is replaced. It has never
        # received a child; production never removes a pre-commit generation.
        os.rmdir(path)
        with guarded(tmp_path / "owners", binding) as guard:
            with pytest.raises(recovery.RecoverableStateError):
                guard.prove_empty(linux_probe=cgroups.recover_linux_cgroup)
        os.mkdir(path)
        assert (os.stat(path).st_dev, os.stat(path).st_ino) != binding.linux_cgroup.directory_identity
        with guarded(tmp_path / "owners", binding) as guard:
            with pytest.raises(recovery.RecoverableStateError):
                guard.prove_empty(linux_probe=cgroups.recover_linux_cgroup)
    finally:
        if containment is not None:
            containment.close()
        owner.close()


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or not os.environ.get("COMMUNITYAI_TEST_CGROUP_ROOT"),
    reason="requires an explicitly delegated native cgroup2 test root and compiled backend",
)
@pytest.mark.parametrize("freeze_after_birth", [False, True])
def test_native_recovery_kills_nested_cgroup_subtree_after_owner_exclusion(tmp_path, freeze_after_birth):
    root = os.environ["COMMUNITYAI_TEST_CGROUP_ROOT"]
    owner = recovery.open_owner_lease(tmp_path / "owners", uuid4().hex)
    containment = process = leaf = None
    pidfds = []
    try:
        reservation = uuid4().hex
        containment = cgroups.prepare_generation(root, owner.owner_binding, reservation)
        binding = recovery.make_generation_binding(
            owner.owner_binding, reservation, kind="worker", claim_digest=DIGEST, linux_cgroup=containment.identity
        )
        containment.bind(binding)
        leaf = Path(root) / binding.containment_name
        nested = leaf / "fixture-nested"
        nested.mkdir()
        ready = tmp_path / "nested-ready.json"
        code = (
            "import json,os,subprocess,sys,time;from pathlib import Path;"
            "Path(sys.argv[1]).write_text(str(os.getpid()));"
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'],start_new_session=True);"
            "ready=Path(sys.argv[2]);temporary=ready.with_suffix('.tmp');"
            "temporary.write_text(json.dumps(dict(worker=os.getpid(),grandchild=child.pid)));"
            "os.replace(temporary,ready);time.sleep(120)"
        )
        process = containment.spawn(
            [sys.executable, "-c", code, str(nested / "cgroup.procs"), str(ready)], env=dict(os.environ)
        )
        containment.resume(process)
        deadline = time.monotonic() + 20
        while not ready.exists():
            if process.poll() is not None:
                pytest.fail("nested native fixture exited: " + process.stdout.read())
            if time.monotonic() >= deadline:
                pytest.fail("nested native fixture did not become ready")
            time.sleep(0.01)
        identities = json.loads(ready.read_text())
        assert identities["worker"] == process.pid
        assert (leaf / "cgroup.procs").read_text().strip() == ""
        assert set((nested / "cgroup.procs").read_text().split()) == {str(pid) for pid in identities.values()}
        assert cgroups._events((leaf / "cgroup.events").read_text())
        assert cgroups._events((nested / "cgroup.events").read_text())
        for pid in identities.values():
            pidfds.append(os.pidfd_open(pid))
        if freeze_after_birth:
            (leaf / "cgroup.freeze").write_text("1")
            deadline = time.monotonic() + 5
            while "frozen 1\n" not in (leaf / "cgroup.events").read_text():
                if time.monotonic() >= deadline:
                    pytest.fail("nested native fixture did not freeze")
                time.sleep(0.01)
            assert containment.has_members()
        with pytest.raises(recovery.RecoverableStateError) as error:
            with guarded(tmp_path / "owners", binding):
                pytest.fail("live owner allowed subtree cleanup")
        assert error.value.reason == "active_owner"

        # Retire this fixture owner's one-use spawn authority before dropping
        # its lease. No callback, metadata body or further old-owner spawn exists.
        containment.close()
        owner.close()
        with guarded(tmp_path / "owners", binding) as guard:
            proof = guard.prove_empty(linux_probe=cgroups.recover_linux_cgroup)
            guard.require_proof(proof)
        assert proof.reason == "linux_cgroup_empty"
        assert not cgroups._events((leaf / "cgroup.events").read_text())
        assert not cgroups._events((nested / "cgroup.events").read_text())
        for pidfd in pidfds:
            poller = select.poll()
            poller.register(pidfd, select.POLLIN)
            assert poller.poll(1000), "subtree proof preceded an exact descendant's death"
        assert process.wait(timeout=5) < 0
        assert leaf.is_dir() and nested.is_dir(), "death proof must preserve pre-commit cgroup identity"
    finally:
        # Test-only cleanup of the exact fresh fixture subtree, including when
        # an assertion failed before the production recovery path was entered.
        try:
            if leaf is not None:
                (leaf / "cgroup.kill").write_text("1")
            if process is not None:
                process.wait(timeout=5)
        finally:
            if process is not None and process.stdout is not None:
                process.stdout.close()
            for pidfd in pidfds:
                os.close(pidfd)
            if containment is not None:
                containment.close()
            owner.close()
