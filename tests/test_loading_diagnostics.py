from types import SimpleNamespace

import pytest
import torch
from torch import nn

from drift.client.lm_head import LMHead
from drift.server.from_pretrained import (
    _find_unconsumed_checkpoint_keys,
    _load_state_dict_from_local_file,
    dequantize_finegrained_fp8_state_dict,
)
from drift.utils.asyncio import patch_hivemind_task_cleanup, safe_cancel_task_if_running


def test_legacy_rotary_frequency_is_derived_but_other_state_stays_strict():
    block = nn.Linear(2, 2, bias=False)
    block._keys_to_ignore_on_load_unexpected = [r"^known_decoy\."]
    state_dict = {
        "weight": torch.ones(2, 2),
        "self_attn.rotary_emb.inv_freq": torch.ones(2),
        "self_attention.rotary_emb.original_inv_freq": torch.ones(2),
        "known_decoy.weight": torch.ones(1),
        "self_attn.rotary_emb.trained_scale": torch.ones(1),
        "mystery_scale": torch.ones(1),
    }

    assert _find_unconsumed_checkpoint_keys(block, state_dict) == [
        "mystery_scale",
        "self_attn.rotary_emb.trained_scale",
    ]


def test_finegrained_fp8_checkpoint_weights_dequantize_from_their_scale_grid():
    fp8_dtype = getattr(torch, "float8_e4m3fn", None)
    if fp8_dtype is None:
        pytest.skip("torch build has no float8_e4m3fn dtype")
    state_dict = {
        "proj.weight": torch.ones(4, 4, dtype=fp8_dtype),
        "proj.weight_scale_inv": torch.tensor([[2.0, 4.0], [0.5, 1.0]]),
        "norm.weight": torch.arange(4, dtype=torch.bfloat16),
    }

    loaded = dequantize_finegrained_fp8_state_dict(state_dict, output_dtype=torch.bfloat16)

    expected = torch.tensor(
        [[2.0, 2.0, 4.0, 4.0], [2.0, 2.0, 4.0, 4.0], [0.5, 0.5, 1.0, 1.0], [0.5, 0.5, 1.0, 1.0]],
        dtype=torch.bfloat16,
    )
    assert torch.equal(loaded["proj.weight"], expected)
    assert loaded["proj.weight"].dtype == torch.bfloat16
    assert torch.equal(loaded["norm.weight"], state_dict["norm.weight"])
    assert "proj.weight_scale_inv" not in loaded


def test_finegrained_fp8_checkpoint_rejects_an_orphan_scale():
    with pytest.raises(ValueError, match="has no matching"):
        dequantize_finegrained_fp8_state_dict({"proj.weight_scale_inv": torch.ones(1, 1)}, output_dtype=torch.bfloat16)


def test_safetensors_block_load_copies_selected_tensors_out_of_the_file_mapping(monkeypatch):
    selected = torch.arange(4, dtype=torch.float32)
    unrelated = torch.ones(2)

    class FakeSafeOpen:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return None

        def keys(self):
            return ("layers.0.weight", "layers.1.weight")

        def get_tensor(self, key):
            return selected if key == "layers.0.weight" else unrelated

    monkeypatch.setattr(
        "drift.server.from_pretrained.safetensors.safe_open",
        lambda *args, **kwargs: FakeSafeOpen(),
    )

    loaded = _load_state_dict_from_local_file("model.safetensors", block_prefix="layers.0.")

    assert set(loaded) == {"layers.0.weight"}
    assert torch.equal(loaded["layers.0.weight"], selected)
    assert loaded["layers.0.weight"].data_ptr() != selected.data_ptr()
    selected.add_(100)
    assert torch.equal(loaded["layers.0.weight"], torch.arange(4, dtype=torch.float32))


def test_chunked_lm_head_warning_reports_actual_dtype_once(monkeypatch):
    config = SimpleNamespace(
        vocab_size=8,
        hidden_size=4,
        use_chunked_forward=True,
        chunked_forward_step=4,
    )
    head = LMHead(config).to(dtype=torch.float16)
    warnings = []
    monkeypatch.setattr("drift.client.lm_head.logger.warning", warnings.append)

    hidden_states = torch.ones(1, 1, config.hidden_size, dtype=torch.float16)
    head(hidden_states)
    head(hidden_states)

    assert len(warnings) == 1
    assert "float16 weights on CPU" in warnings[0]
    assert "bfloat16" not in warnings[0]


def test_hivemind_cleanup_does_not_query_a_closed_global_loop():
    class ClosedLoop:
        def is_closed(self):
            return True

        def is_running(self):
            raise AssertionError("a closed loop must not be queried further")

    class PendingTask:
        def done(self):
            return False

        def get_loop(self):
            return ClosedLoop()

        def cancel(self):
            raise AssertionError("a task on a closed loop must not be cancelled")

    safe_cancel_task_if_running(PendingTask())


def test_hivemind_cleanup_patch_reaches_imported_call_sites():
    import hivemind.p2p.p2p_daemon as p2p_daemon
    import hivemind.p2p.p2p_daemon_bindings.control as control
    import hivemind.utils.asyncio as hivemind_asyncio

    patch_hivemind_task_cleanup()

    assert hivemind_asyncio.cancel_task_if_running is safe_cancel_task_if_running
    assert p2p_daemon.cancel_task_if_running is safe_cancel_task_if_running
    assert control.cancel_task_if_running is safe_cancel_task_if_running
