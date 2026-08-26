import contextlib
import sys
import threading
import traceback

import torch

DUMMY = torch.empty(0)  # dummy tensor that replaces empty prompt or adapter parameters

DUMMY_INT64 = torch.empty(0, dtype=torch.int64)

DUMMY_KEY_PAST = torch.empty((0, 0, 0))


def is_dummy(tensor: torch.Tensor) -> bool:
    return tensor.numel() == 0


def format_all_thread_stacks() -> str:
    """Render the current stack of every live thread, like SIGQUIT or an external profiler dump.

    Exists because the process must be able to dump itself: py-spy cannot attach to uv-managed
    CPython, and Windows has no SIGQUIT -- so a wedged server would otherwise be indistinguishable
    from a slow one.
    """
    names = {t.ident: t.name for t in threading.enumerate()}
    parts = []
    for ident, frame in sorted(sys._current_frames().items()):
        stack = "".join(traceback.format_stack(frame))
        parts.append(f'Thread "{names.get(ident, "unknown")}" (ident {ident}):\n{stack}')
    return "\n".join(parts)


SPECIAL_DTYPE_SIZES = {torch.bool: 1, torch.qint8: 1, torch.qint32: 4}


def get_size_in_bytes(dtype: torch.dtype) -> int:
    if dtype in SPECIAL_DTYPE_SIZES:
        return SPECIAL_DTYPE_SIZES[dtype]
    get_info = torch.finfo if dtype.is_floating_point else torch.iinfo
    return (get_info(dtype).bits * (1 + dtype.is_complex)) // 8


def get_num_attention_heads(attn_module: torch.nn.Module, config) -> int:
    """Number of query heads held by an attention module.

    transformers >=5.0 removed the ``num_heads`` attribute from several attention classes
    (e.g. ``LlamaAttention``), so fall back to deriving it from the query projection. Older
    attention modules that still expose ``num_heads`` (used to count heads per tensor-parallel
    shard) keep working unchanged.
    """
    num_heads = getattr(attn_module, "num_heads", None)
    if num_heads is not None:
        return num_heads
    head_dim = getattr(attn_module, "head_dim", config.hidden_size // config.num_attention_heads)
    for proj_name in ("q_proj", "query_key_value", "query"):
        proj = getattr(attn_module, proj_name, None)
        if proj is not None:
            if proj_name == "query_key_value":
                # fused QKV projection (e.g. BLOOM/Falcon): query heads = total heads
                return config.num_attention_heads
            # tensor_parallel replaces sliced Linears with a wrapper exposing the shard via `.module`
            if not hasattr(proj, "out_features") and hasattr(proj, "module"):
                proj = proj.module
            projection_multiplier = getattr(config, "query_projection_multiplier", 1) if proj_name == "q_proj" else 1
            return proj.out_features // (head_dim * projection_multiplier)
    return config.num_attention_heads


def default_attn_implementation(config) -> str:
    """Best *correct* attention implementation for a server-side block when none is forced.

    Defaults to ``sdpa`` (``torch.nn.functional.scaled_dot_product_attention``), which runs on CPU
    and on GPU dispatches to FlashAttention / memory-efficient kernels -- the modern fast path --
    while staying numerically equivalent to ``eager``. A few features have no correct sdpa path in
    ``transformers`` given how DRIFT-LLM drives attention, so those stay on ``eager``:

      * ALiBi -- Bloom (always) and Falcon with ``alibi=True``: DRIFT-LLM folds ALiBi into a 4D mask
        that the sdpa attention path does not consume, and
      * attention logit softcapping (Gemma 2): silently dropped by sdpa.

    ``flash_attention_2`` is intentionally not selected here: DRIFT-LLM feeds attention an explicit 4D
    mask, whereas FA2 wants the varlen / ``cu_seqlens`` form (a Phase 5 follow-up). On GPU, sdpa
    already routes through FlashAttention for the common case.
    """
    uses_alibi = getattr(config, "alibi", False) or getattr(config, "model_type", None) == "bloom"
    if uses_alibi or getattr(config, "attn_logit_softcapping", None):
        return "eager"
    return "sdpa"


def get_num_key_value_groups(config) -> int:
    groups = getattr(config, "num_key_value_groups", None)
    if groups is not None:
        return groups

    num_heads = getattr(config, "num_attention_heads", 1)
    num_key_value_heads = getattr(config, "num_key_value_heads", num_heads)
    return num_heads // num_key_value_heads


def should_use_eager_attention_on_mps(config, device: torch.device) -> bool:
    return (
        device.type == "mps"
        and getattr(config, "_attn_implementation", None) == "sdpa"
        and get_num_key_value_groups(config) > 1
    )


@contextlib.contextmanager
def mps_gqa_eager_attention(config, device: torch.device):
    previous = getattr(config, "_attn_implementation", None)
    if should_use_eager_attention_on_mps(config, device):
        config._attn_implementation = "eager"
    try:
        yield
    finally:
        config._attn_implementation = previous


def docstring_from(source):
    def add_docstring(dest):
        dest.__doc__ = source.__doc__
        return dest

    return add_docstring
