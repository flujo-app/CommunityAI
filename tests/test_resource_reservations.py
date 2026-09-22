"""Real durable journals with controlled host/volume observations; no model/GPU execution."""

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from drift.model_manifest import ModelManifest
from drift.node import host_resources
from drift.node.host_resources import canonical_cache_root
from drift.node.placement_resources import (
    ArtifactClaim,
    CacheSnapshot,
    ResourceSnapshot,
    VolumeSnapshot,
    WorkerResourceClaim,
)
from drift.node.resource_reservations import ResourceReservationError, ResourceReservationManager


@pytest.fixture
def admission(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    root = canonical_cache_root(cache)
    sample = {"host": 10_000, "volume": 1000, "used": 0}
    calls = []

    def snapshot(claims, *, host_limit_bytes, cache_limits, now, **kwargs):
        calls.append((claims, kwargs))
        return ResourceSnapshot(
            now,
            host_limit_bytes,
            sample["host"],
            tuple(
                CacheSnapshot(
                    path,
                    "same-volume",
                    sample["used"].get(path, 0) if isinstance(sample["used"], dict) else sample["used"],
                    limit,
                )
                for path, limit in cache_limits.items()
            ),
            (VolumeSnapshot("same-volume", sample["volume"]),),
        )

    def launch(worker="gpu-0", *, host_limit=10_000, disk_limit=1000, root=root, persistent=100, staging=200):
        claim = WorkerResourceClaim(
            "template-" + worker, worker, persistent, staging, (ArtifactClaim(root, "weight.bin", "a" * 64, 11),)
        )
        return SimpleNamespace(
            worker_id=worker,
            resource_claim=claim,
            max_host_memory_bytes=host_limit,
            max_disk_bytes=disk_limit,
            placement_cache_root=root,
        )

    directory = tmp_path / "private"
    manager = ResourceReservationManager(directory, snapshot_provider=snapshot, clock=lambda: 100.0)
    return SimpleNamespace(
        manager=manager, directory=directory, snapshot=snapshot, launch=launch, sample=sample, calls=calls, root=root
    )


def records(fixture):
    return json.loads((fixture.directory / "generations.json").read_text(encoding="utf-8"))["reservations"]


def test_preparation_grants_no_reservation_and_acquire_rechecks_live_free_space(admission):
    manager, launch = admission.manager, admission.launch()
    manager.prepare(launch)
    assert records(admission) == []
    admission.sample["host"] = 299
    with pytest.raises(ResourceReservationError):
        manager.acquire(launch)
    assert records(admission) == []
    assert [call[1]["maximum_scan_seconds"] for call in admission.calls] == [30.0, 2.0]


def test_eight_generations_reserve_full_staging_sum_but_one_shared_artifact(admission):
    tokens = [admission.manager.acquire(admission.launch(f"gpu-{i}", host_limit=2400, disk_limit=11)) for i in range(8)]
    assert len(set(tokens)) == 8
    assert len(records(admission)) == 8
    assert sum(row["claim"]["staging_host_bytes"] for row in records(admission)) == 1600
    with pytest.raises(ResourceReservationError):
        admission.manager.acquire(admission.launch("gpu-extra", host_limit=2400, disk_limit=11))
    for token in tokens:
        admission.manager.release(token)
    assert records(admission) == []


def test_retained_staging_is_not_collapsed_to_maximum_without_a_loading_gate(admission):
    token = admission.manager.acquire(admission.launch(host_limit=599))
    with pytest.raises(ResourceReservationError):
        admission.manager.acquire(admission.launch("gpu-1", host_limit=599))
    assert len(records(admission)) == 1
    admission.manager.release(token)


def test_waiting_reservations_are_not_assumed_resident_in_available_ram(admission):
    admission.sample["host"] = 599
    admission.manager.acquire(admission.launch())
    with pytest.raises(ResourceReservationError):
        admission.manager.acquire(admission.launch("gpu-1"))


def test_cache_copies_share_volume_capacity_but_not_disk_growth_credit(admission, tmp_path):
    other = tmp_path / "other-cache"
    other.mkdir()
    admission.sample["volume"] = 21
    admission.manager.acquire(admission.launch())
    with pytest.raises(ResourceReservationError):
        admission.manager.acquire(admission.launch("gpu-1", root=canonical_cache_root(other)))


def test_node_storage_ceiling_is_shared_across_roots_even_with_ample_volume(admission, tmp_path):
    other = tmp_path / "other-cache"
    other.mkdir()
    admission.manager.acquire(admission.launch(disk_limit=11))
    with pytest.raises(ResourceReservationError) as error:
        admission.manager.acquire(admission.launch("gpu-1", root=canonical_cache_root(other), disk_limit=11))
    assert error.value.category == "capacity"


def test_stopped_root_still_consumes_shared_disk_allowance_after_restart(admission, tmp_path):
    token = admission.manager.acquire(admission.launch(disk_limit=11))
    admission.manager.release(token)
    admission.sample["used"] = {admission.root: 11}
    other = tmp_path / "other-cache"
    other.mkdir()
    restarted = ResourceReservationManager(admission.directory, snapshot_provider=admission.snapshot, clock=lambda: 100)
    with pytest.raises(ResourceReservationError):
        restarted.acquire(admission.launch("gpu-1", root=canonical_cache_root(other), disk_limit=21))
    assert (
        admission.root
        in json.loads((admission.directory / "generations.json").read_text(encoding="utf-8"))["cache_roots"]
    )


def test_restarted_or_competing_manager_honors_existing_journal_and_cannot_release_it(admission):
    token = admission.manager.acquire(admission.launch(host_limit=599))
    restarted = ResourceReservationManager(admission.directory, snapshot_provider=admission.snapshot, clock=lambda: 100)
    with pytest.raises(ResourceReservationError):
        restarted.release(token)
    with pytest.raises(ResourceReservationError):
        restarted.acquire(admission.launch("gpu-1", host_limit=599))
    admission.manager.release(token)
    second = restarted.acquire(admission.launch("gpu-1", host_limit=599))
    restarted.release(second)
    assert records(admission) == []


def test_successful_release_does_not_claim_cache_files_were_deleted(admission):
    token = admission.manager.acquire(admission.launch(disk_limit=11))
    admission.manager.release(token)
    admission.sample["used"] = 11
    # The provider gives no SHA verification: existing bytes + missing target
    # growth must fit. Removing a worker cannot erase observed storage usage.
    with pytest.raises(ResourceReservationError):
        admission.manager.acquire(admission.launch("gpu-1", disk_limit=11))


def test_uncertain_atomic_publication_retains_claim_and_quarantines_acquisition(admission, monkeypatch):
    import drift.node.resource_reservations as module

    replace_file = module.os.replace
    admission.manager.register_cache_roots((admission.root,))
    admission.manager.prepare(admission.launch())  # Initialize the durable empty journal.

    def publish_then_fail(source, destination):
        replace_file(source, destination)
        raise OSError("private journal path and uncertain device error")

    monkeypatch.setattr(module.os, "replace", publish_then_fail)
    with pytest.raises(ResourceReservationError) as error:
        admission.manager.acquire(admission.launch())
    assert "private journal path" not in str(error.value)
    assert len(records(admission)) == 1
    monkeypatch.setattr(module.os, "replace", replace_file)
    with pytest.raises(ResourceReservationError):
        admission.manager.acquire(admission.launch("gpu-1"))
    assert len(records(admission)) == 1


@pytest.mark.parametrize("corruption", ["missing", "duplicate", "traversal", "wrong-schema", "oversized"])
def test_corrupt_or_lost_journal_never_resets_live_claims(admission, corruption):
    admission.manager.acquire(admission.launch())
    path = admission.directory / "generations.json"
    if corruption == "missing":
        path.unlink()
    elif corruption == "duplicate":
        path.write_text('{"schema_version":1,"schema_version":1,"reservations":[]}', encoding="utf-8")
    else:
        value = json.loads(path.read_text(encoding="utf-8"))
        if corruption == "traversal":
            value["reservations"][0]["claim"]["artifacts"][0]["relative_path"] = "../secret"
        elif corruption == "wrong-schema":
            value["schema_version"] = True
        else:
            value["reservations"][0]["host_limit"] = 2**64
        path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ResourceReservationError):
        admission.manager.acquire(admission.launch("gpu-1"))


def test_journal_lock_is_shared_and_nonblocking(admission):
    competing = ResourceReservationManager(admission.directory, snapshot_provider=admission.snapshot, clock=lambda: 100)
    with admission.manager._locked():
        with pytest.raises(ResourceReservationError):
            competing.acquire(admission.launch())
    token = competing.acquire(admission.launch())
    competing.release(token)


def test_retained_generation_cannot_be_overwritten_by_same_worker(admission):
    first = admission.manager.acquire(admission.launch())
    second = admission.manager.acquire(admission.launch())
    assert first != second and len(records(admission)) == 2
    admission.manager.release(second)
    assert records(admission)[0]["claim"]["reservation_id"] == first


def test_wrong_cache_and_worker_claims_are_rejected(admission, tmp_path):
    launch = admission.launch()
    launch.worker_id = "different"
    with pytest.raises(ResourceReservationError):
        admission.manager.acquire(launch)
    launch = admission.launch()
    launch.placement_cache_root = str(tmp_path)
    with pytest.raises(ResourceReservationError):
        admission.manager.acquire(launch)


def test_stale_snapshot_is_rejected_before_persisting(admission):
    provider = admission.snapshot

    def stale(*args, **kwargs):
        return replace(provider(*args, **kwargs), observed_at=90)

    admission.manager._snapshot_provider = stale
    with pytest.raises(ResourceReservationError):
        admission.manager.acquire(admission.launch())
    assert records(admission) == []


def test_lost_journal_remains_closed_after_manager_restart(admission):
    admission.manager.acquire(admission.launch())
    (admission.directory / "generations.json").unlink()
    restarted = ResourceReservationManager(admission.directory, snapshot_provider=admission.snapshot, clock=lambda: 100)
    with pytest.raises(ResourceReservationError):
        restarted.acquire(admission.launch("gpu-1"))
    assert not (admission.directory / "generations.json").exists()


def test_own_release_retries_after_uncertain_publication_are_idempotent(admission, monkeypatch):
    import drift.node.resource_reservations as module

    token = admission.manager.acquire(admission.launch())
    replace_file = module.os.replace

    def publish_then_fail(source, destination):
        replace_file(source, destination)
        raise OSError("release fsync or return was uncertain")

    monkeypatch.setattr(module.os, "replace", publish_then_fail)
    with pytest.raises(ResourceReservationError):
        admission.manager.release(token)
    assert records(admission) == []
    monkeypatch.setattr(module.os, "replace", replace_file)
    admission.manager.release(token)
    admission.manager.release(token)
    next_token = admission.manager.acquire(admission.launch())
    admission.manager.release(next_token)
    with pytest.raises(ResourceReservationError):
        admission.manager.release("foreign-or-forged")


def test_reading_journal_may_update_access_time_without_invalidating_identity(admission, monkeypatch):
    import drift.node.resource_reservations as module

    token = admission.manager.acquire(admission.launch())
    regular = module._regular
    count = [0]

    def access_time_changed(path):
        info = regular(path)
        count[0] += 1
        return SimpleNamespace(
            **{
                name: getattr(info, name)
                for name in ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")
            },
            st_atime=count[0],
        )

    monkeypatch.setattr(module, "_regular", access_time_changed)
    admission.manager.release(token)
    assert records(admission) == []


def remembered_roots(admission):
    return json.loads((admission.directory / "generations.json").read_text(encoding="utf-8"))["cache_roots"]


def metadata_manifest():
    document = ModelManifest.load("tests/data/model_manifest_v1_vector.json").to_dict()
    document["artifacts"] = [item for item in document["artifacts"] if item["role"] != "config"]
    document["artifacts"] += [
        dict(role=role, path=path, size=2, sha256=hashlib.sha256(b"{}").hexdigest())
        for role, path in (("config", "config.json"), ("weight_index", "weights.bin.index.json"))
    ]
    return ModelManifest.from_dict(document)


def test_registration_is_durable_without_reservations_or_snapshots(admission):
    admission.manager.register_cache_roots((admission.root,))
    assert remembered_roots(admission) == [admission.root]
    assert records(admission) == []
    assert admission.calls == []
    restarted = ResourceReservationManager(admission.directory, snapshot_provider=admission.snapshot, clock=lambda: 100)
    restarted.register_cache_roots(())
    assert remembered_roots(admission) == [admission.root]


def test_registration_rejects_missing_overlapping_and_unbounded_roots(admission, tmp_path):
    with pytest.raises(ResourceReservationError):
        admission.manager.register_cache_roots((tmp_path / "missing",))
    admission.manager.register_cache_roots((admission.root,))
    nested = tmp_path / "cache" / "nested"
    nested.mkdir()
    with pytest.raises(ResourceReservationError):
        admission.manager.register_cache_roots((nested,))
    with pytest.raises(ResourceReservationError):
        admission.manager.register_cache_roots((admission.root,) * 33)
    assert remembered_roots(admission) == [admission.root]


@pytest.mark.parametrize("operation", ["prepare", "acquire"])
def test_failed_snapshot_cannot_forget_newly_observed_root(admission, operation):
    def failed(*args, **kwargs):
        assert remembered_roots(admission) == [admission.root]
        raise OSError("private root temporarily unreadable")

    admission.manager._snapshot_provider = failed
    with pytest.raises(ResourceReservationError):
        getattr(admission.manager, operation)(admission.launch())
    assert remembered_roots(admission) == [admission.root]
    assert records(admission) == []


def test_never_admitted_root_actual_bytes_remain_in_shared_pool_after_restart(admission, tmp_path, monkeypatch):
    available = [1]
    monkeypatch.setattr(host_resources.psutil, "virtual_memory", lambda: SimpleNamespace(available=available[0]))

    def actual_snapshot(*args, **kwargs):
        return host_resources.snapshot_resources(*args, **kwargs, host_reserve_bytes=0, disk_reserve_bytes=0)

    (tmp_path / "cache" / "planning-metadata").write_bytes(b"123456")
    admission.manager._snapshot_provider = actual_snapshot
    with pytest.raises(ResourceReservationError):
        admission.manager.acquire(admission.launch(disk_limit=100))
    assert records(admission) == []
    assert remembered_roots(admission) == [admission.root]
    available[0] = 10000
    other = tmp_path / "other-cache"
    other.mkdir()
    restarted = ResourceReservationManager(admission.directory, snapshot_provider=actual_snapshot, clock=lambda: 100)
    with pytest.raises(ResourceReservationError) as error:
        restarted.acquire(admission.launch("gpu-1", root=canonical_cache_root(other), disk_limit=16))
    assert error.value.category == "capacity"  # 6 existing + 11 new > 16 shared.


def test_metadata_denial_never_yields_permission_or_rolls_back_root(admission):
    manifest = metadata_manifest()
    called = []
    with pytest.raises(ResourceReservationError):
        with admission.manager.metadata_admission(
            manifest, cache_dir=admission.root, host_limit_bytes=1000, disk_limit_bytes=1000
        ):
            called.append(True)
    assert not called and records(admission) == []
    assert remembered_roots(admission) == [admission.root]


@pytest.mark.parametrize("fail_body", [False, True])
def test_metadata_exact_claim_precedes_body_and_releases_only_after_body_exits(admission, fail_body):
    manifest = metadata_manifest()
    admission.sample["host"] = 2 * 1024**3
    completed = []

    def operation():
        with admission.manager.metadata_admission(
            manifest, cache_dir=admission.root, host_limit_bytes=2 * 1024**3, disk_limit_bytes=1000
        ):
            row = records(admission)[0]
            claim = row["claim"]
            assert claim["persistent_host_bytes"] == 0
            assert claim["staging_host_bytes"] == 512 * 1024**2 + 32 * 4
            expected = {"config.json", "weights.bin.index.json"}
            assert {
                artifact["relative_path"].removeprefix(f"manifest-artifacts/{manifest.digest}/snapshot/")
                for artifact in claim["artifacts"]
            } == expected
            assert all(item["size_bytes"] == 2 for item in claim["artifacts"])
            if fail_body:
                raise LookupError("synchronous loader failed")
            completed.append(True)

    if fail_body:
        with pytest.raises(LookupError):
            operation()
    else:
        operation()
    assert completed == ([] if fail_body else [True])
    assert records(admission) == []
    assert remembered_roots(admission) == [admission.root]


def test_metadata_shared_disk_reservation_denies_second_root_before_writes(admission, tmp_path, monkeypatch):
    manifest = metadata_manifest()
    monkeypatch.setattr(host_resources.psutil, "virtual_memory", lambda: SimpleNamespace(available=2 * 1024**3))

    def actual_snapshot(*args, **kwargs):
        return host_resources.snapshot_resources(*args, **kwargs, host_reserve_bytes=0, disk_reserve_bytes=0)

    admission.manager._snapshot_provider = actual_snapshot
    first = tmp_path / "cache"
    with admission.manager.metadata_admission(
        manifest, cache_dir=first, host_limit_bytes=2 * 1024**3, disk_limit_bytes=7
    ):
        assert len(records(admission)) == 1
        directory = first / "manifest-artifacts" / manifest.digest / "snapshot"
        directory.mkdir(parents=True)
        (directory / "config.json").write_bytes(b"{}")
        (directory / "weights.bin.index.json").write_bytes(b"{}")
    assert records(admission) == []
    other = tmp_path / "second"
    other.mkdir()
    with pytest.raises(ResourceReservationError) as error:
        with admission.manager.metadata_admission(
            manifest, cache_dir=other, host_limit_bytes=2 * 1024**3, disk_limit_bytes=7
        ):
            pytest.fail("second metadata download must not start above shared disk allowance")
    assert error.value.category == "capacity"
    assert list(other.iterdir()) == []
    assert set(remembered_roots(admission)) == {admission.root, canonical_cache_root(other)}


def test_metadata_uncertain_acquire_never_enters_body_or_releases_unknown_claim(admission, monkeypatch):
    import drift.node.resource_reservations as module

    manifest = metadata_manifest()
    admission.sample["host"] = 2 * 1024**3
    admission.manager.register_cache_roots((admission.root,))
    publish = module.os.replace

    def uncertain(source, destination):
        publish(source, destination)
        raise OSError("publication acknowledgement failed")

    monkeypatch.setattr(module.os, "replace", uncertain)
    with pytest.raises(ResourceReservationError):
        with admission.manager.metadata_admission(
            manifest, cache_dir=admission.root, host_limit_bytes=2 * 1024**3, disk_limit_bytes=1000
        ):
            pytest.fail("uncertain acquisition must not grant loader permission")
    assert len(records(admission)) == 1
    assert admission.manager._uncertain


def test_cleanup_does_not_clear_uncertain_root_inventory_publication(admission, tmp_path, monkeypatch):
    import drift.node.resource_reservations as module

    token = admission.manager.acquire(admission.launch())
    other = tmp_path / "second-root"
    other.mkdir()
    publish = module.os.replace

    def uncertain(source, destination):
        publish(source, destination)
        raise OSError("inventory publication acknowledgement failed")

    monkeypatch.setattr(module.os, "replace", uncertain)
    with pytest.raises(ResourceReservationError):
        admission.manager.register_cache_roots((other,))
    monkeypatch.setattr(module.os, "replace", publish)
    admission.manager.release(token)
    assert records(admission) == []
    assert canonical_cache_root(other) in remembered_roots(admission)
    with pytest.raises(ResourceReservationError):
        admission.manager.acquire(admission.launch())
