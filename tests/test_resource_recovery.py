"""Recovery authority uses native owner exclusion, never stale PID inference."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from uuid import uuid4

import pytest

from drift.node import resource_recovery as recovery

DIGEST = "sha256:" + "a" * 64
HOST = "sha256:" + "b" * 64
BOOT = "11111111-1111-4111-8111-111111111111"
NEXT_BOOT = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def owner(tmp_path, monkeypatch):
    identity = recovery.RecoveryIdentity("windows", HOST, None)
    monkeypatch.setattr(recovery, "current_recovery_identity", lambda: identity)
    lease = recovery.open_owner_lease(tmp_path / "recovery", uuid4().hex)
    yield lease, tmp_path / "recovery"
    lease.close()


def binding(lease, kind="worker"):
    return recovery.make_generation_binding(lease.owner_binding, uuid4().hex, kind=kind, claim_digest=DIGEST)


def guard(directory, value, **kwargs):
    return recovery.acquire_recovery_guard(directory, value, expected_claim_digest=DIGEST, **kwargs)


def test_live_owner_excludes_recovery_and_closed_owner_needs_native_empty_proof(owner):
    lease, directory = owner
    value = binding(lease)
    with pytest.raises(recovery.RecoverableStateError) as error:
        with guard(directory, value):
            pytest.fail("live owner must exclude recovery")
    assert error.value.reason == "active_owner"
    lease.require_live()
    lease.close()
    lease.close()
    with pytest.raises(recovery.RecoverableStateError):
        lease.require_live()
    with guard(directory, value) as held:
        assert not os.get_inheritable(held._descriptor)
        with pytest.raises(recovery.RecoverableStateError) as error:
            held.prove_empty(windows_probe=lambda *args: False)
        assert error.value.reason == "cleanup_pending"
        with pytest.raises(recovery.RecoverableStateError):
            held.prove_empty(windows_probe=lambda *args: 1)
        proof = held.prove_empty(
            windows_probe=lambda candidate, authority: authority.require_binding(candidate) or True
        )
        assert proof.reason == "windows_job_empty"
        held.require_proof(proof)
        with pytest.raises(recovery.RecoverableStateError) as error:
            with guard(directory, value):
                pytest.fail("proof holder retains owner exclusion through commit")
        assert error.value.reason == "active_owner"
    with pytest.raises(recovery.RecoverableStateError):
        held.require_proof(proof)
    assert (directory / lease.owner_binding.lease_name).exists()
    assert (directory / lease.owner_binding.descriptor_name).exists()


def test_proof_is_immutable_and_cannot_cross_recovery_guards(owner):
    lease, directory = owner
    value = binding(lease, "metadata")
    lease.close()
    with guard(directory, value) as first:
        proof = first.prove_empty()
        with pytest.raises(FrozenInstanceError):
            proof.reason = "windows_job_empty"
    with guard(directory, value) as second:
        with pytest.raises(recovery.RecoverableStateError):
            second.require_proof(proof)


def test_partial_creation_closes_lease_and_preserves_unverifiable_evidence(tmp_path, monkeypatch):
    directory = tmp_path / "recovery"
    owner_id = uuid4().hex

    def cannot_publish(*args):
        raise OSError("private storage detail")

    monkeypatch.setattr(recovery._private, "_exclusive", cannot_publish)
    with pytest.raises(recovery.RecoverableStateError) as error:
        recovery.open_owner_lease(directory, owner_id)
    assert "private" not in str(error.value)
    assert (directory / (owner_id + ".lease")).exists()
    assert not (directory / (owner_id + ".owner.json")).exists()
    descriptor = os.open(directory / (owner_id + ".lease"), os.O_RDWR)
    recovery._lock(descriptor)  # Failed initialization did not leak a held lock.
    recovery._close(descriptor)


def test_binding_roundtrip_is_pure_and_names_exact_global_generation(owner, monkeypatch):
    lease, _ = owner
    value = binding(lease)
    encoded = value.to_json()
    monkeypatch.setattr(recovery, "current_recovery_identity", lambda: pytest.fail("codec performed I/O"))
    assert recovery.GenerationRecoveryBinding.from_json(json.loads(json.dumps(encoded))) == value
    assert value.containment_name == "Global\\CommunityAI-" + lease.owner_binding.owner_id + "-" + value.reservation_id
    assert value.contract == "windows_job_atomic_v1"
    assert value.reservation_id not in repr(value)
    assert HOST not in repr(value.owner)


@pytest.mark.parametrize(
    "change", ["version", "extra", "contract", "purpose", "job", "claim", "owner", "inode", "identity"]
)
def test_binding_rejects_malformed_or_ambiguous_contracts(owner, change):
    value = binding(owner[0]).to_json()
    if change == "version":
        value["schema_version"] = True
    elif change == "extra":
        value["unexpected"] = "private"
    elif change == "contract":
        value["contract"] = "windows_attach_after_spawn"
    elif change == "purpose":
        value["kind"] = "metadata"
    elif change == "job":
        value["containment_name"] = value["containment_name"].replace("Global", "Local")
    elif change == "claim":
        value["claim_digest"] = "not-a-digest"
    elif change == "owner":
        value["owner"]["owner_id"] = "../private"
    elif change == "inode":
        value["owner"]["lease_identity"][1] = True
    else:
        value["owner"]["identity"]["boot_id"] = BOOT
    with pytest.raises(recovery.RecoverableStateError):
        recovery.GenerationRecoveryBinding.from_json(value)


def test_legacy_and_claim_mismatch_never_receive_authority(owner):
    lease, directory = owner
    value = binding(lease)
    with pytest.raises(recovery.RecoverableStateError) as error:
        recovery.GenerationRecoveryBinding.from_json(None)
    assert error.value.reason == "legacy_state"
    lease.close()
    with pytest.raises(recovery.RecoverableStateError):
        with recovery.acquire_recovery_guard(directory, value, expected_claim_digest="sha256:" + "c" * 64):
            pytest.fail("different claim must not receive authority")
    with guard(directory, value) as held:
        with pytest.raises(recovery.RecoverableStateError):
            held.require_binding(replace(value, claim_digest="sha256:" + "c" * 64))


@pytest.mark.parametrize("change", ["lease_replaced", "lease_missing", "owner_missing", "owner_changed", "hardlink"])
def test_missing_replaced_or_redirected_private_files_fail_closed(owner, change):
    lease, directory = owner
    value = binding(lease)
    lease.close()
    lease_path = directory / value.owner.lease_name
    owner_path = directory / value.owner.descriptor_name
    if change == "lease_replaced":
        temporary = directory / "replacement"
        temporary.write_bytes(b"\0")
        temporary.chmod(0o600)
        os.replace(temporary, lease_path)
    elif change == "lease_missing":
        lease_path.unlink()
    elif change == "owner_missing":
        owner_path.unlink()
    elif change == "hardlink":
        os.link(lease_path, directory / "alias")
    else:
        data = json.loads(owner_path.read_text())
        data["owner_id"] = "0" * 32
        owner_path.write_text(json.dumps(data))
    with pytest.raises(recovery.RecoverableStateError):
        with guard(directory, value):
            pytest.fail("unverified private state must remain retained")


def test_excluded_owner_and_identity_are_revalidated_after_native_callback(owner, monkeypatch):
    lease, directory = owner
    value = binding(lease)
    lease.close()
    with guard(directory, value) as held:

        def changed(candidate, authority):
            authority.require_binding(candidate)
            monkeypatch.setattr(
                recovery,
                "current_recovery_identity",
                lambda: recovery.RecoveryIdentity("windows", "sha256:" + "c" * 64, None),
            )
            return True

        with pytest.raises(recovery.RecoverableStateError):
            held.prove_empty(windows_probe=changed)


def test_cancellation_before_or_during_probe_cannot_publish_proof(owner):
    lease, directory = owner
    value = binding(lease)
    lease.close()
    cancelled = [True]
    with pytest.raises(recovery.RecoverableStateError) as error:
        with guard(directory, value, cancelled=lambda: cancelled[0]):
            pytest.fail("cancelled recovery must not acquire authority")
    assert error.value.reason == "cleanup_pending"
    cancelled[0] = False
    with guard(directory, value, cancelled=lambda: cancelled[0]) as held:

        def cancel_during_probe(*args):
            cancelled[0] = True
            return True

        with pytest.raises(recovery.RecoverableStateError):
            held.prove_empty(windows_probe=cancel_during_probe)


def test_native_error_does_not_leak_private_details(owner):
    lease, directory = owner
    value = binding(lease)
    lease.close()
    with guard(directory, value) as held:

        def unavailable(*args):
            raise OSError("private path and token")

        with pytest.raises(recovery.RecoverableStateError) as error:
            held.prove_empty(windows_probe=unavailable)
    assert error.value.reason == "unverifiable_state"
    assert "private" not in str(error.value)


@pytest.mark.parametrize("new_boot", [False, True])
def test_linux_worker_requires_exact_changed_boot_on_same_host(tmp_path, monkeypatch, new_boot):
    identity = [recovery.RecoveryIdentity("linux", HOST, BOOT)]
    monkeypatch.setattr(recovery, "current_recovery_identity", lambda: identity[0])
    directory = tmp_path / "recovery"
    lease = recovery.open_owner_lease(directory, uuid4().hex)
    value = binding(lease)
    lease.close()
    if new_boot:
        identity[0] = replace(identity[0], boot_id=NEXT_BOOT)
    with guard(directory, value) as held:
        if new_boot:
            proof = held.prove_empty(windows_probe=lambda *args: pytest.fail("Linux must not query Windows"))
            assert proof.reason == "linux_previous_boot"
        else:
            with pytest.raises(recovery.RecoverableStateError) as error:
                held.prove_empty(windows_probe=lambda *args: pytest.fail("same-boot Linux has no whole-tree proof"))
            assert error.value.reason == "unsupported_platform"
    identity[0] = recovery.RecoveryIdentity("linux", "sha256:" + "d" * 64, NEXT_BOOT)
    with pytest.raises(recovery.RecoverableStateError):
        with guard(directory, value):
            pytest.fail("foreign host boot does not prove this host's previous tree is gone")


def test_synchronous_metadata_requires_owner_exclusion_and_has_no_job(owner):
    lease, directory = owner
    value = binding(lease, "metadata")
    assert value.contract == "synchronous_metadata_v1" and value.containment_name is None
    lease.close()
    with guard(directory, value) as held:
        proof = held.prove_empty(windows_probe=lambda *args: pytest.fail("metadata never creates a child job"))
        assert proof.reason == "metadata_owner_excluded"
        held.require_proof(proof)


def test_never_reuse_owner_and_bound_private_inventory(owner, monkeypatch):
    lease, directory = owner
    lease.close()
    with pytest.raises(recovery.RecoverableStateError):
        recovery.open_owner_lease(directory, lease.owner_binding.owner_id)
    monkeypatch.setattr(recovery, "_MAX_OWNERS", 1)
    with pytest.raises(recovery.RecoverableStateError):
        recovery.open_owner_lease(directory, uuid4().hex)


CHILD = r"""
import importlib.util,json,os,sys,types
for name in ('drift','drift.node'):
    package=types.ModuleType(name); package.__path__=[]; sys.modules[name]=package
for name,path in (('worker_loading',os.environ['LOADING_SOURCE']),('resource_recovery',os.environ['RECOVERY_SOURCE'])):
    spec=importlib.util.spec_from_file_location('drift.node.'+name,path)
    module=importlib.util.module_from_spec(spec); sys.modules[spec.name]=module
    setattr(sys.modules['drift.node'],name,module); spec.loader.exec_module(module)
lease=module.open_owner_lease(os.environ['RECOVERY_DIRECTORY'],os.environ['RECOVERY_OWNER'])
print(json.dumps(lease.owner_binding.to_json()),flush=True)
sys.stdin.readline()
os._exit(0)
"""


def test_owner_hard_exit_releases_native_lease_without_deleting_evidence(tmp_path):
    directory = tmp_path / "recovery"
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", CHILD],
        env={
            **os.environ,
            "RECOVERY_DIRECTORY": str(directory),
            "RECOVERY_OWNER": uuid4().hex,
            "LOADING_SOURCE": recovery._private.__file__,
            "RECOVERY_SOURCE": recovery.__file__,
        },
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            raw = pool.submit(process.stdout.readline).result(timeout=8)
        assert raw, process.stderr.read()
        old_owner = recovery.OwnerBinding.from_json(json.loads(raw))
        value = recovery.make_generation_binding(old_owner, uuid4().hex, kind="metadata", claim_digest=DIGEST)
        with pytest.raises(recovery.RecoverableStateError) as error:
            with guard(directory, value):
                pytest.fail("independent active process owns this lease")
        assert error.value.reason == "active_owner"
        process.stdin.write("crash\n")
        process.stdin.flush()
        assert process.wait(timeout=5) == 0
        with guard(directory, value) as held:
            proof = held.prove_empty()
            held.require_proof(proof)
        assert (directory / old_owner.lease_name).exists()
        assert (directory / old_owner.descriptor_name).exists()
    finally:
        if process.poll() is None:
            process.stdin.close()
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()
