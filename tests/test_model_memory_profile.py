import math
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest
import torch

from drift.models.gemma4.cache import Gemma4Cache
from drift.models.qwen3_5.cache import Qwen3_5HybridCache
from drift.server.memory_budget import build_model_memory_profile, estimate_device_memory
from drift.server.server import Server
from drift.utils.convert_block import QuantType
from drift.utils.kv_cache import MLACache, StandardGQACache


class VariableBlock(torch.nn.Module):
    def __init__(self, config, layer_idx=0):
        super().__init__()
        self.weight = torch.nn.Parameter(torch.empty(16 * (layer_idx + 1)))


def _config(**overrides):
    values = dict(
        block_class=VariableBlock,
        torch_dtype=torch.bfloat16,
        num_hidden_layers=3,
        hidden_size=16,
        num_attention_heads=4,
        num_key_value_heads=2,
        num_key_value_groups=2,
        head_dim=4,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _profile(config=None, **overrides):
    values = dict(dtype=torch.bfloat16, quant_type=QuantType.NONE, attn_cache_tokens=8)
    values.update(overrides)
    return build_model_memory_profile(config or _config(), **values)


@pytest.mark.parametrize("num_devices", [1, 2])
def test_exact_spans_and_movable_estimate_match_child_and_previous_formula(num_devices):
    profile = _profile(num_devices=num_devices, adapter_memory_per_block=13)
    server = Server.__new__(Server)
    server.block_config = _config()
    server.tensor_parallel_devices = (torch.device("cpu"),) * num_devices
    server._block_memory_bytes_by_block = profile.block_memory_bytes
    server._adapter_memory_per_block = 13

    assert profile.block_weight_bytes == (32, 65, 97)
    assert profile.block_cache_bytes == (256, 256, 256)
    assert profile.block_memory_prefix_sums == (0, 288, 609, 962)
    for start in range(profile.num_blocks):
        for end in range(start + 1, profile.num_blocks + 1):
            expected = math.ceil(
                2 * 1024**3 * num_devices / 14336 * 16
                + sum(profile.block_memory_bytes[start:end])
                + (end - start) * 13
            )
            assert profile.estimate_span(start, end) == expected
            assert profile.estimate(end - start, range(start, end)) == expected
            assert server._estimate_device_memory(end - start, range(start, end)) == expected
    assert profile.estimate(2) == profile.estimate_span(1, 3)
    assert server._estimate_device_memory(2) == profile.estimate(2)


@pytest.mark.parametrize("quant_type", [QuantType.NONE, QuantType.FP8_DEQUANT])
def test_weight_bytes_use_pinned_execution_dtype_even_for_fp8_source(quant_type):
    config = _config(torch_dtype=torch.float32, quantization_config={"quant_method": "fp8"})
    half = _profile(config, dtype=torch.bfloat16, quant_type=quant_type)
    full = _profile(config, dtype=torch.float32, quant_type=quant_type)

    assert half.block_weight_bytes == (32, 65, 97)
    assert full.block_weight_bytes == (65, 129, 194)
    assert half.dtype == torch.bfloat16
    assert half.quant_type == quant_type
    assert half.block_cache_bytes[0] * 2 == full.block_cache_bytes[0]


@pytest.mark.parametrize("groups,expected_tokens", [(1, 4096), (2, 16384)])
def test_default_cache_tokens_match_runtime(groups, expected_tokens):
    profile = _profile(_config(num_key_value_groups=groups), attn_cache_tokens=None)
    assert profile.attn_cache_tokens == expected_tokens
    assert profile.block_cache_bytes == (2 * 16 * expected_tokens // groups * 2,) * 3


def test_gemma_hybrid_cache_geometry_is_preserved_per_layer():
    config = _config(
        kv_cache_strategy=Gemma4Cache,
        layer_types=["sliding_attention", "full_attention", "sliding_attention"],
        global_head_dim=8,
        num_global_key_value_heads=4,
    )
    profile = _profile(config)
    assert profile.block_cache_bytes == (256, 1024, 256)
    assert profile.estimate_span(0, 1) < profile.estimate_span(1, 2)


@pytest.mark.parametrize("head_dim", [4, 8])
def test_qwen_recurrent_state_is_charged_above_short_dense_cache(head_dim):
    config = _config(
        kv_cache_strategy=Qwen3_5HybridCache,
        head_dim=head_dim,
        layer_types=["linear_attention", "full_attention", "linear_attention"],
        linear_num_key_heads=2,
        linear_num_value_heads=4,
        linear_key_head_dim=4,
        linear_value_head_dim=8,
        linear_conv_kernel_dim=4,
        mamba_ssm_dtype="float32",
    )
    profile = _profile(config, attn_cache_tokens=1)
    # BF16 convolution window plus FP32 recurrent state, versus a short dense KV cache.
    assert profile.block_cache_bytes == (896, 8 * head_dim, 896)
    for block_type in ("linear_attention", "full_attention"):
        module = SimpleNamespace(
            block_type=block_type,
            linear_attn=SimpleNamespace(conv_dim=48, conv_kernel_size=4, num_v_heads=4, head_k_dim=4, head_v_dim=8),
        )
        strategy = Qwen3_5HybridCache(config, module=module)
        block_index = config.layer_types.index(block_type)
        for tokens in (1, 100):
            descriptors = strategy.get_cache_descriptors(
                batch_size=1,
                max_length=tokens,
                dtype=torch.bfloat16,
                devices=(torch.device("cpu"),),
                shard_num_heads=(4,),
            )
            actual_bytes = sum(
                math.prod(descriptor.shape) * torch.tensor([], dtype=descriptor.dtype).element_size()
                for descriptor in descriptors
            )
            assert (
                strategy.estimate_cache_bytes(config, tokens, dtype=torch.bfloat16, block_index=block_index)
                == actual_bytes
            )


def test_profile_copies_inputs_and_is_immutable():
    profile = _profile()
    weights = list(profile.block_weight_bytes)
    copied = replace(profile, block_weight_bytes=weights)
    weights[0] = 1
    assert copied.block_weight_bytes == profile.block_weight_bytes
    with pytest.raises(FrozenInstanceError):
        copied.hidden_size = 32


@pytest.mark.parametrize(
    "overrides",
    [
        {"num_hidden_layers": 0},
        {"num_hidden_layers": True},
        {"hidden_size": -1},
        {"num_attention_heads": 0},
        {"num_key_value_groups": 3},
        {"head_dim": 0},
        {"head_dim": None, "hidden_size": 17},
        {"kv_cache_strategy": object},
    ],
)
def test_rejects_invalid_or_unaccounted_model_geometry(overrides):
    with pytest.raises(ValueError):
        _profile(_config(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {"dtype": "auto"},
        {"dtype": torch.uint8},
        {"quant_type": None},
        {"quant_type": "fp8_dequant"},
        {"attn_cache_tokens": 0},
        {"attn_cache_tokens": True},
        {"num_devices": 0},
        {"adapter_memory_per_block": -1},
    ],
)
def test_rejects_unresolved_or_invalid_runtime_inputs(overrides):
    with pytest.raises(ValueError):
        _profile(**overrides)


@pytest.mark.parametrize("size", [-1, True, 1.5, float("inf"), float("nan")])
def test_rejects_invalid_cache_estimator_result(size):
    class BrokenCache(StandardGQACache):
        @classmethod
        def estimate_cache_bytes(cls, *args, **kwargs):
            return size

    with pytest.raises(ValueError, match="block cache bytes"):
        _profile(_config(kv_cache_strategy=BrokenCache))


@pytest.mark.parametrize("start,end", [(-1, 1), (0, 0), (2, 1), (0, 4), (True, 2), (0, 1.5)])
def test_rejects_invalid_spans(start, end):
    with pytest.raises(ValueError):
        _profile().estimate_span(start, end)


@pytest.mark.parametrize("indices", [[0], [0, 0], [-1, 0], [0, 3], [0, True]])
def test_rejects_inconsistent_explicit_indices(indices):
    with pytest.raises(ValueError):
        _profile().estimate(2, indices)


@pytest.mark.parametrize("dtype", [torch.bfloat16, torch.float32])
@pytest.mark.parametrize("shard_heads", [(4,), (2, 2)])
@pytest.mark.parametrize("tokens", [1, 17])
def test_mla_cache_estimate_matches_actual_asymmetric_tensor_descriptors(dtype, shard_heads, tokens):
    config = _config(qk_nope_head_dim=6, qk_rope_head_dim=2, v_head_dim=4, kv_cache_strategy=MLACache)
    descriptors = MLACache(config).get_cache_descriptors(
        batch_size=1,
        max_length=tokens,
        dtype=dtype,
        devices=(torch.device("cpu"),) * len(shard_heads),
        shard_num_heads=shard_heads,
    )
    actual_bytes = sum(
        math.prod(descriptor.shape) * torch.tensor([], dtype=descriptor.dtype).element_size()
        for descriptor in descriptors
    )
    estimate = MLACache.estimate_cache_bytes(config, tokens, dtype=dtype, block_index=0)
    assert estimate == actual_bytes
    assert estimate > StandardGQACache.estimate_cache_bytes(config, tokens, dtype=dtype)
    assert _profile(config, dtype=dtype, attn_cache_tokens=tokens).block_cache_bytes == (actual_bytes,) * 3


@pytest.mark.parametrize("head_dim", [None, 4, 12])
@pytest.mark.parametrize("groups", [1, 2])
@pytest.mark.parametrize("shard_heads", [(4,), (2, 2)])
def test_dense_cache_estimate_matches_descriptors_with_explicit_head_geometry(head_dim, groups, shard_heads):
    config = _config(head_dim=head_dim, num_key_value_groups=groups)
    descriptors = StandardGQACache(config).get_cache_descriptors(
        batch_size=1,
        max_length=17,
        dtype=torch.float32,
        devices=(torch.device("cpu"),) * len(shard_heads),
        shard_num_heads=shard_heads,
    )
    actual_bytes = sum(math.prod(descriptor.shape) * 4 for descriptor in descriptors)
    assert StandardGQACache.estimate_cache_bytes(config, 17, dtype=torch.float32) == actual_bytes
    assert _profile(config, dtype=torch.float32, attn_cache_tokens=17).block_cache_bytes == (actual_bytes,) * 3


def test_raw_estimator_keeps_zero_workspace_fixture_compatible():
    assert (
        estimate_device_memory((100, 200, 300), hidden_size=0, num_devices=1, num_blocks=2, adapter_memory_per_block=10)
        == 520
    )
