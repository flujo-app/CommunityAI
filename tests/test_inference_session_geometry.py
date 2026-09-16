"""Session geometry through the real handler and wire codec, with recording CPU workers."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import torch
from hivemind.compression import deserialize_torch_tensor, serialize_torch_tensor
from hivemind.proto import runtime_pb2
from hivemind.utils.serializer import MSGPackSerializer

from drift.server import block_functions
from drift.server.admission import AdmissionRejected
from drift.server.handler import TransformerConnectionHandler
from drift.server.task_prioritizer import TaskPrioritizerBase
from drift.utils.convert_block import QuantType
from drift.utils.packaging import pack_args_kwargs


class RecordingPool:
    def __init__(self, max_batch_size):
        self.max_batch_size = max_batch_size
        self.calls = []

    async def submit_task(self, hidden_states, hypo_ids, infos, *prompts, priority):
        self.calls.append((tuple(hidden_states.shape), tuple(info.prefix_length for info in infos)))
        return (hidden_states,)


class RecordingPrioritizer(TaskPrioritizerBase):
    def __init__(self):
        self.calls = []

    def prioritize(self, *inputs, **kwargs):
        self.calls.append(tuple(inputs[0].shape))
        return 7.0


class RecordingCache:
    paged = False

    def __init__(self, events):
        self.events = events
        self.reservations = []

    def descriptors(self, batch_size, max_length):
        self.reservations.append((batch_size, max_length))
        return ("cache-descriptor",)

    @asynccontextmanager
    async def allocate_cache(self, *descriptors, timeout):
        self.events.append("cache_acquire")
        try:
            yield tuple(range(len(descriptors)))
        finally:
            self.events.append("cache_release")


def wire_request(shape, *, max_length=16, packed=False, metadata=None):
    tensors = (torch.ones(shape), torch.empty(0), torch.empty(0, dtype=torch.int64))
    request_metadata = {"max_length": max_length, **(metadata or {})}
    if packed:
        tensors, request_metadata["args_structure"] = pack_args_kwargs(*tensors)
    return runtime_pb2.ExpertRequest(
        uid="test.0 test.1",
        tensors=[serialize_torch_tensor(tensor, runtime_pb2.CompressionType.NONE) for tensor in tensors],
        metadata=MSGPackSerializer.dumps(request_metadata),
    )


def redirected_request(shape, *, max_length=16, logical_constant=...):
    """Keep tensor zero admissible while packed args select tensor one or a constant."""
    tensors = (torch.ones(2, 1, 4), torch.ones(shape), torch.empty(0), torch.empty(0, dtype=torch.int64))
    activation = b"__T1" if logical_constant is ... else logical_constant
    return runtime_pb2.ExpertRequest(
        uid="test.0 test.1",
        tensors=[serialize_torch_tensor(tensor, runtime_pb2.CompressionType.NONE) for tensor in tensors],
        metadata=MSGPackSerializer.dumps(
            {"max_length": max_length, "args_structure": [[activation, b"__T2", b"__T3"], {}]}
        ),
    )


def make_session(requests, *, pool_limits=(64, 16)):
    """Use actual handler methods, including stream iteration and cache allocation."""
    events = []
    cache = RecordingCache(events)
    pools = [RecordingPool(limit) for limit in pool_limits]
    prioritizer = RecordingPrioritizer()
    handler = object.__new__(TransformerConnectionHandler)
    handler.module_backends = {
        f"test.{index}": SimpleNamespace(
            config=SimpleNamespace(hidden_size=4),
            dtype=torch.float32,
            inference_pool=pool,
            donor_layer_types=[],
            memory_cache=cache,
            get_inference_cache_descriptors=cache.descriptors,
            outputs_schema=(SimpleNamespace(dtype=torch.float32, compression=runtime_pb2.CompressionType.NONE),),
        )
        for index, pool in enumerate(pools)
    }

    def acquire(remote_id):
        events.append("lease_acquire")
        return SimpleNamespace(release=lambda: events.append("lease_release"))

    handler._admission_state = SimpleNamespace(acquire=acquire)
    handler.step_timeout = 2.0
    handler.session_timeout = 10.0
    handler.inference_max_length = 256
    handler.manifest_digest = None
    handler.adapters = ()
    handler._prioritizer = prioritizer
    handler.quant_type = QuantType.NONE
    handler._log_request = lambda *args, **kwargs: None
    handler._create_output_push_task = lambda *args, **kwargs: None

    async def incoming():
        for request in requests:
            yield request
        yield runtime_pb2.ExpertRequest()  # The client's explicit stream-closing sentinel.

    return SimpleNamespace(
        iterator=handler.rpc_inference(incoming(), SimpleNamespace(remote_id="fixture-peer")),
        events=events,
        cache=cache,
        pools=pools,
        prioritizer=prioritizer,
    )


def assert_released(session, *, max_length):
    assert session.events == ["lease_acquire", "cache_acquire", "cache_release", "lease_release"]
    assert session.cache.reservations == [(2, max_length), (2, max_length)]


BAD_CONTINUATION_SHAPES = [
    pytest.param((1, 1, 4), id="batch-shrink"),
    pytest.param((3, 1, 4), id="batch-growth"),
    pytest.param((0, 1, 4), id="zero-batch"),
    pytest.param((2, 1, 3), id="narrow-hidden"),
    pytest.param((2, 1, 5), id="wide-hidden"),
    pytest.param((2, 1, 0), id="empty-hidden"),
    pytest.param((), id="scalar"),
    pytest.param((2, 4), id="rank-two"),
    pytest.param((2, 1, 4, 1), id="rank-four"),
    pytest.param((2, 9, 4), id="minimum-pool-token-limit"),
    pytest.param((1, 0, 4), id="zero-token-batch-shrink"),
    pytest.param((3, 0, 4), id="zero-token-batch-growth"),
    pytest.param((2, 0, 5), id="zero-token-hidden-mismatch"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", BAD_CONTINUATION_SHAPES)
@pytest.mark.parametrize("packed", [False, True], ids=["flat", "packed"])
async def test_bad_continuation_header_rejects_before_decode_and_releases_session(shape, packed):
    session = make_session([wire_request((2, 1, 4), packed=packed), wire_request(shape, packed=packed)])
    try:
        await session.iterator.__anext__()
        previous_calls = [list(pool.calls) for pool in session.pools]
        previous_priorities = list(session.prioritizer.calls)
        with patch.object(block_functions, "deserialize_torch_tensor", wraps=deserialize_torch_tensor) as decode:
            with pytest.raises(AdmissionRejected):
                await session.iterator.__anext__()
        assert decode.call_count == 0
        assert [pool.calls for pool in session.pools] == previous_calls
        assert session.prioritizer.calls == previous_priorities
        assert_released(session, max_length=16)
    finally:
        await session.iterator.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("packed", [False, True], ids=["flat", "packed"])
async def test_continuation_prefix_overflow_rejects_before_decode_and_releases_session(packed):
    session = make_session(
        [wire_request((2, 3, 4), max_length=4, packed=packed), wire_request((2, 2, 4), max_length=4, packed=packed)]
    )
    try:
        await session.iterator.__anext__()
        with patch.object(block_functions, "deserialize_torch_tensor", wraps=deserialize_torch_tensor) as decode:
            with pytest.raises(AdmissionRejected):
                await session.iterator.__anext__()
        assert decode.call_count == 0
        assert session.pools[0].calls == [((2, 3, 4), (0, 0))]
        assert session.pools[1].calls == []
        assert session.prioritizer.calls == [(2, 3, 4)]
        assert_released(session, max_length=4)
    finally:
        await session.iterator.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "shape,first_shape,max_length",
    [
        pytest.param((3, 1, 4), (2, 1, 4), 16, id="batch-growth"),
        pytest.param((2, 1, 5), (2, 1, 4), 16, id="hidden-width"),
        pytest.param((2, 9, 4), (2, 1, 4), 16, id="pool-token-limit"),
        pytest.param((2, 4), (2, 1, 4), 16, id="rank-two"),
        pytest.param((1, 0, 4), (2, 1, 4), 16, id="zero-token-batch"),
        pytest.param((2, 2, 4), (2, 3, 4), 4, id="prefix-limit"),
    ],
)
async def test_unpacked_activation_cannot_bypass_valid_header_geometry(shape, first_shape, max_length):
    session = make_session(
        [redirected_request(first_shape, max_length=max_length), redirected_request(shape, max_length=max_length)]
    )
    try:
        await session.iterator.__anext__()
        previous_calls = [list(pool.calls) for pool in session.pools]
        previous_priorities = list(session.prioritizer.calls)
        with patch.object(block_functions, "deserialize_torch_tensor", wraps=deserialize_torch_tensor) as decode:
            with pytest.raises(AdmissionRejected):
                await session.iterator.__anext__()
        assert decode.call_count == 0  # Resolve the effective packed tensor's header before decoding.
        assert [pool.calls for pool in session.pools] == previous_calls
        assert session.prioritizer.calls == previous_priorities
        assert_released(session, max_length=max_length)
    finally:
        await session.iterator.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, 7, True], ids=["none", "integer", "boolean"])
async def test_non_tensor_unpacked_activation_rejects_and_releases_session(value):
    session = make_session([redirected_request((2, 1, 4), logical_constant=value)])
    try:
        with pytest.raises(AdmissionRejected):
            await session.iterator.__anext__()
        assert [pool.calls for pool in session.pools] == [[], []]
        assert session.prioritizer.calls == []
        assert_released(session, max_length=16)
    finally:
        await session.iterator.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("packed", [False, True], ids=["flat", "packed"])
async def test_injected_decoder_shape_disagreement_rejects_before_dispatch(packed):
    """A decoder double isolates disagreement with the validated protobuf shape."""
    session = make_session([wire_request((2, 1, 4), packed=packed), wire_request((2, 2, 4), packed=packed)])

    def decode_with_wrong_length(tensor):
        decoded = deserialize_torch_tensor(tensor)
        return decoded[:, :1, :] if tuple(tensor.size) == (2, 2, 4) else decoded

    try:
        await session.iterator.__anext__()
        with patch.object(block_functions, "deserialize_torch_tensor", side_effect=decode_with_wrong_length) as decode:
            with pytest.raises(AdmissionRejected):
                await session.iterator.__anext__()
        assert decode.call_count > 0
        assert session.pools[0].calls == [((2, 1, 4), (0, 0))]
        assert session.pools[1].calls == []
        assert session.prioritizer.calls == [(2, 1, 4)]
        assert_released(session, max_length=16)
    finally:
        await session.iterator.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("packed", [False, True], ids=["flat", "packed"])
@pytest.mark.parametrize(
    "steps,max_length,pool_limits,expected_positions",
    [
        pytest.param([(0, {}), (1, {})], 4, (64, 16), [[(0, 0)], []], id="zero-preallocation"),
        pytest.param([(2, {}), (1, {})], 4, (64, 16), [[(0, 0), (2, 2)], []], id="merged"),
        pytest.param([(65, {}), (1, {})], 80, (256, 192), [[(0,), (65, 65)], [(0,)]], id="separate-then-merged"),
        pytest.param(
            [(3, {}), (2, {"start_from_position": 1}), (1, {})],
            4,
            (64, 16),
            [[(0, 0), (1, 1), (3, 3)], []],
            id="rewind-within-budget",
        ),
        pytest.param([(3, {}), (2, {})], 5, (64, 16), [[(0, 0), (3, 3)], []], id="exact-prefix-limit"),
        pytest.param(
            [(4, {}), (1, {"start_from_position": 3})],
            4,
            (64, 16),
            [[(0, 0), (3, 3)], []],
            id="rewind-from-full-cache",
        ),
        pytest.param([(8, {}), (8, {})], 16, (64, 16), [[(0, 0), (8, 8)], []], id="exact-pool-limit"),
        pytest.param(
            [(4, {}), (0, {}), (0, {"start_from_position": 0}), (1, {})],
            4,
            (64, 16),
            [[(0, 0), (0, 0)], []],
            id="zero-token-at-full-cache-and-rewind",
        ),
    ],
)
async def test_valid_session_geometry_preserves_results_positions_and_cleanup(
    steps, max_length, pool_limits, expected_positions, packed
):
    session = make_session(
        [
            wire_request((2, length, 4), max_length=max_length, packed=packed, metadata=metadata)
            for length, metadata in steps
        ],
        pool_limits=pool_limits,
    )
    try:
        responses = [response async for response in session.iterator]
        assert len(responses) == len(steps)
        for response, (length, _) in zip(responses, steps):
            assert len(response.tensors) == 1
            assert torch.equal(deserialize_torch_tensor(response.tensors[0]), torch.ones(2, length, 4))
        assert [[call[1] for call in pool.calls] for pool in session.pools] == expected_positions
        assert session.prioritizer.calls == [(2, length, 4) for length, _ in steps]
        assert_released(session, max_length=max_length)
    finally:
        await session.iterator.aclose()
