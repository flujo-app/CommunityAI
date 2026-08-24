import os
from typing import Optional, Union

from hivemind.utils.logging import get_logger
from transformers import PretrainedConfig
from transformers.models.gemma4 import Gemma4TextConfig
from transformers.models.gemma4.modeling_gemma4 import Gemma4TextAttention

from drift.client.config import ClientConfig
from drift.client.lm_head import LMHeadConfig
from drift.client.ptune import PTuneConfig
from drift.models.gemma4.block import WrappedGemma4Block
from drift.models.gemma4.cache import Gemma4Cache

logger = get_logger(__name__)

# Text-only Gemma 4 checkpoints store transformer layers under ``model.layers.*``; the released
# multimodal ``Gemma4ForConditionalGeneration`` checkpoints (e.g. google/gemma-4-E2B-it) nest the
# whole text tower under ``model.language_model.*`` alongside a vision/audio tower.
_TEXT_MODEL_TYPE = Gemma4TextConfig.model_type  # "gemma4_text"
_TEXT_ONLY_BLOCK_PREFIX = "model.layers"
_MULTIMODAL_BLOCK_PREFIX = "model.language_model.layers"
# kwargs that PretrainedConfig.get_config_dict() understands, used to peek at the raw config.json.
_PEEK_KWARGS = ("cache_dir", "revision", "token", "use_auth_token", "force_download", "local_files_only", "subfolder")


def _peek_top_level_model_type(model_name_or_path: Union[str, os.PathLike, None], **kwargs) -> Optional[str]:
    """Read the checkpoint's top-level ``model_type`` without building a full config.

    Used to tell a multimodal wrapper (``gemma4``) apart from a text-only checkpoint (``gemma4_text``);
    ``super().from_pretrained`` transparently extracts the nested ``text_config`` for both, but only the
    wrapper stores its weights under the ``model.language_model.`` container prefix.
    """
    if model_name_or_path is None:
        return None
    peek_kwargs = {k: kwargs[k] for k in _PEEK_KWARGS if k in kwargs}
    try:
        config_dict, _ = PretrainedConfig.get_config_dict(model_name_or_path, **peek_kwargs)
    except Exception as e:  # missing config / network / auth -- fall back to the text-only default
        logger.debug(f"Could not peek at {model_name_or_path} config.json ({e!r}); assuming a text-only checkpoint")
        return None
    return config_dict.get("model_type")


def is_multimodal_wrapper_checkpoint(model_name_or_path: Union[str, os.PathLike, None], **kwargs) -> bool:
    """True if the checkpoint is a multimodal Gemma 4 wrapper (text tower under ``model.language_model.``)."""
    model_type = _peek_top_level_model_type(model_name_or_path, **kwargs)
    return model_type is not None and model_type != _TEXT_MODEL_TYPE


class DistributedGemma4Config(Gemma4TextConfig, ClientConfig, PTuneConfig, LMHeadConfig):
    block_class = WrappedGemma4Block
    attn_class = Gemma4TextAttention
    block_prefix = _TEXT_ONLY_BLOCK_PREFIX
    kv_cache_strategy = Gemma4Cache

    @property
    def num_key_value_groups(self):
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
