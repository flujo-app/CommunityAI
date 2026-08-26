import os
from typing import Optional, Union

from hivemind.utils.logging import get_logger
from transformers.models.gemma4_unified import Gemma4UnifiedTextConfig
from transformers.models.gemma4_unified.modeling_gemma4_unified import Gemma4UnifiedTextAttention

from drift.client.config import ClientConfig
from drift.client.lm_head import LMHeadConfig
from drift.client.ptune import PTuneConfig
from drift.models.gemma4.cache import Gemma4Cache
from drift.models.gemma4.config import _peek_top_level_model_type
from drift.models.gemma4_unified.block import WrappedGemma4UnifiedBlock

logger = get_logger(__name__)

# Text-only checkpoints store transformer layers under ``model.layers.*``; the released multimodal
# ``Gemma4UnifiedForConditionalGeneration`` checkpoints (e.g. google/gemma-4-12B-it) nest the whole
# text tower under ``model.language_model.*`` alongside vision/audio towers.
_TEXT_MODEL_TYPE = Gemma4UnifiedTextConfig.model_type  # "gemma4_unified_text"
_TEXT_ONLY_BLOCK_PREFIX = "model.layers"
_MULTIMODAL_BLOCK_PREFIX = "model.language_model.layers"


def is_multimodal_wrapper_checkpoint(model_name_or_path: Union[str, os.PathLike, None], **kwargs) -> bool:
    """True if the checkpoint is a multimodal wrapper (text tower under ``model.language_model.``)."""
    model_type = _peek_top_level_model_type(model_name_or_path, **kwargs)
    return model_type is not None and model_type != _TEXT_MODEL_TYPE


class DistributedGemma4UnifiedConfig(Gemma4UnifiedTextConfig, ClientConfig, PTuneConfig, LMHeadConfig):
    block_class = WrappedGemma4UnifiedBlock
    attn_class = Gemma4UnifiedTextAttention
    block_prefix = _TEXT_ONLY_BLOCK_PREFIX
    kv_cache_strategy = Gemma4Cache

    @property
    def num_key_value_groups(self):
        # The sliding-attention grouping; full-attention layers may use their own kv-head count
        # (num_global_key_value_heads), which the backend derives per block off the attention module.
        return self.num_attention_heads // self.num_key_value_heads

    @classmethod
    def from_pretrained(
        cls, model_name_or_path: Union[str, os.PathLike, None], *args, dht_prefix: Optional[str] = None, **kwargs
    ):
        logger.info("Make sure you follow the Gemma terms of use: https://ai.google.dev/gemma/terms")

        loading_from_repo = model_name_or_path is not None and not os.path.isdir(model_name_or_path)
        if loading_from_repo and dht_prefix is None:
            dht_prefix = str(model_name_or_path).split("/")[-1]  # Use only repo name to merge blocks across accounts
            dht_prefix = dht_prefix.replace(".", "-")
            logger.info(f"Using DHT prefix: {dht_prefix}")

        # Detect the multimodal wrapper before loading: super().from_pretrained() collapses to the text
        # config and loses the outer model_type, but we need it to locate the block weights.
        multimodal = is_multimodal_wrapper_checkpoint(model_name_or_path, **kwargs)

        result = super().from_pretrained(model_name_or_path, *args, dht_prefix=dht_prefix, **kwargs)
        config = result[0] if isinstance(result, tuple) else result
        config.block_prefix = _MULTIMODAL_BLOCK_PREFIX if multimodal else _TEXT_ONLY_BLOCK_PREFIX
        config.use_cache = (
            True  # use_cache=False leads to identical results but is slower and not supported by DRIFT-LLM
        )
        return result
