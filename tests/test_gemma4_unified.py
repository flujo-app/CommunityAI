"""
Offline tests for Gemma 4 Unified (the dense mid-size branch, e.g. google/gemma-4-12B-it).

The traits that differ from the plain GQA models and from Gemma 4 (E2B):
  * ``attention_k_eq_v``: full-attention layers have no ``v_proj`` and their own kv-head count
    (``num_global_key_value_heads``) on top of the wider ``global_head_dim`` -- so the KV cache
    geometry varies per block in BOTH head count and head dim.
  * a trained ``layer_scalar`` persistent buffer on every layer (randomized here so loader bugs
    that drop buffers cannot hide behind init values).
  * KV sharing exists but is config-gated (off in the released 12B); exercised here explicitly.

Everything is synthesized on disk / in memory and runs single-process -- no download or swarm.
"""
import os

import pytest
import torch
from safetensors.torch import save_file

from drift.models.gemma4_unified.block import WrappedGemma4UnifiedBlock
from drift.models.gemma4_unified.config import DistributedGemma4UnifiedConfig
from drift.server.from_pretrained import load_pretrained_block
from drift.utils.auto_config import AutoDistributedConfig
from drift.utils.kv_cache import StandardGQACache

ATOL = 3e-5


def _tiny_config(num_kv_shared_layers=0):
    from transformers.models.gemma4_unified import Gemma4UnifiedTextConfig

    # Mirrors gemma-4-12B-it's structure at toy size: k_eq_v full layers with their own (MQA)
    # kv-head count and wider head_dim; sliding layers plain GQA.
    return Gemma4UnifiedTextConfig(
        vocab_size=256,
        hidden_size=64,
        intermediate_size=128,
        num_hidden_layers=6,
        num_attention_heads=4,
        num_key_value_heads=2,
        num_global_key_value_heads=1,
        head_dim=16,
        global_head_dim=32,
        attention_k_eq_v=True,
        layer_types=[
            "sliding_attention",
            "sliding_attention",
            "full_attention",
            "sliding_attention",
            "sliding_attention",
            "full_attention",
        ],
        num_kv_shared_layers=num_kv_shared_layers,
        sliding_window=512,
        final_logit_softcapping=30.0,
        tie_word_embeddings=True,
    )


def _stock_model_and_refs(cfg, input_ids):
    """Build a stock model (with randomized layer_scalar) and capture each layer's exact input/output."""
    from transformers.models.gemma4_unified import Gemma4UnifiedTextModel

    torch.manual_seed(0)
    model = Gemma4UnifiedTextModel(cfg).eval()
    with torch.no_grad():
        for layer in model.layers:
            layer.layer_scalar.copy_(0.5 + torch.rand(1))

    layer_in, layer_out = {}, {}

    def pre_hook(idx):
        def hook(_module, args, kwargs):
            layer_in[idx] = (args[0] if args else kwargs["hidden_states"]).detach().clone()

        return hook

    def out_hook(idx):
        def hook(_module, _args, _kwargs, output):
            layer_out[idx] = (output[0] if isinstance(output, tuple) else output).detach().clone()

        return hook

    for i, layer in enumerate(model.layers):
        layer.register_forward_pre_hook(pre_hook(i), with_kwargs=True)
        layer.register_forward_hook(out_hook(i), with_kwargs=True)

    with torch.inference_mode():
        model(input_ids, use_cache=False)

    return model, layer_in, layer_out


def _build_block(cfg, model, layer_idx):
    block = WrappedGemma4UnifiedBlock(cfg, layer_idx=layer_idx).eval()
    missing, unexpected = block.load_state_dict(model.layers[layer_idx].state_dict(), strict=False)
    assert not unexpected, f"layer {layer_idx} unexpected keys: {unexpected}"
    assert all("rotary" in name for name in missing), f"layer {layer_idx} unexpected missing keys: {missing}"
    return block


def test_unified_block_stack_matches_hf():
    cfg = _tiny_config()
    input_ids = torch.randint(0, cfg.vocab_size, (1, 6))
    model, layer_in, layer_out = _stock_model_and_refs(cfg, input_ids)

    for i in range(cfg.num_hidden_layers):
        block = _build_block(cfg, model, i)
        # Full-attention layers must actually exercise k_eq_v (no v_proj module at all).
        if cfg.layer_types[i] == "full_attention":
            assert block.self_attn.v_proj is None
        with torch.inference_mode():
            (out,) = block(layer_in[i])
        assert torch.allclose(
            out, layer_out[i], atol=ATOL
        ), f"layer {i} ({cfg.layer_types[i]}): {(out - layer_out[i]).abs().max().item()}"


def test_unified_block_stack_with_kv_sharing():
    """Donors at layers 2 (full) / 3 (sliding), consumers at 4 (sliding) / 5 (full)."""
    cfg = _tiny_config(num_kv_shared_layers=2)
    input_ids = torch.randint(0, cfg.vocab_size, (1, 6))
    model, layer_in, layer_out = _stock_model_and_refs(cfg, input_ids)

    shared_kv_states = {}
    for i in range(cfg.num_hidden_layers):
        block = _build_block(cfg, model, i)
        with torch.inference_mode():
            (out,) = block(layer_in[i], shared_kv_states=shared_kv_states)
        assert torch.allclose(out, layer_out[i], atol=ATOL), (
            f"layer {i} ({cfg.layer_types[i]}, shared={block.is_kv_shared_layer}): "
            f"{(out - layer_out[i]).abs().max().item()}"
        )

    assert set(shared_kv_states) == {"sliding_attention", "full_attention"}


@pytest.mark.parametrize("layer_idx", [0, 2])  # sliding GQA (2x16) and full k_eq_v MQA (1x32)
def test_unified_block_cache_roundtrip(layer_idx):
    cfg = _tiny_config()
    input_ids = torch.randint(0, cfg.vocab_size, (1, 5))
    model, layer_in, layer_out = _stock_model_and_refs(cfg, input_ids)

    block = _build_block(cfg, model, layer_idx)
    x = layer_in[layer_idx]
    with torch.inference_mode():
        prefill_out, _ = block(x, use_cache=True)

        past, steps = None, []
        for i in range(input_ids.shape[1]):
            step_out, past = block(x[:, i : i + 1], layer_past=past, use_cache=True)
            steps.append(step_out)
        stepwise_out = torch.cat(steps, dim=1)

    assert torch.allclose(prefill_out, layer_out[layer_idx], atol=ATOL), (
        (prefill_out - layer_out[layer_idx]).abs().max()
    )
    assert torch.allclose(stepwise_out, layer_out[layer_idx], atol=ATOL), (
        (stepwise_out - layer_out[layer_idx]).abs().max()
    )


def test_cache_descriptors_honor_per_block_kv_groups():
    """The backend passes the block's own num_key_value_groups; the strategy must prefer it."""
    from drift.models.gemma4_unified.config import DistributedGemma4UnifiedConfig

    cfg = _tiny_config()
    # The server builds the strategy from the Distributed config (which carries the
    # num_key_value_groups property the strategy falls back on).
    strategy = StandardGQACache(DistributedGemma4UnifiedConfig(**cfg.to_dict()))
    common = dict(dtype=torch.float32, devices=[torch.device("cpu")], shard_num_heads=[4])

    # Sliding-layer geometry from the config fallback: 4 heads // 2 groups -> 2 kv heads of 16.
    keys, values = strategy.get_cache_descriptors(1, 8, **common)
    assert tuple(keys.shape) == (1, 2, 16, 8) and tuple(values.shape) == (1, 2, 8, 16)

    # Full-layer geometry via the overrides: 4 // 4 -> 1 kv head of 32 (num_global_key_value_heads).
    keys, values = strategy.get_cache_descriptors(1, 8, head_dim=32, num_key_value_groups=4, **common)
    assert tuple(keys.shape) == (1, 1, 32, 8) and tuple(values.shape) == (1, 1, 8, 32)

    # The attention modules carry exactly the values the backend derives them from.
    model = _stock_model_and_refs(cfg, torch.randint(0, cfg.vocab_size, (1, 2)))[0]
    assert model.layers[0].self_attn.num_key_value_groups == 2 and model.layers[0].self_attn.head_dim == 16
    assert model.layers[2].self_attn.num_key_value_groups == 4 and model.layers[2].self_attn.head_dim == 32


def test_cache_estimate_honors_per_block_kv_geometry():
    cfg = DistributedGemma4UnifiedConfig(**_tiny_config().to_dict())
    strategy = cfg.kv_cache_strategy
    max_length = 8

    # Sliding: K + V, two KV heads of 16 elements each, float32.
    assert strategy.estimate_cache_bytes(cfg, max_length, dtype=torch.float32, block_index=0) == 2048
    # Full: K + V, one global KV head of 32 elements, also float32.
    assert strategy.estimate_cache_bytes(cfg, max_length, dtype=torch.float32, block_index=2) == 2048
    assert strategy.estimate_cache_bytes(cfg, max_length, dtype=torch.float32) == 2048


@pytest.fixture(scope="module")
def wrapper_checkpoint(tmp_path_factory):
    """A tiny multimodal wrapper checkpoint (gemma-4-12B-it layout); returns (path, stock text model)."""
    from transformers.models.gemma4_unified import Gemma4UnifiedConfig, Gemma4UnifiedTextModel

    text_cfg = _tiny_config()
    torch.manual_seed(0)
    text_model = Gemma4UnifiedTextModel(text_cfg).eval()
    with torch.no_grad():
        for layer in text_model.layers:
            layer.layer_scalar.copy_(0.5 + torch.rand(1))

    path = tmp_path_factory.mktemp("gemma4_unified_wrapper")
    state_dict = {f"model.language_model.{k}": v for k, v in text_model.state_dict().items()}
    state_dict["model.vision_embedder.pos_embedding"] = torch.randn(4, 2, 8)  # must be ignored on load
    save_file(state_dict, os.path.join(path, "model.safetensors"), metadata={"format": "pt"})

    wrapper_cfg = Gemma4UnifiedConfig(text_config=text_cfg.to_dict())
    wrapper_cfg.architectures = ["Gemma4UnifiedForConditionalGeneration"]
    wrapper_cfg.save_pretrained(path)
    return str(path), text_model


def test_config_dispatch_uses_nested_text_config(wrapper_checkpoint):
    path, _ = wrapper_checkpoint
    cfg = AutoDistributedConfig.from_pretrained(path)
    assert type(cfg).__name__ == "DistributedGemma4UnifiedConfig"
    assert cfg.model_type == "gemma4_unified_text"  # dispatched onto the nested text config
    assert cfg.attention_k_eq_v is True and cfg.num_global_key_value_heads == 1
    assert cfg.block_prefix == "model.language_model.layers"


def test_wrapper_blocks_load_bit_exact(wrapper_checkpoint):
    """Every block loads from model.language_model.layers.* (incl. the layer_scalar buffer and the
    absent v_proj on k_eq_v layers) and matches the stock layer output."""
    path, text_model = wrapper_checkpoint
    cfg = AutoDistributedConfig.from_pretrained(path)

    input_ids = torch.randint(0, cfg.vocab_size, (1, 6))
    layer_in, layer_out = {}, {}

    def pre_hook(i):
        def hook(_m, args, kwargs):
            layer_in[i] = (args[0] if args else kwargs["hidden_states"]).detach().clone()

        return hook

    def out_hook(i):
        def hook(_m, _args, _kwargs, output):
            layer_out[i] = (output[0] if isinstance(output, tuple) else output).detach().clone()

        return hook

    for i, layer in enumerate(text_model.layers):
        layer.register_forward_pre_hook(pre_hook(i), with_kwargs=True)
        layer.register_forward_hook(out_hook(i), with_kwargs=True)
    with torch.inference_mode():
        text_model(input_ids, use_cache=False)

    for i in range(cfg.num_hidden_layers):
        block = load_pretrained_block(path, i, config=cfg, torch_dtype=torch.float32).eval()
        assert torch.equal(block.layer_scalar, text_model.layers[i].layer_scalar)
        with torch.inference_mode():
            (out,) = block(layer_in[i])
        assert torch.allclose(out, layer_out[i], atol=ATOL), (i, (out - layer_out[i]).abs().max().item())
