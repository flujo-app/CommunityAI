import os
from typing import Optional, Union

from hivemind.utils.logging import get_logger
from transformers import PretrainedConfig
from transformers.models.qwen3_5 import Qwen3_5TextConfig
from transformers.models.qwen3_5.modeling_qwen3_5 import Qwen3_5Attention

from drift.client.config import ClientConfig
from drift.client.lm_head import LMHeadConfig
from drift.client.ptune import PTuneConfig
from drift.models.qwen3_5.block import WrappedQwen3_5Block
from drift.models.qwen3_5.cache import Qwen3_5HybridCache

logger = get_logger(__name__)

_TEXT_MODEL_TYPE = Qwen3_5TextConfig.model_type
_TEXT_ONLY_BLOCK_PREFIX = "model.layers"
_MULTIMODAL_BLOCK_PREFIX = "model.language_model.layers"
_PEEK_KWARGS = ("cache_dir", "revision", "token", "use_auth_token", "force_download", "local_files_only", "subfolder")


def _peek_top_level_model_type(model_name_or_path: Union[str, os.PathLike, None], **kwargs) -> Optional[str]:
    if model_name_or_path is None:
        return None
    peek_kwargs = {key: kwargs[key] for key in _PEEK_KWARGS if key in kwargs}
    try:
        config_dict, _ = PretrainedConfig.get_config_dict(model_name_or_path, **peek_kwargs)
    except Exception as exc:
        raise RuntimeError(f"Could not inspect Qwen3.5 checkpoint config at {model_name_or_path}") from exc
    return config_dict.get("model_type")


def is_multimodal_wrapper_checkpoint(model_name_or_path: Union[str, os.PathLike, None], **kwargs) -> bool:
    model_type = _peek_top_level_model_type(model_name_or_path, **kwargs)
    return model_type is not None and model_type != _TEXT_MODEL_TYPE


class DistributedQwen3_5Config(Qwen3_5TextConfig, ClientConfig, PTuneConfig, LMHeadConfig):
    block_class = WrappedQwen3_5Block
    attn_class = Qwen3_5Attention
    kv_cache_strategy = Qwen3_5HybridCache
    block_prefix = _TEXT_ONLY_BLOCK_PREFIX
    # Qwen3.5's q_proj emits [query, output_gate] for every query head.
    query_projection_multiplier = 2

    @property
    def num_key_value_groups(self):
        return self.num_attention_heads // self.num_key_value_heads

    @classmethod
    def from_pretrained(
        cls, model_name_or_path: Union[str, os.PathLike, None], *args, dht_prefix: Optional[str] = None, **kwargs
    ):
        loading_from_repo = model_name_or_path is not None and not os.path.isdir(model_name_or_path)
        if loading_from_repo and dht_prefix is None:
            dht_prefix = str(model_name_or_path).split("/")[-1].replace(".", "-")
            logger.info(f"Using DHT prefix: {dht_prefix}")

        multimodal = is_multimodal_wrapper_checkpoint(model_name_or_path, **kwargs)
        result = super().from_pretrained(model_name_or_path, *args, dht_prefix=dht_prefix, **kwargs)
        config = result[0] if isinstance(result, tuple) else result
        config.block_prefix = _MULTIMODAL_BLOCK_PREFIX if multimodal else _TEXT_ONLY_BLOCK_PREFIX
        config.use_cache = True
        return result
