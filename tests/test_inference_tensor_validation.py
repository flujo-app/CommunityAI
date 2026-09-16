"""Inference index contract through the real wire codec and CPU recording workers."""

from types import SimpleNamespace

import pytest
import torch
from hivemind.compression import deserialize_torch_tensor, serialize_torch_tensor
from hivemind.proto import runtime_pb2
from hivemind.utils.serializer import MSGPackSerializer

from drift.server.admission import AdmissionRejected
from drift.server.block_functions import iterate_rpc_inference
from drift.server.task_prioritizer import TaskPrioritizerBase
from drift.utils.convert_block import QuantType
from drift.utils.packaging import pack_args_kwargs


class RecordingPool:
    def __init__(self):
        self.calls = []

    async def submit_task(self, hidden_states, hypo_ids, infos, *prompts, priority):
        self.calls.append(
            (tuple(hidden_states.shape), hypo_ids.clone(), tuple(info.prefix_length for info in infos), priority)
        )
        return (hidden_states,)


class RecordingPrioritizer(TaskPrioritizerBase):
    def __init__(self):
        self.calls = []

    def prioritize(self, *inputs, points=0.0, **kwargs):
        self.calls.append((tuple(inputs[0].shape), inputs[1].clone(), kwargs["type"]))
        return 7.0


def make_iterator(steps, *, structured_args=False):
    """Use three cache rows and two blocks, with no real model or hardware dependency."""
    pools = [RecordingPool(), RecordingPool()]
    backends = [
        SimpleNamespace(
            dtype=torch.float32,
            inference_pool=pool,
            donor_layer_types=[],
            outputs_schema=(SimpleNamespace(dtype=torch.float32, compression=runtime_pb2.CompressionType.NONE),),
        )
        for pool in pools
    ]
    requests = []
    args_structure = None
    for length, hypo_ids in steps:
        tensors = (torch.ones(3, length, 4), torch.empty(0), hypo_ids)
        metadata = {}
        if structured_args:
            tensors, args_structure = pack_args_kwargs(*tensors)
            metadata["args_structure"] = args_structure
        request = runtime_pb2.ExpertRequest(
            uid="test.0 test.1",
            tensors=[serialize_torch_tensor(tensor, runtime_pb2.CompressionType.NONE) for tensor in tensors],
            metadata=MSGPackSerializer.dumps(metadata),
        )
        requests.append(request)

    # Session argument structure takes the same MSGPack round trip as handler metadata.
    args_structure = MSGPackSerializer.loads(requests[0].metadata).get("args_structure")

    async def inputs():
        for request in requests:
            yield request, MSGPackSerializer.loads(request.metadata)

    prioritizer = RecordingPrioritizer()
    iterator = iterate_rpc_inference(
        requested_uids=("test.0", "test.1"),
        requested_backends=backends,
        active_adapter=None,
        input_iterator=inputs(),
        cache_handles=((0,), (1,)),
        max_length=256,
        prioritizer=prioritizer,
        points=0,
        quant_type=QuantType.NONE,
        args_structure=args_structure,
    )
    return iterator, pools, prioritizer


INVALID_HYPOTHESES = [
    pytest.param(torch.tensor([0.0, 1.0, 2.0]), id="float32"),
    pytest.param(torch.tensor([0, 1, 2], dtype=torch.int32), id="int32"),
    pytest.param(torch.tensor([False, True, True]), id="boolean"),
    pytest.param(torch.empty(0), id="empty-wrong-dtype"),
    pytest.param(torch.tensor(0, dtype=torch.int64), id="scalar"),
    pytest.param(torch.tensor([[0, 1, 2]], dtype=torch.int64), id="matrix"),
    pytest.param(torch.empty((1, 0), dtype=torch.int64), id="empty-matrix"),
    pytest.param(torch.tensor([0, 1], dtype=torch.int64), id="too-few"),
    pytest.param(torch.tensor([0, 1, 2, 0], dtype=torch.int64), id="too-many"),
    pytest.param(torch.tensor([0, -1, 2], dtype=torch.int64), id="negative"),
    pytest.param(torch.tensor([0, 1, 3], dtype=torch.int64), id="upper-bound"),
    pytest.param(torch.tensor([0, 1, -(2**63)], dtype=torch.int64), id="minimum-int64"),
    pytest.param(torch.tensor([0, 1, 2**63 - 1], dtype=torch.int64), id="maximum-int64"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("hypo_ids", INVALID_HYPOTHESES)
@pytest.mark.parametrize("length", [0, 1, 43], ids=["zero-token", "merged", "separate"])
@pytest.mark.parametrize("after_prefill", [False, True], ids=["initial", "continuation"])
@pytest.mark.parametrize("structured_args", [False, True], ids=["flat", "packed"])
async def test_invalid_hypotheses_rejected_before_prioritization_or_dispatch(
    hypo_ids, length, after_prefill, structured_args
):
    steps = ([(1, torch.empty(0, dtype=torch.int64))] if after_prefill else []) + [(length, hypo_ids)]
    iterator, pools, prioritizer = make_iterator(steps, structured_args=structured_args)
    try:
        if after_prefill:
            await iterator.__anext__()
        pool_calls_before = [len(pool.calls) for pool in pools]
        priority_calls_before = len(prioritizer.calls)
        with pytest.raises(AdmissionRejected):
            await iterator.__anext__()
        assert [len(pool.calls) for pool in pools] == pool_calls_before
        assert len(prioritizer.calls) == priority_calls_before
    finally:
        await iterator.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hypo_ids",
    [
        pytest.param(torch.empty(0, dtype=torch.int64), id="dummy"),
        pytest.param(torch.tensor([0, 1, 2], dtype=torch.int64), id="identity"),
        pytest.param(torch.tensor([2, 0, 1], dtype=torch.int64), id="permutation"),
        pytest.param(torch.tensor([1, 1, 0], dtype=torch.int64), id="duplicates"),
    ],
)
@pytest.mark.parametrize("length", [0, 1, 43], ids=["zero-token", "merged", "separate"])
@pytest.mark.parametrize("structured_args", [False, True], ids=["flat", "packed"])
async def test_valid_hypotheses_preserve_dispatch_and_output(hypo_ids, length, structured_args):
    iterator, pools, prioritizer = make_iterator(
        [(length, hypo_ids), (length, hypo_ids)], structured_args=structured_args
    )
    try:
        outputs = [output async for output in iterator]
    finally:
        await iterator.aclose()

    assert len(outputs) == 2
    for tensors, can_push, _, response_metadata in outputs:
        assert torch.equal(deserialize_torch_tensor(tensors[0]), torch.ones(3, length, 4))
        assert can_push is True
        assert response_metadata == {}
    assert len(prioritizer.calls) == 2
    for shape, received_ids, operation in prioritizer.calls:
        assert shape == (3, length, 4)
        assert torch.equal(received_ids, hypo_ids)
        assert operation == "inference"

    if length == 0:
        assert [len(pool.calls) for pool in pools] == [0, 0]
    elif length == 1:
        assert [len(pool.calls) for pool in pools] == [2, 0]
        assert [call[2] for call in pools[0].calls] == [(0, 0), (1, 1)]
    else:
        assert [len(pool.calls) for pool in pools] == [2, 2]
        for pool in pools:
            assert [call[2] for call in pool.calls] == [(0,), (43,)]
    for pool in pools:
        for shape, received_ids, _, priority in pool.calls:
            assert shape == (3, length, 4)
            assert torch.equal(received_ids, hypo_ids)
            assert priority == 7.0
