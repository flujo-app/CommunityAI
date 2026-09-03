from types import SimpleNamespace

import pytest
import torch

from drift.server.block_utils import get_block_size
from drift.server.server import Server, parse_block_indices
from drift.utils.convert_block import QuantType


def _budget_server(*, tensor_parallel_devices=(torch.device("cuda:0"),)):
    server = Server.__new__(Server)
    server.device = tensor_parallel_devices[0]
    server.tensor_parallel_devices = tensor_parallel_devices
    server.device_memory_limits = None
    server.block_config = SimpleNamespace(hidden_size=0, num_hidden_layers=3)
    server._block_memory_bytes_by_block = (100, 200, 300)
    server._adapter_memory_per_block = 10
    return server


def test_memory_estimator_uses_exact_range_and_worst_movable_layers():
    server = _budget_server()

    assert server._estimate_device_memory(2, range(0, 2)) == 320
    assert server._estimate_device_memory(2) == 520


def test_automatic_block_count_respects_effective_memory(monkeypatch):
    server = _budget_server()
    monkeypatch.setattr("drift.server.server.get_device_total_memory", lambda device: 1_000)
    server.device_memory_limits = (515,)

    assert server._choose_num_blocks() == 1

    server.device_memory_limits = (520,)
    assert server._choose_num_blocks() == 2


def test_tensor_parallel_budget_is_per_accelerator(monkeypatch):
    server = _budget_server(tensor_parallel_devices=(torch.device("cuda:0"), torch.device("cuda:1")))
    server.device_memory_limits = (260, 300)
    monkeypatch.setattr("drift.server.server.get_device_total_memory", lambda device: 1_000)

    assert server._choose_num_blocks() == 2


@pytest.mark.parametrize("value", ["-1:2", "0:0", "2:1", "0:4", "0:1:2", "bad"])
def test_block_range_parser_rejects_invalid_or_out_of_bounds_ranges(value):
    with pytest.raises(ValueError, match="block_indices"):
        parse_block_indices(value, 3)


def test_block_range_parser_accepts_exact_bounded_range():
    assert list(parse_block_indices("1:3", 3)) == [1, 2]


def test_block_size_uses_the_requested_hybrid_layer_geometry():
    class VariableBlock(torch.nn.Module):
        def __init__(self, config, layer_idx=0):
            super().__init__()
            self.weight = torch.nn.Parameter(torch.empty(layer_idx + 1))

    config = SimpleNamespace(
        block_class=VariableBlock,
        torch_dtype=torch.float32,
    )

    first = get_block_size(
        config,
        "memory",
        dtype=torch.float32,
        quant_type=QuantType.NONE,
        layer_idx=0,
        eps=0,
    )
    third = get_block_size(
        config,
        "memory",
        dtype=torch.float32,
        quant_type=QuantType.NONE,
        layer_idx=2,
        eps=0,
    )

    assert first == 4
    assert third == 12


def test_fp8_dequant_memory_budget_uses_execution_dtype():
    class Block(torch.nn.Module):
        def __init__(self, config, layer_idx=0):
            super().__init__()
            self.weight = torch.nn.Parameter(torch.empty(16))

    config = SimpleNamespace(block_class=Block, torch_dtype=torch.bfloat16)

    assert (
        get_block_size(
            config,
            "memory",
            dtype=torch.bfloat16,
            quant_type=QuantType.FP8_DEQUANT,
            eps=0,
        )
        == 32
    )
