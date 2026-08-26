"""Device-portability tests: a DRIFT-LLM block must produce the same result on any accelerator.

These build a tiny Llama block on CPU (the reference) and re-run it on each available accelerator
(NVIDIA CUDA / Intel XPU / Apple MPS), asserting the outputs and the KV-cache round-trip match. On a
machine with no accelerator every case is skipped, so the suite stays green in CPU-only CI while
giving real coverage wherever a GPU exists. They need no swarm, network, or model download.
"""
from types import SimpleNamespace

import pytest
import torch

from drift.utils.hardware import (
    ACCELERATOR_TYPES,
    empty_device_cache,
    get_device_name,
    get_device_total_memory,
    is_accelerator,
    set_device_memory_limit,
    synchronize_device,
)

SEQ_LEN = 8
# Cross-device tolerance: different backends use different reduction orders, so this is looser than
# the same-device bit-exact checks in test_dense_blocks.py but still tight enough to catch real bugs.
ATOL = 3e-4


def _available_accelerators():
    available = []
    for device_type in ACCELERATOR_TYPES:
        backend = getattr(torch, device_type, None)
        is_available = getattr(backend, "is_available", None)
        if is_available is not None and is_available():
            available.append(device_type)
    return available


AVAILABLE = _available_accelerators()
requires_accelerator = pytest.mark.skipif(not AVAILABLE, reason="no accelerator (cuda/xpu/mps) available")


def _tiny_llama_block():
    from transformers.models.llama import LlamaConfig

    from drift.models.llama.block import WrappedLlamaBlock

    cfg = LlamaConfig(
        hidden_size=64,
        num_attention_heads=4,
        num_key_value_heads=2,
        intermediate_size=128,
        num_hidden_layers=2,
        vocab_size=128,
    )
    cfg._attn_implementation = "eager"
    torch.manual_seed(0)
    return WrappedLlamaBlock(cfg, layer_idx=0).eval()


def _run(block, hidden):
    """Prefill + stepwise decode through the BLOOM-layout cache; return (prefill_out, stepwise_out, past)."""
    with torch.inference_mode():
        prefill_out, _ = block(hidden, use_cache=True)
        past, steps = None, []
        for i in range(hidden.shape[1]):
            step_out, past = block(hidden[:, i : i + 1], layer_past=past, use_cache=True)
            steps.append(step_out)
    return prefill_out, torch.cat(steps, dim=1), past


@pytest.mark.parametrize("device_type", AVAILABLE)
def test_block_matches_cpu_on_accelerator(device_type):
    block = _tiny_llama_block()
    torch.manual_seed(1)
    hidden = torch.randn(1, SEQ_LEN, block.config.hidden_size, dtype=torch.float32)

    cpu_prefill, cpu_stepwise, cpu_past = _run(block, hidden)

    device = torch.device(device_type)
    device_block = _tiny_llama_block().to(device)
    device_block.load_state_dict(block.state_dict())
    dev_prefill, dev_stepwise, dev_past = _run(device_block, hidden.to(device))

    assert torch.allclose(dev_prefill.cpu(), cpu_prefill, atol=ATOL), (dev_prefill.cpu() - cpu_prefill).abs().max()
    assert torch.allclose(dev_stepwise.cpu(), cpu_stepwise, atol=ATOL), (dev_stepwise.cpu() - cpu_stepwise).abs().max()
    assert torch.allclose(dev_past[0].cpu(), cpu_past[0], atol=ATOL)
    assert torch.allclose(dev_past[1].cpu(), cpu_past[1], atol=ATOL)


@pytest.mark.parametrize("device_type", AVAILABLE)
def test_hardware_helpers_on_real_device(device_type):
    device = torch.device(device_type)
    assert is_accelerator(device)
    assert get_device_total_memory(device) > 0
    assert get_device_name(device)  # non-empty
    # these must not raise on a live device
    empty_device_cache(device)
    synchronize_device(device)


def test_device_memory_limit_clamps_and_uses_indexed_backend(monkeypatch):
    calls = []
    backend = SimpleNamespace(set_per_process_memory_fraction=lambda fraction, device: calls.append((fraction, device)))
    monkeypatch.setattr("drift.utils.hardware._backend", lambda device_type: backend)
    monkeypatch.setattr("drift.utils.hardware.get_device_total_memory", lambda device: 1_000)

    device = torch.device("cuda:2")
    assert set_device_memory_limit(device, 2_000) == 1_000
    assert calls == [(1.0, device)]


def test_device_memory_limit_supports_mps_signature_and_recommended_memory(monkeypatch):
    calls = []
    backend = SimpleNamespace(
        recommended_max_memory=lambda: 2_000,
        set_per_process_memory_fraction=lambda fraction: calls.append(fraction),
    )
    monkeypatch.setattr("drift.utils.hardware._backend", lambda device_type: backend)

    device = torch.device("mps")
    assert get_device_total_memory(device) == 2_000
    assert set_device_memory_limit(device, 500) == 500
    assert calls == [0.25]


def test_device_memory_limit_fails_closed_without_backend_support(monkeypatch):
    monkeypatch.setattr("drift.utils.hardware._backend", lambda device_type: SimpleNamespace())
    monkeypatch.setattr("drift.utils.hardware.get_device_total_memory", lambda device: 1_000)

    with pytest.raises(RuntimeError, match="enforceable allocator"):
        set_device_memory_limit(torch.device("xpu:0"), 500)
    with pytest.raises(ValueError, match="require an accelerator"):
        set_device_memory_limit(torch.device("cpu"), 500)


@requires_accelerator
def test_at_least_one_accelerator_detected():
    # A trivial guard so a fully-skipped run is visibly distinct from a passing one in the report.
    assert AVAILABLE
