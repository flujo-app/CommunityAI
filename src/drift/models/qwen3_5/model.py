from typing import Optional

import hivemind
import torch
import torch.nn as nn
from transformers.models.qwen3_5.modeling_qwen3_5 import (
    Qwen3_5ForCausalLM,
    Qwen3_5ModelOutputWithPast,
    Qwen3_5PreTrainedModel,
    Qwen3_5TextModel,
)

from drift.client.from_pretrained import FromPretrainedMixin
from drift.client.lm_head import LMHead
from drift.client.ptune import PTuneMixin
from drift.client.remote_generation import RemoteGenerationMixin, RemotePastKeyValues
from drift.client.remote_sequential import RemoteSequential
from drift.models.qwen3_5.config import DistributedQwen3_5Config, is_multimodal_wrapper_checkpoint

_KEYS_TO_IGNORE_ON_LOAD_UNEXPECTED = [
    r"^model\.layers\.",
    r"^model\.language_model\.layers\.",
    r"^model\.visual\.",
    r"^mtp\.",
]


class _Qwen3_5WrapperLoadMixin:
    @classmethod
    def from_pretrained(cls, model_name_or_path, *args, **kwargs):
        if is_multimodal_wrapper_checkpoint(model_name_or_path, **kwargs):
            key_mapping = kwargs.setdefault("key_mapping", {})
            key_mapping.setdefault(r"^model\.language_model\.", "model.")
        return super().from_pretrained(model_name_or_path, *args, **kwargs)


class DistributedQwen3_5Model(_Qwen3_5WrapperLoadMixin, FromPretrainedMixin, PTuneMixin, Qwen3_5TextModel):
    _keys_to_ignore_on_load_missing = PTuneMixin._keys_to_ignore_on_load_missing
    _keys_to_ignore_on_load_unexpected = _KEYS_TO_IGNORE_ON_LOAD_UNEXPECTED
    config_class = DistributedQwen3_5Config

    def __init__(self, config: DistributedQwen3_5Config, *, dht: Optional[hivemind.DHT] = None):
        num_layers, config.num_hidden_layers = config.num_hidden_layers, 0
        super().__init__(config)
        assert len(self.layers) == 0
        config.num_hidden_layers = num_layers
        self.layers = RemoteSequential(config, dht=dht)
        self.requires_grad_(False)
        self.init_prompts(config)

    def forward(
        self,
        input_ids: Optional[torch.LongTensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[RemotePastKeyValues] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        cache_position: Optional[torch.LongTensor] = None,
        **kwargs,
    ) -> Qwen3_5ModelOutputWithPast:
        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds")
        if input_ids is not None:
            input_shape = input_ids.size()
            input_ids = input_ids.view(-1, input_shape[-1])
        elif inputs_embeds is not None:
            input_shape = inputs_embeds.size()[:-1]
        else:
            raise ValueError("You must specify input_ids or inputs_embeds")

        assert attention_mask is None or (attention_mask == 1).all(), "Custom attention masks are not supported"
        checked_position_ids = position_ids[0] if position_ids is not None and position_ids.ndim == 3 else position_ids
        assert (
            checked_position_ids is None or (checked_position_ids[:, 1:] - checked_position_ids[:, :-1] == 1).all()
        ), "Non-consecutive position_ids are not supported"
        assert use_cache is None or use_cache, f"{use_cache=} is not supported"
        assert not output_attentions, f"{output_attentions=} is not supported"
        assert not output_hidden_states, f"{output_hidden_states=} is not supported"
        assert return_dict is None or return_dict, f"{return_dict=} is not supported"

        if inputs_embeds is None:
            inputs_embeds = self.embed_tokens(input_ids)

        use_prompts = self.config.tuning_mode and "ptune" in self.config.tuning_mode and self.layers.position == 0
        if use_prompts:
            prompts, intermediate_prompts = self.get_prompt(inputs_embeds.shape[0])
            inputs_embeds = torch.cat([prompts, inputs_embeds], dim=1)
        else:
            prompts = intermediate_prompts = None

        hidden_states = self.layers(
            inputs_embeds,
            prompts=intermediate_prompts,
            hypo_ids=past_key_values.hypo_ids if past_key_values is not None else None,
        )
        if past_key_values is None:
            past_key_values = RemotePastKeyValues()
        past_key_values.update_seen(hidden_states.size(1))

        if use_prompts:
            hidden_states = hidden_states[:, self.pre_seq_len :]
        hidden_states = self.norm(hidden_states).view(input_shape + (hidden_states.size(-1),))
        return Qwen3_5ModelOutputWithPast(last_hidden_state=hidden_states, past_key_values=past_key_values)

    @property
    def word_embeddings(self) -> nn.Embedding:
        return self.embed_tokens

    @property
    def word_embeddings_layernorm(self) -> nn.Module:
        return nn.Identity()

    @property
    def h(self) -> RemoteSequential:
        return self.layers

    @property
    def ln_f(self) -> nn.Module:
        return self.norm


class DistributedQwen3_5ForCausalLM(
    _Qwen3_5WrapperLoadMixin, FromPretrainedMixin, RemoteGenerationMixin, Qwen3_5ForCausalLM
):
    _keys_to_ignore_on_load_missing = DistributedQwen3_5Model._keys_to_ignore_on_load_missing
    _keys_to_ignore_on_load_unexpected = DistributedQwen3_5Model._keys_to_ignore_on_load_unexpected
    config_class = DistributedQwen3_5Config

    def __init__(self, config: DistributedQwen3_5Config):
        Qwen3_5PreTrainedModel.__init__(self, config)
        self.model = DistributedQwen3_5Model(config)
        self.vocab_size = config.vocab_size
        self.lm_head = LMHead(config)
        self.post_init()

    def get_output_embeddings(self):
        return self.lm_head

    @property
    def transformer(self) -> DistributedQwen3_5Model:
        return self.model
