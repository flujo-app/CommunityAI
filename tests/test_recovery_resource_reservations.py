"""Journal/recovery integration with native owner locks and controlled resources."""

import json
import sys
import threading
import time

import pytest
from test_loading_resource_reservations import loading_admission
from test_resource_reservations import admission, metadata_manifest, records

from drift.node import resource_reservations as resources
from drift.node.resource_reservations import ResourceReservationError, ResourceReservationManager


@pytest.fixture
def recovery_admission(loading_admission):
    f = loading_admission
    f.managers = []

    def new_manager():
        manager = ResourceReservationManager(
            f.directory,
            snapshot_provider=f.snapshot,
            clock=lambda: 100,
            loading_protocol=True,
            recovery_protocol=True,
        )
        f.managers.append(manager)
        return manager

    f.new_manager = new_manager
    f.manager = new_manager()
    yield f
    # These fixtures never spawn children. Native crash/tree proof has separate
    # process integration coverage; force no claimed application-level proof here.
    for manager in f.managers:
        manager.close(timeout=3)
        for containment in manager._recovery_containments.values():
            containment.close()
        if manager._owner_lease is not None:
            manager._owner_lease.close()


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def stop_fixture_owner(manager):
    """Simulate no-child owner disappearance; never use this in product code."""
    for containment in manager._recovery_containments.values():
        containment.close()
    manager._owner_lease.close()


def test_new_journal_binds_exact_claim_loading_and_native_containment(recovery_admission, monkeypatch):
    f = recovery_admission
    token = f.manager.acquire(f.launch())
    entry = records(f)[0]
    document = json.loads((f.directory / "generations.json").read_text(encoding="utf-8"))
    assert document["schema_version"] == 2
    assert entry["recovery"]["reservation_id"] == token
    assert entry["recovery"]["owner"]["owner_id"] == entry["owner"]
    assert entry["loading"]["nonce"] == f.manager.loading_binding_for_token(token).nonce
    with monkeypatch.context() as patch:
        patch.setattr(f.manager, "_locked", lambda *args: pytest.fail("pure lookup did I/O"))
        assert f.manager.recovery_containment_for_token(token) is f.manager._recovery_containments[token]
        assert f.manager.recovery_snapshot()["state"] == "ready"
    f.manager.release(token)
    assert records(f) == []
    assert f.manager.close()


def test_live_owner_excludes_recovery_even_with_empty_job(recovery_admission):
    f = recovery_admission
    token = f.manager.acquire(f.launch())
    other = f.new_manager()
    before = (f.directory / "generations.json").read_bytes()
    assert not other.recover()
    assert other.recovery_snapshot() == dict(state="blocked", reason="active_owner", retryable=True)
    assert (f.directory / "generations.json").read_bytes() == before
    with pytest.raises(ResourceReservationError):
        other.acquire(f.launch("gpu-1"))
    f.manager.release(token)
    assert other.recover()


def test_legacy_entry_is_not_inferred_dead_or_removed(recovery_admission):
    f = recovery_admission
    legacy = ResourceReservationManager(f.directory, snapshot_provider=f.snapshot, clock=lambda: 100)
    token = legacy.acquire(f.launch())
    before = (f.directory / "generations.json").read_bytes()
    assert not f.manager.recover()
    assert f.manager.recovery_snapshot()["reason"] == "legacy_state"
    assert (f.directory / "generations.json").read_bytes() == before
    with pytest.raises(ResourceReservationError):
        f.manager.acquire(f.launch("gpu-1"))
    legacy.release(token)
    assert f.manager.recover()


@pytest.mark.skipif(sys.platform != "win32", reason="native named Job Objects require Windows")
def test_abandoned_unspawned_generation_reclaims_only_after_owner_exclusion(recovery_admission):
    f = recovery_admission
    token = f.manager.acquire(f.launch())
    loading = f.manager.loading_binding_for_token(token)
    stop_fixture_owner(f.manager)
    restarted = f.new_manager()
    assert restarted.recover()
    assert records(f) == []
    assert not loading._path("binding").exists()
    assert (f.directory / "loading" / "loading.lock").exists()
    assert (f.directory / "loading" / "loading-gate.json").exists()
    assert f.root in json.loads((f.directory / "generations.json").read_text(encoding="utf-8"))["cache_roots"]


@pytest.mark.parametrize("field", ["persistent_host_bytes", "nonce", "claim_digest", "owner", "missing_loading"])
def test_changed_claim_or_binding_cannot_authorize_recovery(recovery_admission, field):
    f = recovery_admission
    f.manager.acquire(f.launch())
    stop_fixture_owner(f.manager)
    path = f.directory / "generations.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    entry = doc["reservations"][0]
    if field == "persistent_host_bytes":
        entry["claim"][field] += 1
    elif field == "nonce":
        entry["loading"][field] = "d" * 32
    elif field == "claim_digest":
        entry["recovery"][field] = "sha256:" + "d" * 64
    elif field == "owner":
        entry["owner"] = "d" * 32
    else:
        entry["loading"] = None
    path.write_text(json.dumps(doc), encoding="utf-8")
    before = path.read_bytes()
    restarted = f.new_manager()
    assert not restarted.recover()
    assert restarted.recovery_snapshot()["reason"] == "unverifiable_state"
    assert path.read_bytes() == before


def test_metadata_reservation_records_no_child_contract_and_releases_normally(recovery_admission):
    f = recovery_admission
    f.sample["host"] = 2 * 1024**3
    manifest = metadata_manifest()
    with f.manager.metadata_admission(manifest, cache_dir=f.root, host_limit_bytes=1024**3, disk_limit_bytes=1000):
        entry = records(f)[0]
        assert entry["recovery"]["kind"] == "metadata"
        assert entry["recovery"]["contract"] == "synchronous_metadata_v1"
        assert entry["loading"] is None
        assert not f.new_manager().recover()
    assert records(f) == []


def test_background_recovery_runs_without_start_and_refreshes_owner_release(recovery_admission):
    f = recovery_admission
    token = f.manager.acquire(f.launch())
    other = f.new_manager()
    other.start_recovery()
    runner = other._recovery_thread
    other.start_recovery()
    assert other._recovery_thread is runner
    wait_for(lambda: other.recovery_snapshot()["reason"] == "active_owner")
    f.manager.release(token)
    wait_for(lambda: other.recovery_snapshot()["state"] == "ready")
    assert other.close()


def test_ready_background_owner_wakes_after_later_block_without_start(recovery_admission):
    f = recovery_admission
    f.manager.start_recovery()
    wait_for(lambda: f.manager.recovery_snapshot()["state"] == "ready")
    other = f.new_manager()
    token = other.acquire(f.launch())
    with pytest.raises(ResourceReservationError):
        f.manager.prepare(f.launch("gpu-1"))
    wait_for(lambda: f.manager.recovery_snapshot()["reason"] == "active_owner")
    other.release(token)
    assert other.close()
    wait_for(lambda: f.manager.recovery_snapshot()["state"] == "ready")
    assert f.manager.close()


def test_native_journal_lock_contention_retries_without_resetting_state(recovery_admission):
    f = recovery_admission
    assert f.manager.recover()
    other = f.new_manager()
    with f.manager._locked():
        before = (f.directory / "generations.json").read_bytes()
        other.start_recovery()
        wait_for(lambda: other._recovery_thread.is_alive())
        assert not other.recover()
        assert other.recovery_snapshot() == dict(state="checking", reason="checking", retryable=True)
        assert (f.directory / "generations.json").read_bytes() == before
    wait_for(lambda: other.recovery_snapshot()["state"] == "ready")
    assert other.close()


@pytest.mark.skipif(sys.platform != "win32", reason="native named Job Objects require Windows")
def test_background_recovery_retries_slow_cleanup_without_start(recovery_admission, monkeypatch):
    from drift.node import worker_recovery_containment
    from drift.node.resource_recovery import RecoverableStateError

    f = recovery_admission
    f.manager.acquire(f.launch())
    stop_fixture_owner(f.manager)
    other = f.new_manager()
    original = worker_recovery_containment.recover_windows_containment
    blocked = threading.Event()
    permit = threading.Event()

    def delayed(binding, guard):
        if not permit.is_set():
            blocked.set()
            raise RecoverableStateError("cleanup_pending")
        return original(binding, guard)

    monkeypatch.setattr(worker_recovery_containment, "recover_windows_containment", delayed)
    other.start_recovery()
    assert blocked.wait(3)
    wait_for(lambda: other.recovery_snapshot()["reason"] == "cleanup_pending")
    assert len(records(f)) == 1 and other.recovery_snapshot()["retryable"]
    permit.set()
    wait_for(lambda: other.recovery_snapshot()["state"] == "ready")
    assert records(f) == [] and other.close()


def test_failed_prepublication_containment_cleans_only_unpublished_binding(recovery_admission, monkeypatch):
    from drift.node import worker_recovery_containment

    f = recovery_admission
    with monkeypatch.context() as patch:
        patch.setattr(
            worker_recovery_containment,
            "create_recovery_containment",
            lambda binding: (_ for _ in ()).throw(OSError("native boundary unavailable")),
        )
        with pytest.raises(ResourceReservationError):
            f.manager.acquire(f.launch())
    assert records(f) == []
    assert not f.manager._loading_bindings and not f.manager._recovery_containments
    assert not list((f.directory / "loading").glob("*.binding.json"))
    token = f.manager.acquire(f.launch())
    f.manager.release(token)


def test_journal_corruption_updates_previously_ready_cached_status(recovery_admission):
    f = recovery_admission
    assert f.manager.recover() and f.manager.recovery_snapshot()["state"] == "ready"
    (f.directory / "generations.json").write_text("corrupt", encoding="utf-8")
    with pytest.raises(ResourceReservationError):
        f.manager.acquire(f.launch())
    assert f.manager.recovery_snapshot() == dict(state="blocked", reason="unverifiable_state", retryable=False)


def test_close_retains_owner_exclusion_until_owned_generations_are_released(recovery_admission):
    f = recovery_admission
    token = f.manager.acquire(f.launch())
    assert not f.manager.close()
    other = f.new_manager()
    assert not other.recover()
    assert other.recovery_snapshot()["reason"] == "active_owner"
    with pytest.raises(ResourceReservationError):
        f.manager.acquire(f.launch("gpu-1"))
    f.manager.release(token)
    assert f.manager.close()
    assert other.recover()


@pytest.mark.skipif(sys.platform != "win32", reason="native named Job Objects require Windows")
def test_recovery_write_failure_retains_state_until_fresh_verified_attempt(recovery_admission, monkeypatch):
    f = recovery_admission
    f.manager.acquire(f.launch())
    stop_fixture_owner(f.manager)
    restarted = f.new_manager()
    original = restarted._write
    with monkeypatch.context() as patch:
        patch.setattr(restarted, "_write", lambda entries: (_ for _ in ()).throw(OSError("private failure")))
        assert not restarted.recover()
    assert len(records(f)) == 1
    # The proof is obtained again; already-completed loading cleanup is idempotent.
    assert restarted.recover()
    assert records(f) == []


@pytest.mark.skipif(sys.platform != "win32", reason="native named Job Objects require Windows")
def test_cancel_after_cleanup_keeps_journal_and_allows_fresh_proof(recovery_admission, monkeypatch):
    f = recovery_admission
    f.manager.acquire(f.launch())
    stop_fixture_owner(f.manager)
    restarted = f.new_manager()
    cancel = threading.Event()
    cleanup = resources.cleanup_loading_binding

    def clean_then_cancel(binding):
        cleanup(binding)
        cancel.set()

    with monkeypatch.context() as patch:
        patch.setattr(resources, "cleanup_loading_binding", clean_then_cancel)
        assert not restarted.recover(cancelled=cancel.is_set)
    assert len(records(f)) == 1
    assert restarted.recover()
    assert records(f) == []
