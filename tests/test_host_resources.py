"""Real local file accounting; no weights, accelerator work or network requests."""

import hashlib
import io
import os
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest
import torch

from drift.model_manifest import ManifestArtifact, ManifestBlockArtifactPlan
from drift.node import host_resources as host
from drift.node.placement_resources import ArtifactClaim, WorkerResourceClaim, evaluate_resources
from drift.server.memory_budget import ModelMemoryProfile
from drift.utils.convert_block import QuantType


def profile(quant=QuantType.NONE, weights=(100, 200, 300), dtype=torch.float32):
    return ModelMemoryProfile(1, 1, dtype, quant, 1, weights, (10,) * len(weights))


def plan(start=0, end=1, *, size=400, extension="safetensors"):
    return ManifestBlockArtifactPlan(
        start,
        end,
        (
            ManifestArtifact("config", "config.json", "a" * 64, 10),
            ManifestArtifact("weight", "model." + extension, "b" * 64, size),
        ),
    )


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(host.psutil, "virtual_memory", lambda: SimpleNamespace(available=10000))
    return host.canonical_cache_root(tmp_path)


def claim(root, name="model.bin", data=b"weights"):
    return ArtifactClaim(root, name, hashlib.sha256(data).hexdigest(), len(data))


def worker(*artifacts):
    return WorkerResourceClaim("generation-1", "gpu-0", 10, 20, artifacts)


def snapshot(root, *artifacts, **kwargs):
    return host.snapshot_resources(
        [worker(*artifacts)],
        host_limit_bytes=10000,
        cache_limits=MappingProxyType({root: 10000}),
        now=100,
        host_reserve_bytes=0,
        disk_reserve_bytes=0,
        **kwargs,
    )


@pytest.mark.parametrize(
    "quant,weight,multiplier",
    [(QuantType.NONE, 404, 1), (QuantType.INT8, 101, 4), (QuantType.NF4, 54, 8), (QuantType.FP8_DEQUANT, 202, 2)],
)
def test_dense_host_geometry_expands_quantized_device_weights(quant, weight, multiplier):
    dtype = torch.bfloat16 if quant == QuantType.FP8_DEQUANT else torch.float32
    result = host.estimate_host_memory(profile(quant, (weight,), dtype), plan())
    assert result.dense_parameter_bytes == (weight + 1) * multiplier
    assert result.dense_parameter_bytes >= 404
    assert result.persistent_bytes >= host.WORKER_OVERHEAD_BYTES + result.dense_parameter_bytes
    assert result.staging_bytes >= 6 * result.dense_parameter_bytes + 2 * result.checkpoint_bytes


@pytest.mark.parametrize("extension", ["bin", "safetensors"])
def test_full_shard_and_selected_clone_payload_is_reserved_even_for_one_layer(extension):
    result = host.estimate_host_memory(profile(), plan(size=10**9, extension=extension))
    assert result.checkpoint_bytes == 10**9
    assert result.staging_bytes > 2 * 10**9
    assert result.dense_parameter_bytes == 101


def test_span_and_heterogeneous_layers_affect_estimate_without_charging_gpu_cache_as_host_weights():
    small = host.estimate_host_memory(profile(), plan(0, 1))
    large = host.estimate_host_memory(profile(), plan(0, 3))
    assert large.dense_parameter_bytes == 603
    assert large.persistent_bytes > small.persistent_bytes
    assert large.staging_bytes > small.staging_bytes


@pytest.mark.parametrize("device", ["cpu", "mps", "xpu"])
def test_unsupported_runtime_cannot_receive_cuda_host_estimate(device):
    with pytest.raises(ValueError, match="CUDA"):
        host.estimate_host_memory(profile(), plan(), device=device)


@pytest.mark.parametrize("changes", [dict(num_devices=2), dict(adapter_memory_per_block=1)])
def test_estimator_rejects_unaccounted_runtime_variants(changes):
    with pytest.raises(ValueError):
        host.estimate_host_memory(replace(profile(), **changes), plan())


def test_estimator_rejects_invalid_span_and_unhandled_checkpoint_format():
    with pytest.raises(ValueError):
        host.estimate_host_memory(profile(), plan(0, 4))
    with pytest.raises(ValueError, match="format"):
        host.estimate_host_memory(profile(), plan(extension="zip"))


def test_missing_root_fails_without_creating_it(tmp_path):
    missing = tmp_path / "absent"
    with pytest.raises(host.HostResourceError, match="unavailable"):
        host.canonical_cache_root(missing)
    assert not missing.exists()


def test_existing_usage_includes_unrelated_and_partial_files_missing_artifact_gets_no_credit(root):
    Path(root, "unrelated").write_bytes(b"1234")
    Path(root, "partial.incomplete").write_bytes(b"12345")
    result = snapshot(root, claim(root))
    assert result.caches[0].used_bytes == 9
    assert not result.caches[0].verified_present_artifacts
    admitted = evaluate_resources([worker(claim(root))], snapshot=result, now=100, maximum_age_seconds=1)
    assert dict(admitted.cache_growth_bytes)[root] == 7


def test_verified_existing_file_receives_credit_and_real_native_volume_identity(root):
    artifact = claim(root)
    Path(root, artifact.relative_path).write_bytes(b"weights")
    result = snapshot(root, artifact)
    assert result.caches[0].verified_present_artifacts == (artifact,)
    assert result.caches[0].used_bytes == 7
    assert result.volumes[0].volume_id == result.caches[0].volume_id
    assert result.volumes[0].available_bytes > 0
    assert result.host_available_bytes == 10000


def test_existing_hardlink_aliases_count_storage_once_but_independent_copies_do_not(root):
    Path(root, "first").write_bytes(b"abc")
    os.link(Path(root, "first"), Path(root, "linked"))
    Path(root, "copy").write_bytes(b"abc")
    result = snapshot(root, claim(root, "first", b"abc"), claim(root, "linked", b"abc"))
    assert result.caches[0].used_bytes == 6
    assert len(result.caches[0].verified_present_artifacts) == 2


def test_independent_roots_share_real_volume_available_bytes(root):
    first, second = Path(root, "one"), Path(root, "two")
    first.mkdir()
    second.mkdir()
    roots = [host.canonical_cache_root(first), host.canonical_cache_root(second)]
    result = host.snapshot_resources([], host_limit_bytes=1000, cache_limits=dict.fromkeys(roots, 1000), now=1)
    assert len(result.volumes) == 1
    assert result.caches[0].volume_id == result.caches[1].volume_id


@pytest.mark.parametrize("data", [b"wrong!!", b"short"])
def test_corrupt_present_artifact_fails_without_credit(root, data):
    Path(root, "model.bin").write_bytes(data)
    with pytest.raises(host.HostResourceError, match="differs"):
        snapshot(root, claim(root))


def test_verification_cache_reuses_only_completed_unchanged_file_checks(root, monkeypatch):
    Path(root, "model.bin").write_bytes(b"weights")
    cache = host.VerificationCache()
    first = snapshot(root, claim(root), verification_cache=cache)
    monkeypatch.setattr(host.os, "open", lambda *args, **kwargs: pytest.fail("cached file must not be reopened"))
    second = snapshot(root, claim(root), verification_cache=cache, maximum_hash_bytes=0)
    assert second == first


def test_stat_change_invalidates_verification_cache_even_for_same_size(root):
    path = Path(root, "model.bin")
    path.write_bytes(b"weights")
    cache = host.VerificationCache()
    snapshot(root, claim(root), verification_cache=cache)
    path.write_bytes(b"changed")
    old = path.stat()
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns + 1000000000))
    with pytest.raises(host.HostResourceError, match="hash differs"):
        snapshot(root, claim(root), verification_cache=cache)


def test_partial_hash_is_never_cached(root):
    Path(root, "model.bin").write_bytes(b"weights")
    cache = host.VerificationCache()
    with pytest.raises(host.HostResourceError, match="byte bound"):
        snapshot(root, claim(root), verification_cache=cache, maximum_hash_bytes=1)
    assert not cache._entries


def test_file_mutation_after_hash_rejects_entire_snapshot(root, monkeypatch):
    path = Path(root, "model.bin")
    path.write_bytes(b"weights")
    original = host._volume

    def mutate(path_arg, info):
        path.write_bytes(b"replacement")
        return original(path_arg, info)

    monkeypatch.setattr(host, "_volume", mutate)
    with pytest.raises(host.HostResourceError, match="changed"):
        snapshot(root, claim(root))


def test_unrelated_directory_membership_mutation_is_detected(root, monkeypatch):
    original = host._volume

    def mutate(path, info):
        Path(root, "new-file").write_bytes(b"new")
        return original(path, info)

    monkeypatch.setattr(host, "_volume", mutate)
    with pytest.raises(host.HostResourceError, match="changed"):
        snapshot(root)


def test_linked_cache_or_ancestor_is_rejected(root, tmp_path):
    target = Path(root, "target")
    target.mkdir()
    linked = Path(root, "linked")
    try:
        linked.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("native symlink creation privilege is unavailable")
    with pytest.raises(host.HostResourceError, match="links"):
        host.canonical_cache_root(linked)
    with pytest.raises(host.HostResourceError, match="links"):
        snapshot(root)


def test_reparse_attribute_is_rejected_even_without_symlink_mode(root, monkeypatch):
    original = host.os.lstat

    def reparse(path):
        result = original(path)
        if str(path) == root:
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
        return result

    monkeypatch.setattr(host.os, "lstat", reparse)
    with pytest.raises(host.HostResourceError, match="reparse"):
        snapshot(root)


def test_overlapping_cache_roots_and_nested_mounts_reject(root, monkeypatch):
    nested = Path(root, "nested")
    nested.mkdir()
    with pytest.raises(host.HostResourceError, match="overlap"):
        host.snapshot_resources([], host_limit_bytes=1, cache_limits={root: 1, str(nested): 1}, now=1)
    monkeypatch.setattr(host, "_linux_mounts", lambda: (("/", str(nested), "ext4"),))
    with pytest.raises(host.HostResourceError, match="mount"):
        snapshot(root)


def test_bind_mount_ancestor_rejects_even_on_same_device(root, monkeypatch):
    monkeypatch.setattr(host, "_linux_mounts", lambda: (("/physical/elsewhere", root, "ext4"),))
    with pytest.raises(host.HostResourceError, match="mount"):
        snapshot(root)


@pytest.mark.parametrize("payload", ["", "malformed mount information"])
def test_linux_topology_must_be_observable_before_alias_checks(monkeypatch, payload):
    monkeypatch.setattr(host.sys, "platform", "linux")
    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: io.StringIO(payload))
    with pytest.raises(host.HostResourceError, match="topology"):
        host._linux_mounts()


def test_unreadable_filesystem_is_fixed_public_error(root, monkeypatch):
    monkeypatch.setattr(host.os, "scandir", lambda path: (_ for _ in ()).throw(PermissionError("private-path-token")))
    with pytest.raises(host.HostResourceError) as error:
        snapshot(root)
    assert "private" not in str(error.value)


def test_cooperative_timeout_and_entry_bound_fail_closed(root, monkeypatch):
    Path(root, "one").touch()
    with pytest.raises(host.HostResourceError, match="entry"):
        snapshot(root, maximum_entries=1)
    tick = iter([0, 31])
    monkeypatch.setattr(host.time, "monotonic", lambda: next(tick))
    with pytest.raises(host.HostResourceError, match="timed out"):
        snapshot(root)


def test_default_safety_reserves_reduce_available_not_configured_limit(root):
    result = host.snapshot_resources([], host_limit_bytes=3000, cache_limits={root: 1000}, now=1)
    assert result.host_configured_limit_bytes == 3000
    assert result.host_available_bytes == 0


@pytest.mark.parametrize("bad", [True, -1, 1.5, float("nan"), 2**64])
def test_malformed_host_limits_fail_before_filesystem_access(root, bad):
    with pytest.raises(ValueError):
        host.snapshot_resources([], host_limit_bytes=bad, cache_limits={root: 1000}, now=1)


def test_verification_cache_has_finite_lru_capacity():
    cache = host.VerificationCache(2)
    cache._add((1,))
    cache._add((2,))
    assert cache._contains((1,))
    cache._add((3,))
    assert not cache._contains((2,))
    assert len(cache._entries) == 2
