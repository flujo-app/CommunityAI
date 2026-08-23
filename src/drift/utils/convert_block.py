"""
Tools for converting transformer blocks, applying quantization and/or tensor parallelism
"""
from enum import Enum
from typing import Optional, Sequence

import torch
import torch.nn as nn
from hivemind.utils.logging import get_logger, use_hivemind_log_handler
from transformers import PretrainedConfig

from drift.utils.misc import get_num_attention_heads
from drift.utils.tensor_parallel import TensorParallel
from drift.utils.tensor_parallel.configs import get_tensor_parallel_config

use_hivemind_log_handler("in_root_logger")
logger = get_logger(__name__)


class QuantType(Enum):
    NONE = 0
    INT8 = 1  # 8-bit as in the LLM.int8() paper
    NF4 = 2  # 4-bit as in the QLoRA paper


def convert_block(
    block: nn.Module,
    block_index: int,
    config: PretrainedConfig,
    tensor_parallel_devices: Sequence[torch.device],
    output_device: torch.device,
    quant_type: QuantType,
    freeze: bool = True,
    adapters: Optional[Sequence[str]] = None,
    **kwargs,
) -> TensorParallel:
    """
    Optimize a transformer block for use in a DRIFT-LLM server, apply tensor parallelism and/or LLM.8bit quantization

    :note: some optimizations will modify the input block in-place!
    :param block: a single transformer block, either pre-trained or newly initialized
    :param config: HF transformers config for the full model
    :param tensor_parallel_devices: if specified, use tensor parallelism to split the model between these devices
    :note: if there is only a single device, model wil still be wrapped with TensorParallel (for uniformity)
    :param output_device: if tensor_parallel_devices is True, output
    :param quant_type: quantization type
    :param freeze: if True (default), make all module parameters non-trainable
    :return: a module that acts like the original block, but runs with all specified optimizations

    """
    if freeze:
        block.requires_grad_(False)

    block = make_tensor_parallel(block, config, tensor_parallel_devices, output_device=output_device)

    if quant_type != QuantType.NONE:
        block = quantize_module(block, quant_type=quant_type)

    for shard, device in zip(block.module_shards, block.devices):
        shard.to(device)

    if adapters:
        from drift.utils.peft import add_adapter_to_block, create_lora_adapter, load_peft

        create_lora_adapter(block)
        for adapter_name in adapters:
            adapter_config, adapter_state_dict = load_peft(
                adapter_name,
                block_idx=block_index,
                **kwargs,
            )
            add_adapter_to_block(block, block_index, adapter_name, adapter_config, adapter_state_dict)

    return block


def quantize_module(model: nn.Module, *, quant_type: QuantType) -> nn.Module:
    # Import bitsandbytes only when necessary, so DRIFT-LLM runs on platforms not supported by bitsandbytes
    try:
        import bitsandbytes as bnb
    except ImportError as e:
        raise ImportError(
            f"bitsandbytes is required for {quant_type.name} quantization but could not be imported. "
            f"Quantization is only supported on CUDA GPUs; on other devices (Intel XPU, Apple MPS, CPU) "
            f"run the server with --quant_type none."
        ) from e

    for n, module in model.named_children():
        if len(list(module.children())) > 0:
            quantize_module(module, quant_type=quant_type)

        if isinstance(module, torch.nn.Linear) and n not in ["lm_head", "score"]:
            assert module.weight.device.type == "cpu", f"expected linear layers on CPU, got {module.weight.device}"
            if quant_type == QuantType.INT8:
                model._modules[n] = bnb.nn.Linear8bitLt(
                    module.in_features,
                    module.out_features,
                    module.bias is not None,
                    has_fp16_weights=False,
                    threshold=6.0,  # Default from the LLM.int8() paper
                )
                model._modules[n].weight = bnb.nn.Int8Params(
                    module.weight.data, requires_grad=False, has_fp16_weights=False
                ).to(module.weight.dtype)
            elif quant_type == QuantType.NF4:
                compress_statistics = True
                model._modules[n] = bnb.nn.LinearNF4(
                    module.in_features,
                    module.out_features,
                    module.bias is not None,
                    compress_statistics=compress_statistics,
                )
                model._modules[n].weight = bnb.nn.Params4bit(
                    module.weight.data,
                    requires_grad=False,
                    quant_type="nf4",
                    blocksize=64,
                    compress_statistics=compress_statistics,
                ).to(module.weight.dtype)
            else:
                raise ValueError(f"Unsupported quant_type='{quant_type}'")
            model._modules[n].bias = module.bias
    return model


def make_tensor_parallel(
    block: nn.Module, model_config: PretrainedConfig, devices: Sequence[torch.device], output_device: torch.device
) -> nn.Module:
    tp_config = get_tensor_parallel_config(model_config, devices)
    if tp_config is None and len(devices) > 1:
        logger.warning(
            f"No head-parallel tensor-parallel config for model_type={model_config.model_type!r}; "
            f"falling back to the generic auto config (correct but communication-heavy)"
        )
    tp_block = TensorParallel(block, devices, config=tp_config, output_device=output_device, delay_init=True)
    total_heads = 0
    for tp_shard in tp_block.module_shards:
        shard_heads = 0
        for submodule in tp_shard.modules():
            if isinstance(submodule, model_config.attn_class):
                shard_heads += get_num_attention_heads(submodule, model_config)
        if shard_heads == 0:
            cache_num_heads = getattr(tp_shard, "cache_num_heads", None)
            if cache_num_heads is None:
                raise TypeError(
                    f"{type(tp_shard).__name__} contains no {model_config.attn_class} and does not expose "
                    "cache_num_heads"
                )
            if len(devices) > 1:
                raise NotImplementedError(
                    f"Tensor-parallel recurrent caching is not implemented for {type(tp_shard).__name__}; "
                    "serve this block on one device"
                )
            shard_heads = cache_num_heads
        total_heads += shard_heads
    assert total_heads == model_config.num_attention_heads, (
        f"Tensor-parallel head split is inconsistent: counted {total_heads} query heads across "
        f"{len(tp_block.module_shards)} shard(s), expected {model_config.num_attention_heads}"
    )
    return tp_block


def check_device_balance(devices: Sequence[torch.device]):
    if not all(device.type == "cuda" for device in devices):
        logger.warning("Running tensor parallelism on non-GPU devices; proceed at your own risk")
        return
    unique_device_capabilities = set(map(torch.cuda.get_device_capability, devices))
    if len(unique_device_capabilities) > 1:
        logger.warning(
            f"Found GPUs with uneven capabilities: {unique_device_capabilities}. "
            f"Using GPUs with different performance will cause the server to wait for the slowest GPU."
        )

    memory_per_device = tuple(torch.cuda.get_device_properties(device).total_memory for device in devices)
    used_memory = min(memory_per_device) * len(memory_per_device)
    wasted_memory_rate = (sum(memory_per_device) - used_memory) / sum(memory_per_device)
    if wasted_memory_rate > 0.05:
        logger.warning(
            f"GPU devices have highly uneven memory, {wasted_memory_rate * 100:.2f}% memory is wasted. "
            f"Consider running high-memory GPUs in a separate server."
        )
