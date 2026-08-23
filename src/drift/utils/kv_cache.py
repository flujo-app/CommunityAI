"""
Pluggable KV-cache strategies.

``TransformerBackend`` owns the server-side attention cache but not its *layout*: how many
tensors a block needs, their shapes, how a prefix is sliced out to feed the next step, and how
freshly computed keys/values are written back. Those details differ between model families
(standard dense GQA, sliding-window, DeepSeek-style MLA latents), so they live behind a
``KVCacheStrategy`` that the backend consumes without knowing the layout.

A model package selects its strategy by setting ``kv_cache_strategy`` on its distributed config
(alongside ``block_class`` / ``attn_class``); the backend defaults to :class:`StandardGQACache`,
which is the historical BLOOM-layout cache shared by every dense model DRIFT-LLM serves today.

Sliding-window models (Mistral, Gemma) also use :class:`StandardGQACache`: correctness past the
window boundary comes from the windowed *attention mask* applied in the block, not from bounding
the cache, so a full-length cache stays exact. Physically bounding the allocation to the window
(a ring buffer) is a memory optimization that conflicts with DRIFT-LLM' absolute ``prefix_length``
addressing and is deferred to the paged-cache engine in Phase 5.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from itertools import chain
from typing import Any, Optional, Sequence

import torch
from hivemind.utils.tensor_descr import TensorDescriptor
from transformers import PretrainedConfig

from drift.utils.tensor_parallel import PerDeviceTensors


class KVCacheStrategy(ABC):
    """Encapsulates a model family's on-device KV cache layout.

    Instances are created once per backend from the block config and hold no per-request state,
    so a single strategy object serves every inference session on that backend.
    """

    supports_paged_cache = True

    def __init__(self, config: PretrainedConfig, *, module: Optional[Any] = None):
        self.config = config
        self.module = module

    @abstractmethod
    def get_cache_descriptors(
        self,
        batch_size: int,
        max_length: int,
        *,
        dtype: torch.dtype,
        devices: Sequence[torch.device],
        shard_num_heads: Sequence[int],
        head_dim: Optional[int] = None,
        num_key_value_groups: Optional[int] = None,
    ) -> Sequence[TensorDescriptor]:
        """Describe the cache tensors to allocate for one inference session.

        ``devices`` / ``shard_num_heads`` describe the tensor-parallel shards (one entry each);
        for a single-device block both have length one. ``head_dim`` and ``num_key_value_groups``,
        when given, override the config-derived values -- Gemma 4's full-attention layers use a
        wider ``global_head_dim`` than sliding layers, and Gemma 4 Unified additionally gives them
        their own key/value head count (``num_global_key_value_heads``), so neither the block's
        head_dim nor its kv-head count can be read off the (single) config.
        """

    @abstractmethod
    def select_layer_past(
        self, cache_tensors: Sequence[torch.Tensor], prefix_length: int, *, num_shards: int
    ) -> Sequence[torch.Tensor]:
        """Slice the first ``prefix_length`` tokens out of the cache to feed the block as ``layer_past``."""

    @abstractmethod
    def update_cache(
        self, cache_tensors: Sequence[torch.Tensor], new_kvs: Sequence[torch.Tensor], prefix_length: int
    ) -> None:
        """Write the block's freshly computed keys/values back into the cache in place."""

    def reorder_cache_inplace(self, cache_tensors: Sequence[torch.Tensor], hypo_ids: torch.Tensor) -> None:
        """Reorder the batch dimension after beam/hypothesis selection.

        Standard K/V caches reorder every allocated tensor. Architectures may override this when
        their allocation contains accounting-only reservation tensors that do not participate in
        inference.
        """

        for cache_tensor in cache_tensors:
            cache_tensor[...] = cache_tensor[hypo_ids.to(cache_tensor.device)]


class StandardGQACache(KVCacheStrategy):
    """Default BLOOM-layout cache shared by every dense (MHA/GQA) model.

    Per tensor-parallel shard it stores two tensors::

        key:   [batch, num_kv_heads, head_dim, max_length]
        value: [batch, num_kv_heads, max_length, head_dim]

    where ``num_kv_heads`` is the shard's slice of the key/value heads. Multi-query and
    grouped-query attention fall out naturally: a shard holding ``q`` query heads holds
    ``q // num_key_value_groups`` key/value heads, so the same formula covers MHA
    (``groups == 1``), GQA and MQA, on one device or split across several.
    """

    def get_cache_descriptors(
        self,
        batch_size: int,
        max_length: int,
        *,
        dtype: torch.dtype,
        devices: Sequence[torch.device],
        shard_num_heads: Sequence[int],
        head_dim: Optional[int] = None,
        num_key_value_groups: Optional[int] = None,
    ) -> Sequence[TensorDescriptor]:
        # Prefer the block's actual head_dim (Gemma 4 full-attention layers use `global_head_dim`,
        # wider than sliding layers); then an explicit config head_dim (several archs set one that
        # differs from hidden_size // num_attention_heads); then the standard derivation. Same for
        # the grouping ratio: Gemma 4 Unified full-attention layers have their own kv-head count.
        if head_dim is None:
            head_dim = (
                getattr(self.config, "head_dim", None) or self.config.hidden_size // self.config.num_attention_heads
            )
        if num_key_value_groups is None:
            num_key_value_groups = self.config.num_key_value_groups
        cache_tensors = []
        for device, num_heads in zip(devices, shard_num_heads):
            num_kv_heads = num_heads // num_key_value_groups
            keys = TensorDescriptor((batch_size, num_kv_heads, head_dim, max_length), dtype=dtype, device=device)
            values = TensorDescriptor((batch_size, num_kv_heads, max_length, head_dim), dtype=dtype, device=device)
            cache_tensors.extend((keys, values))
        return cache_tensors

    def select_layer_past(
        self, cache_tensors: Sequence[torch.Tensor], prefix_length: int, *, num_shards: int
    ) -> Sequence[torch.Tensor]:
        key_cache, value_cache = list(cache_tensors[0::2]), list(cache_tensors[1::2])
        for i in range(len(key_cache)):
            key_cache[i] = key_cache[i].flatten(0, 1)[:, :, :prefix_length]
            # shape: [batch * num_kv_heads, head_dim, kv_length]
            value_cache[i] = value_cache[i].flatten(0, 1)[:, :prefix_length]
            # shape: [batch * num_kv_heads, kv_length, head_dim]
        layer_past = tuple(chain(*zip(key_cache, value_cache)))
        return PerDeviceTensors(*layer_past) if num_shards > 1 else layer_past

    def update_cache(
        self, cache_tensors: Sequence[torch.Tensor], new_kvs: Sequence[torch.Tensor], prefix_length: int
    ) -> None:
        new_length = new_kvs[0].shape[-1]  # key (BLOOM): [b*kv, k_head_dim, new_length]
        for cache_key, new_key in zip(cache_tensors[0::2], new_kvs[0::2]):
            new_key = new_key.view(*cache_key.shape[:3], new_length)
            cache_key[:, :, :, prefix_length:new_length] = new_key[:, :, :, prefix_length:new_length]
        for cache_value, new_value in zip(cache_tensors[1::2], new_kvs[1::2]):
            # value head dim comes from the value cache tensor itself (may differ from the key's for MLA)
            new_value = new_value.view(*cache_value.shape[:2], new_length, cache_value.shape[3])
            cache_value[:, :, prefix_length:new_length, :] = new_value[:, :, prefix_length:new_length, :]


class MLACache(StandardGQACache):
    """KV cache for DeepSeek-style Multi-head Latent Attention (MLA).

    HF's DeepSeek attention *decompresses* the KV latent (``kv_b_proj``) before writing to the
    cache, so DRIFT-LLM keeps the per-head BLOOM layout rather than caching the latent. MLA only
    breaks one assumption of :class:`StandardGQACache`: keys and values have different head dims
    (key ``= qk_nope_head_dim + qk_rope_head_dim``, value ``= v_head_dim``), so only the descriptor
    sizing changes here -- ``select_layer_past`` / ``update_cache`` already handle asymmetric dims.

    Caching the compressed latent instead (MLA's memory win) would need a custom attention kernel
    and belongs with the paged-cache engine (Phase 5); this keeps DeepSeek correct and swarm-ready.
    """

    def get_cache_descriptors(
        self,
        batch_size: int,
        max_length: int,
        *,
        dtype: torch.dtype,
        devices: Sequence[torch.device],
        shard_num_heads: Sequence[int],
        head_dim: Optional[int] = None,  # unused: MLA key/value head dims are asymmetric (see below)
        num_key_value_groups: Optional[int] = None,  # unused: MLA reads the grouping off its config
    ) -> Sequence[TensorDescriptor]:
        key_head_dim = self.config.qk_nope_head_dim + self.config.qk_rope_head_dim
        value_head_dim = self.config.v_head_dim
        cache_tensors = []
        for device, num_heads in zip(devices, shard_num_heads):
            num_kv_heads = num_heads // self.config.num_key_value_groups
            keys = TensorDescriptor((batch_size, num_kv_heads, key_head_dim, max_length), dtype=dtype, device=device)
            values = TensorDescriptor(
                (batch_size, num_kv_heads, max_length, value_head_dim), dtype=dtype, device=device
            )
            cache_tensors.extend((keys, values))
        return cache_tensors
