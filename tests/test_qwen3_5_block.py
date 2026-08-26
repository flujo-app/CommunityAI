"""Offline parity and hybrid-cache tests for Qwen3.5."""

import torch

from drift.models.qwen3_5.block import WrappedQwen3_5Block
from drift.models.qwen3_5.cache import Qwen3_5HybridCache
from drift.models.qwen3_5.config import DistributedQwen3_5Config

ATOL = 3e-5


def _tiny_config():
    cfg = DistributedQwen3_5Config(
        vocab_size=128,
        hidden_size=64,
        intermediate_size=128,
        num_hidden_layers=4,
        num_attention_heads=4,
        num_key_value_heads=2,
        head_dim=16,
        linear_num_key_heads=2,
        linear_num_value_heads=2,
        linear_key_head_dim=8,
        linear_value_head_dim=8,
        linear_conv_kernel_dim=4,
        max_position_embeddings=128,
    )
    cfg._attn_implementation = "eager"
    return cfg


def _stock_model_with_captures(cfg, input_ids):
    from transformers.models.qwen3_5 import Qwen3_5TextModel

    torch.manual_seed(0)
    model = Qwen3_5TextModel(cfg).eval()
    layer_inputs, layer_outputs = {}, {}

    def pre_hook(index):
        def hook(_module, args, kwargs):
            layer_inputs[index] = (args[0] if args else kwargs["hidden_states"]).detach().clone()

        return hook

    def output_hook(index):
        def hook(_module, _args, _kwargs, output):
            layer_outputs[index] = (output[0] if isinstance(output, tuple) else output).detach().clone()

        return hook

    for index, layer in enumerate(model.layers):
        layer.register_forward_pre_hook(pre_hook(index), with_kwargs=True)
        layer.register_forward_hook(output_hook(index), with_kwargs=True)
    with torch.inference_mode():
        reference = model(input_ids, use_cache=False).last_hidden_state
    return model, reference, layer_inputs, layer_outputs


def _wrapped_blocks(cfg, stock_model):
    blocks = []
    for index, stock_layer in enumerate(stock_model.layers):
        block = WrappedQwen3_5Block(cfg, layer_idx=index).eval()
        missing, unexpected = block.load_state_dict(stock_layer.state_dict(), strict=False)
        assert not missing and not unexpected
        blocks.append(block)
    return blocks


def test_qwen3_5_block_stack_matches_hf():
    cfg = _tiny_config()
    input_ids = torch.randint(0, cfg.vocab_size, (1, 6))
    model, _, layer_inputs, layer_outputs = _stock_model_with_captures(cfg, input_ids)

    for index, block in enumerate(_wrapped_blocks(cfg, model)):
        with torch.inference_mode():
            (actual,) = block(layer_inputs[index])
        assert torch.allclose(actual, layer_outputs[index], atol=ATOL), (
            index,
            cfg.layer_types[index],
            (actual - layer_outputs[index]).abs().max(),
        )


def test_qwen3_5_prefill_and_decode_cache_matches_hf():
    cfg = _tiny_config()
    input_ids = torch.randint(0, cfg.vocab_size, (1, 7))
    model, reference, _, _ = _stock_model_with_captures(cfg, input_ids)
    blocks = _wrapped_blocks(cfg, model)
    device = torch.device("cpu")
    strategies = [Qwen3_5HybridCache(cfg, module=block) for block in blocks]
    caches = []
    for strategy in strategies:
        descriptors = strategy.get_cache_descriptors(
            1,
            input_ids.shape[1],
            dtype=torch.float32,
            devices=[device],
            shard_num_heads=[cfg.num_attention_heads],
        )
        caches.append([torch.zeros(descriptor.shape, dtype=descriptor.dtype) for descriptor in descriptors])

    outputs = []
    for start, end in ((0, 4), (4, 5), (5, 7)):
        hidden_states = model.embed_tokens(input_ids[:, start:end])
        for block, strategy, cache in zip(blocks, strategies, caches):
            layer_past = strategy.select_layer_past(cache, start, num_shards=1)
            with torch.inference_mode():
                hidden_states, new_states = block(hidden_states, layer_past=layer_past, use_cache=True)
            strategy.update_cache(cache, new_states, start)
        outputs.append(model.norm(hidden_states))

    actual = torch.cat(outputs, dim=1)
    assert torch.allclose(actual, reference, atol=ATOL), (actual - reference).abs().max()


def test_qwen3_5_linear_cache_layout_and_accounting():
    cfg = _tiny_config()
    block = WrappedQwen3_5Block(cfg, layer_idx=0)
    strategy = Qwen3_5HybridCache(cfg, module=block)
    descriptors = strategy.get_cache_descriptors(
        2,
        32,
        dtype=torch.bfloat16,
        devices=[torch.device("cpu")],
        shard_num_heads=[cfg.num_attention_heads],
    )

    assert descriptors[0].shape == (2, 48, 4)
    assert descriptors[0].dtype == torch.bfloat16
    assert descriptors[1].shape == (2, 2, 8, 8)
    assert descriptors[1].dtype == torch.float32
    assert len(descriptors) == 3 and descriptors[2].dtype == torch.uint8
    assert strategy.supports_paged_cache is False


def test_qwen3_5_full_attention_delegates_to_standard_cache():
    cfg = _tiny_config()
    block = WrappedQwen3_5Block(cfg, layer_idx=3)
    strategy = Qwen3_5HybridCache(cfg, module=block)
    keys, values = strategy.get_cache_descriptors(
        1,
        32,
        dtype=torch.float32,
        devices=[torch.device("cpu")],
        shard_num_heads=[cfg.num_attention_heads],
    )
    assert keys.shape == (1, cfg.num_key_value_heads, cfg.head_dim, 32)
    assert values.shape == (1, cfg.num_key_value_heads, 32, cfg.head_dim)
    assert strategy.supports_paged_cache is True
