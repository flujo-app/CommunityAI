"""Native Windows jobs, abrupt owner death and real descendant recovery."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import psutil
import pytest

from drift.node.resource_recovery import (
    GenerationRecoveryBinding,
    RecoverableStateError,
    acquire_recovery_guard,
    make_generation_binding,
    open_owner_lease,
)
from drift.node.worker_recovery_containment import (
    RecoveryContainmentError,
    WindowsRecoveryContainment,
    create_recovery_containment,
    recover_windows_containment,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows atomic job qualification")
DIGEST = "sha256:" + "c" * 64


def options(**updates):
    result = dict(
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=os.environ.copy(),
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    result.update(updates)
    return result


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("native child did not reach the expected state")


@pytest.fixture
def generation(tmp_path):
    lease = open_owner_lease(tmp_path / "owners", uuid4().hex)
    binding = make_generation_binding(lease.owner_binding, uuid4().hex, kind="worker", claim_digest=DIGEST)
    job = create_recovery_containment(binding)
    yield lease, binding, job
    if job._job is not None:
        job.terminate()
        wait_for(lambda: not job.has_members())
        job.close()
    lease.close()


def test_child_is_contained_and_suspended_before_any_user_code(generation, tmp_path):
    _, _, job = generation
    marker = tmp_path / "executed"
    command = [
        sys.executable,
        "-c",
        "from pathlib import Path;import sys;Path(sys.argv[1]).write_text('ran');sys.stdout.buffer.write('héllo'.encode('utf-8'))",
        str(marker),
    ]
    process = job.spawn(command, **options())
    assert process.pid > 0 and process.poll() is None and job.has_members()
    job.attach(process)
    # User code is unable to execute while the actual primary thread is suspended.
    time.sleep(0.08)
    assert not marker.exists()
    with pytest.raises(subprocess.TimeoutExpired):
        process.wait(timeout=0.01)
    job.resume(process)
    assert process.wait(timeout=10) == 0
    assert process.stdout.read().strip() == "héllo"
    assert marker.read_text() == "ran"
    wait_for(lambda: not job.has_members())
    with pytest.raises(RecoveryContainmentError):
        job.spawn(command, **options())


def test_existing_generation_job_is_never_adopted_or_modified(generation):
    _, binding, job = generation
    with pytest.raises(RecoveryContainmentError):
        WindowsRecoveryContainment(binding)
    assert not job.has_members()


@pytest.mark.parametrize(
    "change",
    [
        {"creationflags": 0x01000000},
        {"stdin": None},
        {"stdout": None},
        {"text": False},
        {"env": {"KEY": "value", "key": "other"}},
        {"env": {"KEY": "bad\0value"}},
    ],
)
def test_unsupported_spawn_contract_has_no_process_effect(generation, change):
    _, _, job = generation
    with pytest.raises(RecoveryContainmentError):
        job.spawn([sys.executable, "-c", "raise SystemExit(99)"], **options(**change))
    assert not job._spawned and not job.has_members()


def test_failed_native_creation_leaves_empty_job_and_no_reusable_generation(generation, tmp_path):
    _, _, job = generation
    with pytest.raises(RecoveryContainmentError):
        job.spawn([str(tmp_path / "does-not-exist.exe")], **options())
    assert job._spawned and not job.has_members()


def test_owner_lease_and_other_inheritable_handles_are_not_inherited(generation, tmp_path):
    import msvcrt

    lease, _, job = generation
    extra = os.open(tmp_path / "private", os.O_RDWR | os.O_CREAT)
    os.set_inheritable(extra, True)
    handle = msvcrt.get_osfhandle(extra)
    try:
        source = (
            "import ctypes,sys;from ctypes import wintypes as w;"
            "k=ctypes.WinDLL('kernel32',use_last_error=True);"
            "k.GetHandleInformation.argtypes=(w.HANDLE,ctypes.POINTER(w.DWORD));"
            "flags=w.DWORD();print(int(bool(k.GetHandleInformation(int(sys.argv[1]),ctypes.byref(flags)))),flush=True)"
        )
        # Use the base interpreter so a venv launcher cannot hide an inherited handle.
        process = job.spawn([sys._base_executable, "-c", source, str(handle)], **options())
        job.resume(process)
        assert process.wait(timeout=10) == 0
        assert process.stdout.read().strip() == "0"
        assert not os.get_inheritable(lease._descriptor)
    finally:
        os.close(extra)


_OWNER = r"""
import json, os, sys, time
from pathlib import Path
from uuid import uuid4
from drift.node.resource_recovery import open_owner_lease, make_generation_binding
from drift.node.worker_recovery_containment import create_recovery_containment
root, stage = Path(sys.argv[1]), sys.argv[2]
lease = open_owner_lease(root / 'owners', uuid4().hex)
binding = make_generation_binding(lease.owner_binding, uuid4().hex, kind='worker', claim_digest='sha256:'+'c'*64)
job = create_recovery_containment(binding)
root.joinpath('binding.json').write_text(json.dumps(binding.to_json()))
child = None
if stage != 'before_spawn':
    grandchild = "import os,time;from pathlib import Path;Path("+repr(str(root/'grandchild'))+").write_text(str(os.getpid()));time.sleep(120)"
    source = "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',"+repr(grandchild)+"]);"
    source += "time.sleep(120)" if stage != 'root_exited' else "time.sleep(0.3)"
    import subprocess
    child = job.spawn([sys.executable,'-c',source], stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',bufsize=1,env=os.environ.copy(),creationflags=subprocess.CREATE_NO_WINDOW)
    if stage != 'before_resume':
        job.resume(child)
        deadline = time.monotonic()+15
        while not (root/'grandchild').exists():
            if time.monotonic()>deadline: raise RuntimeError('descendant boot timeout')
            time.sleep(.01)
        if stage == 'root_exited': child.wait(timeout=10)
payload = json.dumps({'owner_pid':os.getpid(),'child_pid':None if child is None else child.pid})
root.joinpath('ready.pending').write_text(payload)
os.replace(root/'ready.pending', root/'ready.json')
sys.stdin.readline()
os._exit(93)
"""


@pytest.mark.parametrize("stage", ["before_spawn", "before_resume", "after_resume", "root_exited"])
def test_abrupt_owner_death_recovers_complete_native_tree_under_real_exclusive_lease(tmp_path, stage):
    owner = subprocess.Popen(
        [sys.executable, "-c", _OWNER, str(tmp_path), stage],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    holder = None
    binding = None
    try:
        # A fresh interpreter imports the real package (including installed
        # dependencies). Bound startup separately from native cleanup, and use
        # the explicit handoff so a partial fixture file is never read.
        ready = tmp_path / "ready.json"
        wait_for(lambda: ready.exists() or owner.poll() is not None, timeout=45)
        assert ready.exists(), owner.stderr.read()
        info = json.loads(ready.read_text())
        binding = GenerationRecoveryBinding.from_json(json.loads((tmp_path / "binding.json").read_text()))
        # Retain a query-only handle: this keeps the job alive after owner crash,
        # making recovery prove/terminate actual descendants instead of relying
        # solely on KILL_ON_JOB_CLOSE. It grants no creation/assignment authority.
        from drift.node.worker_recovery_containment import _WindowsAPI

        api = _WindowsAPI()
        holder = api.k.OpenJobObjectW(0x0004, False, binding.containment_name)
        assert holder
        with pytest.raises(RecoverableStateError, match="still active"):
            with acquire_recovery_guard(tmp_path / "owners", binding, expected_claim_digest=DIGEST):
                pytest.fail("live owner must exclude recovery")
        if stage != "before_spawn":
            assert api.active(holder)
        grandchild = None
        if stage in ("after_resume", "root_exited"):
            grandchild = psutil.Process(int((tmp_path / "grandchild").read_text()))
            assert grandchild.is_running()
        # Kill the actual interpreter, not merely a Windows venv launcher.
        psutil.Process(info["owner_pid"]).kill()
        owner.wait(timeout=10)
        with acquire_recovery_guard(tmp_path / "owners", binding, expected_claim_digest=DIGEST) as guard:
            proof = guard.prove_empty(windows_probe=recover_windows_containment)
            guard.require_proof(proof)
            assert not api.active(holder)
        if grandchild is not None:
            wait_for(lambda: not grandchild.is_running())
        if stage == "before_resume":
            assert not (tmp_path / "grandchild").exists()
    finally:
        if owner.poll() is None:
            try:
                owner.stdin.write("crash\n")
                owner.stdin.flush()
                owner.wait(timeout=10)
            except Exception:
                for child in psutil.Process(owner.pid).children(recursive=True):
                    child.kill()
                owner.kill()
        if holder is not None:
            # No leaked sleeping descendants even when an assertion fails.
            kill_handle = api.k.OpenJobObjectW(0x0008, False, binding.containment_name)
            if kill_handle:
                api.k.TerminateJobObject(kill_handle, 1)
                api.k.CloseHandle(kill_handle)
            api.k.CloseHandle(holder)


def test_absent_job_proof_requires_exact_guard_and_guard_cannot_be_reused(generation, tmp_path):
    lease, binding, job = generation
    job.close()
    with pytest.raises(RecoveryContainmentError):
        recover_windows_containment(binding, object())
    lease.close()
    with acquire_recovery_guard(tmp_path / "owners", binding, expected_claim_digest=DIGEST) as guard:
        assert recover_windows_containment(binding, guard)
    with pytest.raises(RecoverableStateError):
        recover_windows_containment(binding, guard)


def test_cancelled_guard_never_opens_or_terminates_live_job(generation, tmp_path):
    lease, binding, job = generation
    process = job.spawn([sys.executable, "-c", "import time;time.sleep(120)"], **options())
    job.resume(process)
    lease.close()
    cancelled = [False]
    with acquire_recovery_guard(
        tmp_path / "owners", binding, expected_claim_digest=DIGEST, cancelled=lambda: cancelled[0]
    ) as guard:
        cancelled[0] = True
        with pytest.raises(RecoverableStateError):
            recover_windows_containment(binding, guard)
        assert process.poll() is None and job.has_members()


def test_verified_nonempty_timeout_is_retryable_without_claiming_empty(generation, tmp_path, monkeypatch):
    from drift.node import worker_recovery_containment as native

    lease, binding, job = generation
    process = job.spawn([sys.executable, "-c", "import time;time.sleep(120)"], **options())
    job.resume(process)
    lease.close()
    original = native._WindowsAPI.active
    with acquire_recovery_guard(tmp_path / "owners", binding, expected_claim_digest=DIGEST) as guard:
        # Model a kernel tree still draining after successful termination. Real
        # Open/QueryLimits/Terminate calls still use this fixture's actual job.
        monkeypatch.setattr(native._WindowsAPI, "active", lambda self, handle: True)
        with pytest.raises(RecoverableStateError) as error:
            recover_windows_containment(binding, guard, timeout=0)
        assert error.value.reason == "cleanup_pending"
        monkeypatch.setattr(native._WindowsAPI, "active", original)
        assert recover_windows_containment(binding, guard)
    assert process.wait(timeout=5) is not None
