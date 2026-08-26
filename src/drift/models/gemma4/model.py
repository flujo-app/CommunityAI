from typing import Optional

import hivemind
import torch
import torch.nn as nn
from hivemind.utils.logging import get_logger
from transformers.modeling_outputs import BaseModelOutputWithPast
from transformers.models.gemma4.modeling_gemma4 import (
    Gemma4ForCausalLM,
    Gemma4PreTrainedModel,
    Gemma4RMSNorm,
    Gemma4TextModel,
    Gemma4TextModelOutputWithPast,
    Gemma4TextScaledWordEmbedding,
)

from drift.client.from_pretrained import FromPretrainedMixin
from drift.client.lm_head import LMHead
from drift.client.ptune import PTuneMixin
from drift.client.remote_generation import RemoteGenerationMixin, RemotePastKeyValues
from drift.client.remote_sequential import RemoteSequential
from drift.models.gemma4.config import DistributedGemma4Config, is_multimodal_wrapper_checkpoint

logger = get_logger(__name__)

# The client hosts only the text tower's embeddings/norm; these live under ``model.layers.*`` (remote
# blocks) or the multimodal towers, none of which the client instantiates.
_KEYS_TO_IGNORE_ON_LOAD_UNEXPECTED = [
    r"^model\.layers\.",  # transformer blocks -- hosted by the swarm, and their multimodal-wrapper form
    r"^model\.language_model\.layers\.",  # (matched before key_mapping strips the wrapper prefix)
    r"^model\.vision_tower\.",  # multimodal towers we do not serve
    r"^model\.audio_tower\.",
    r"^model\.multi_modal_projector\.",
    r"^multi_modal_projector\.",
]


class _Gemma4WrapperLoadMixin:
    """Loads the text tower out of either a text-only or a multimodal Gemma 4 checkpoint.

    The released multimodal checkpoints (e.g. google/gemma-4-E2B-it) nest the whole text tower under
    ``model.language_model.*`` next to vision/audio towers; strip that container on load so the client
    embeddings line up, and let the ignore patterns above drop the towers we don't host.
    """

    @classmethod
    def from_pretrained(cls, model_name_or_path, *args, **kwargs):
        checkpoint_source = model_name_or_path
        detection_kwargs = kwargs
        artifact_verifier = kwargs.get("artifact_verifier")
        if artifact_verifier is not None:
            checkpoint_source = artifact_verifier.ensure_startup_metadata()
            detection_kwargs = dict(kwargs)
            detection_kwargs["local_files_only"] = True
            detection_kwargs.pop("revision", None)
            detection_kwargs.pop("force_download", None)
        if is_multimodal_wrapper_checkpoint(checkpoint_source, **detection_kwargs):
            key_mapping = kwargs.setdefault("key_mapping", {})
            key_mapping.setdefault(r"^model\.language_model\.", "model.")
        return super().from_pretrained(model_name_or_path, *args, **kwargs)


class DistributedGemma4Model(_Gemma4WrapperLoadMixin, FromPretrainedMixin, PTuneMixin, Gemma4TextModel):
    """Gemma4TextModel, but all transformer layers are hosted by the swarm.

    The client keeps the embeddings (including the Per-Layer Embedding table ``embed_tokens_per_layer``
    and its projection), computes the per-layer inputs, and threads the per-layer slice to each remote
    block. The KV-sharing donor K/V is propagated block-to-block by the swarm itself.
    """

    _keys_to_ignore_on_load_missing = PTuneMixin._keys_to_ignore_on_load_missing
    _keys_to_ignore_on_load_unexpected = _KEYS_TO_IGNORE_ON_LOAD_UNEXPECTED

    config_class = DistributedGemma4Config

    def __init__(self, config: DistributedGemma4Config, *, dht: Optional[hivemind.DHT] = None):
        n_layer, config.num_hidden_layers = config.num_hidden_layers, 0  # Prevent initialization of local layers
        super().__init__(config)
        assert len(self.layers) == 0
        config.num_hidden_layers = n_layer

        # The Per-Layer Embedding modules are sized `num_hidden_layers * hidden_size_per_layer_input`, so
        # the layer-count trick above built them empty; rebuild them at the true size so the checkpoint loads.
        if self.hidden_size_per_layer_input:
            self.embed_tokens_per_layer = Gemma4TextScaledWordEmbedding(
                config.vocab_size_per_layer_input,
                n_layer * config.hidden_size_per_layer_input,
                self.padding_idx,
                embed_scale=config.hidden_size_per_layer_input**0.5,
            )
            self.per_layer_model_projection = nn.Linear(
                config.hidden_size,
                n_layer * config.hidden_size_per_layer_input,
                bias=False,
            )
            self.per_layer_projection_norm = Gemma4RMSNorm(config.hidden_size_per_layer_input, eps=config.rms_norm_eps)

        self.layers = RemoteSequential(config, dht=dht)

        self.requires_grad_(False)  # Forbid accumulate grads for embeddings and layernorm
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
        per_layer_inputs: Optional[torch.Tensor] = None,
        **kwargs,
    ) -> BaseModelOutputWithPast:
        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is not None:
            input_shape = input_ids.size()
            input_ids = input_ids.view(-1, input_shape[-1])
        elif inputs_embeds is not None:
            input_shape = inputs_embeds.size()[:-1]
        else:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        # The causal mask will be added on the server-side
        assert (
            attention_mask is None or (attention_mask == 1).all()
        ), f"Custom attention masks are not supported, {attention_mask=}"
        if cache_position is not None:
            assert position_ids is not None and torch.all(torch.eq(cache_position, position_ids)).item()
        assert (
            position_ids is None or (position_ids[:, 1:] - position_ids[:, :-1] == 1).all()
        ), f"Non-consecutive position_ids are not supported, {position_ids=}"
        assert use_cache is None or use_cache, f"{use_cache=} is not supported"
        assert not output_attentions, f"{output_attentions=} is not supported"
        assert not output_hidden_states, f"{output_hidden_states=} is not supported"
        assert return_dict is None or return_dict, f"{return_dict=} is not supported"

        if inputs_embeds is None:
            inputs_embeds = self.embed_tokens(input_ids)

        # Per-Layer Embeddings: the token-identity + context-aware signal fed into every remote block.
        # Computed here (the client owns the embedding tables) and sliced per block server-side. Shape
        # is [num_layers, batch, seq, per_layer_dim] to mirror the deep-prompt span-slicing convention.
        if self.hidden_size_per_layer_input:
            # `per_layer_inputs` may arrive pre-computed (the token-identity component) from the causal
            # LM wrapper; otherwise derive it from input_ids. Then add the context-aware projection and
            # lay it out as [num_layers, batch, seq, per_layer_dim] to slice per span like deep prompts.
            if per_layer_inputs is None:
                per_layer_inputs = self.get_per_layer_inputs(input_ids, inputs_embeds)
            per_layer_inputs = self.project_per_layer_inputs(inputs_embeds, per_layer_inputs)
            per_layer_inputs = per_layer_inputs.permute(2, 0, 1, 3).contiguous()
        else:
            per_layer_inputs = None

        use_prompts = self.config.tuning_mode and "ptune" in self.config.tuning_mode and self.layers.position == 0
        assert not (
            use_prompts and self.hidden_size_per_layer_input
        ), "ptune is not supported together with Gemma 4 per-layer embeddings"
        if use_prompts:
            batch_size = inputs_embeds.shape[0]
            prompts, intermediate_prompts = self.get_prompt(batch_size)
            inputs_embeds = torch.cat([prompts, inputs_embeds], dim=1)
        else:
            prompts = intermediate_prompts = None

        hidden_states = inputs_embeds
        output_shape = input_shape + (hidden_states.size(-1),)

        hidden_states = self.layers(
            hidden_states,
            prompts=intermediate_prompts,
            per_layer_inputs=per_layer_inputs,
            hypo_ids=past_key_values.hypo_ids if past_key_values is not None else None,
        )

        if past_key_values is None:
            past_key_values = RemotePastKeyValues()
        past_key_values.update_seen(hidden_states.size(1))

        # Remove prefix
        if use_prompts:
            hidden_states = hidden_states[:, self.pre_seq_len :]

        # Add last hidden state
        hidden_states = self.norm(hidden_states)
        hidden_states = hidden_states.view(output_shape)

        # The stock Gemma4ForCausalLM wrapper reads `outputs.shared_kv_states`, so return the
        # Gemma-4 output type. KV sharing is resolved server-side within each span, so the client
        # never propagates donor K/V itself -- `shared_kv_states=None` is correct here.
        return Gemma4TextModelOutputWithPast(
            last_hidden_state=hidden_states,
            past_key_values=past_key_values,
            hidden_states=None,
            attentions=None,
            shared_kv_states=None,
        )

    @property
    def word_embeddings(self) -> nn.Embedding:  # For compatibility with RemoteGenerationMixin
        return self.embed_tokens

    @property
    def word_embeddings_layernorm(self) -> nn.Module:  # For compatibility with RemoteGenerationMixin
        return nn.Identity()

    @property
    def h(self) -> RemoteSequential:  # For compatibility with RemoteGenerationMixin
        return self.layers

    @property
    def ln_f(self) -> nn.Module:  # For compatibility with RemoteGenerationMixin
        return self.norm


class DistributedGemma4ForCausalLM(
    _Gemma4WrapperLoadMixin, FromPretrainedMixin, RemoteGenerationMixin, Gemma4ForCausalLM
):
    _keys_to_ignore_on_load_missing = DistributedGemma4Model._keys_to_ignore_on_load_missing
    _keys_to_ignore_on_load_unexpected = DistributedGemma4Model._keys_to_ignore_on_load_unexpected

    config_class = DistributedGemma4Config

    def __init__(self, config: DistributedGemma4Config):
        Gemma4PreTrainedModel.__init__(self, config)
        self.model = DistributedGemma4Model(config)
        self.vocab_size = config.vocab_size
        self.lm_head = LMHead(config)

        # Initialize weights and apply final processing
        self.post_init()

    def get_output_embeddings(self):
        return self.lm_head

    @property
    def transformer(self) -> DistributedGemma4Model:  # For compatibility with RemoteGenerationMixin
        return self.model


class DistributedGemma4ForSequenceClassification(_Gemma4WrapperLoadMixin, FromPretrainedMixin, Gemma4PreTrainedModel):
    _keys_to_ignore_on_load_missing = DistributedGemma4Model._keys_to_ignore_on_load_missing
    _keys_to_ignore_on_load_unexpected = DistributedGemma4Model._keys_to_ignore_on_load_unexpected

    config_class = DistributedGemma4Config

    def __init__(self, config):
        Gemma4PreTrainedModel.__init__(self, config)
        self.num_labels = config.num_labels

        self.model = DistributedGemma4Model(config)
        self.score = nn.Linear(config.hidden_size, self.num_labels, bias=False)

        # Initialize weights and apply final processing
        self.post_init()

    @property
    def transformer(self) -> DistributedGemma4Model:  # For compatibility with RemoteGenerationMixin
        return self.model
