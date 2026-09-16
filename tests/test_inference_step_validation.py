"""Real serializer/iterator regressions; backend execution is a recording CPU double."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
import torch
from hivemind.compression import deserialize_torch_tensor, serialize_torch_tensor
from hivemind.proto import runtime_pb2
from hivemind.utils.serializer import MSGPackSerializer

from drift.server import block_functions
from drift.server.admission import AdmissionRejected
from drift.server.task_prioritizer import DummyTaskPrioritizer
from drift.utils.convert_block import QuantType


class RecordingPool:
    def __init__(self):
        self.positions = []
        self.max_batch_size = 512

    async def submit_task(self, hidden_states, hypo_ids, infos, *prompts, priority):
        self.positions.append(tuple(info.prefix_length for info in infos))
        return (hidden_states,)


def make_iterator(steps, *, max_length=512):
    pools = [RecordingPool(), RecordingPool()]
    backends = [
        SimpleNamespace(
            dtype=torch.float32,
            config=SimpleNamespace(hidden_size=4),
            inference_pool=pool,
            donor_layer_types=[],
            outputs_schema=(SimpleNamespace(dtype=torch.float32, compression=runtime_pb2.CompressionType.NONE),),
        )
        for pool in pools
    ]

    async def inputs():
        for length, metadata in steps:
            tensors = (torch.ones(1, length, 4), torch.empty(0), torch.empty(0, dtype=torch.int64))
            request = runtime_pb2.ExpertRequest(
                uid="test.0 test.1",
                tensors=[serialize_torch_tensor(tensor, runtime_pb2.CompressionType.NONE) for tensor in tensors],
                metadata=MSGPackSerializer.dumps(metadata),
            )
            yield request, MSGPackSerializer.loads(request.metadata)

    iterator = block_functions.iterate_rpc_inference(
        requested_uids=("test.0", "test.1"),
        requested_backends=backends,
        active_adapter=None,
        input_iterator=inputs(),
        cache_handles=((0,), (1,)),
        max_length=max_length,
        session_batch_size=1,
        prioritizer=DummyTaskPrioritizer(),
        points=0,
        quant_type=QuantType.NONE,
    )
    return iterator, pools


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [-1, -(2**63), True, False, 0.0, 0.5, float("nan"), float("inf"), float("-inf"), "0", None, [], {}, 2**63 - 1],
)
@pytest.mark.parametrize("after_prefill", [False, True])
async def test_invalid_rewind_rejected_before_decode_or_backend(value, after_prefill):
    steps = ([(3, {})] if after_prefill else []) + [(1, {"start_from_position": value})]
    iterator, pools = make_iterator(steps)
    try:
        if after_prefill:
            await iterator.__anext__()
        before = [list(pool.positions) for pool in pools]
        with patch.object(block_functions, "deserialize_torch_tensor", wraps=deserialize_torch_tensor) as decode:
            with pytest.raises(AdmissionRejected, match="inference cache position is invalid"):
                await iterator.__anext__()
        assert decode.call_count == 0
        assert [pool.positions for pool in pools] == before
    finally:
        await iterator.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("length", [1, 129])
async def test_valid_rewind_preserves_merged_and_separate_backend_positions(length):
    steps = [(length, {}), (1, {"start_from_position": length}), (1, {"start_from_position": 0}), (1, {})]
    iterator, pools = make_iterator(steps)
    outputs = [output async for output in iterator]
    assert len(outputs) == 4
    for (tensors, _, _, _), (expected_length, _) in zip(outputs, steps):
        assert torch.equal(deserialize_torch_tensor(tensors[0]), torch.ones(1, expected_length, 4))
    if length == 1:
        assert pools[0].positions == [(0, 0), (1, 1), (0, 0), (1, 1)]
        assert pools[1].positions == []
    else:
        assert pools[0].positions == [(0,), (129, 129), (0, 0), (1, 1)]
        assert pools[1].positions == [(0,)]


@pytest.mark.asyncio
async def test_zero_token_preallocation_and_rewind_do_not_execute_backends():
    iterator, pools = make_iterator([(0, {"start_from_position": 0}), (0, {}), (1, {})])
    outputs = [output async for output in iterator]
    assert len(outputs) == 3
    assert pools[0].positions == [(0, 0)]
    assert pools[1].positions == []


@pytest.mark.asyncio
async def test_rewind_cannot_return_to_discarded_prefix_or_bypass_max_length():
    iterator, pools = make_iterator([(3, {}), (1, {"start_from_position": 0}), (1, {"start_from_position": 2})])
    try:
        await iterator.__anext__()
        await iterator.__anext__()
        with pytest.raises(AdmissionRejected, match="inference cache position is invalid"):
            await iterator.__anext__()
        assert pools[0].positions == [(0, 0), (0, 0)]
    finally:
        await iterator.aclose()

    iterator, pools = make_iterator([(2, {}), (2, {"start_from_position": 2})], max_length=3)
    try:
        await iterator.__anext__()
        with pytest.raises(AdmissionRejected, match="inference activation shape exceeds the session limits"):
            await iterator.__anext__()
        assert pools[0].positions == [(0, 0)]
    finally:
        await iterator.aclose()
