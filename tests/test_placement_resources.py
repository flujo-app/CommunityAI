"""Pure aggregate claims; no model files, hardware or process launches."""

import os
from dataclasses import replace

import pytest

from drift.node.placement_resources import (
    MAX_BYTES,
    MAX_FILES,
    ArtifactClaim,
    CacheSnapshot,
    ResourceSnapshot,
    VolumeSnapshot,
    WorkerResourceClaim,
    evaluate_resources,
)

GIB = 1024**3


@pytest.fixture
def root(tmp_path):
    return os.path.normcase(os.path.normpath(str(tmp_path / "cache")))


def artifact(root, name="model.safetensors", size=10, digest="a" * 64):
    return ArtifactClaim(root, "manifest-artifacts/manifest/snapshot/" + name, digest, size)


def worker(index=0, *, persistent=1, staging=2, files=(), reservation=None):
    return WorkerResourceClaim(reservation or f"generation-{index}", f"gpu-{index}", persistent, staging, files)


def snapshot(root, *, used=0, limit=1000, available=1000, present=(), host_limit=1000, host_available=1000):
    return ResourceSnapshot(
        100.0,
        host_limit,
        host_available,
        (CacheSnapshot(root, "volume", used, limit, present),),
        (VolumeSnapshot("volume", available),),
    )


def evaluate(claims, state, **kwargs):
    return evaluate_resources(claims, snapshot=state, now=101.0, maximum_age_seconds=2.0, **kwargs)


def test_eight_cards_share_one_unsharded_file_but_each_needs_persistent_and_load_memory(root):
    whole_checkpoint = artifact(root, size=80 * GIB)
    claims = tuple(worker(i, persistent=GIB, staging=80 * GIB, files=(whole_checkpoint,)) for i in range(8))
    state = snapshot(root, limit=100 * GIB, available=100 * GIB, host_limit=128 * GIB, host_available=128 * GIB)
    concurrent = evaluate(claims, state)
    assert not concurrent.admitted
    assert concurrent.artifact_union_bytes == 80 * GIB
    assert concurrent.persistent_host_bytes == 8 * GIB
    assert concurrent.staging_host_bytes == 640 * GIB
    assert concurrent.reasons == ("configured host memory ceiling exceeded", "fresh available host memory exceeded")
    serial = evaluate(claims, state, serialized_loading=True)
    assert serial.admitted and serial.additional_host_bytes == 88 * GIB
    assert dict(serial.cache_growth_bytes)[root] == 80 * GIB


def test_eight_overlapping_sharded_artifact_sets_count_union_once(root):
    shards = tuple(artifact(root, f"shard-{i}.safetensors", size=10 * GIB, digest=f"{i:064x}") for i in range(9))
    claims = tuple(worker(i, files=shards[i : i + 2]) for i in range(8))
    result = evaluate(claims, snapshot(root, limit=100 * GIB, available=100 * GIB))
    assert result.admitted and result.artifact_union_bytes == 90 * GIB
    assert dict(result.cache_growth_bytes)[root] == 90 * GIB


def test_same_hash_different_copies_are_not_speculatively_deduplicated(root):
    first, second = artifact(root, "first"), artifact(root, "second")
    result = evaluate((worker(files=(first, second, first)),), snapshot(root))
    assert result.artifact_union_bytes == 20
    assert dict(result.cache_growth_bytes)[root] == 20


def test_separate_roots_on_one_volume_share_available_space(root):
    other = root + "-other"
    state = ResourceSnapshot(
        100,
        1000,
        1000,
        (CacheSnapshot(root, "disk", 0, 100), CacheSnapshot(other, "disk", 0, 100)),
        (VolumeSnapshot("disk", 15),),
    )
    result = evaluate((worker(files=(artifact(root), artifact(other))),), state)
    assert result.artifact_union_bytes == 20
    assert dict(result.volume_growth_bytes) == {"disk": 20}
    assert result.reasons == ("fresh available volume storage exceeded",)


def test_separate_volumes_have_independent_availability(root):
    other = root + "-other"
    state = ResourceSnapshot(
        100,
        1000,
        1000,
        (CacheSnapshot(root, "disk1", 0, 10), CacheSnapshot(other, "disk2", 0, 10)),
        (VolumeSnapshot("disk1", 10), VolumeSnapshot("disk2", 10)),
    )
    assert evaluate((worker(files=(artifact(root), artifact(other))),), state).admitted


def test_verified_existing_files_cost_no_growth_and_other_usage_is_not_erased(root):
    cached = artifact(root, size=80)
    new = artifact(root, "new", size=20)
    state = snapshot(root, used=80, limit=90, present=(cached,))
    result = evaluate((worker(files=(cached, new)),), state)
    assert result.artifact_union_bytes == 100
    assert dict(result.cache_growth_bytes)[root] == 20
    assert dict(result.cache_projected_bytes)[root] == 100
    assert result.reasons == ("configured cache storage ceiling exceeded",)
    # Old worker stopped, but the measured cache files remain allocated.
    after_stop = evaluate((worker(files=(new,)),), state)
    assert after_stop.cache_projected_bytes == result.cache_projected_bytes
    assert not after_stop.admitted


def test_unverified_or_partial_files_receive_no_speculative_growth_credit(root):
    result = evaluate((worker(files=(artifact(root, size=100),)),), snapshot(root, used=40, limit=120))
    assert dict(result.cache_projected_bytes)[root] == 140
    assert not result.admitted


def test_verified_inventory_cannot_claim_bytes_absent_from_measured_usage(root):
    present = (artifact(root), artifact(root, "different", digest="b" * 64))
    with pytest.raises(ValueError, match="below its verified"):
        evaluate((), snapshot(root, used=10, present=present))
    # Existing hardlink aliases may be represented by one physical file in the
    # sampler's usage. Their mere equal hash grants no credit for missing files.
    aliases = (artifact(root), artifact(root, "alias"))
    assert evaluate((), snapshot(root, used=10, present=aliases)).admitted


@pytest.mark.parametrize("path", ["CON", "con.txt", "foo/LPT1.bin", "file\nname", "file*name"])
def test_portable_device_names_and_control_characters_cannot_alias_cache_files(root, path):
    with pytest.raises(ValueError, match="artifact path"):
        ArtifactClaim(root, path, "a" * 64, 1)


def test_retained_cleanup_reservations_overlap_changed_generation_until_verified_release(root):
    old = worker(persistent=80, staging=5)
    new = worker(persistent=40, staging=10, reservation="replacement")
    state = snapshot(root, host_limit=100, host_available=60)
    failed_cleanup = evaluate((new,), state, retained_claims=(old,))
    assert failed_cleanup.persistent_host_bytes == 120
    assert failed_cleanup.staging_host_bytes == 15
    assert failed_cleanup.additional_host_bytes == 55
    assert failed_cleanup.reasons == ("configured host memory ceiling exceeded",)
    assert evaluate((new,), state).admitted  # Caller explicitly provides verified release.


def test_unchanged_retained_claim_is_not_double_charged_or_released(root):
    old = worker(persistent=80, staging=5)
    result = evaluate((old,), snapshot(root, host_limit=85, host_available=5), retained_claims=(old,))
    assert result.admitted and result.persistent_host_bytes == 80 and result.additional_host_bytes == 5


def test_fresh_host_availability_constrains_new_memory_even_below_configured_limit(root):
    result = evaluate((worker(persistent=80, staging=5),), snapshot(root, host_available=84))
    assert result.reasons == ("fresh available host memory exceeded",)


@pytest.mark.parametrize("change", [{"sha256": "b" * 64}, {"size_bytes": 11}])
@pytest.mark.parametrize("existing", [False, True])
def test_conflicting_exact_file_claims_fail_even_if_one_is_verified_present(root, change, existing):
    first = artifact(root)
    changed = replace(first, **change)
    state = snapshot(root, used=10, present=(changed,) if existing else ())
    claims = (worker(files=(first,)),) if existing else (worker(files=(first, changed)),)
    with pytest.raises(ValueError, match="conflicting"):
        evaluate(claims, state)


def test_native_case_aliases_cannot_bypass_file_conflict(root):
    if os.path.normcase("FILE") == "FILE":
        pytest.skip("native filesystem path comparison is case sensitive")
    first = artifact(root, "FILE")
    second = artifact(root, "file", digest="b" * 64)
    with pytest.raises(ValueError, match="conflicting"):
        evaluate((worker(files=(first, second)),), snapshot(root))


@pytest.mark.parametrize("bad", [True, -1, 1.5, float("nan"), float("inf"), MAX_BYTES + 1])
def test_invalid_byte_claims_and_ceilings_fail_closed(root, bad):
    for factory in (
        lambda: artifact(root, size=bad),
        lambda: worker(persistent=bad),
        lambda: worker(staging=bad),
        lambda: snapshot(root, used=bad),
        lambda: snapshot(root, limit=bad),
        lambda: snapshot(root, available=bad),
        lambda: snapshot(root, host_limit=bad),
        lambda: snapshot(root, host_available=bad),
    ):
        with pytest.raises(ValueError):
            factory()


@pytest.mark.parametrize(
    "path", ["../outside", "/absolute", "a/../b", "a//b", "a\\b", "C:stream", "./a", "a.", "a ", "a\0b"]
)
def test_noncanonical_artifact_paths_are_rejected(root, path):
    with pytest.raises(ValueError, match="artifact path"):
        ArtifactClaim(root, path, "a" * 64, 1)


@pytest.mark.parametrize("digest", ["A" * 64, "x" * 64, "a" * 63, None])
def test_invalid_hashes_rejected(root, digest):
    with pytest.raises(ValueError, match="SHA256"):
        artifact(root, digest=digest)


@pytest.mark.parametrize("observed", [98.9, 102, float("nan"), float("inf"), True])
def test_snapshot_freshness_is_required(root, observed):
    with pytest.raises(ValueError):
        evaluate((), replace(snapshot(root), observed_at=observed))


@pytest.mark.parametrize(
    "problem", ["overlap", "duplicate_root", "missing_volume", "duplicate_volume", "missing_cache"]
)
def test_inconsistent_storage_topology_is_rejected(root, problem):
    state = snapshot(root)
    files = (artifact(root),)
    if problem == "overlap":
        state = replace(state, caches=(*state.caches, CacheSnapshot(os.path.join(root, "nested"), "volume", 0, 10)))
    elif problem == "duplicate_root":
        state = replace(state, caches=state.caches * 2)
    elif problem == "missing_volume":
        state = replace(state, volumes=())
    elif problem == "duplicate_volume":
        state = replace(state, volumes=state.volumes * 2)
    else:
        files = (artifact(root + "-missing"),)
    with pytest.raises(ValueError):
        evaluate((worker(files=files),), state)


def test_reservation_identity_cannot_silently_release_or_overwrite_uncertain_claims(root):
    old = worker(persistent=80)
    with pytest.raises(ValueError, match="cannot be replaced"):
        evaluate((replace(old, persistent_host_bytes=1),), snapshot(root), retained_claims=(old,))
    with pytest.raises(ValueError, match="reservation IDs"):
        evaluate((), snapshot(root), retained_claims=(old, old))
    with pytest.raises(ValueError, match="worker IDs"):
        evaluate((old, replace(old, worker_id="GPU-0", reservation_id="second")), snapshot(root))


def test_inputs_are_bounded_and_copied(root):
    files = [artifact(root)]
    claim = worker(files=files)
    files.clear()
    assert len(claim.artifacts) == 1
    with pytest.raises(ValueError, match="bounded"):
        worker(files=(artifact(root),) * (MAX_FILES + 1))
    with pytest.raises(ValueError, match="bounded"):
        evaluate(tuple(worker(i) for i in range(17)), snapshot(root))
    with pytest.raises(ValueError, match="serialization"):
        evaluate((), snapshot(root), serialized_loading=1)


def test_empty_map_still_checks_existing_storage_usage(root):
    result = evaluate((), snapshot(root, used=11, limit=10))
    assert not result.admitted
    assert result.additional_host_bytes == 0
    assert dict(result.cache_growth_bytes)[root] == 0
