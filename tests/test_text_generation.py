"""Long chat prompts and overlapping answer/title requests on text peers."""

import asyncio
import contextlib
import threading
import queue
from types import SimpleNamespace
from unittest.mock import patch

import torch
from torch import nn

from drift.models.qwen3_5.model import DistributedQwen3_5ForCausalLM
from drift.server.text_generation import TextGenerationEngine
from drift.api.server import _RequestCancelled
from test_qwen3_5_block import _tiny_config, _wrapped_blocks


def test_chunked_generation_preserves_remote_cache_and_full_prompt():
    from transformers.models.qwen3_5 import Qwen3_5ForCausalLM
    from drift.models.qwen3_5.cache import Qwen3_5HybridCache

    torch.set_num_threads(1)
    cfg = _tiny_config()
    # Include a second full-attention block. Its isolated cache must not use
    # the first full-attention block's (empty) cache to size its causal mask.
    cfg.num_hidden_layers = 8
    cfg.layer_types = ["linear_attention"] * 3 + ["full_attention"]
    cfg.layer_types *= 2
    cfg.max_position_embeddings = 1024
    torch.manual_seed(7)
    reference = Qwen3_5ForCausalLM(cfg).eval()

    class LocalBlocks(nn.Module):
        """Actual worker blocks/caches behind the distributed generation interface."""

        def __init__(self, config, **kwargs):
            super().__init__()
            self.config = config
            self.blocks = nn.ModuleList(_wrapped_blocks(config, reference.model))
            self.active_session = None
            self.sizes = []

        @property
        def position(self):
            return self.active_session.position

        @contextlib.contextmanager
        def inference_session(self, max_length):
            self.active_session = SimpleNamespace(position=0, output_ids=None)
            self.strategies = [Qwen3_5HybridCache(cfg, module=b) for b in self.blocks]
            self.caches = [
                [
                    torch.zeros(d.shape, dtype=d.dtype)
                    for d in s.get_cache_descriptors(
                        1,
                        max_length,
                        dtype=torch.float32,
                        devices=[torch.device("cpu")],
                        shard_num_heads=[cfg.num_attention_heads],
                    )
                ]
                for s in self.strategies
            ]
            try:
                yield self.active_session
            finally:
                self.active_session = None

        def forward(self, hidden, **kwargs):
            self.sizes.append(hidden.shape[1])
            for block, strategy, cache in zip(self.blocks, self.strategies, self.caches):
                past = strategy.select_layer_past(cache, self.position, num_shards=1)
                hidden, new_states = block(hidden, layer_past=past, use_cache=True)
                strategy.update_cache(cache, new_states, self.position)
            self.active_session.position += hidden.shape[1]
            return hidden

    with patch("drift.models.qwen3_5.model.RemoteSequential", LocalBlocks):
        actual = DistributedQwen3_5ForCausalLM(cfg).eval()
    actual.model.embed_tokens.load_state_dict(reference.model.embed_tokens.state_dict())
    actual.model.norm.load_state_dict(reference.model.norm.state_dict())
    actual.lm_head.load_state_dict(reference.lm_head.state_dict())
    for block, reference_block in zip(actual.model.layers.blocks, reference.model.layers):
        block.load_state_dict(reference_block.state_dict())
    prompt = torch.randint(3, cfg.vocab_size, (1, 533))
    with torch.inference_mode():
        expected = reference.generate(prompt, max_new_tokens=3, do_sample=False, eos_token_id=None)
        result = actual.generate(prompt, max_new_tokens=3, do_sample=False, eos_token_id=None, prefill_chunk_size=64)
    assert torch.equal(result, expected)
    assert actual.model.layers.sizes == [64] * 8 + [21, 1, 1]
    assert torch.equal(result[:, :533], prompt)


def test_overlapping_requests_queue_and_cancel_without_parallel_generation():
    async def check():
        engine = TextGenerationEngine(None, request_timeout=10)
        started, release = threading.Event(), threading.Event()
        seen = []

        def generate(payload, key, cancel, out):
            seen.append(key)
            started.set()
            assert release.wait(5)
            out.put({"type": "done"})

        engine._generate = generate
        payload = lambda n: {"request_id": str(n) * 32, "chat": True}
        first = engine.stream(payload(1), "peer")
        first_task = asyncio.create_task(anext(first))
        assert await asyncio.to_thread(started.wait, 2)
        second = engine.stream(payload(2), "peer")
        assert (await anext(second))["type"] == "heartbeat"
        third = engine.stream(payload(3), "peer")
        assert (await anext(third))["type"] == "heartbeat"
        fourth = engine.stream(payload(4), "peer")
        assert (await anext(fourth))["code"] == "busy"
        duplicate = engine.stream(payload(1), "peer")
        assert (await anext(duplicate))["code"] == "busy"
        engine.cancel("3" * 32, "peer")
        await third.aclose()
        release.set()
        await first_task
        await first.aclose()
        frames = [frame async for frame in second]
        assert frames[-1]["type"] == "done"
        assert seen == [("peer", "1" * 32), ("peer", "2" * 32)]
        engine.close()

    asyncio.run(check())


def test_disconnect_stops_prefill_before_the_next_chunk_and_removes_hook():
    cancel = _RequestCancelled()

    class Model(nn.Module):
        calls = 0

        def forward(self, inputs):
            self.calls += 1
            cancel.event.set()
            return inputs

        def generate(self, inputs, **kwargs):
            for part in inputs.split(64, dim=1):
                self(part)
            raise AssertionError("Cancelled prefill must stop before finishing")

    model = Model()
    tokenizer = SimpleNamespace(
        eos_token_id=None, apply_chat_template=lambda *a, **kw: {"input_ids": torch.ones(1, 533).long()}
    )
    engine = TextGenerationEngine(SimpleNamespace(model=model, tokenizer=tokenizer))
    output = queue.Queue()
    engine._generate(
        {"chat": True, "body": {"model": "auto", "messages": [{"role": "user", "content": "hi"}]}},
        ("peer", "request"),
        cancel,
        output,
    )
    assert model.calls == 1
    assert not model._forward_pre_hooks
    assert output.empty()


def test_chat_stops_on_tokenizer_turn_end_as_well_as_model_end_of_text():
    captured = {}

    class Model(nn.Module):
        generation_config = SimpleNamespace(eos_token_id=248044)

        def generate(self, inputs, **kwargs):
            captured.update(kwargs)
            return inputs

    tokenizer = SimpleNamespace(
        eos_token_id=248046, apply_chat_template=lambda *a, **kw: {"input_ids": torch.ones(1, 8).long()}
    )
    engine = TextGenerationEngine(SimpleNamespace(model=Model(), tokenizer=tokenizer))
    output = queue.Queue()
    engine._generate(
        {"chat": True, "body": {"model": "auto", "messages": [{"role": "user", "content": "hi"}]}},
        ("peer", "request"),
        _RequestCancelled(),
        output,
    )
    assert captured["eos_token_id"] == [248044, 248046]
    assert output.get_nowait()["finish_reason"] == "stop"
