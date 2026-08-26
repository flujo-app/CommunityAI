"""Qwen3.5 hybrid decoder block."""

from __future__ import annotations

from typing import Optional, Tuple

import torch
from transformers.cache_utils import DynamicCache
from transformers.masking_utils import create_causal_mask, create_recurrent_attention_mask
from transformers.models.qwen3_5.modeling_qwen3_5 import Qwen3_5DecoderLayer, Qwen3_5TextRotaryEmbedding

from drift.models._gqa_block import BloomLayoutCacheMixin
from drift.utils.misc import default_attn_implementation, mps_gqa_eager_attention


class WrappedQwen3_5Block(BloomLayoutCacheMixin, Qwen3_5DecoderLayer):
    """A DRIFT wrapper for one Qwen3.5 linear- or full-attention decoder layer."""

    def __init__(self, config, layer_idx: int = 0):
        super().__init__(config, layer_idx=layer_idx)
        self.config = config
        self.global_layer_idx = layer_idx
        self.cache_num_heads = config.num_attention_heads
        if getattr(config, "_attn_implementation", None) is None:
            config._attn_implementation = default_attn_implementation(config)
        self.rotary_emb = Qwen3_5TextRotaryEmbedding(config=config)

    def forward(
        self,
        hidden_states: torch.Tensor,
        *args,
        attention_mask: Optional[torch.Tensor] = None,
        layer_past: Optional[Tuple[torch.Tensor, ...]] = None,
        use_cache: bool = False,
        **kwargs,
    ) -> Tuple[torch.FloatTensor, ...]:
        batch_size, seq_length, _ = hidden_states.shape
        past_key_values = DynamicCache(config=self.config) if use_cache or layer_past is not None else None

        past_length = 0
        if layer_past is not None:
            cache_layer = past_key_values.layers[self.global_layer_idx]
            if self.block_type == "full_attention":
                past_key, past_value = self._reorder_cache_from_bloom(layer_past, batch_size)
                past_length = past_key.shape[2]
                past_key_values.update(past_key, past_value, self.global_layer_idx)
            else:
                if len(layer_past) != 2:
                    raise ValueError(f"Qwen3.5 linear attention expected 2 cached states, got {len(layer_past)}")
                cache_layer.update_conv_state(layer_past[0])
                cache_layer.update_recurrent_state(layer_past[1])

        cache_position = torch.arange(past_length, past_length + seq_length, device=hidden_states.device)
        text_position_ids = cache_position.unsqueeze(0).expand(batch_size, -1)
        position_embeddings = self.rotary_emb(hidden_states, text_position_ids)

        if self.block_type == "full_attention":
            causal_mask = create_causal_mask(
                self.config,
                hidden_states,
                attention_mask,
                past_key_values,
                position_ids=text_position_ids,
            )
        else:
            causal_mask = create_recurrent_attention_mask(
                self.config,
                hidden_states,
                attention_mask,
                past_key_values,
            )

        with mps_gqa_eager_attention(self.config, hidden_states.device):
            output = super().forward(
                hidden_states,
                position_embeddings=position_embeddings,
                attention_mask=causal_mask,
                position_ids=text_position_ids,
                past_key_values=past_key_values,
                use_cache=use_cache,
                cache_position=cache_position,
                **kwargs,
            )
        hidden_states = output[0] if isinstance(output, tuple) else output

        if not use_cache:
            return (hidden_states,)
        cache_layer = past_key_values.layers[self.global_layer_idx]
        if self.block_type == "full_attention":
            return hidden_states, self._reorder_cache_to_bloom((cache_layer.keys, cache_layer.values), batch_size)
        return hidden_states, (cache_layer.conv_states, cache_layer.recurrent_states)
