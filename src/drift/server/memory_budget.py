"""Shared pre-load device-memory estimates for serving and placement.

Inputs must come from the verified model config and resolved runtime profile. This module
does not fetch metadata or weights, probe devices, or estimate artifact storage / host RAM.
Weight sizes retain the runtime's quantization/metadata approximation; cache sizes come from
the model's cache strategy. These are admission estimates, not measured memory usage.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import accumulate
from typing import Optional, Sequence

import torch
from transformers import PretrainedConfig

from drift.server.block_utils import get_block_size
from drift.utils.convert_block import QuantType
from drift.utils.kv_cache import KVCacheStrategy, StandardGQACache


def _integer(value: int, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _workspace_bytes(hidden_size: int, num_devices: int) -> int:
    _integer(hidden_size, "hidden_size")
    _integer(num_devices, "num_devices", minimum=1)
    # Same per-device workspace allowance used by the server before loading weights.
    return math.ceil(2 * 1024**3 * num_devices / 14336 * hidden_size)


def _total_memory_bytes(
    block_memory_bytes: int, *, hidden_size: int, num_devices: int, num_blocks: int, adapter_memory_per_block: int
) -> int:
    _integer(hidden_size, "hidden_size")
    _integer(num_devices, "num_devices", minimum=1)
    # Keep the original server formula, including its final rounding, in one place.
    return math.ceil(
        2 * 1024**3 * num_devices / 14336 * hidden_size
        + block_memory_bytes
        + num_blocks * _integer(adapter_memory_per_block, "adapter_memory_per_block")
    )


def estimate_device_memory(
    block_memory_bytes_by_block: Sequence[int],
    *,
    hidden_size: int,
    num_devices: int,
    num_blocks: int,
    block_indices: Optional[Sequence[int]] = None,
    adapter_memory_per_block: int = 0,
) -> int:
    """Estimate an exact selection, or conservatively the largest ``num_blocks`` layers.

    Each layer's byte count includes weights and attention cache, excluding workspace and
    adapters. Explicit indices must be distinct and match ``num_blocks``. The movable case
    preserves the server's worst-layer admission rule for heterogeneous architectures.
    """
    sizes = tuple(_integer(size, "block_memory_bytes") for size in block_memory_bytes_by_block)
    _integer(num_blocks, "num_blocks", minimum=1)
    if num_blocks > len(sizes):
        raise ValueError("num_blocks exceeds the model's layer count")
    if block_indices is None:
        block_memory_bytes = sum(sorted(sizes, reverse=True)[:num_blocks])
    else:
        indices = tuple(_integer(index, "block index") for index in block_indices)
        if len(indices) != num_blocks or len(set(indices)) != num_blocks:
            raise ValueError("block_indices must contain num_blocks distinct indices")
        if any(index >= len(sizes) for index in indices):
            raise ValueError("block_indices exceed the model's layer count")
        block_memory_bytes = sum(sizes[index] for index in indices)
    return _total_memory_bytes(
        block_memory_bytes,
        hidden_size=hidden_size,
        num_devices=num_devices,
        num_blocks=num_blocks,
        adapter_memory_per_block=adapter_memory_per_block,
    )


@dataclass(frozen=True)
class ModelMemoryProfile:
    """Immutable, resolved device-memory geometry; artifact bytes are deliberately separate."""

    hidden_size: int
    num_devices: int
    dtype: torch.dtype
    quant_type: QuantType
    attn_cache_tokens: int
    block_weight_bytes: tuple[int, ...]
    block_cache_bytes: tuple[int, ...]
    adapter_memory_per_block: int = 0
    block_memory_bytes: tuple[int, ...] = field(init=False)
    block_memory_prefix_sums: tuple[int, ...] = field(init=False)
    workspace_bytes: int = field(init=False)

    def __post_init__(self) -> None:
        _integer(self.hidden_size, "hidden_size", minimum=1)
        _integer(self.num_devices, "num_devices", minimum=1)
        _integer(self.attn_cache_tokens, "attn_cache_tokens", minimum=1)
        _integer(self.adapter_memory_per_block, "adapter_memory_per_block")
        if self.dtype not in (torch.float16, torch.bfloat16, torch.float32):
            raise ValueError("dtype must be an explicit supported execution dtype")
        if not isinstance(self.quant_type, QuantType):
            raise ValueError("quant_type must be an explicit QuantType")
        weights = tuple(_integer(size, "block weight bytes", minimum=1) for size in self.block_weight_bytes)
        caches = tuple(_integer(size, "block cache bytes") for size in self.block_cache_bytes)
        if not weights or len(weights) != len(caches):
            raise ValueError("weight and cache sizes must describe the same non-empty model")
        sizes = tuple(weight + cache for weight, cache in zip(weights, caches))
        object.__setattr__(self, "block_weight_bytes", weights)
        object.__setattr__(self, "block_cache_bytes", caches)
        object.__setattr__(self, "block_memory_bytes", sizes)
        object.__setattr__(self, "block_memory_prefix_sums", (0, *accumulate(sizes)))
        object.__setattr__(self, "workspace_bytes", _workspace_bytes(self.hidden_size, self.num_devices))

    @property
    def num_blocks(self) -> int:
        return len(self.block_memory_bytes)

    def estimate_span(self, start_block: int, end_block: int) -> int:
        """Estimate one non-empty half-open span in constant time."""
        _integer(start_block, "start_block")
        _integer(end_block, "end_block", minimum=1)
        if not start_block < end_block <= self.num_blocks:
            raise ValueError("span must be non-empty and within the model's layer count")
        return _total_memory_bytes(
            self.block_memory_prefix_sums[end_block] - self.block_memory_prefix_sums[start_block],
            hidden_size=self.hidden_size,
            num_devices=self.num_devices,
            num_blocks=end_block - start_block,
            adapter_memory_per_block=self.adapter_memory_per_block,
        )

    def estimate(self, num_blocks: int, block_indices: Optional[Sequence[int]] = None) -> int:
        return estimate_device_memory(
            self.block_memory_bytes,
            hidden_size=self.hidden_size,
            num_devices=self.num_devices,
            num_blocks=num_blocks,
            block_indices=block_indices,
            adapter_memory_per_block=self.adapter_memory_per_block,
        )


def build_model_memory_profile(
    config: PretrainedConfig,
    *,
    dtype: torch.dtype,
    quant_type: QuantType,
    attn_cache_tokens: Optional[int] = None,
    num_devices: int = 1,
    adapter_memory_per_block: int = 0,
) -> ModelMemoryProfile:
    """Build from verified config and explicit runtime dtype/quantization, without weights.

    Layer modules are constructed with meta parameters by ``get_block_size``. Manifest
    identity verification and resolution of device-supported runtime choices belong to the
    caller; passing ``auto`` or an unresolved quantization choice is rejected.
    """
    num_blocks = _integer(config.num_hidden_layers, "num_hidden_layers", minimum=1)
    _integer(config.hidden_size, "hidden_size", minimum=1)
    num_heads = _integer(config.num_attention_heads, "num_attention_heads", minimum=1)
    groups = _integer(config.num_key_value_groups, "num_key_value_groups", minimum=1)
    if num_heads % groups:
        raise ValueError("num_attention_heads must be divisible by num_key_value_groups")
    head_dim = getattr(config, "head_dim", None)
    if head_dim is not None:
        _integer(head_dim, "head_dim", minimum=1)
    elif config.hidden_size % num_heads:
        raise ValueError("hidden_size must be divisible by num_attention_heads when head_dim is absent")
    _integer(num_devices, "num_devices", minimum=1)
    _integer(adapter_memory_per_block, "adapter_memory_per_block")
    if dtype not in (torch.float16, torch.bfloat16, torch.float32):
        raise ValueError("dtype must be an explicit supported execution dtype")
    if not isinstance(quant_type, QuantType):
        raise ValueError("quant_type must be an explicit QuantType")
    if attn_cache_tokens is None:
        attn_cache_tokens = 16384 if groups > 1 else 4096
    _integer(attn_cache_tokens, "attn_cache_tokens", minimum=1)
    cache_strategy = getattr(config, "kv_cache_strategy", StandardGQACache)
    if not isinstance(cache_strategy, type) or not issubclass(cache_strategy, KVCacheStrategy):
        raise ValueError("config must provide a supported KV cache strategy")
    weights = []
    caches = []
    for block_index in range(num_blocks):
        weights.append(
            _integer(
                get_block_size(config, "memory", dtype=dtype, quant_type=quant_type, layer_idx=block_index),
                "block weight bytes",
                minimum=1,
            )
        )
        caches.append(
            _integer(
                cache_strategy.estimate_cache_bytes(config, attn_cache_tokens, dtype=dtype, block_index=block_index),
                "block cache bytes",
            )
        )
    return ModelMemoryProfile(
        hidden_size=config.hidden_size,
        num_devices=num_devices,
        dtype=dtype,
        quant_type=quant_type,
        attn_cache_tokens=attn_cache_tokens,
        block_weight_bytes=tuple(weights),
        block_cache_bytes=tuple(caches),
        adapter_memory_per_block=adapter_memory_per_block,
    )
