"""Inference-cache layout for Qwen3.5's hybrid text decoder."""

from __future__ import annotations

from typing import Any, Sequence

import torch
from hivemind.utils.tensor_descr import TensorDescriptor

from drift.utils.kv_cache import KVCacheStrategy, StandardGQACache
from drift.utils.misc import get_size_in_bytes


def _resolve_state_dtype(config) -> torch.dtype:
    dtype = getattr(config, "mamba_ssm_dtype", torch.float32)
    if isinstance(dtype, torch.dtype):
        return dtype
    if isinstance(dtype, str) and hasattr(torch, dtype):
        resolved = getattr(torch, dtype)
        if isinstance(resolved, torch.dtype):
            return resolved
    raise ValueError(f"Unsupported Qwen3.5 recurrent-state dtype: {dtype!r}")


class Qwen3_5HybridCache(KVCacheStrategy):
    """Block-aware cache for alternating Gated DeltaNet and full-attention layers.

    Full-attention blocks delegate to the historical BLOOM-layout K/V cache. Linear-attention
    blocks keep two fixed-size tensors per sequence: the causal-convolution window and the
    float32 recurrent delta state. A byte reservation pads short requests up to the standard
    full-attention accounting envelope so the existing DHT ``cache_tokens_available`` signal
    remains conservative and comparable across heterogeneous blocks.
    """

    def __init__(self, config, *, module: Any = None):
        super().__init__(config, module=module)
        if module is None:
            raise ValueError("Qwen3_5HybridCache requires the concrete block module")

        shards = tuple(getattr(module, "module_shards", (module,)))
        block_types = {getattr(shard, "block_type", None) for shard in shards}
        if len(block_types) != 1 or None in block_types:
            raise ValueError(f"Inconsistent or missing Qwen3.5 block types: {sorted(map(str, block_types))}")
        self.block_type = block_types.pop()
        if self.block_type not in ("linear_attention", "full_attention"):
            raise ValueError(f"Unsupported Qwen3.5 block type: {self.block_type!r}")

        self.shards = shards
        self.standard_cache = StandardGQACache(config, module=module)
        self.supports_paged_cache = self.block_type == "full_attention"

    @classmethod
    def estimate_cache_bytes(
        cls,
        config,
        max_length: int,
        *,
        dtype: torch.dtype,
        block_index: int | None = None,
    ) -> int:
        standard_bytes = super().estimate_cache_bytes(
            config,
            max_length,
            dtype=dtype,
            block_index=block_index,
        )
        if block_index is not None and config.layer_types[block_index] == "full_attention":
            return standard_bytes

        key_dim = config.linear_num_key_heads * config.linear_key_head_dim
        value_dim = config.linear_num_value_heads * config.linear_value_head_dim
        conv_dim = key_dim * 2 + value_dim
        state_bytes = conv_dim * config.linear_conv_kernel_dim * get_size_in_bytes(
            dtype
        ) + config.linear_num_value_heads * config.linear_key_head_dim * config.linear_value_head_dim * get_size_in_bytes(
            _resolve_state_dtype(config)
        )
        return max(standard_bytes, state_bytes)

    def get_cache_descriptors(
        self,
        batch_size: int,
        max_length: int,
        *,
        dtype: torch.dtype,
        devices: Sequence[torch.device],
        shard_num_heads: Sequence[int],
        head_dim=None,
        num_key_value_groups=None,
    ) -> Sequence[TensorDescriptor]:
        if self.block_type == "full_attention":
            return self.standard_cache.get_cache_descriptors(
                batch_size,
                max_length,
                dtype=dtype,
                devices=devices,
                shard_num_heads=shard_num_heads,
                head_dim=head_dim,
                num_key_value_groups=num_key_value_groups,
            )

        if len(devices) != 1 or len(self.shards) != 1:
            raise NotImplementedError("Qwen3.5 linear-attention caching currently requires one device")
        mixer = self.shards[0].linear_attn
        device = devices[0]
        recurrent_dtype = _resolve_state_dtype(self.config)
        conv = TensorDescriptor((batch_size, mixer.conv_dim, mixer.conv_kernel_size), dtype=dtype, device=device)
        recurrent = TensorDescriptor(
            (batch_size, mixer.num_v_heads, mixer.head_k_dim, mixer.head_v_dim),
            dtype=recurrent_dtype,
            device=device,
        )

        # Preserve the standard cache-token accounting contract. The reservation contains no
        # inference data and is deliberately omitted from layer_past and beam reordering.
        virtual_bytes_per_batch = (
            2 * self.config.num_key_value_heads * self.config.head_dim * max_length * get_size_in_bytes(dtype)
        )
        state_bytes_per_batch = mixer.conv_dim * mixer.conv_kernel_size * get_size_in_bytes(
            dtype
        ) + mixer.num_v_heads * mixer.head_k_dim * mixer.head_v_dim * get_size_in_bytes(recurrent_dtype)
        reservation_bytes = max(0, virtual_bytes_per_batch - state_bytes_per_batch)
        descriptors = [conv, recurrent]
        if reservation_bytes:
            descriptors.append(TensorDescriptor((batch_size, reservation_bytes), dtype=torch.uint8, device=device))
        return descriptors

    def select_layer_past(self, cache_tensors, prefix_length: int, *, num_shards: int):
        if self.block_type == "full_attention":
            return self.standard_cache.select_layer_past(cache_tensors, prefix_length, num_shards=num_shards)
        if prefix_length == 0:
            return None
        return tuple(cache_tensors[:2])

    def update_cache(self, cache_tensors, new_kvs, prefix_length: int) -> None:
        if self.block_type == "full_attention":
            self.standard_cache.update_cache(cache_tensors, new_kvs, prefix_length)
            return
        if len(new_kvs) != 2:
            raise ValueError(f"Qwen3.5 linear attention returned {len(new_kvs)} states, expected 2")
        cache_tensors[0].copy_(new_kvs[0])
        cache_tensors[1].copy_(new_kvs[1])

    def reorder_cache_inplace(self, cache_tensors, hypo_ids: torch.Tensor) -> None:
        if self.block_type == "full_attention":
            self.standard_cache.reorder_cache_inplace(cache_tensors, hypo_ids)
            return
        for cache_tensor in cache_tensors[:2]:
            cache_tensor[...] = cache_tensor[hypo_ids.to(cache_tensor.device)]
