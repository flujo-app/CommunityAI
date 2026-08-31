"""
Offline tests for the paged KV cache (Stage 5a): ``PagedKVPool`` + ``MemoryCache`` paged mode.

Drives a real transformer block through the page pool (gather/scatter/reorder) exactly as
``TransformerBackend._paged_inference_step`` does and checks bit-exact agreement with the block's
own full-sequence output and with the contiguous ``StandardGQACache``. Also covers the asymmetric
MLA key/value head dims and the paged admission/packing logic. No swarm / network / download.
"""
import pytest
import torch

from drift.server.memory_cache import AllocationFailed, MemoryCache, PagedKVPool
from drift.utils.kv_cache import MLACache, StandardGQACache

ATOL = 3e-5
CPU = torch.device("cpu")


def _llama_config(head_dim=None):
    from transformers.models.llama import LlamaConfig

    hidden, heads, kv = 64, 4, 2
    kwargs = dict(
        hidden_size=hidden,
        num_attention_heads=heads,
        num_key_value_heads=kv,
        intermediate_size=128,
        num_hidden_layers=2,
        vocab_size=128,
    )
    if head_dim is not None:
        kwargs["head_dim"] = head_dim
    cfg = LlamaConfig(**kwargs)
    cfg.num_key_value_groups = heads // kv
    return cfg


def _deepseek_config():
    from transformers.models.deepseek_v3 import DeepseekV3Config

    cfg = DeepseekV3Config(
        hidden_size=64,
        intermediate_size=128,
        num_hidden_layers=3,
        num_attention_heads=4,
        num_key_value_heads=4,
        vocab_size=128,
        q_lora_rank=24,
        kv_lora_rank=16,
        qk_rope_head_dim=8,
        qk_nope_head_dim=16,
        v_head_dim=16,
        n_routed_experts=4,
        num_experts_per_tok=2,
        n_shared_experts=1,
        moe_intermediate_size=32,
        n_group=1,
        topk_group=1,
        first_k_dense_replace=1,
        norm_topk_prob=True,
    )
    cfg._attn_implementation = "eager"
    cfg.num_key_value_groups = cfg.num_attention_heads // cfg.num_key_value_heads
    return cfg


def _pool_from_strategy(strategy, cfg, page_size, batch_size, num_pages=512):
    key_d, val_d = strategy.get_cache_descriptors(
        1, 1, dtype=torch.float32, devices=[CPU], shard_num_heads=[cfg.num_attention_heads]
    )
    pool = PagedKVPool(
        num_pages=num_pages,
        page_size=page_size,
        num_kv_heads=key_d.size[1],
        k_head_dim=key_d.size[2],
        v_head_dim=val_d.size[3],
        dtype=torch.float32,
        device=CPU,
    )
    pool.register_slot(0, batch_size)
    return pool


def _drive_prefill_decode(block, pool, x, n_prefill, total, slot=0):
    """Mirror TransformerBackend._paged_inference_step: prefill then one token per step."""
    outs = []
    with torch.inference_mode():
        out, new_kvs = block(x[:, :n_prefill], layer_past=pool.gather(slot, 0), use_cache=True)
        pool.scatter(slot, new_kvs, 0)
        outs.append(out)
        for t in range(n_prefill, total):
            out, new_kvs = block(x[:, t : t + 1], layer_past=pool.gather(slot, t), use_cache=True)
            pool.scatter(slot, new_kvs, t)
            outs.append(out)
    return torch.cat(outs, dim=1)


@pytest.mark.parametrize("page_size", [1, 4, 16])
@pytest.mark.parametrize("head_dim", [None, 48])
def test_paged_pool_matches_block(page_size, head_dim):
    from drift.models.llama.block import WrappedLlamaBlock

    torch.manual_seed(0)
    cfg = _llama_config(head_dim=head_dim)
    cfg._attn_implementation = "eager"
    block = WrappedLlamaBlock(cfg).eval()
    pool = _pool_from_strategy(StandardGQACache(cfg), cfg, page_size, batch_size=1)

    total, n_prefill = 8, 5
    x = torch.randn(1, total, cfg.hidden_size)
    with torch.inference_mode():
        ref_out, _ = block(x, use_cache=True)
    got = _drive_prefill_decode(block, pool, x, n_prefill, total)
    assert torch.allclose(got, ref_out, atol=ATOL), (got - ref_out).abs().max()


def test_paged_pool_mla_asymmetric_head_dims():
    from drift.models.deepseek_v3.block import WrappedDeepseekV3Block

    torch.manual_seed(0)
    cfg = _deepseek_config()
    # Isolate MLA cache gather/scatter from MoE top-k routing: tiny CPU rounding
    # differences between batched prefill and one-token decode can flip an expert tie.
    # The dedicated DeepSeek block tests cover both dense and MoE layers.
    block = WrappedDeepseekV3Block(cfg, layer_idx=0).eval()  # dense layer, same MLA attention
    pool = _pool_from_strategy(MLACache(cfg), cfg, page_size=4, batch_size=1)
    assert pool.k_head_dim != pool.v_head_dim  # 24 (qk_nope+qk_rope) vs 16 (v_head_dim)

    total, n_prefill = 6, 4
    x = torch.randn(1, total, cfg.hidden_size)
    with torch.inference_mode():
        ref_out, _ = block(x, use_cache=True)
    got = _drive_prefill_decode(block, pool, x, n_prefill, total)
    assert torch.allclose(got, ref_out, atol=ATOL), (got - ref_out).abs().max()


@pytest.mark.parametrize("hypo_ids", [[1, 0, 2], [0, 0, 1], [2, 2, 2]])
def test_paged_reorder_matches_contiguous(hypo_ids):
    """A beam-search reorder of pages must equal the contiguous cache[...] = cache[hypo_ids]."""
    torch.manual_seed(0)
    cfg = _llama_config()
    strategy = StandardGQACache(cfg)
    B, H = 3, cfg.num_key_value_heads
    kd = cfg.hidden_size // cfg.num_attention_heads
    length = 10

    pool = _pool_from_strategy(strategy, cfg, page_size=4, batch_size=B)
    key_full = torch.randn(B * H, kd, length)
    value_full = torch.randn(B * H, length, kd)
    pool.scatter(0, (key_full, value_full), 0)

    cache = [
        torch.zeros(*d.size, dtype=d.dtype)
        for d in strategy.get_cache_descriptors(
            B, 32, dtype=torch.float32, devices=[CPU], shard_num_heads=[cfg.num_attention_heads]
        )
    ]
    strategy.update_cache(cache, (key_full, value_full), 0)

    hypo = torch.tensor(hypo_ids)
    pool.reorder(0, hypo)
    ck, cv = strategy.select_layer_past(cache, length, num_shards=1)
    ck = ck.reshape(B, H, kd, length)[hypo].reshape(B * H, kd, length)
    cv = cv.reshape(B, H, length, kd)[hypo].reshape(B * H, length, kd)
    pk, pv = pool.gather(0, length)
    assert torch.equal(pk, ck)
    assert torch.equal(pv, cv)


def test_paged_pool_frees_pages_on_slot_release():
    cfg = _llama_config()
    pool = _pool_from_strategy(StandardGQACache(cfg), cfg, page_size=4, batch_size=1, num_pages=8)
    H = cfg.num_key_value_heads
    kd = cfg.hidden_size // cfg.num_attention_heads
    assert pool.num_free_pages == 8
    pool.scatter(0, (torch.randn(H, kd, 9), torch.randn(H, 9, kd)), 0)  # 9 tokens -> ceil(9/4)=3 pages
    assert pool.num_used_pages == 3
    pool.free_slot(0)
    assert pool.num_free_pages == 8


@pytest.mark.asyncio
async def test_paged_admission_decoupled_from_max_length():
    # Paged admission is optimistic: sessions are admitted while the shared page pool has room,
    # regardless of their max_length (which the contiguous cache would reserve up front).
    cache = MemoryCache(max_size_bytes=1 << 20, paged=True, page_size=16)
    cache.configure_paged_pool(num_pages=3, num_kv_heads=1, k_head_dim=4, v_head_dim=4, dtype=torch.float32, device=CPU)
    cache.runtime_pid += 1  # pretend the handler runs in a different process than the runtime

    async with cache.allocate_paged_slots(2, batch_size=1, timeout=0) as slots:
        assert len(slots) == 2
        cache._paged_used_pages.value = 3  # runtime has now consumed every page
        with pytest.raises(AllocationFailed):
            async with cache.allocate_paged_slots(1, batch_size=1, timeout=0):
                pass
        cache._paged_used_pages.value = 1  # a page was freed
        async with cache.allocate_paged_slots(1, batch_size=1, timeout=0) as more:
            assert len(more) == 1
