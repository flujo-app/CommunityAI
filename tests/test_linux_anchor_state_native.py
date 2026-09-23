"""Native storage/locks and real cgroup observations; systemd is a FIXTURE."""

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_linux_anchor_native import running  # noqa: F401

from drift.node import linux_anchor as anchor, linux_anchor_state as state
from drift.node.resource_recovery import RecoverableStateError

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or not os.environ.get("COMMUNITYAI_TEST_CGROUP_ROOT"),
    reason="requires private native cgroup fixture",
)


@pytest.fixture
def journal(running):
    root, directory, start = running
    _process, report = start()
    service = anchor.inspect_service()
    profiles = tuple(anchor.cg.LinuxCgroupProfile.from_json(p) for p in report["profiles"])

    def validate():
        anchor._require(anchor.inspect_service() == layout.service)
        anchor._require(anchor._observe_layout(layout.service) == layout.profiles)

    layout = SimpleNamespace(service=service, profiles=profiles, validate=validate)
    profile = directory / "private-profile"
    profile.mkdir(mode=0o700)
    owner = state.AnchorState(profile, layout, initialize=True)
    yield SimpleNamespace(owner=owner, profile=profile, layout=layout, root=root)
    owner.close()


def test_native_atomic_state_cas_and_same_invocation_reopen(journal):
    owner = journal.owner
    assert owner.value["phase"] == "checking" and owner.value["revision"] == 0
    assert not os.get_inheritable(owner.lease.fd)
    value = owner.write(0, phase="idle")
    value["phase"] = "blocked"
    assert owner.value["phase"] == "idle"
    with pytest.raises(RecoverableStateError):
        owner.write(0, phase="starting")
    assert not owner.poisoned and owner.value["revision"] == 1
    owner.close()
    reopened = state.AnchorState(journal.profile, journal.layout)
    try:
        assert reopened.value["phase"] == "idle" and reopened.value["revision"] == 1
    finally:
        reopened.close()


def test_other_process_cannot_take_profile_lease(journal):
    # No project imports or mock lock: a second kernel flock must be denied.
    script = "import fcntl,os,sys; fd=os.open(sys.argv[1],os.O_RDWR); fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)"
    result = subprocess.run(
        [sys.executable, "-c", script, str(journal.owner.lease.path)], capture_output=True, timeout=5
    )
    assert result.returncode != 0 and b"BlockingIOError" in result.stderr
    journal.owner.close()
    assert (
        subprocess.run([sys.executable, "-c", script, str(journal.profile / "anchor-state.lock")], timeout=5).returncode
        == 0
    )


def test_held_state_lease_transfers_without_an_unlock_or_reopen_gap(journal, monkeypatch):
    import fcntl

    journal.owner.close()
    lease = state.PrivateLease(journal.profile, "anchor-state.lock", create=False)
    descriptor = lease.fd
    identity = lease.identity
    script = "import fcntl,os,sys; fd=os.open(sys.argv[1],os.O_RDWR); fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)"
    before = subprocess.run([sys.executable, "-c", script, str(lease.path)], capture_output=True, timeout=5)
    assert before.returncode != 0 and b"BlockingIOError" in before.stderr

    original_open, original_close = state.os.open, state.os.close

    def guarded_open(path, *args, **kwargs):
        assert os.fspath(path) != os.fspath(lease.path)
        return original_open(path, *args, **kwargs)

    def guarded_close(fd):
        assert fd != descriptor
        return original_close(fd)

    def forbidden_flock(*_args):
        raise AssertionError("unexpected flock")

    with monkeypatch.context() as guard:
        guard.setattr(state.os, "open", guarded_open)
        guard.setattr(state.os, "close", guarded_close)
        guard.setattr(fcntl, "flock", forbidden_flock)
        owner = state.AnchorState(journal.profile, journal.layout, held_lease=lease)
    try:
        assert owner.lease is lease and owner.lease.fd == descriptor
        assert anchor._lock_identity(os.fstat(owner.lease.fd)) == identity
        during = subprocess.run([sys.executable, "-c", script, str(lease.path)], capture_output=True, timeout=5)
        assert during.returncode != 0 and b"BlockingIOError" in during.stderr
        assert owner.value["revision"] == 0
    finally:
        owner.close()
    assert lease.fd is None
    assert subprocess.run([sys.executable, "-c", script, str(lease.path)], timeout=5).returncode == 0


@pytest.mark.parametrize("invalid", ["wrong_root", "wrong_name", "closed", "replaced", "initialize"])
def test_invalid_held_state_lease_is_closed_without_mutating_state(journal, invalid):
    owner = journal.owner
    owner.close()
    before = owner.path.read_bytes()
    fingerprint = state.private._fingerprint(state.private._stat(owner.path))

    if invalid == "wrong_root":
        other = journal.profile / "other-profile"
        other.mkdir(mode=0o700)
        lease = state.PrivateLease(other, "anchor-state.lock")
        lease.close()
        lease = state.PrivateLease(other, "anchor-state.lock", create=False)
    elif invalid == "wrong_name":
        lease = state.node_lease(journal.profile)
        lease.close()
        lease = state.node_lease(journal.profile, create=False)
    else:
        lease = state.PrivateLease(journal.profile, "anchor-state.lock", create=False)
        if invalid == "closed":
            lease.close()
        elif invalid == "replaced":
            lease.path.rename(journal.profile / "retained-state-lock")
            lease.path.write_bytes(b"")
            lease.path.chmod(0o600)

    with pytest.raises(RecoverableStateError):
        state.AnchorState(
            journal.profile,
            journal.layout,
            initialize=invalid == "initialize",
            held_lease=lease,
        )
    assert lease.fd is None
    assert owner.path.read_bytes() == before
    assert state.private._fingerprint(state.private._stat(owner.path)) == fingerprint


def test_new_held_lease_cannot_recreate_missing_state_in_retained_profile(journal):
    root = journal.profile / "retained-incomplete-profile"
    root.mkdir(mode=0o700)
    (root / "anchor").mkdir(mode=0o700)
    retained = root / "retained-evidence"
    retained.write_bytes(b"existing profile evidence, never reset")
    retained.chmod(0o600)
    lease = state.PrivateLease(root, "anchor-state.lock")
    assert lease.created is True
    before = {p.name: p.lstat().st_ino for p in root.iterdir()}
    owner = None
    try:
        with pytest.raises(RecoverableStateError):
            owner = state.AnchorState(root, journal.layout, initialize=False, held_lease=lease)
        assert lease.fd is None
        assert not (root / "anchor" / "state.json").exists()
        assert {p.name: p.lstat().st_ino for p in root.iterdir()} == before
        assert retained.read_bytes() == b"existing profile evidence, never reset"
    finally:
        if owner is not None:
            owner.close()
        lease.close()


def test_held_state_lease_rejects_changed_layout_binding_without_mutation(journal):
    owner = journal.owner
    owner.close()
    before = owner.path.read_bytes()
    fingerprint = state.private._fingerprint(state.private._stat(owner.path))
    lease = state.PrivateLease(journal.profile, "anchor-state.lock", create=False)
    changed = SimpleNamespace(
        service=replace(journal.layout.service, invocation="f" * 32),
        profiles=journal.layout.profiles,
        validate=lambda: None,
    )
    with pytest.raises(RecoverableStateError):
        state.AnchorState(journal.profile, changed, held_lease=lease)
    assert lease.fd is None
    assert owner.path.read_bytes() == before
    assert state.private._fingerprint(state.private._stat(owner.path)) == fingerprint


def test_node_lease_is_separate_noninherited_and_never_removed(journal):
    first = state.node_lease(journal.profile)
    try:
        assert not os.get_inheritable(first.fd)
        with pytest.raises(RecoverableStateError):
            state.node_lease(journal.profile)
        journal.owner.validate()
    finally:
        first.close()
    assert (journal.profile / "node-lifetime.lock").is_file()
    second = state.node_lease(journal.profile)
    second.close()


@pytest.mark.parametrize("after_publish", [False, True])
def test_hard_owner_exit_releases_lock_but_retains_unacknowledged_intent(journal, after_publish):
    profile = journal.profile / "hard-exit-profile"
    profile.mkdir(mode=0o700)
    pid = os.fork()
    if pid == 0:
        try:
            owner = state.AnchorState(profile, journal.layout, initialize=True)
            if after_publish:
                original = state.private._replace

                def publish_without_ack(*args):
                    original(*args)
                    os._exit(75)

                state.private._replace = publish_without_ack
                owner.write(0, phase="idle")
            os._exit(75)
        except BaseException:
            os._exit(99)
    _pid, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 75
    recovered = state.AnchorState(profile, journal.layout)
    try:
        assert recovered.value["revision"] == int(after_publish)
        assert recovered.value["phase"] == ("idle" if after_publish else "checking")
        assert not {"clean", "admission", "maintenance"} & recovered.value.keys()
    finally:
        recovered.close()


def test_replaced_closed_marker_cannot_adopt_retained_state(journal):
    owner = journal.owner
    path = owner.lease.path
    owner.close()
    path.rename(journal.profile / "retained-marker")
    path.write_bytes(b"")
    path.chmod(0o600)
    with pytest.raises(RecoverableStateError):
        state.AnchorState(journal.profile, journal.layout)
    assert json.loads(owner.path.read_text())["revision"] == 0


@pytest.mark.parametrize("keep_other_data", [False, True])
def test_combined_marker_and_state_directory_loss_never_implies_first_use(journal, keep_other_data):
    owner = journal.owner
    owner.close()
    if keep_other_data:
        (journal.profile / "retained-node-config").write_bytes(b"private")
    owner.path.parent.rename(journal.profile.parent / "retained-anchor-evidence")
    (journal.profile / "anchor-state.lock").rename(journal.profile.parent / "retained-marker-evidence")
    with pytest.raises(RecoverableStateError):
        state.AnchorState(journal.profile, journal.layout)
    assert not owner.path.parent.exists() and not (journal.profile / "anchor-state.lock").exists()
    if keep_other_data:
        with pytest.raises(RecoverableStateError):
            state.AnchorState(journal.profile, journal.layout, initialize=True)
        assert (journal.profile / "retained-node-config").read_bytes() == b"private"


def test_first_use_requires_explicit_empty_profile_initialization(journal):
    profile = journal.profile / "fresh-profile"
    profile.mkdir(mode=0o700)
    with pytest.raises(RecoverableStateError):
        state.AnchorState(profile, journal.layout)
    assert list(profile.iterdir()) == []
    owner = state.AnchorState(profile, journal.layout, initialize=True)
    owner.close()
    with pytest.raises(RecoverableStateError):
        state.AnchorState(profile, journal.layout, initialize=True)


@pytest.mark.parametrize("boundary", ["file", "directory"])
def test_reopen_requires_successful_durability_confirmation(journal, monkeypatch, boundary):
    owner = journal.owner
    owner.close()
    if boundary == "file":
        monkeypatch.setattr(state.os, "fsync", lambda *args: (_ for _ in ()).throw(OSError("private volume")))
    else:
        monkeypatch.setattr(state, "_sync_directory", lambda *args: (_ for _ in ()).throw(OSError("private volume")))
    with pytest.raises(RecoverableStateError) as error:
        state.AnchorState(journal.profile, journal.layout)
    assert "private" not in str(error.value) and json.loads(owner.path.read_text())["revision"] == 0


@pytest.mark.parametrize("competing", ["write", "close"])
def test_same_instance_operations_cannot_overlap_publication(journal, monkeypatch, competing):
    owner = journal.owner
    entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
    original = state.private._replace

    def held_replace(*args):
        entered.set()
        assert release.wait(5)
        original(*args)

    def other():
        attempted.set()
        return owner.close() if competing == "close" else owner.write(0, phase="blocked")

    monkeypatch.setattr(state.private, "_replace", held_replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(owner.write, 0, phase="idle")
        try:
            assert entered.wait(5)
            second = pool.submit(other)
            assert attempted.wait(5)
            assert not second.done() and owner.lease.fd is not None
            with pytest.raises(TimeoutError):
                second.result(timeout=0.1)
        finally:
            release.set()
        assert first.result(timeout=5)["phase"] == "idle"
        if competing == "write":
            with pytest.raises(RecoverableStateError):
                second.result(timeout=5)
            assert not owner.poisoned and owner.value["revision"] == 1
        else:
            assert second.result(timeout=5) is None and owner.lease is None
    assert json.loads(owner.path.read_text())["phase"] == "idle"


@pytest.mark.parametrize("target", ["state", "directory", "marker"])
def test_loss_is_not_reinitialized(journal, target):
    owner = journal.owner
    owner.close()
    if target == "state":
        owner.path.unlink()
    elif target == "directory":
        owner.path.parent.rename(journal.profile / "retained-anchor")
    else:
        owner.lease = None
        (journal.profile / "anchor-state.lock").unlink()
    with pytest.raises(RecoverableStateError):
        state.AnchorState(journal.profile, journal.layout)
    with pytest.raises(RecoverableStateError):
        state.AnchorState(journal.profile, journal.layout, initialize=True)
    if target == "state":
        assert not owner.path.exists()
    if target == "directory":
        assert not owner.path.parent.exists()
    if target == "marker":
        assert json.loads(owner.path.read_text())["revision"] == 0


@pytest.mark.parametrize("target", ["state", "directory", "marker", "profile"])
def test_replacement_poison_is_sticky_even_if_original_is_restored(journal, target):
    owner = journal.owner
    path = dict(state=owner.path, directory=owner.path.parent, marker=owner.lease.path, profile=journal.profile)[target]
    saved = path.with_name(path.name + "-retained")
    path.rename(saved)
    if saved.is_dir():
        path.mkdir(mode=0o700)
    else:
        path.write_bytes(saved.read_bytes())
        path.chmod(0o600)
    with pytest.raises(RecoverableStateError):
        owner.validate()
    assert owner.poisoned
    if path.is_dir():
        path.rmdir()
    else:
        path.unlink()
    saved.rename(path)
    with pytest.raises(RecoverableStateError):
        owner.write(0, phase="idle")
    assert json.loads(owner.path.read_text())["revision"] == 0


@pytest.mark.parametrize("mutation", ["invocation", "boot", "layout"])
def test_new_invocation_boot_or_cgroup_identity_cannot_adopt(journal, monkeypatch, mutation):
    owner = journal.owner
    owner.close()
    if mutation == "invocation":
        journal.layout.service = replace(journal.layout.service, invocation="b" * 32)
    elif mutation == "boot":
        identity = state.current_recovery_identity()
        monkeypatch.setattr(
            state,
            "current_recovery_identity",
            lambda: replace(identity, boot_id="22222222-2222-4222-8222-222222222222"),
        )
    else:
        path = journal.root / "nodes"
        path.rmdir()
        path.mkdir(mode=0o700)
    with pytest.raises(RecoverableStateError):
        state.AnchorState(journal.profile, journal.layout)
    assert json.loads(owner.path.read_text())["revision"] == 0


@pytest.mark.parametrize("boundary", ["before_replace", "after_replace", "directory_fsync", "after_read", "interrupt"])
def test_interrupted_write_never_acknowledges_and_cannot_retry(journal, monkeypatch, boundary):
    owner = journal.owner
    original = state.private._replace
    synced = state._sync_directory
    reads = owner._read

    def failing_replace(path, value):
        if boundary == "interrupt":
            raise KeyboardInterrupt()
        if boundary != "before_replace":
            original(path, value)
        if boundary in {"before_replace", "after_replace"}:
            raise OSError("private storage details")

    def failing_sync(*args):
        if boundary == "directory_fsync":
            raise OSError("private mount")
        synced(*args)

    calls = []

    def failing_read():
        calls.append(True)
        if boundary == "after_read" and len(calls) == 2:
            raise OSError("private readback")
        return reads()

    monkeypatch.setattr(state.private, "_replace", failing_replace)
    monkeypatch.setattr(state, "_sync_directory", failing_sync)
    monkeypatch.setattr(owner, "_read", failing_read)
    with pytest.raises(KeyboardInterrupt if boundary == "interrupt" else RecoverableStateError):
        owner.write(0, phase="idle")
    assert owner.poisoned
    revision = json.loads(owner.path.read_text())["revision"]
    assert revision == (0 if boundary in {"before_replace", "interrupt"} else 1)
    with pytest.raises(RecoverableStateError):
        owner.write(revision, phase="idle")
    assert json.loads(owner.path.read_text())["revision"] == revision
    assert not list(owner.path.parent.glob(".loading-*"))


@pytest.mark.parametrize("boundary", ["mkdir", "exclusive", "dirsync"])
def test_failed_first_initialization_keeps_marker_and_refuses_reset(journal, monkeypatch, boundary):
    root = journal.profile / "new-profile"
    root.mkdir(mode=0o700)
    original_sync = state._sync_directory
    if boundary == "mkdir":
        original_mkdir = type(root).mkdir

        def failed_mkdir(path, *args, **kwargs):
            if path == root / "anchor":
                raise OSError()
            return original_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(type(root), "mkdir", failed_mkdir)
    if boundary == "exclusive":
        monkeypatch.setattr(state.private, "_exclusive", lambda *args: (_ for _ in ()).throw(OSError()))
    if boundary == "dirsync":

        def fail(path, identity):
            if path.name == "anchor":
                raise OSError()
            original_sync(path, identity)

        monkeypatch.setattr(state, "_sync_directory", fail)
    with pytest.raises(RecoverableStateError):
        state.AnchorState(root, journal.layout, initialize=True)
    assert (root / "anchor-state.lock").is_file()
    # If a full state reached disk but its fsync acknowledgement was lost,
    # reopening is not a clean grant: it remains checking in this invocation.
    if boundary == "dirsync":
        monkeypatch.setattr(state, "_sync_directory", original_sync)
        reopened = state.AnchorState(root, journal.layout)
        assert reopened.value["phase"] == "checking"
        reopened.close()
    else:
        with pytest.raises(RecoverableStateError):
            state.AnchorState(root, journal.layout)


@pytest.mark.parametrize(
    "payload", [b'{"schema_version":1,"schema_version":1}', b"[" * 1100 + b"]" * 1100, b"{}" * 5000, b'{"secret":NaN}']
)
def test_corrupt_or_unbounded_state_is_fixed_error_and_retained(journal, payload):
    owner = journal.owner
    owner.path.write_bytes(payload)
    with pytest.raises(RecoverableStateError) as error:
        owner.validate()
    assert owner.poisoned and "secret" not in str(error.value)
    assert owner.path.read_bytes() == payload


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public"])
def test_unsafe_marker_never_opens_a_new_owner(journal, kind):
    owner = journal.owner
    owner.close()
    path = journal.profile / "anchor-state.lock"
    if kind == "symlink":
        path.rename(journal.profile / "original-lock")
        path.symlink_to(journal.profile / "original-lock")
    elif kind == "hardlink":
        os.link(path, journal.profile / "second-link")
    else:
        path.chmod(0o644)
    with pytest.raises(RecoverableStateError):
        state.AnchorState(journal.profile, journal.layout)
