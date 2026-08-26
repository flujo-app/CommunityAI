import asyncio
import threading
from types import SimpleNamespace

import pytest
import torch
from hivemind.compression import deserialize_torch_tensor, serialize_torch_tensor
from hivemind.dht.node import Blacklist
from hivemind.moe.client.remote_expert_worker import RemoteExpertWorker
from hivemind.proto import runtime_pb2
from hivemind.utils.serializer import MSGPackSerializer

from drift.client.config import ClientConfig
from drift.client.inference_session import InferenceSession, _ServerInferenceSession
from drift.client.routing.sequence_info import RemoteSequenceInfo
from drift.client.routing.sequence_manager import RemoteSequenceManager, SequenceManagerState
from drift.data_structures import RemoteModuleInfo, RemoteSpanInfo, ServerInfo, ServerState
from drift.utils.misc import DUMMY, DUMMY_INT64, is_dummy


def _span(peer_id: str, start: int, end: int) -> RemoteSpanInfo:
    return RemoteSpanInfo(peer_id, start, end, ServerInfo(ServerState.ONLINE, throughput=1.0))


def _rpc_info():
    tensor_schema = SimpleNamespace(compression=runtime_pb2.CompressionType.NONE)
    return {"inference_schema": ((tensor_schema, tensor_schema, tensor_schema), {})}


def test_request_failure_immediately_removes_stale_peer_from_routes():
    failed_peer = "failed"
    spare_peer = "spare"
    online = ServerInfo(ServerState.ONLINE, throughput=1.0)
    sequence_info = RemoteSequenceInfo.make_empty(["model.0", "model.1"])
    sequence_info.update_(
        [
            RemoteModuleInfo(f"model.{block_index}", {failed_peer: online, spare_peer: online})
            for block_index in range(2)
        ]
    )

    manager = object.__new__(RemoteSequenceManager)
    manager.state = SequenceManagerState(
        sequence_info=sequence_info,
        banned_peers=Blacklist(base_time=15, backoff_rate=2.0),
    )
    manager.lock_changes = threading.Lock()

    manager.on_request_failure(failed_peer)

    assert failed_peer in manager.state.banned_peers
    assert all(failed_peer not in info.servers for info in sequence_info.block_infos)
    assert all([span.peer_id for span in spans] == [spare_peer] for spans in sequence_info.spans_containing_block)


@pytest.fixture
def coroutine_runner(monkeypatch):
    try:
        previous_loop = asyncio.get_event_loop()
    except RuntimeError:
        previous_loop = None
    test_loop = asyncio.new_event_loop()
    monkeypatch.setattr(RemoteExpertWorker, "run_coroutine", test_loop.run_until_complete)
    try:
        yield
    finally:
        test_loop.close()
        asyncio.set_event_loop(previous_loop)


def test_server_session_replays_full_prefix_from_position_zero(coroutine_runner):
    session = _ServerInferenceSession(
        ClientConfig(replay_chunk_size=3),
        _span("replacement", 0, 2),
        "model.0 model.1",
        _rpc_info(),
        inputs_queue=None,
        outputs_aiter=None,
        max_length=8,
    )
    requests = []

    async def _echo_step(request):
        requests.append(request)
        session.stepped = True  # mirrors _ServerInferenceSession._step()
        hidden_states = deserialize_torch_tensor(request.tensors[0])
        return runtime_pb2.ExpertResponse(
            tensors=[serialize_torch_tensor(hidden_states + 1, runtime_pb2.CompressionType.NONE)]
        )

    session._step = _echo_step

    prefix = torch.arange(9, dtype=torch.float32).view(1, 3, 3)
    current = torch.full((1, 1, 3), 10.0)
    per_layer_prefix = torch.arange(12, dtype=torch.float32).view(2, 1, 3, 2)
    per_layer_current = torch.full((2, 1, 1, 2), 20.0)
    replay_prompts = torch.full((2, 1, 1, 3), 30.0)

    session.prepare_for_replay(
        history=prefix,
        per_layer_history=per_layer_prefix,
        prompts=replay_prompts,
        target_position=4,
    )
    outputs, _ = session.step(
        current,
        DUMMY,
        DUMMY_INT64,
        step_id="replayed-step",
        per_layer_inputs=per_layer_current,
    )

    replay_metadata = [MSGPackSerializer.loads(request.metadata) for request in requests]
    replay_tensors = [[deserialize_torch_tensor(tensor) for tensor in request.tensors] for request in requests]
    expected_history = torch.cat([prefix, current], dim=1)
    expected_per_layer_history = torch.cat([per_layer_prefix, per_layer_current], dim=2)

    assert [metadata["start_from_position"] for metadata in replay_metadata] == [0, 3]
    assert [tensors[0].shape[1] for tensors in replay_tensors] == [3, 1]
    assert torch.equal(torch.cat([tensors[0] for tensors in replay_tensors], dim=1), expected_history)
    assert all(torch.equal(tensors[1], replay_prompts) for tensors in replay_tensors)
    assert torch.equal(torch.cat([tensors[3] for tensors in replay_tensors], dim=2), expected_per_layer_history)
    assert torch.equal(outputs, expected_history + 1)
    assert session.position == 4
    assert not session.needs_replay

    next_inputs = torch.full((1, 1, 3), 40.0)
    next_per_layer = torch.full((2, 1, 1, 2), 50.0)
    session.step(
        next_inputs,
        DUMMY,
        DUMMY_INT64,
        step_id="ordinary-step",
        per_layer_inputs=next_per_layer,
    )
    next_metadata = MSGPackSerializer.loads(requests[2].metadata)
    next_tensors = [deserialize_torch_tensor(tensor) for tensor in requests[2].tensors]

    assert next_metadata["start_from_position"] == 4
    assert next_tensors[0].shape[1] == 1
    assert next_tensors[3].shape[2] == 1
    assert session.position == 5
    assert session.history.shape[1] == 5
    assert session.per_layer_history.shape[2] == 5


def test_server_session_preserves_full_prefix_when_chunked_replay_fails(coroutine_runner):
    session = _ServerInferenceSession(
        ClientConfig(replay_chunk_size=2),
        _span("replacement", 0, 2),
        "model.0 model.1",
        _rpc_info(),
        inputs_queue=None,
        outputs_aiter=None,
        max_length=8,
    )
    requests = []

    async def _fail_second_chunk(request):
        requests.append(request)
        session.stepped = True  # mirrors _ServerInferenceSession._step()
        if len(requests) == 2:
            raise EOFError("replacement disappeared during replay")
        hidden_states = deserialize_torch_tensor(request.tensors[0])
        return runtime_pb2.ExpertResponse(
            tensors=[serialize_torch_tensor(hidden_states + 1, runtime_pb2.CompressionType.NONE)]
        )

    session._step = _fail_second_chunk

    prefix = torch.arange(12, dtype=torch.float32).view(1, 4, 3)
    current = torch.full((1, 1, 3), 10.0)
    per_layer_prefix = torch.arange(16, dtype=torch.float32).view(2, 1, 4, 2)
    per_layer_current = torch.full((2, 1, 1, 2), 20.0)
    replay_prompts = torch.full((2, 1, 1, 3), 30.0)

    session.prepare_for_replay(
        history=prefix,
        per_layer_history=per_layer_prefix,
        prompts=replay_prompts,
        target_position=5,
    )
    with pytest.raises(EOFError, match="during replay"):
        session.step(
            current,
            DUMMY,
            DUMMY_INT64,
            step_id="failed-replay",
            per_layer_inputs=per_layer_current,
        )

    replay_metadata = [MSGPackSerializer.loads(request.metadata) for request in requests]
    replay_tensors = [[deserialize_torch_tensor(tensor) for tensor in request.tensors] for request in requests]
    expected_history = torch.cat([prefix, current], dim=1)
    expected_per_layer_history = torch.cat([per_layer_prefix, per_layer_current], dim=2)

    assert [metadata["start_from_position"] for metadata in replay_metadata] == [0, 2]
    assert [tensors[0].shape[1] for tensors in replay_tensors] == [2, 2]
    assert session.position == 2
    assert not session.needs_replay
    assert torch.equal(session.history, expected_history)
    assert torch.equal(session.per_layer_history, expected_per_layer_history)
    assert torch.equal(session.replay_prompts, replay_prompts)

    retry_session = _ServerInferenceSession(
        ClientConfig(replay_chunk_size=2),
        _span("replacement-retry", 0, 2),
        "model.0 model.1",
        _rpc_info(),
        inputs_queue=None,
        outputs_aiter=None,
        max_length=8,
    )
    retry_requests = []

    async def _echo_retry_chunk(request):
        retry_requests.append(request)
        retry_session.stepped = True  # mirrors _ServerInferenceSession._step()
        hidden_states = deserialize_torch_tensor(request.tensors[0])
        return runtime_pb2.ExpertResponse(
            tensors=[serialize_torch_tensor(hidden_states + 1, runtime_pb2.CompressionType.NONE)]
        )

    retry_session._step = _echo_retry_chunk
    retry_session.prepare_for_replay(
        history=session.history,
        per_layer_history=session.per_layer_history,
        prompts=session.replay_prompts,
        target_position=5,
    )
    retry_outputs, _ = retry_session.step(
        current,
        DUMMY,
        DUMMY_INT64,
        step_id="retry-replay",
        per_layer_inputs=per_layer_current,
    )

    retry_metadata = [MSGPackSerializer.loads(request.metadata) for request in retry_requests]
    retry_tensors = [[deserialize_torch_tensor(tensor) for tensor in request.tensors] for request in retry_requests]
    assert [metadata["start_from_position"] for metadata in retry_metadata] == [0, 2, 4]
    assert [tensors[0].shape[1] for tensors in retry_tensors] == [2, 2, 1]
    assert torch.equal(torch.cat([tensors[0] for tensors in retry_tensors], dim=1), expected_history)
    assert torch.equal(torch.cat([tensors[3] for tensors in retry_tensors], dim=2), expected_per_layer_history)
    assert all(torch.equal(tensors[1], replay_prompts) for tensors in retry_tensors)
    assert torch.equal(retry_outputs, expected_history + 1)
    assert retry_session.position == 5
    assert not retry_session.needs_replay


def test_server_session_rejects_beam_replay():
    session = _ServerInferenceSession(
        ClientConfig(),
        _span("replacement", 0, 1),
        "model.0",
        _rpc_info(),
        inputs_queue=None,
        outputs_aiter=None,
        max_length=8,
    )
    session.prepare_for_replay(
        history=torch.zeros(2, 3, 4),
        per_layer_history=None,
        prompts=None,
        target_position=4,
    )

    with pytest.raises(NotImplementedError, match="beam"):
        session.step(
            torch.zeros(2, 1, 4),
            DUMMY,
            torch.tensor([1, 0]),
            step_id="beam-replay",
        )


class _FakeServerSession:
    def __init__(self, span: RemoteSpanInfo, *, fail_on_call=None):
        self.span = span
        self.history = None
        self.per_layer_history = None
        self.replay_prompts = None
        self.position = 0
        self.needs_replay = False
        self.target_position = None
        self.fail_on_call = fail_on_call
        self.call_count = 0
        self.input_lengths = []
        self.next_session = None
        self.closed = False

    def prepare_for_replay(self, *, history, per_layer_history, prompts, target_position):
        self.history = history
        self.per_layer_history = per_layer_history
        self.replay_prompts = prompts
        self.target_position = target_position
        self.needs_replay = True

    def step(self, inputs, prompts, hypo_ids, *, step_id, per_layer_inputs=None, shared_kv_states=None):
        del hypo_ids, step_id, shared_kv_states
        current_tokens = inputs.shape[1]
        has_per_layer_inputs = per_layer_inputs is not None and not is_dummy(per_layer_inputs)

        if self.needs_replay:
            if self.history is None:
                self.history = inputs
            elif self.history.shape[1] < self.target_position:
                missing = self.target_position - self.history.shape[1]
                self.history = torch.cat([self.history, inputs[:, -missing:]], dim=1)
            inputs = self.history

            if self.per_layer_history is not None and self.per_layer_history.shape[2] < self.target_position:
                missing = self.target_position - self.per_layer_history.shape[2]
                self.per_layer_history = torch.cat([self.per_layer_history, per_layer_inputs[:, :, -missing:]], dim=2)
            current_tokens = self.target_position
        else:
            if self.history is None:
                self.history = inputs
            elif self.history.shape[1] == self.position:
                self.history = torch.cat([self.history, inputs[:, -current_tokens:]], dim=1)
            if has_per_layer_inputs:
                if self.per_layer_history is None:
                    self.per_layer_history = per_layer_inputs
                elif self.per_layer_history.shape[2] == self.position:
                    self.per_layer_history = torch.cat([self.per_layer_history, per_layer_inputs], dim=2)
            if not is_dummy(prompts):
                self.replay_prompts = prompts

        self.call_count += 1
        self.input_lengths.append(inputs.shape[1])
        if self.call_count == self.fail_on_call:
            raise EOFError("worker disappeared")

        self.position += current_tokens
        self.needs_replay = False
        return inputs + self.span.length, {}

    def __enter__(self):
        return self

    def __exit__(self, *exc_details):
        self.closed = True


class _FakeSequenceManager:
    def __init__(self):
        self.block_uids = tuple(f"model.{index}" for index in range(4))
        self.config = ClientConfig(max_retries=3, min_backoff=0, max_backoff=0)
        self.make_sequence_calls = 0
        self.failures = []

    def __len__(self):
        return len(self.block_uids)

    def make_sequence(self, start, end, **kwargs):
        del kwargs
        self.make_sequence_calls += 1
        if self.make_sequence_calls == 1:
            assert (start, end) == (0, 4)
            return [_span("failed", 0, 2), _span("downstream", 2, 4)]
        assert (start, end) == (0, 2)
        return [_span("replacement-a", 0, 1), _span("replacement-b", 1, 2)]

    def on_request_success(self, peer_id):
        pass

    def on_request_failure(self, peer_id):
        self.failures.append(peer_id)

    def get_retry_delay(self, attempt_no):
        return 0


def test_inference_session_replays_split_replacement_and_slices_before_downstream(monkeypatch):
    manager = _FakeSequenceManager()
    inference_session = InferenceSession(manager, max_length=8)
    created = {}

    def _make_sessions(spans):
        sessions = []
        for span in spans:
            fake = _FakeServerSession(span, fail_on_call=2 if span.peer_id == "failed" else None)
            created[span.peer_id] = fake
            sessions.append(fake)
        return sessions

    monkeypatch.setattr(inference_session, "_enter_server_sessions", _make_sessions)

    prefix = torch.arange(12, dtype=torch.float32).view(1, 3, 4)
    prefix_per_layer = torch.arange(24, dtype=torch.float32).view(4, 1, 3, 2)
    prompts = torch.arange(16, dtype=torch.float32).view(4, 1, 1, 4)
    first_outputs = inference_session.step(prefix, prompts=prompts, per_layer_inputs=prefix_per_layer)
    assert torch.equal(first_outputs, prefix + 4)

    current = torch.full((1, 1, 4), 100.0)
    current_per_layer = torch.full((4, 1, 1, 2), 200.0)
    recovered_outputs = inference_session.step(current, per_layer_inputs=current_per_layer)

    replacement_a = created["replacement-a"]
    replacement_b = created["replacement-b"]
    downstream = created["downstream"]

    assert torch.equal(recovered_outputs, current + 4)
    assert manager.failures == ["failed"]
    assert replacement_a.input_lengths == [4]
    assert replacement_b.input_lengths == [4]
    assert downstream.input_lengths == [3, 1]
    assert replacement_a.position == replacement_b.position == downstream.position == 4
    assert torch.equal(
        replacement_a.per_layer_history,
        torch.cat([prefix_per_layer[0:1], current_per_layer[0:1]], dim=2),
    )
    assert torch.equal(
        replacement_b.per_layer_history,
        torch.cat([prefix_per_layer[1:2], current_per_layer[1:2]], dim=2),
    )
    assert torch.equal(replacement_a.replay_prompts, prompts[0:1])
    assert torch.equal(replacement_b.replay_prompts, prompts[1:2])


class _OffsetBoundarySequenceManager(_FakeSequenceManager):
    def make_sequence(self, start, end, **kwargs):
        del kwargs
        self.make_sequence_calls += 1
        if self.make_sequence_calls == 1:
            assert (start, end) == (0, 4)
            return [
                _span("upstream", 0, 1),
                _span("failed-middle", 1, 3),
                _span("downstream", 3, 4),
            ]

        assert (start, end) == (1, 3)
        # The final span deliberately crosses the failed span's old boundary. The
        # client clamps it to block 3 before opening replacement sessions.
        return [_span("replacement-a", 1, 2), _span("replacement-b", 2, 4)]


def test_inference_session_replays_offset_replacement_and_preserves_route_alignment(monkeypatch):
    manager = _OffsetBoundarySequenceManager()
    inference_session = InferenceSession(manager, max_length=8)
    created = {}

    def _make_sessions(spans):
        sessions = []
        for span in spans:
            fake = _FakeServerSession(
                span,
                fail_on_call=2 if span.peer_id == "failed-middle" else None,
            )
            created[span.peer_id] = fake
            sessions.append(fake)
        return sessions

    monkeypatch.setattr(inference_session, "_enter_server_sessions", _make_sessions)

    prefix = torch.arange(12, dtype=torch.float32).view(1, 3, 4)
    prefix_per_layer = torch.arange(24, dtype=torch.float32).view(4, 1, 3, 2)
    prompts = torch.arange(16, dtype=torch.float32).view(4, 1, 1, 4)
    first_outputs = inference_session.step(prefix, prompts=prompts, per_layer_inputs=prefix_per_layer)
    assert torch.equal(first_outputs, prefix + 4)

    current = torch.full((1, 1, 4), 100.0)
    current_per_layer = torch.full((4, 1, 1, 2), 200.0)
    recovered_outputs = inference_session.step(current, per_layer_inputs=current_per_layer)

    upstream = created["upstream"]
    failed_middle = created["failed-middle"]
    replacement_a = created["replacement-a"]
    replacement_b = created["replacement-b"]
    downstream = created["downstream"]

    assert torch.equal(recovered_outputs, current + 4)
    assert manager.failures == ["failed-middle"]
    assert manager.make_sequence_calls == 2
    assert failed_middle.closed
    assert replacement_a.span.start == 1 and replacement_a.span.end == 2
    assert replacement_b.span.start == 2 and replacement_b.span.end == 3
    assert upstream.input_lengths == [3, 1]
    assert replacement_a.input_lengths == [4]
    assert replacement_b.input_lengths == [4]
    assert downstream.input_lengths == [3, 1]
    assert inference_session.position == 4
    assert replacement_a.position == replacement_b.position == downstream.position == 4
    assert torch.equal(
        replacement_a.history,
        torch.cat([prefix + 1, current + 1], dim=1),
    )
    assert torch.equal(
        replacement_a.per_layer_history,
        torch.cat([prefix_per_layer[1:2], current_per_layer[1:2]], dim=2),
    )
    assert torch.equal(
        replacement_b.per_layer_history,
        torch.cat([prefix_per_layer[2:3], current_per_layer[2:3]], dim=2),
    )
    assert torch.equal(replacement_a.replay_prompts, prompts[1:2])
    assert torch.equal(replacement_b.replay_prompts, prompts[2:3])


class _ReplayFailureSequenceManager(_FakeSequenceManager):
    def __init__(self):
        super().__init__()
        self.make_sequence_requests = []

    def make_sequence(self, start, end, **kwargs):
        self.make_sequence_calls += 1
        self.make_sequence_requests.append((start, end, kwargs))
        assert kwargs == {"mode": "min_latency", "cache_tokens_needed": 8}

        if self.make_sequence_calls == 1:
            assert (start, end) == (0, 4)
            return [_span("failed", 0, 2), _span("downstream", 2, 4)]
        assert (start, end) == (0, 2)
        if self.make_sequence_calls == 2:
            return [_span("replacement", 0, 2)]
        assert self.make_sequence_calls == 3
        return [_span("replacement-retry", 0, 2)]


def test_inference_session_recovers_when_replacement_fails_during_replay(monkeypatch):
    manager = _ReplayFailureSequenceManager()
    inference_session = InferenceSession(manager, max_length=8)
    created = {}

    def _make_sessions(spans):
        sessions = []
        for span in spans:
            fail_on_call = {"failed": 2, "replacement": 1}.get(span.peer_id)
            fake = _FakeServerSession(span, fail_on_call=fail_on_call)
            created[span.peer_id] = fake
            sessions.append(fake)
        return sessions

    monkeypatch.setattr(inference_session, "_enter_server_sessions", _make_sessions)

    prefix = torch.arange(12, dtype=torch.float32).view(1, 3, 4)
    prefix_per_layer = torch.arange(24, dtype=torch.float32).view(4, 1, 3, 2)
    prompts = torch.arange(16, dtype=torch.float32).view(4, 1, 1, 4)
    first_outputs = inference_session.step(prefix, prompts=prompts, per_layer_inputs=prefix_per_layer)
    assert torch.equal(first_outputs, prefix + 4)

    current = torch.full((1, 1, 4), 100.0)
    current_per_layer = torch.full((4, 1, 1, 2), 200.0)
    recovered_outputs = inference_session.step(current, per_layer_inputs=current_per_layer)

    failed = created["failed"]
    replacement = created["replacement"]
    replacement_retry = created["replacement-retry"]
    downstream = created["downstream"]
    expected_history = torch.cat([prefix, current], dim=1)
    expected_per_layer_history = torch.cat([prefix_per_layer[0:2], current_per_layer[0:2]], dim=2)

    assert torch.equal(recovered_outputs, current + 4)
    assert manager.failures == ["failed", "replacement"]
    assert manager.make_sequence_requests == [
        (0, 4, {"mode": "min_latency", "cache_tokens_needed": 8}),
        (0, 2, {"mode": "min_latency", "cache_tokens_needed": 8}),
        (0, 2, {"mode": "min_latency", "cache_tokens_needed": 8}),
    ]
    assert failed.closed
    assert replacement.closed
    assert failed.input_lengths == [3, 1]
    assert replacement.input_lengths == [4]
    assert replacement_retry.input_lengths == [4]
    assert downstream.input_lengths == [3, 1]
    assert inference_session.position == 4
    assert replacement_retry.position == downstream.position == 4
    assert torch.equal(replacement.history, expected_history)
    assert torch.equal(replacement_retry.history, expected_history)
    assert torch.equal(replacement.per_layer_history, expected_per_layer_history)
    assert torch.equal(replacement_retry.per_layer_history, expected_per_layer_history)
    assert torch.equal(replacement.replay_prompts, prompts[0:2])
    assert torch.equal(replacement_retry.replay_prompts, prompts[0:2])
    assert torch.equal(
        downstream.history,
        torch.cat([prefix + 2, current + 2], dim=1),
    )
    assert torch.equal(
        downstream.per_layer_history,
        torch.cat([prefix_per_layer[2:4], current_per_layer[2:4]], dim=2),
    )
