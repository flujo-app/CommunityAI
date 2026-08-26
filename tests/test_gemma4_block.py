"""
Offline exact-match tests for the Gemma 4 block (per-layer inputs + KV sharing).

Checks the wrapped block against a tiny stock HF ``Gemma4TextModel`` across all four layer roles
(sliding/full x donor/sharing), the per-layer-input side channel, and the BLOOM cache round-trip on
a non-sharing layer. Also covers the MoE variant of the family (e.g. google/gemma-4-26B-A4B-it):
same ``gemma4`` model type, but with ``enable_moe_block`` (in-layer router + experts alongside the
dense MLP), ``attention_k_eq_v`` full layers (no v_proj, own MQA kv-head count and wider
``global_head_dim``), proportional partial RoPE on full layers, and PLE / KV sharing switched off.
No swarm / network / download required (does not import test_utils).
"""
import pytest
import torch

from drift.models.gemma4.block import WrappedGemma4Block
from drift.models.gemma4.config import DistributedGemma4Config

ATOL = 3e-5


def _tiny_config(impl="eager"):
    from transformers.models.gemma4 import Gemma4TextConfig

    # 6 layers, types chosen so both a sliding and a full attention type have a non-shared donor
    # (last non-shared of that type) followed by a sharing consumer:
    #   idx:        0        1        2      3        4        5
    #   type:    sliding  sliding   full  sliding  sliding   full
    #   shared:     -        -       -      -        yes      yes   (num_kv_shared_layers=2)
    #   donor:                     full  sliding
    layer_types = [
        "sliding_attention",
        "sliding_attention",
        "full_attention",
        "sliding_attention",
        "sliding_attention",
        "full_attention",
    ]
    cfg = Gemma4TextConfig(
        vocab_size=128,
        hidden_size=64,
        intermediate_size=128,
        num_hidden_layers=6,
        num_attention_heads=4,
        num_key_value_heads=1,  # multi-query attention, as in the released checkpoints
        head_dim=16,
        global_head_dim=32,  # full-attention layers use a wider head than sliding ones
        layer_types=layer_types,
        num_kv_shared_layers=2,
        sliding_window=1024,
        hidden_size_per_layer_input=8,
        vocab_size_per_layer_input=128,
        enable_moe_block=False,
    )
    cfg._attn_implementation = impl
    return cfg


def test_gemma4_cache_estimate_honors_per_block_head_dim():
    cfg = DistributedGemma4Config(**_tiny_config().to_dict())
    strategy = cfg.kv_cache_strategy
    max_length = 8

    # Sliding: K + V, one KV head, 16 elements per head, float32.
    assert strategy.estimate_cache_bytes(cfg, max_length, dtype=torch.float32, block_index=0) == 1024
    # Full: K + V, one KV head, the wider 32-element global head, float32.
    assert strategy.estimate_cache_bytes(cfg, max_length, dtype=torch.float32, block_index=2) == 2048
    # With no block selected, admission accounting must use the largest layer geometry.
    assert strategy.estimate_cache_bytes(cfg, max_length, dtype=torch.float32) == 2048


def _model_and_per_layer_inputs(cfg, input_ids):
    """Build a stock model, capture each layer's exact input/output, and the per-layer inputs."""
    from transformers.models.gemma4 import Gemma4TextModel

    torch.manual_seed(0)
    model = Gemma4TextModel(cfg).eval()

    layer_in, layer_out = {}, {}

    def pre_hook(idx):
        def hook(_module, args, kwargs):
            layer_in[idx] = (args[0] if args else kwargs["hidden_states"]).detach().clone()

        return hook

    def out_hook(idx):
        def hook(_module, _args, _kwargs, output):
            out = output[0] if isinstance(output, tuple) else output
            layer_out[idx] = out.detach().clone()

        return hook

    for i, layer in enumerate(model.layers):
        layer.register_forward_pre_hook(pre_hook(i), with_kwargs=True)
        layer.register_forward_hook(out_hook(i), with_kwargs=True)

    with torch.inference_mode():
        model(input_ids, use_cache=False)
        inputs_embeds = model.embed_tokens(input_ids)
        per_layer_inputs = model.get_per_layer_inputs(input_ids, inputs_embeds)
        per_layer_inputs = model.project_per_layer_inputs(inputs_embeds, per_layer_inputs)

    return model, layer_in, layer_out, per_layer_inputs


def test_gemma4_block_stack_matches_hf():
    cfg = _tiny_config()
    input_ids = torch.randint(0, cfg.vocab_size, (1, 6))
    model, layer_in, layer_out, per_layer_inputs = _model_and_per_layer_inputs(cfg, input_ids)

    shared_kv_states = {}  # threaded through the stack exactly as the stock model does
    for i in range(cfg.num_hidden_layers):
        block = WrappedGemma4Block(cfg, layer_idx=i).eval()
        missing, unexpected = block.load_state_dict(model.layers[i].state_dict(), strict=False)
        assert not unexpected, f"layer {i} unexpected keys: {unexpected}"
        assert all("rotary" in name for name in missing), f"layer {i} unexpected missing keys: {missing}"

        with torch.inference_mode():
            (out,) = block(
                layer_in[i],
                per_layer_input=per_layer_inputs[:, :, i, :],
                shared_kv_states=shared_kv_states,
            )
        assert torch.allclose(out, layer_out[i], atol=ATOL), (
            f"layer {i} ({cfg.layer_types[i]}, shared={block.is_kv_shared_layer}): "
            f"{(out - layer_out[i]).abs().max().item()}"
        )

    # The stack must have actually exercised KV sharing (both donors fired, both consumers read).
    assert set(shared_kv_states) == {"sliding_attention", "full_attention"}


def _clone_across_wire(shared_kv_states):
    """Mimic serialization of shared K/V between two servers (spans)."""
    return {t: (k.clone(), v.clone()) for t, (k, v) in shared_kv_states.items()}


def test_gemma4_pipeline_across_spans():
    """Full stack split into spans, threading hidden + per-layer-inputs + donor K/V across span
    boundaries -- with each donor and its consumer deliberately in *different* spans."""
    cfg = _tiny_config()
    input_ids = torch.randint(0, cfg.vocab_size, (1, 6))
    model, _, _, per_layer_inputs = _model_and_per_layer_inputs(cfg, input_ids)
    with torch.inference_mode():
        reference = model(input_ids, use_cache=False).last_hidden_state
        inputs_embeds = model.embed_tokens(input_ids)

    blocks = []
    for i in range(cfg.num_hidden_layers):
        block = WrappedGemma4Block(cfg, layer_idx=i).eval()
        block.load_state_dict(model.layers[i].state_dict(), strict=False)
        blocks.append(block)

    # donors are at layers 2 (full) and 3 (sliding); consumers at 4 (sliding) and 5 (full).
    # These spans put every donor in a strictly earlier span than its consumer.
    spans = [(0, 2), (2, 4), (4, 6)]
    hidden = inputs_embeds
    shared_kv_states = {}
    with torch.inference_mode():
        for start, end in spans:
            shared_kv_states = _clone_across_wire(shared_kv_states)  # crosses the "wire"
            for i in range(start, end):
                (hidden,) = blocks[i](
                    hidden,
                    per_layer_input=per_layer_inputs[:, :, i, :],
                    shared_kv_states=shared_kv_states,
                )
        hidden = model.norm(hidden)

    assert torch.allclose(hidden, reference, atol=ATOL), (hidden - reference).abs().max()


def _moe_tiny_config():
    """Mirrors google/gemma-4-26B-A4B-it's structure at toy size: MoE block in every layer,
    k_eq_v full-attention layers with their own MQA kv-head count and wider head_dim, proportional
    partial RoPE on full layers, PLE and KV sharing off."""
    from transformers.models.gemma4 import Gemma4TextConfig

    return Gemma4TextConfig(
        vocab_size=128,
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
        num_kv_shared_layers=0,
        sliding_window=1024,
        hidden_size_per_layer_input=0,
        enable_moe_block=True,
        num_experts=8,
        top_k_experts=2,
        moe_intermediate_size=32,
        rope_parameters={
            "full_attention": {"rope_type": "proportional", "rope_theta": 1_000_000.0, "partial_rotary_factor": 0.25},
            "sliding_attention": {"rope_type": "default", "rope_theta": 10_000.0},
        },
        final_logit_softcapping=30.0,
        tie_word_embeddings=True,
    )


def _moe_model_and_refs(cfg, input_ids):
    """Stock MoE model (with randomized layer_scalar, as the released checkpoints ship values != 1)
    and each layer's exact input/output."""
    from transformers.models.gemma4 import Gemma4TextModel

    torch.manual_seed(0)
    model = Gemma4TextModel(cfg).eval()
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


def test_gemma4_moe_block_stack_matches_hf():
    cfg = _moe_tiny_config()
    input_ids = torch.randint(0, cfg.vocab_size, (1, 6))
    model, layer_in, layer_out = _moe_model_and_refs(cfg, input_ids)

    for i in range(cfg.num_hidden_layers):
        block = WrappedGemma4Block(cfg, layer_idx=i).eval()
        missing, unexpected = block.load_state_dict(model.layers[i].state_dict(), strict=False)
        assert not unexpected, f"layer {i} unexpected keys: {unexpected}"
        assert all("rotary" in name for name in missing), f"layer {i} unexpected missing keys: {missing}"

        # Every layer must carry the MoE block; full layers must actually exercise k_eq_v.
        assert block.enable_moe_block and block.experts.num_experts == cfg.num_experts
        if cfg.layer_types[i] == "full_attention":
            assert block.self_attn.v_proj is None

        with torch.inference_mode():
            (out,) = block(layer_in[i])
        assert torch.allclose(
            out, layer_out[i], atol=ATOL
        ), f"layer {i} ({cfg.layer_types[i]}): {(out - layer_out[i]).abs().max().item()}"


@pytest.mark.parametrize("layer_idx", [0, 2])  # sliding GQA (2x16) and full k_eq_v MQA (1x32, partial rope)
def test_gemma4_moe_block_cache_roundtrip(layer_idx):
    cfg = _moe_tiny_config()
    input_ids = torch.randint(0, cfg.vocab_size, (1, 5))
    model, layer_in, layer_out = _moe_model_and_refs(cfg, input_ids)

    block = WrappedGemma4Block(cfg, layer_idx=layer_idx).eval()
    block.load_state_dict(model.layers[layer_idx].state_dict(), strict=False)

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


def test_gemma4_nonshared_block_cache_roundtrip():
    cfg = _tiny_config()
    input_ids = torch.randint(0, cfg.vocab_size, (1, 5))
    model, layer_in, layer_out, per_layer_inputs = _model_and_per_layer_inputs(cfg, input_ids)

    layer_idx = 0  # non-sharing, non-donor sliding layer -> needs no shared_kv_states
    block = WrappedGemma4Block(cfg, layer_idx=layer_idx).eval()
    block.load_state_dict(model.layers[layer_idx].state_dict(), strict=False)

    x, pli = layer_in[layer_idx], per_layer_inputs[:, :, layer_idx, :]
    n = input_ids.shape[1]
    with torch.inference_mode():
        prefill_out, _ = block(x, per_layer_input=pli, use_cache=True)

        past, steps = None, []
        for i in range(n):
            step_out, past = block(x[:, i : i + 1], per_layer_input=pli[:, i : i + 1], layer_past=past, use_cache=True)
            steps.append(step_out)
        stepwise_out = torch.cat(steps, dim=1)

    assert torch.allclose(prefill_out, layer_out[layer_idx], atol=ATOL), (
        (prefill_out - layer_out[layer_idx]).abs().max()
    )
    assert torch.allclose(stepwise_out, layer_out[layer_idx], atol=ATOL), (
        (stepwise_out - layer_out[layer_idx]).abs().max()
    )
