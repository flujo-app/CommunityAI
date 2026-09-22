"""Cancellable manager operations use real journals; availability is controlled."""

import hashlib
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_resource_reservations import admission, records

from drift.node import host_resources
from drift.node.host_resources import ResourceScanPending
from drift.node.resource_reservations import ResourceReservationError


def test_pending_verification_retries_without_reserving_partial_evidence(admission):
    original = admission.snapshot
    attempts = []
    caches = []

    def snapshot(*args, **kwargs):
        attempts.append(kwargs["maximum_scan_seconds"])
        caches.append(kwargs["verification_cache"])
        assert records(admission) == []
        if len(attempts) in (1, 2, 4):
            raise ResourceScanPending("verification is incomplete")
        return original(*args, **kwargs)

    admission.manager._snapshot_provider = snapshot
    token = admission.manager.acquire_cancellable(admission.launch(), threading.Event())
    assert attempts == [30.0, 30.0, 30.0, 2.0, 2.0]
    assert all(cache is caches[0] for cache in caches)
    assert len(records(admission)) == 1
    admission.manager.release(token)
    assert records(admission) == []


def test_cancel_interrupts_mutex_wait_without_waiting_for_blocked_owner(admission):
    cancel = threading.Event()
    finished = threading.Event()
    errors = []

    def acquire():
        try:
            admission.manager.acquire_cancellable(admission.launch(), cancel)
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    with admission.manager._mutex:
        thread = threading.Thread(target=acquire, daemon=True)
        thread.start()
        cancel.set()
        assert finished.wait(1), "cancel waited for the busy manager mutex"
    thread.join(timeout=1)
    assert len(errors) == 1 and isinstance(errors[0], ResourceReservationError)
    assert not admission.directory.exists()


def test_cancel_after_initial_marker_still_establishes_recoverable_empty_journal(admission):
    cancel = threading.Event()
    original = admission.manager._write

    def write(entries):
        original(entries)
        if not entries:
            cancel.set()

    admission.manager._write = write
    with pytest.raises(ResourceReservationError):
        admission.manager.acquire_cancellable(admission.launch(), cancel)
    assert records(admission) == []
    admission.manager._write = original
    token = admission.manager.acquire(admission.launch())
    admission.manager.release(token)


def test_cancel_after_fresh_sample_prevents_generation_publication(admission):
    cancel = threading.Event()

    def snapshot(*args, **kwargs):
        sample = admission.snapshot(*args, **kwargs)
        if kwargs["maximum_scan_seconds"] == 2.0:
            cancel.set()
        return sample

    admission.manager._snapshot_provider = snapshot
    with pytest.raises(ResourceReservationError):
        admission.manager.acquire_cancellable(admission.launch(), cancel)
    assert records(admission) == []


def test_cancel_racing_durable_commit_returns_cleanup_token(admission):
    cancel = threading.Event()
    original = admission.manager._write

    def write(entries):
        original(entries)
        if entries:
            cancel.set()

    admission.manager._write = write
    token = admission.manager.acquire_cancellable(admission.launch(), cancel)
    assert cancel.is_set() and records(admission)[0]["claim"]["reservation_id"] == token
    admission.manager.release(token)
    assert records(admission) == []


def test_capacity_failure_is_not_retried_as_pending_verification(admission):
    admission.sample["host"] = 299
    with pytest.raises(ResourceReservationError) as error:
        admission.manager.acquire_cancellable(admission.launch(), threading.Event())
    assert error.value.category == "capacity"
    assert len(admission.calls) == 2
    assert records(admission) == []


def test_cancel_during_pending_backoff_invalidates_partial_cache(admission):
    cancel = threading.Event()
    discard = Mock(wraps=admission.manager._verification_cache.discard_pending)
    admission.manager._verification_cache.discard_pending = discard

    def snapshot(*args, **kwargs):
        cancel.set()
        raise ResourceScanPending("verification is incomplete")

    admission.manager._snapshot_provider = snapshot
    with pytest.raises(ResourceReservationError):
        admission.manager.acquire_cancellable(admission.launch(), cancel)
    assert discard.called
    assert records(admission) == []


def test_small_hash_budget_eventually_admits_exact_native_file(admission):
    content = bytes(range(64))
    (Path(admission.root) / "weight.bin").write_bytes(content)
    launch = admission.launch()
    artifact = replace(launch.resource_claim.artifacts[0], sha256=hashlib.sha256(content).hexdigest(), size_bytes=64)
    launch.resource_claim = replace(launch.resource_claim, artifacts=(artifact,))
    incomplete = []

    def snapshot(*args, **kwargs):
        try:
            return host_resources.snapshot_resources(
                *args, **kwargs, maximum_hash_bytes=7, host_reserve_bytes=0, disk_reserve_bytes=0
            )
        except ResourceScanPending:
            incomplete.append(True)
            assert records(admission) == []
            raise

    admission.manager._snapshot_provider = snapshot
    token = admission.manager.acquire_cancellable(launch, threading.Event())
    assert len(incomplete) >= 9
    assert len(records(admission)) == 1
    admission.manager.release(token)


def test_legacy_sync_pending_remains_fixed_public_failure(admission):
    admission.manager._snapshot_provider = Mock(side_effect=ResourceScanPending("private path detail"))
    with pytest.raises(ResourceReservationError, match="shared resource admission is unavailable") as error:
        admission.manager.acquire(admission.launch())
    assert "private" not in str(error.value)
    assert records(admission) == []


def test_other_worker_cache_cancellation_preserves_this_workers_start_intent(admission):
    original = admission.snapshot
    attempted = []
    cancel = threading.Event()

    def snapshot(*args, **kwargs):
        attempted.append(True)
        assert records(admission) == []
        if len(attempted) == 1:
            cache = kwargs["verification_cache"]
            epoch = cache._generation()
            # An independent request cancelled and invalidated the shared cache.
            cache.discard_pending()
            cache._check_generation(epoch)
            pytest.fail("old epoch unexpectedly remained valid")
        return original(*args, **kwargs)

    admission.manager._snapshot_provider = snapshot
    token = admission.manager.acquire_cancellable(admission.launch(), cancel)
    assert not cancel.is_set() and len(attempted) == 3
    assert len(records(admission)) == 1
    admission.manager.release(token)
