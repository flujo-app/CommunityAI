from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Optional, Sequence, Tuple, Union

import torch
from hivemind.moe.expert_uid import ExpertUID
from hivemind.moe.server.module_backend import ModuleBackend
from hivemind.utils import get_logger
from hivemind.utils.tensor_descr import BatchTensorDescriptor, TensorDescriptor
from transformers import PretrainedConfig

from drift.data_structures import InferenceMetadata
from drift.server.memory_cache import MemoryCache
from drift.server.task_pool import PrioritizedTaskPool
from drift.utils.kv_cache import StandardGQACache
from drift.utils.misc import get_num_attention_heads, get_size_in_bytes, is_dummy
from drift.utils.tensor_parallel import TensorParallel

logger = get_logger(__name__)


def _extract_produced_shared_kv(
    shared_kv_states: Optional[Dict[str, Any]], seeded_keys: set
) -> Tuple[torch.Tensor, ...]:
    """Flatten the donor K/V a block newly wrote into ``shared_kv_states`` (keys absent from ``seeded_keys``).

    Returns ``(k0, v0, k1, v1, ...)`` in sorted layer-type order so the caller can pair the tensors back
    with their layer types deterministically without shipping the (string) keys through the tensor pool.
    """
    if not shared_kv_states:
        return ()
    produced = []
    for layer_type in sorted(k for k in shared_kv_states if k not in seeded_keys):
        key, value = shared_kv_states[layer_type]
        # The task pool moves outputs into shared memory, which needs contiguous tensors.
        produced.extend((key.contiguous(), value.contiguous()))
    return tuple(produced)


class TransformerBackend(ModuleBackend):
    """A wrapper for a transformer block that can process requests for forward, backward and inference"""

    _peft_module = None

    def __init__(
        self,
        *args,
        config: PretrainedConfig,
        memory_cache: MemoryCache,
        backend_dtype: torch.dtype,
        max_chunk_size_bytes: int,
        **kwargs,
    ):
        import drift.utils.peft as _peft_module

        self._peft_module = _peft_module

        super().__init__(*args, **kwargs)
        assert isinstance(self.module, TensorParallel)
        self.config = config
        self.memory_cache = memory_cache
        self.max_chunk_size_bytes = max_chunk_size_bytes

        for name, param in self.module.named_parameters():
            assert not param.requires_grad, f"Block parameters must not accumulate gradients, but {name} does"
        for name, buf in self.module.named_buffers():
            assert not buf.requires_grad, f"Block parameters must not accumulate gradients, but {name} does"

        max_batch_size = self.forward_pool.max_batch_size
        device = self.module.devices[self.module.output_device_index]
        self.inference_pool = PrioritizedTaskPool(
            self.inference_step, max_batch_size=max_batch_size, device=device, name=f"{self.name}_inference"
        )  # note: inference_pools may be merged later, see merge_inference_pools_inplace
        self.forward_pool = PrioritizedTaskPool(
            self.forward, max_batch_size=max_batch_size, device=device, name=f"{self.name}_forward"
        )
        self.backward_pool = PrioritizedTaskPool(
            self.backward, max_batch_size=max_batch_size, device=device, name=f"{self.name}_backward"
        )

        self.dtype = backend_dtype
        self.dtype_bytes = get_size_in_bytes(self.dtype)
        self.shard_num_heads = []
        shard_head_dims = []
        shard_kv_groups = []
        donor_layer_types = set()
        for shard in self.module.module_shards:
            found_attention = False
            for submodule in shard.modules():
                if isinstance(submodule, config.attn_class):
                    found_attention = True
                    self.shard_num_heads.append(get_num_attention_heads(submodule, config))
                    shard_head_dims.append(getattr(submodule, "head_dim", None))
                    shard_kv_groups.append(getattr(submodule, "num_key_value_groups", None))
                    # Gemma 4 KV-sharing: a donor layer stores its full-length K/V for the consumer
                    # layers of the same attention type. Record which type(s) this block donates so the
                    # server can ship them to a consumer hosted downstream (see block_functions).
                    if getattr(submodule, "store_full_length_kv", False):
                        donor_layer_types.add(submodule.layer_type)
            if not found_attention:
                # Hybrid architectures may contain blocks whose token mixer is recurrent rather
                # than self-attention. Such a wrapper exposes the logical query-head count solely
                # for backend scheduling/chunk estimates; its cache strategy owns the real state
                # geometry.
                cache_num_heads = getattr(shard, "cache_num_heads", None)
                if cache_num_heads is None:
                    raise TypeError(
                        f"{type(shard).__name__} contains no {config.attn_class} and does not expose cache_num_heads"
                    )
                if len(self.module.devices) > 1:
                    raise NotImplementedError(
                        f"Tensor-parallel recurrent caching is not implemented for {type(shard).__name__}; "
                        "serve this block on one device"
                    )
                self.shard_num_heads.append(cache_num_heads)
                shard_head_dims.append(None)
                shard_kv_groups.append(None)
        assert len(self.shard_num_heads) == len(self.module.devices)
        assert sum(self.shard_num_heads) == config.num_attention_heads

        # All shards of a block share one attention type, so this block donates 0 or 1 layer type.
        self.donor_layer_types = sorted(donor_layer_types)

        # Some architectures use a per-layer-type KV geometry the (single) config can't express:
        # Gemma 4 full-attention layers use `global_head_dim`, and Gemma 4 Unified ones additionally
        # have their own kv-head count (`num_global_key_value_heads`). The attention module carries
        # the values actually in use, so derive the cache geometry from it. All shards of one block
        # share a layer_type, hence one value each; None means "fall back to config".
        block_head_dims = {d for d in shard_head_dims if d is not None}
        assert len(block_head_dims) <= 1, f"Inconsistent head_dim across shards: {shard_head_dims}"
        self.cache_head_dim = block_head_dims.pop() if block_head_dims else None
        block_kv_groups = {g for g in shard_kv_groups if g is not None}
        assert len(block_kv_groups) <= 1, f"Inconsistent num_key_value_groups across shards: {shard_kv_groups}"
        self.cache_kv_groups = block_kv_groups.pop() if block_kv_groups else None

        # The cache layout (descriptor shapes, prefix selection, write-back) is owned by a
        # pluggable strategy so that non-standard layouts (sliding window, MLA) can be added
        # without touching the backend. Models select one via `config.kv_cache_strategy`.
        self.cache_strategy = getattr(config, "kv_cache_strategy", StandardGQACache)(config, module=self.module)
        if self.memory_cache.paged and not self.cache_strategy.supports_paged_cache:
            raise NotImplementedError(
                f"Paged inference caching is not implemented for {type(self.cache_strategy).__name__}; "
                "start the worker with --cache contiguous"
            )

        self.inference_schema = (
            (
                *self.args_schema,
                BatchTensorDescriptor((), dtype=self.dtype),
                BatchTensorDescriptor((), dtype=torch.int64),
            ),
            self.kwargs_schema,
        )

        self.cache_bytes_per_token: Dict[torch.device, int] = Counter()
        for descr in self.get_inference_cache_descriptors(batch_size=1, max_length=1):
            self.cache_bytes_per_token[descr.device] += descr.numel() * get_size_in_bytes(descr.dtype)

        if self.memory_cache.paged:
            self._configure_paged_pool()

    def _configure_paged_pool(self) -> None:
        """Register this block's slice of the shared paged pool (single tensor-parallel shard only)."""
        assert len(self.module.devices) == 1, "paged KV cache does not support tensor parallelism yet"
        key_descr, value_descr = self.get_inference_cache_descriptors(batch_size=1, max_length=1)
        device = key_descr.device
        page_bytes = self.cache_bytes_per_token[device] * self.memory_cache.page_size
        self.memory_cache.configure_paged_pool(
            num_pages=int(self.memory_cache.max_size_bytes // page_bytes),
            num_kv_heads=key_descr.size[1],  # key descr: [batch, kv_heads, k_head_dim, len]
            k_head_dim=key_descr.size[2],
            v_head_dim=value_descr.size[3],  # value descr: [batch, kv_heads, len, v_head_dim]
            dtype=self.dtype,
            device=device,
        )

    def get_inference_cache_descriptors(self, batch_size: int, max_length: int) -> Sequence[TensorDescriptor]:
        """Create tensor descriptors for attention cache tensors used during inference_step"""
        return self.cache_strategy.get_cache_descriptors(
            batch_size,
            max_length,
            dtype=self.dtype,
            devices=self.module.devices,
            shard_num_heads=self.shard_num_heads,
            head_dim=self.cache_head_dim,
            num_key_value_groups=self.cache_kv_groups,
        )

    def forward(self, *inputs: Union[torch.Tensor, str]) -> Tuple[torch.Tensor, ...]:
        *inputs, active_adapter = inputs
        with self._peft_module.using_adapter(active_adapter):
            return super().forward(*inputs)

    def backward(self, *inputs: Union[torch.Tensor, str]) -> Tuple[torch.Tensor, ...]:
        *inputs, active_adapter = inputs
        with self._peft_module.using_adapter(active_adapter):
            return super().backward(*inputs)

    @torch.inference_mode()
    def inference_step(
        self,
        hidden_states: torch.Tensor,
        hypo_ids: torch.LongTensor,
        inference_info: InferenceMetadata,
    ) -> Tuple[torch.Tensor, ...]:
        assert hidden_states.ndim == 3, "expected hidden states to be 3-dimensional: [batch_size, seq_len, hid_size]"

        with self._peft_module.using_adapter(inference_info.active_adapter):
            if self.memory_cache.paged:
                return self._paged_inference_step(hidden_states, hypo_ids, inference_info)

            shared_kv_states = inference_info.shared_kv_states
            seeded_kv_keys = set(shared_kv_states) if shared_kv_states else set()
            # The task pool moves only the positional hidden_states onto the runtime device; the Gemma 4
            # side channels (per_layer_input, shared_kv_states) travel inside InferenceMetadata and arrive
            # on CPU. Move them here so a GPU-hosted block doesn't mix devices. shared_kv_states is moved
            # in place because a merged span shares one dict across its blocks by reference.
            device = hidden_states.device
            per_layer_input = inference_info.per_layer_input
            if per_layer_input is not None and per_layer_input.device != device:
                per_layer_input = per_layer_input.to(device)
            if shared_kv_states:
                for layer_type, (key, value) in shared_kv_states.items():
                    if key.device != device or value.device != device:
                        shared_kv_states[layer_type] = (key.to(device), value.to(device))
            with self.memory_cache.use_cache(*inference_info.cache_handles) as cache_tensors:
                self._reorder_cache_inplace(cache_tensors, hypo_ids)
                max_chunk_length = self._estimate_max_chunk_length(hidden_states, inference_info)
                layer_past = self.cache_strategy.select_layer_past(
                    cache_tensors, inference_info.prefix_length, num_shards=len(self.module.module_shards)
                )
                output_hidden_states, new_kvs = self._forward_chunked(
                    hidden_states,
                    layer_past,
                    max_chunk_length,
                    per_layer_input=per_layer_input,
                    shared_kv_states=shared_kv_states,
                )
                # KV-sharing consumer blocks (Gemma 4) keep no cache of their own, so new_kvs is None.
                if new_kvs is not None:
                    self.cache_strategy.update_cache(cache_tensors, new_kvs, inference_info.prefix_length)
                # A donor block writes its full-length K/V into shared_kv_states inside this (worker)
                # process. Return the newly written K/V so it can cross the process/wire boundary to a
                # consumer hosted downstream -- the mutated dict itself is not visible to the caller.
                return (output_hidden_states, *_extract_produced_shared_kv(shared_kv_states, seeded_kv_keys))

    def _paged_inference_step(
        self, hidden_states: torch.Tensor, hypo_ids: torch.LongTensor, inference_info: InferenceMetadata
    ) -> Tuple[torch.Tensor, ...]:
        (slot_id,) = inference_info.cache_handles
        with self.memory_cache.use_paged_pool() as pool:
            if not is_dummy(hypo_ids):
                pool.reorder(slot_id, hypo_ids)
            max_chunk_length = self._estimate_max_chunk_length(hidden_states, inference_info)
            layer_past = pool.gather(slot_id, inference_info.prefix_length)
            output_hidden_states, new_kvs = self._forward_chunked(hidden_states, layer_past, max_chunk_length)
            pool.scatter(slot_id, new_kvs, inference_info.prefix_length)
            return (output_hidden_states,)

    def _forward_chunked(
        self,
        hidden_states: torch.Tensor,
        layer_past,
        max_chunk_length: int,
        per_layer_input: Optional[torch.Tensor] = None,
        shared_kv_states: Optional[Dict[str, Any]] = None,
    ):
        # We chunk the inputs so that peak memory for long sequences fits into `autograd_memory`
        # reserved in `Server._choose_num_blocks()`. This saves us from OOMs if `max_chunk_size_bytes`
        # is at least 4-6x less than `autograd_memory`.
        seq_len = hidden_states.shape[1]
        output_hidden_states = torch.empty_like(hidden_states) if seq_len > max_chunk_length else None
        for offset in range(0, seq_len, max_chunk_length):
            hidden_states_chunk = hidden_states[:, offset : offset + max_chunk_length, :]
            # Per-layer inputs are seq-aligned to the hidden states, so slice them the same way; the
            # donor K/V dict (shared_kv_states) is not seq-chunked -- consumer blocks read the full donor
            # K/V, and donor blocks write the full-length K/V into it (last chunk wins).
            extra_kwargs = {}
            if per_layer_input is not None:
                extra_kwargs["per_layer_input"] = per_layer_input[:, offset : offset + max_chunk_length, :]
            if shared_kv_states is not None:
                extra_kwargs["shared_kv_states"] = shared_kv_states
            output_hidden_states_chunk, new_kvs = self.module.forward(
                hidden_states_chunk, layer_past=layer_past, use_cache=True, **extra_kwargs
            )
            if seq_len > max_chunk_length:
                output_hidden_states[:, offset : offset + max_chunk_length] = output_hidden_states_chunk
            else:
                output_hidden_states = output_hidden_states_chunk  # saves one memcopy
            layer_past = new_kvs
        return output_hidden_states, new_kvs

    def _estimate_max_chunk_length(self, hidden_states: torch.Tensor, inference_info: InferenceMetadata) -> int:
        # We assume that attention logit matrices are the main thing that consumes memory, given that
        # the model uses multi-query attention
        batch_size, seq_length, hidden_size = hidden_states.shape
        worst_case_length = inference_info.prefix_length + seq_length
        attn_bytes_per_token = max(self.shard_num_heads) * batch_size * self.dtype_bytes * worst_case_length
        return max(1, self.max_chunk_size_bytes // attn_bytes_per_token)

    def _reorder_cache_inplace(self, cache_tensors: torch.Tensor, hypo_ids: torch.Tensor):
        """If hypo_ids is specified, reorder elements of each cache tensor in-place by taking indices from hypo_ids"""
        if not is_dummy(hypo_ids):
            self.cache_strategy.reorder_cache_inplace(cache_tensors, hypo_ids)

    def get_pools(self) -> Sequence[PrioritizedTaskPool]:
        return self.forward_pool, self.backward_pool, self.inference_pool

    def get_info(self) -> Dict[str, Any]:
        """Get module parameters and stats. Used by RemoteExpert to check shapes and for DMoE orchestration."""
        return dict(super().get_info(), inference_schema=self.inference_schema)

    def shutdown(self):
        # Break the cyclic references, otherwise TransformerBackend may be not garbage-collected
        self.forward_pool = self.backward_pool = self.inference_pool = None

        # Explicitly free the GPU memory. This is not necessary at the time this code is written,
        # but may help to avoid future issues when the module is not garbage-collected for some reasons
        dummy = torch.tensor([])
        for p in self.module.parameters():
            p.data = dummy


def merge_inference_pools_inplace(backends: Dict[ExpertUID, TransformerBackend]):
    """Replace each backend's rpc_inference pools with a combined pool runs multiple blocks in one call"""
    assert len(backends) != 0 and all(isinstance(b, TransformerBackend) for b in backends.values())
    first_pool = next(iter(backends.values())).inference_pool
    merged_pool = PrioritizedTaskPool(
        _MergedInferenceStep(backends),
        max_batch_size=first_pool.max_batch_size,
        device=first_pool.device,
        name=f"merged_inference",
    )
    for backend in backends.values():
        assert not backend.inference_pool.is_alive()
        backend.inference_pool = merged_pool


class _MergedInferenceStep:
    def __init__(self, backends: Dict[ExpertUID, TransformerBackend]):
        self.backends = backends

    @torch.inference_mode()
    def __call__(
        self,
        hidden_states: torch.Tensor,
        hypo_ids: torch.LongTensor,
        inference_infos: Sequence[InferenceMetadata],
        *optional_prompts: Optional[torch.Tensor],
    ) -> Tuple[torch.Tensor, ...]:
        assert len(inference_infos) == len(
            optional_prompts
        ), f"found {len(inference_infos)} blocks but {len(optional_prompts)} prompts"
        # All blocks in a merged span share one shared_kv_states dict (by reference), so a donor and a
        # consumer on this server interoperate in-process. Snapshot the seeded keys up front so we can
        # return only the K/V produced *here* for consumers hosted downstream.
        shared_kv_states = inference_infos[0].shared_kv_states if inference_infos else None
        seeded_kv_keys = set(shared_kv_states) if shared_kv_states else set()
        for inference_info, optional_prompt in zip(inference_infos, optional_prompts):
            if optional_prompt is not None:
                hidden_states[:, : optional_prompt.shape[1]] += optional_prompt
            # inference_step also returns each block's produced donor K/V (for the non-merged path);
            # here the shared dict already collects them, so keep only the hidden states.
            hidden_states, *_ = self.backends[inference_info.uid].inference_step(
                hidden_states, hypo_ids, inference_info
            )
        return (hidden_states, *_extract_produced_shared_kv(shared_kv_states, seeded_kv_keys))
