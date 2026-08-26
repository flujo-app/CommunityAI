"""Inference-cache accounting for Gemma 4 decoder variants."""

from __future__ import annotations

from typing import Optional

import torch
from transformers import PretrainedConfig

from drift.utils.kv_cache import StandardGQACache
from drift.utils.misc import get_size_in_bytes


class Gemma4Cache(StandardGQACache):
    """Standard GQA cache with Gemma 4's per-layer cache geometry.

    Sliding-attention layers use ``head_dim`` and ``num_key_value_heads``. Full-attention
    layers use the wider ``global_head_dim`` and, on Unified/MoE variants, may also use a
    distinct ``num_global_key_value_heads``. The descriptors already receive these values
    from each concrete block; this override makes pre-load admission accounting agree with
    those descriptors.
    """

    @staticmethod
    def _geometry(config: PretrainedConfig, layer_type: str) -> tuple[int, int]:
        if layer_type == "full_attention":
            num_kv_heads = getattr(config, "num_global_key_value_heads", None)
            num_kv_heads = num_kv_heads or config.num_key_value_heads
            head_dim = getattr(config, "global_head_dim", None) or config.head_dim
            return num_kv_heads, head_dim
        return config.num_key_value_heads, config.head_dim

    @classmethod
    def estimate_cache_bytes(
        cls,
        config: PretrainedConfig,
        max_length: int,
        *,
        dtype: torch.dtype,
        block_index: Optional[int] = None,
    ) -> int:
        layer_types = tuple(getattr(config, "layer_types", ("sliding_attention",)))
        if block_index is not None:
            layer_types = (layer_types[block_index],)

        values_per_token = max(
            2 * num_kv_heads * head_dim
            for num_kv_heads, head_dim in (cls._geometry(config, layer_type) for layer_type in layer_types)
        )
        return values_per_token * max_length * get_size_in_bytes(dtype)
