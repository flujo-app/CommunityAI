from __future__ import annotations

import os
from typing import Any, Union

from transformers import AutoConfig, AutoModelForCausalLM, PreTrainedModel

_WRAPPED_TEXT_WEIGHT_PREFIXES = {
    "gemma4": r"^model\.language_model\.",
    "gemma4_unified": r"^model\.language_model\.",
    "qwen3_5": r"^model\.language_model\.",
}
_CONFIG_LOADING_KWARGS = (
    "cache_dir",
    "force_download",
    "local_files_only",
    "revision",
    "subfolder",
    "token",
    "trust_remote_code",
    "use_auth_token",
)


def load_reference_model_for_causal_lm(model_name_or_path: Union[str, os.PathLike], **kwargs: Any) -> PreTrainedModel:
    """Load the stock text model represented by an exact checkpoint.

    Some supported releases are multimodal wrappers even though DRIFT serves their
    nested text tower. Transformers' causal-LM auto class cannot load those outer
    configs directly, so use the official nested text config and strip the wrapper's
    language-model prefix while reading the same checkpoint weights.

    Unknown wrapper layouts fail closed instead of silently selecting another model.
    """
    config_kwargs = {key: kwargs[key] for key in _CONFIG_LOADING_KWARGS if key in kwargs}
    outer_config = AutoConfig.from_pretrained(model_name_or_path, **config_kwargs)
    text_config = outer_config.get_text_config(decoder=True)

    if text_config is not outer_config:
        weight_prefix = _WRAPPED_TEXT_WEIGHT_PREFIXES.get(outer_config.model_type)
        if weight_prefix is None:
            raise ValueError(
                f"No exact stock text-reference mapping is registered for wrapper model type "
                f"{outer_config.model_type!r}"
            )
        if "config" in kwargs and kwargs["config"] is not text_config:
            raise ValueError("A caller-supplied config cannot override a checkpoint's nested text config")
        key_mapping = dict(kwargs.get("key_mapping", {}))
        key_mapping.setdefault(weight_prefix, "model.")
        kwargs["config"] = text_config
        kwargs["key_mapping"] = key_mapping

    return AutoModelForCausalLM.from_pretrained(model_name_or_path, **kwargs)
