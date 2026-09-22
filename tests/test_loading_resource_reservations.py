"""Real private loading bindings remain subordinate to durable reservations."""

import threading
import time

import pytest
from test_resource_reservations import admission, metadata_manifest, records

from drift.node import resource_reservations as resources
from drift.node.resource_reservations import ResourceReservationError, ResourceReservationManager
from drift.node.worker_loading import loading_gate


@pytest.fixture
def loading_admission(admission):
    admission.manager = ResourceReservationManager(
        admission.directory, snapshot_provider=admission.snapshot, clock=lambda: 100.0, loading_protocol=True
    )
    original = admission.launch

    def launch(*args, **kwargs):
        value = original(*args, **kwargs)
        value.placement_manifest_digest = "sha256:" + "b" * 64
        value.placement_artifact_set_digest = "c" * 64
        value.placement_artifact_bytes = 11
        value.block_indices = "0:1"
        return value

    admission.launch = launch
    return admission


def test_loading_binding_is_published_with_reservation_and_lookup_has_no_io(loading_admission, monkeypatch):
    f = loading_admission
    token = f.manager.acquire(f.launch())
    binding = f.manager.loading_binding_for_token(token)
    assert binding.token == token and records(f)[0]["claim"]["reservation_id"] == token
    with monkeypatch.context() as patch:
        patch.setattr(f.manager, "_locked", lambda *args, **kwargs: pytest.fail("binding lookup performed I/O"))
        assert f.manager.loading_binding_for_token(token) is binding
    f.manager.release(token)
    assert records(f) == []
    with pytest.raises(ResourceReservationError):
        f.manager.loading_binding_for_token(token)


def test_gate_does_not_discount_retained_loading_memory(loading_admission):
    f = loading_admission
    first = f.manager.acquire(f.launch(host_limit=599))
    with pytest.raises(ResourceReservationError) as error:
        f.manager.acquire(f.launch("gpu-1", host_limit=599))
    assert error.value.category == "capacity" and len(records(f)) == 1
    f.manager.release(first)


def test_new_generation_never_reuses_binding_or_nonce(loading_admission):
    f = loading_admission
    first = f.manager.acquire(f.launch())
    old = f.manager.loading_binding_for_token(first)
    f.manager.release(first)
    second = f.manager.acquire(f.launch())
    fresh = f.manager.loading_binding_for_token(second)
    assert first != second and old.nonce != fresh.nonce
    assert old.binding_digest == fresh.binding_digest
    f.manager.release(second)


def test_invalid_claim_does_not_quarantine_an_untouched_loading_gate(loading_admission):
    f = loading_admission
    invalid = f.launch()
    invalid.placement_manifest_digest = "invalid"
    with pytest.raises(ResourceReservationError):
        f.manager.acquire(invalid)
    assert records(f) == [] and not (f.directory / "loading").exists()
    token = f.manager.acquire(f.launch())
    f.manager.release(token)


def test_gate_initialization_is_owned_by_journal_admission(loading_admission, monkeypatch):
    f = loading_admission
    original = resources.initialize_loading_gate
    calls = []

    def initialize(path):
        assert f.manager._mutex._is_owned()
        assert records(f) == []
        calls.append(path)
        return original(path)

    monkeypatch.setattr(resources, "initialize_loading_gate", initialize)
    token = f.manager.acquire(f.launch())
    assert calls == [f.directory / "loading"]
    f.manager.release(token)


def test_precommit_cancel_cleans_descriptor_without_publishing_reservation(loading_admission, monkeypatch):
    f = loading_admission
    cancel = threading.Event()
    original = resources.create_loading_binding

    def create(*args, **kwargs):
        binding = original(*args, **kwargs)
        cancel.set()
        return binding

    monkeypatch.setattr(resources, "create_loading_binding", create)
    with pytest.raises(ResourceReservationError):
        f.manager.acquire_cancellable(f.launch(), cancel)
    assert records(f) == [] and f.manager._loading_bindings == {}


def test_binding_cleanup_failure_retains_reservation_until_retry(loading_admission, monkeypatch):
    f = loading_admission
    token = f.manager.acquire(f.launch())
    with monkeypatch.context() as patch:
        patch.setattr(resources, "cleanup_loading_binding", lambda binding: (_ for _ in ()).throw(OSError("private")))
        with pytest.raises(ResourceReservationError):
            f.manager.release(token)
        assert len(records(f)) == 1
        assert f.manager.loading_binding_for_token(token).token == token
    f.manager.release(token)
    assert records(f) == []


def test_cleanup_retry_after_published_journal_removal_is_idempotent(loading_admission, monkeypatch):
    f = loading_admission
    token = f.manager.acquire(f.launch())
    original = f.manager._write

    def write(entries):
        original(entries)
        if not entries:
            raise OSError("fsync failed after publication")

    with monkeypatch.context() as patch:
        patch.setattr(f.manager, "_write", write)
        with pytest.raises(ResourceReservationError):
            f.manager.release(token)
    assert records(f) == [] and token in f.manager._owned
    f.manager.release(token)
    assert token not in f.manager._owned


def test_uncertain_acquisition_keeps_binding_and_journal_for_manual_recovery(loading_admission, monkeypatch):
    f = loading_admission
    original = f.manager._write

    def write(entries):
        original(entries)
        if entries:
            raise OSError("fsync failed after publication")

    monkeypatch.setattr(f.manager, "_write", write)
    with pytest.raises(ResourceReservationError):
        f.manager.acquire(f.launch())
    assert len(records(f)) == 1 and len(f.manager._loading_bindings) == 1
    with pytest.raises(ResourceReservationError):
        f.manager.acquire(f.launch("gpu-1"))


@pytest.mark.parametrize("cancel_waiter", [False, True])
def test_metadata_reserves_then_waits_for_same_gate_and_cancellation_skips_body(loading_admission, cancel_waiter):
    f = loading_admission
    f.sample["host"] = 2**40
    f.manager.register_cache_roots((f.root,))
    cancel, entered, finished = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def metadata():
        try:
            with f.manager.metadata_admission(
                metadata_manifest(),
                cache_dir=f.root,
                host_limit_bytes=2**40,
                disk_limit_bytes=1000,
                cancelled=cancel.is_set,
            ):
                entered.set()
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=metadata, daemon=True)
    with loading_gate(f.directory / "loading"):
        thread.start()
        deadline = time.monotonic() + 5
        while not records(f):
            assert time.monotonic() < deadline, errors
            time.sleep(0.01)
        assert not entered.is_set() and not f.manager._loading_bindings
        if cancel_waiter:
            cancel.set()
            assert finished.wait(2), "gate cancellation waited for the active loader"
            assert not entered.is_set() and records(f) == []
    assert finished.wait(5)
    thread.join(timeout=1)
    assert bool(errors) == cancel_waiter
    assert entered.is_set() != cancel_waiter
    assert records(f) == []
