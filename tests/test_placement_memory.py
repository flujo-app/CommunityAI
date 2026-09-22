import hashlib
import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import Mock

import pytest
import torch

from drift.model_manifest import ManifestArtifactVerifier, ManifestError, ModelManifest
from drift.node import placement_memory
from drift.node.placement_memory import PlacementMemoryCache, load_placement_memory
from drift.utils.convert_block import QuantType


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    def forbidden_download(*args, **kwargs):
        raise AssertionError("local placement fixture must never download")

    monkeypatch.setattr("huggingface_hub.hf_hub_download", forbidden_download)
    monkeypatch.setattr(ManifestArtifactVerifier, "_resumable_hub_download", forbidden_download)
    monkeypatch.setattr(torch.cuda, "_lazy_init", forbidden_download)
    roots = []

    def create(*, dtype="bfloat16", quantization="none", config_changes=None, weight_map=None, raw_config=None):
        root = tmp_path / str(len(roots))
        root.mkdir()
        roots.append(root)
        config = {
            "model_type": "llama",
            "architectures": ["LlamaForCausalLM"],
            "num_hidden_layers": 3,
            "hidden_size": 32,
            "intermediate_size": 64,
            "num_attention_heads": 4,
            "num_key_value_heads": 2,
            "head_dim": 8,
            "vocab_size": 32,
            "max_position_embeddings": 64,
            "torch_dtype": "float32",
        }
        if quantization == "fp8_dequant":
            config["quantization_config"] = {"quant_method": "fp8"}
        config.update(config_changes or {})
        config_bytes = json.dumps(config).encode() if raw_config is None else raw_config
        index = {
            "weight_map": (
                {f"model.layers.{index}.attention.weight": "shared.safetensors" for index in range(3)}
                if weight_map is None
                else weight_map
            )
        }
        index_bytes = json.dumps(index).encode()
        artifacts = []
        for name, role, content in (
            ("config.json", "config", config_bytes),
            ("model.safetensors.index.json", "weight_index", index_bytes),
        ):
            (root / name).write_bytes(content)
            artifacts.append(
                {"path": name, "role": role, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
            )
        # These bytes deliberately do not exist. Planning must never open weights.
        artifacts.append({"path": "shared.safetensors", "role": "weight", "size": 13, "sha256": "0" * 64})
        artifacts.append({"path": "tokenizer.json", "role": "tokenizer", "size": 2, "sha256": "1" * 64})
        source = json.loads(Path("tests/data/model_manifest_v1_vector.json").read_text())
        source["model"].update(num_blocks=3, context_length=64)
        source["runtime"].update(dtype=dtype, quantization=quantization)
        source["artifacts"] = artifacts
        return ModelManifest.from_dict(source), root

    return create


def _load(manifest, root, **kwargs):
    options = dict(
        device="cpu", cache_dir=root / "cache", max_disk_space=1024**2, artifact_root=root, attn_cache_tokens=8
    )
    options.update(kwargs)
    return load_placement_memory(manifest, **options)


def test_verified_metadata_only_builds_immutable_manifest_bound_profile(snapshot, monkeypatch):
    manifest, root = snapshot()
    accesses = []
    original = ManifestArtifactVerifier.ensure_path

    def metadata_only(self, path, **kwargs):
        assert self.allowed_paths == frozenset({"config.json", "model.safetensors.index.json"})
        accesses.append(path)
        return original(self, path, **kwargs)

    monkeypatch.setattr(ManifestArtifactVerifier, "ensure_path", metadata_only)
    metadata = _load(manifest, root)
    assert set(accesses) == {"config.json", "model.safetensors.index.json"}
    assert not (root / "shared.safetensors").exists()
    assert metadata.manifest_digest == manifest.digest_id
    assert metadata.cache_root == (root / "cache").resolve()
    assert metadata.metadata_root == root.resolve()
    assert metadata.block_prefix == "model.layers"
    assert metadata.memory_profile.dtype == torch.bfloat16
    assert metadata.memory_profile.quant_type is QuantType.NONE
    assert metadata.memory_profile.num_blocks == 3
    assert metadata.memory_profile.estimate_span(0, 3) > metadata.memory_profile.estimate_span(0, 1)
    assert not metadata.native_quantization_probe_required
    with pytest.raises(TypeError):
        metadata.weight_map["new"] = "outside.safetensors"
    with pytest.raises(FrozenInstanceError):
        metadata.block_prefix = "other.layers"


def test_fp8_source_expands_to_pinned_bf16_memory_not_artifact_bytes(snapshot):
    manifest, root = snapshot(quantization="fp8_dequant")
    metadata = _load(manifest, root)
    profile = metadata.memory_profile
    assert profile.dtype == torch.bfloat16
    assert profile.quant_type is QuantType.FP8_DEQUANT
    assert sum(profile.block_weight_bytes) > 13
    dense, dense_root = snapshot()
    assert profile.block_weight_bytes == _load(dense, dense_root).memory_profile.block_weight_bytes


@pytest.mark.parametrize("quantization", ["int8", "nf4"])
def test_declared_native_quantization_is_estimated_without_probe_or_fallback(snapshot, quantization):
    manifest, root = snapshot(quantization=quantization)
    metadata = _load(manifest, root, device="cuda:3")
    assert metadata.native_quantization_probe_required
    assert metadata.device == "cuda:3"
    assert metadata.memory_profile.quant_type is QuantType[quantization.upper()]
    dense, dense_root = snapshot()
    assert metadata.memory_profile.block_weight_bytes[0] < _load(dense, dense_root).memory_profile.block_weight_bytes[0]


@pytest.mark.parametrize("device", ["cuda", "cuda:7", "cpu"])
def test_profile_is_not_a_hardware_availability_probe(snapshot, device):
    manifest, root = snapshot()
    metadata = _load(manifest, root, device=device)
    assert metadata.device == ("cuda:0" if device == "cuda" else device)


@pytest.mark.parametrize("device", ["mps", "xpu", "meta"])
def test_unqualified_device_contract_rejects_before_metadata_access(snapshot, monkeypatch, device):
    manifest, root = snapshot()
    ensure = Mock(side_effect=AssertionError("unsupported device must reject before metadata"))
    monkeypatch.setattr(ManifestArtifactVerifier, "ensure_startup_metadata", ensure)
    with pytest.raises(ManifestError, match="CPU or CUDA"):
        _load(manifest, root, device=device)
    assert not ensure.called


def test_cpu_float16_rejects_like_child_runtime(snapshot):
    manifest, root = snapshot(dtype="float16")
    with pytest.raises(ManifestError, match="float16 is not supported on CPU"):
        _load(manifest, root)


def test_profile_cache_is_bounded_and_keys_manifest_root_device_and_cache_tokens(snapshot, monkeypatch):
    manifest, root = snapshot()
    cache = PlacementMemoryCache(max_entries=2)
    build = Mock(wraps=placement_memory.build_model_memory_profile)
    monkeypatch.setattr(placement_memory, "build_model_memory_profile", build)
    first = _load(manifest, root, profile_cache=cache)
    same = _load(manifest, root, profile_cache=cache)
    assert same.memory_profile is first.memory_profile
    assert same.weight_map is not first.weight_map
    assert build.call_count == 1
    assert _load(manifest, root, profile_cache=cache, attn_cache_tokens=9).memory_profile is not first.memory_profile
    assert _load(manifest, root, profile_cache=cache, device="cuda:1").memory_profile is not first.memory_profile
    assert len(cache) == 2
    assert _load(manifest, root, profile_cache=cache).memory_profile is not first.memory_profile
    assert build.call_count == 4
    changed, changed_root = snapshot(dtype="float32")
    assert _load(changed, changed_root, profile_cache=cache).memory_profile.dtype == torch.float32
    assert (
        _load(manifest, root, profile_cache=cache, cache_dir=root / "other-cache").memory_profile
        is not first.memory_profile
    )
    assert len(cache) == 2


@pytest.mark.parametrize("filename", ["config.json", "model.safetensors.index.json"])
def test_corruption_rejects_even_when_a_matching_profile_was_cached(snapshot, filename):
    manifest, root = snapshot()
    cache = PlacementMemoryCache()
    _load(manifest, root, profile_cache=cache)
    (root / filename).write_bytes(b"{}")
    with pytest.raises(ManifestError):
        _load(manifest, root, profile_cache=cache)
    assert len(cache) == 1


def test_profile_cache_separates_snapshot_roots_even_with_identical_manifest_and_cache_root(snapshot):
    manifest, root = snapshot()
    identical, other_root = snapshot()
    assert identical.digest_id == manifest.digest_id
    cache = PlacementMemoryCache()
    first = _load(manifest, root, profile_cache=cache)
    second = _load(identical, other_root, profile_cache=cache, cache_dir=root / "cache")
    assert first.memory_profile is not second.memory_profile
    assert first.metadata_root != second.metadata_root
    assert len(cache) == 2


def test_unsharded_metadata_never_opens_full_checkpoint(snapshot):
    manifest, root = snapshot()
    document = manifest.to_dict()
    document["artifacts"] = [artifact for artifact in document["artifacts"] if artifact["role"] != "weight_index"]
    for artifact in document["artifacts"]:
        if artifact["role"] == "weight":
            artifact["path"] = "model.safetensors"
    metadata = _load(ModelManifest.from_dict(document), root)
    assert metadata.weight_map is None
    assert metadata.memory_profile.num_blocks == 3
    assert not (root / "model.safetensors").exists()


@pytest.mark.parametrize("unsupported", ["version", "adapter"])
def test_runtime_contract_rejects_before_metadata_access(snapshot, monkeypatch, unsupported):
    manifest, root = snapshot()
    document = manifest.to_dict()
    if unsupported == "version":
        document["runtime"].update(minimum_version="9.0.0", maximum_version_exclusive="10.0.0")
    else:
        document["runtime"]["adapter_profile"] = "sha256:" + "f" * 64
    ensure = Mock(side_effect=AssertionError("unsupported profile must not materialize metadata"))
    monkeypatch.setattr(ManifestArtifactVerifier, "ensure_startup_metadata", ensure)
    with pytest.raises(ManifestError):
        _load(ModelManifest.from_dict(document), root)
    assert not ensure.called


def test_missing_local_metadata_never_attempts_download(snapshot):
    manifest, root = snapshot()
    (root / "model.safetensors.index.json").unlink()
    with pytest.raises(ManifestError):
        _load(manifest, root)


@pytest.mark.parametrize(
    "config_changes,match",
    [
        ({"architectures": ["OtherForCausalLM"]}, "architecture"),
        ({"num_hidden_layers": 4}, "blocks"),
        ({"max_position_embeddings": 65}, "context length"),
        ({"quantization_config": {"quant_method": "fp8"}}, "pre-quantized"),
        ({"configuration_files": ["config.5.0.0.json"]}, "redirects"),
    ],
)
def test_unmatched_or_redirected_config_rejects(snapshot, config_changes, match):
    manifest, root = snapshot(config_changes=config_changes)
    with pytest.raises(ManifestError, match=match):
        _load(manifest, root)


def test_fp8_profile_requires_exact_source_quantization(snapshot):
    manifest, root = snapshot(quantization="fp8_dequant", config_changes={"quantization_config": {}})
    with pytest.raises(ManifestError, match="requires source config"):
        _load(manifest, root)


@pytest.mark.parametrize(
    "weight_map,match",
    [
        ({"model.layers.0.weight": "shared.safetensors"}, "no parameters"),
        ({"model.layers.0.weight": "../outside.safetensors"}, "non-normalized"),
        ({"model.layers.0.weight": "missing.safetensors"}, "not declared"),
        ({"model.layers.0.weight": "config.json"}, "non-checkpoint"),
    ],
)
def test_entire_index_is_validated_before_profile(snapshot, weight_map, match):
    manifest, root = snapshot(weight_map=weight_map)
    with pytest.raises(ManifestError, match=match):
        _load(manifest, root)


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"a":NaN}', b"[]"])
def test_ambiguous_or_invalid_config_json_rejects_before_transformers(snapshot, raw):
    manifest, root = snapshot(raw_config=raw)
    with pytest.raises(ManifestError, match="Placement config"):
        _load(manifest, root)


@pytest.mark.parametrize("role", ["config", "weight_index"])
def test_declared_metadata_limit_rejects_before_materialization(snapshot, monkeypatch, role):
    manifest, root = snapshot()
    document = manifest.to_dict()
    for artifact in document["artifacts"]:
        if artifact["role"] == role:
            artifact["size"] = 17 * 1024**2
    manifest = ModelManifest.from_dict(document)
    ensure = Mock(side_effect=AssertionError("oversize metadata must not materialize"))
    monkeypatch.setattr(ManifestArtifactVerifier, "ensure_startup_metadata", ensure)
    with pytest.raises(ManifestError, match="bounded planning size"):
        _load(manifest, root, max_disk_space=100 * 1024**2)
    assert not ensure.called


def test_disk_metadata_budget_and_layer_bound_reject(snapshot):
    manifest, root = snapshot()
    with pytest.raises(ManifestError, match="disk budget"):
        _load(manifest, root, max_disk_space=1)
    document = manifest.to_dict()
    document["model"]["num_blocks"] = 513
    with pytest.raises(ManifestError, match="bounded automatic layer count"):
        _load(ModelManifest.from_dict(document), root)


@pytest.mark.parametrize("capacity", [0, -1, True, 17])
def test_profile_cache_capacity_is_bounded(capacity):
    with pytest.raises(ValueError):
        PlacementMemoryCache(capacity)
