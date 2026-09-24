import asyncio
import logging
import threading
from types import SimpleNamespace

import pytest

import drift.text_mesh as text_mesh
from drift.text_request import RequestContext, RequestDeadlineExceeded


def _candidate(peer_id, *, max_context_tokens=32, max_output_tokens=16):
    return {
        "peer_id": peer_id,
        "max_context_tokens": max_context_tokens,
        "max_output_tokens": max_output_tokens,
    }


def _response(frame=None, *, raw=None, tensors=()):
    metadata = raw if raw is not None else text_mesh.encode(dict(frame, manifest_digest="model-digest"))
    return SimpleNamespace(metadata=metadata, tensors=tensors)


class _Stream:
    def __init__(self, responses, *, close_gate=None):
        self._responses = iter(responses)
        self.close_gate = close_gate
        self.closed = 0
        self.close_completed = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            item = next(self._responses)
        except StopIteration:
            raise StopAsyncIteration from None
        if isinstance(item, BaseException):
            raise item
        if asyncio.iscoroutine(item):
            item = await item
        return item

    async def aclose(self):
        self.closed += 1
        if self.close_gate is not None:
            await self.close_gate.wait()
        self.close_completed += 1


class _Stub:
    def __init__(self, streams):
        self._streams = iter(streams)
        self.generated = []
        self.cancelled = []

    async def rpc_generate(self, request):
        body = text_mesh.decode(request.metadata, text_mesh.MAX_REQUEST_BYTES)
        self.generated.append(body)
        stream = next(self._streams)
        if isinstance(stream, BaseException):
            raise stream
        return stream

    async def rpc_cancel(self, request):
        body = text_mesh.decode(request.metadata, 1024)
        self.cancelled.append(body["request_id"])
        return _Stream([_response({"type": "cancelled"})])


class _P2P:
    def __init__(self, stubs):
        self.stubs = stubs
        self.shutdown_calls = 0

    async def shutdown(self):
        self.shutdown_calls += 1


class _DHT:
    def __init__(self, p2p, *, replicate=None):
        self.p2p = p2p
        self.replicate = replicate
        self.replicate_calls = 0

    async def replicate_p2p(self):
        self.replicate_calls += 1
        if self.replicate is not None:
            return await self.replicate()
        return self.p2p


@pytest.fixture
def mesh(monkeypatch):
    peers = []
    monkeypatch.setattr(text_mesh, "discover_text_peers", lambda *_args, **_kwargs: list(peers))
    monkeypatch.setattr(text_mesh, "PeerID", SimpleNamespace(from_base58=lambda value: value))
    monkeypatch.setattr(
        text_mesh.TextPeerProtocol,
        "get_stub",
        staticmethod(lambda p2p, peer_id: p2p.stubs[peer_id]),
    )
    return peers


def _client(dht, *, request_timeout=15, total_timeout=60):
    manifest = SimpleNamespace(digest_id="model-digest")
    return text_mesh.TextPeerClient(
        manifest,
        initial_peers=[],
        request_timeout=request_timeout,
        total_timeout=total_timeout,
        dht=dht,
    )


async def _collect(client, body=None, *, chat=False, context=None):
    return [
        frame
        async for frame in client.stream(
            {"max_tokens": 8} if body is None else body,
            chat=chat,
            context=context,
        )
    ]


@pytest.mark.asyncio
async def test_one_deadline_covers_discovery_before_transport(mesh):
    now = [10.0]

    def discover(*_args, **_kwargs):
        now[0] = 12.0
        return [_candidate("peer-a")]

    p2p = _P2P({})
    dht = _DHT(p2p)
    text_mesh.discover_text_peers = discover
    context = RequestContext.start(1.0, clock=lambda: now[0])

    with pytest.raises(RequestDeadlineExceeded, match="^Request deadline exceeded$"):
        await _collect(_client(dht), context=context)

    assert dht.replicate_calls == 0
    assert p2p.shutdown_calls == 0


@pytest.mark.asyncio
async def test_each_peer_attempt_has_private_wire_identity(mesh):
    mesh.extend([_candidate("peer-a"), _candidate("peer-b")])
    first = _Stub(
        [
            _Stream(
                [
                    _response(
                        {
                            "type": "error",
                            "code": "busy",
                            "message": "PRIVATE REMOTE DIAGNOSTIC",
                        }
                    )
                ]
            )
        ]
    )
    second = _Stub(
        [
            _Stream(
                [
                    _response(
                        {
                            "type": "done",
                            "finish_reason": "stop",
                            "usage": {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
                        }
                    )
                ]
            )
        ]
    )
    p2p = _P2P({"peer-a": first, "peer-b": second})
    context = RequestContext.start(30)

    frames = await _collect(_client(_DHT(p2p)), context=context)

    first_id = first.generated[0]["request_id"]
    second_id = second.generated[0]["request_id"]
    assert first_id != second_id
    assert first.cancelled == [first_id]
    assert second.cancelled == []
    assert context.request_id not in (first_id, second_id)
    assert frames[-1]["type"] == "done"
    assert p2p.shutdown_calls == 1


@pytest.mark.asyncio
async def test_malformed_peer_error_is_safe_and_not_a_client_400(mesh, caplog):
    mesh.append(_candidate("peer-a"))
    secret = "SECRET REMOTE FAILURE DETAIL"
    stub = _Stub(
        [
            _Stream(
                [
                    _response(
                        {
                            "type": "error",
                            "code": "busy",
                            "message": secret,
                        }
                    )
                ]
            )
        ]
    )
    caplog.set_level(logging.WARNING)

    with pytest.raises(text_mesh.TextPeerUnavailable) as raised:
        await _collect(_client(_DHT(_P2P({"peer-a": stub}))))

    assert secret not in str(raised.value)
    assert secret not in caplog.text
    assert type(raised.value) is text_mesh.TextPeerUnavailable


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frame",
    [
        {"type": "delta", "text": 7},
        {
            "type": "done",
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 1, "completion_tokens": 3, "total_tokens": 4},
        },
        {
            "type": "done",
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 9},
        },
    ],
)
async def test_malformed_response_has_typed_unavailable_error(mesh, frame):
    mesh.append(_candidate("peer-a", max_context_tokens=8, max_output_tokens=4))
    stub = _Stub([_Stream([_response(frame)])])

    with pytest.raises(text_mesh.TextPeerMalformedResponse, match=r"^The community peer sent an invalid response\.$"):
        await _collect(_client(_DHT(_P2P({"peer-a": stub}))), {"max_tokens": 2})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "responses",
    [
        [_response({"type": "heartbeat"}) for _ in range(4097)],
        [_response({"type": "delta", "text": "x" * 60000}) for _ in range(18)],
    ],
)
async def test_response_frame_and_utf8_output_budgets(mesh, responses):
    mesh.append(_candidate("peer-a", max_context_tokens=262144, max_output_tokens=262144))
    stub = _Stub([_Stream(responses)])

    with pytest.raises(text_mesh.TextPeerMalformedResponse):
        await _collect(_client(_DHT(_P2P({"peer-a": stub}))), {"max_tokens": 262144})


@pytest.mark.asyncio
async def test_no_retry_after_output_or_deadline(mesh):
    mesh.extend([_candidate("peer-a"), _candidate("peer-b")])
    first = _Stub([_Stream([_response({"type": "delta", "text": "started"}), TimeoutError("private")])])
    second = _Stub([_Stream([])])
    p2p = _P2P({"peer-a": first, "peer-b": second})

    with pytest.raises(text_mesh.TextPeerUnavailable, match="connection stopped"):
        await _collect(_client(_DHT(p2p)))
    assert second.generated == []

    now = [10.0]

    async def expire():
        now[0] = 12.0
        return _Stream([])

    first = _Stub([])
    first.rpc_generate = lambda _request: expire()
    second = _Stub([_Stream([])])
    p2p = _P2P({"peer-a": first, "peer-b": second})
    context = RequestContext.start(1.0, clock=lambda: now[0])
    with pytest.raises(RequestDeadlineExceeded):
        await _collect(_client(_DHT(p2p)), context=context)
    assert second.generated == []


@pytest.mark.asyncio
async def test_caller_cancellation_does_not_retry(mesh):
    mesh.extend([_candidate("peer-a"), _candidate("peer-b")])
    started = asyncio.Event()
    never = asyncio.Event()

    class BlockingStream(_Stream):
        async def __anext__(self):
            started.set()
            await never.wait()

    first = _Stub([BlockingStream([])])
    second = _Stub([_Stream([])])
    p2p = _P2P({"peer-a": first, "peer-b": second})
    task = asyncio.create_task(_collect(_client(_DHT(p2p))))
    await asyncio.wait_for(started.wait(), 1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert second.generated == []
    assert first.cancelled == [first.generated[0]["request_id"]]
    assert p2p.shutdown_calls == 1


@pytest.mark.asyncio
async def test_late_replicate_result_is_shutdown(mesh):
    mesh.append(_candidate("peer-a"))
    p2p = _P2P({})
    started = asyncio.Event()
    release = asyncio.Event()

    async def returns_after_local_timeout():
        started.set()
        await release.wait()
        return p2p

    dht = _DHT(p2p, replicate=returns_after_local_timeout)
    context = RequestContext.start(0.2)
    request = asyncio.create_task(_collect(_client(dht), context=context))
    await asyncio.wait_for(started.wait(), 1)

    with pytest.raises(RequestDeadlineExceeded):
        await request
    release.set()
    for _ in range(20):
        if p2p.shutdown_calls:
            break
        await asyncio.sleep(0.01)
    assert p2p.shutdown_calls == 1


@pytest.mark.asyncio
async def test_second_cancellation_closes_late_cancel_stream(mesh):
    mesh.extend([_candidate("peer-a"), _candidate("peer-b")])
    response_started = asyncio.Event()
    cancel_started = asyncio.Event()
    release_cancel = asyncio.Event()
    never = asyncio.Event()

    class BlockingResponses(_Stream):
        async def __anext__(self):
            response_started.set()
            await never.wait()

    late_cancel_stream = _Stream([_response({"type": "cancelled"})])
    first = _Stub([BlockingResponses([])])

    async def late_cancel(request):
        body = text_mesh.decode(request.metadata, 1024)
        first.cancelled.append(body["request_id"])
        cancel_started.set()
        await release_cancel.wait()
        return late_cancel_stream

    first.rpc_cancel = late_cancel
    second = _Stub([_Stream([])])
    p2p = _P2P({"peer-a": first, "peer-b": second})
    task = asyncio.create_task(_collect(_client(_DHT(p2p))))
    await asyncio.wait_for(response_started.wait(), 1)
    task.cancel()
    await asyncio.wait_for(cancel_started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    release_cancel.set()
    for _ in range(20):
        if late_cancel_stream.closed:
            break
        await asyncio.sleep(0.01)
    assert late_cancel_stream.closed == 1
    assert second.generated == []


@pytest.mark.asyncio
async def test_cancellation_resistant_reads_are_closed_after_they_finish(mesh, monkeypatch):
    mesh.extend([_candidate("peer-a"), _candidate("peer-b")])
    response_started = asyncio.Event()
    response_cancelled = asyncio.Event()
    cancel_receipt_started = asyncio.Event()
    cancel_receipt_cancelled = asyncio.Event()
    release = asyncio.Event()

    class ResistantStream:
        def __init__(self, started, cancelled):
            self.started = started
            self.cancelled = cancelled
            self.close_attempts = 0
            self.close_successes = 0

            async def source():
                try:
                    self.started.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        self.cancelled.set()
                        await release.wait()
                    yield _response({"type": "heartbeat"})
                finally:
                    self.close_successes += 1

            self.source = source()

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await anext(self.source)

        async def aclose(self):
            self.close_attempts += 1
            await self.source.aclose()

    responses = ResistantStream(response_started, response_cancelled)
    cancel_receipt = ResistantStream(cancel_receipt_started, cancel_receipt_cancelled)
    first = _Stub([responses])

    async def cancel(_request):
        return cancel_receipt

    first.rpc_cancel = cancel
    second = _Stub([_Stream([])])
    monkeypatch.setattr(text_mesh, "CLEANUP_SECONDS", 0.05)
    task = asyncio.create_task(_collect(_client(_DHT(_P2P({"peer-a": first, "peer-b": second})))))
    await asyncio.wait_for(response_started.wait(), 1)
    task.cancel()
    await asyncio.wait_for(response_cancelled.wait(), 1)
    await asyncio.wait_for(cancel_receipt_started.wait(), 1)
    await asyncio.wait_for(cancel_receipt_cancelled.wait(), 1)
    with pytest.raises(asyncio.CancelledError):
        await task

    assert responses.close_attempts == 0
    assert cancel_receipt.close_attempts == 0
    assert second.generated == []
    release.set()
    for _ in range(50):
        if responses.close_successes and cancel_receipt.close_successes:
            break
        await asyncio.sleep(0.01)
    assert (responses.close_attempts, responses.close_successes) == (1, 1)
    assert (cancel_receipt.close_attempts, cancel_receipt.close_successes) == (1, 1)


@pytest.mark.asyncio
async def test_attempt_cleanup_and_transport_shutdown_are_bounded(mesh, monkeypatch):
    mesh.append(_candidate("peer-a"))
    never = asyncio.Event()
    responses = _Stream([TimeoutError("private")], close_gate=never)
    stub = _Stub([responses])

    async def stuck_cancel(_request):
        await never.wait()

    stub.rpc_cancel = stuck_cancel
    p2p = _P2P({"peer-a": stub})
    shutdown_finished = asyncio.Event()

    async def stuck_shutdown():
        p2p.shutdown_calls += 1
        await never.wait()
        shutdown_finished.set()

    p2p.shutdown = stuck_shutdown
    monkeypatch.setattr(text_mesh, "CLEANUP_SECONDS", 0.05)
    monkeypatch.setattr(text_mesh, "SHUTDOWN_SECONDS", 0.05)

    with pytest.raises(text_mesh.TextPeerUnavailable):
        await asyncio.wait_for(_collect(_client(_DHT(p2p))), 0.5)

    assert responses.closed == 1
    assert p2p.shutdown_calls == 1
    assert responses.close_completed == 0
    assert not shutdown_finished.is_set()
    never.set()
    await asyncio.wait_for(shutdown_finished.wait(), 1)
    for _ in range(20):
        if responses.close_completed:
            break
        await asyncio.sleep(0.01)
    assert responses.close_completed == 1


@pytest.mark.asyncio
async def test_expired_observation_budget_still_owns_eventual_stream_close():
    stream = _Stream([])
    client = _client(_DHT(_P2P({})))
    pending = asyncio.get_running_loop().create_future()
    pending.set_result(None)

    await client._close_after_pending(
        stream,
        pending,
        asyncio.get_running_loop().time() - 1,
    )
    for _ in range(20):
        if stream.closed:
            break
        await asyncio.sleep(0.01)
    assert stream.closed == 1


@pytest.mark.asyncio
async def test_expired_cleanup_cancels_and_retains_precreated_task():
    client = _client(_DHT(_P2P({})))
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def operation():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(operation())
    await asyncio.wait_for(started.wait(), 1)
    try:
        result = await client._bounded_cleanup(task, asyncio.get_running_loop().time() - 1)
        assert result is text_mesh._MISSING
        await asyncio.wait_for(cancelled.wait(), 1)
        assert task.cancelled()
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_late_transport_cleanup_remains_owned_after_grace(mesh, monkeypatch):
    gate = asyncio.Event()
    started = asyncio.Event()
    finished = asyncio.Event()
    p2p = _P2P({})

    async def shutdown():
        p2p.shutdown_calls += 1
        started.set()
        await gate.wait()
        finished.set()

    p2p.shutdown = shutdown
    monkeypatch.setattr(text_mesh, "SHUTDOWN_SECONDS", 0.05)
    _client(_DHT(p2p))._abandon_p2p(p2p)
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.sleep(0.1)
    assert not finished.is_set()

    gate.set()
    await asyncio.wait_for(finished.wait(), 1)
    assert p2p.shutdown_calls == 1


@pytest.mark.asyncio
async def test_initially_expired_late_cleanup_still_runs(mesh, monkeypatch):
    ran = asyncio.Event()
    p2p = _P2P({})

    async def shutdown():
        p2p.shutdown_calls += 1
        ran.set()

    p2p.shutdown = shutdown
    monkeypatch.setattr(text_mesh, "SHUTDOWN_SECONDS", 0.0)
    _client(_DHT(p2p))._abandon_p2p(p2p)
    await asyncio.wait_for(ran.wait(), 1)
    assert p2p.shutdown_calls == 1


@pytest.mark.asyncio
async def test_discovery_gate_outlives_timed_out_owner_and_drops_queued_requests(mesh):
    calls = 0
    entered = threading.Event()
    release = threading.Event()

    def discover(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(2)
        return []

    text_mesh.discover_text_peers = discover
    client = _client(_DHT(_P2P({})))
    first = asyncio.create_task(_collect(client, context=RequestContext.start(0.1)))
    for _ in range(100):
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert entered.is_set()
    timed_out = asyncio.create_task(_collect(client, context=RequestContext.start(0.05)))
    cancelled = asyncio.create_task(_collect(client, context=RequestContext.start(1)))
    await asyncio.sleep(0.02)
    cancelled.cancel()

    with pytest.raises(RequestDeadlineExceeded):
        await first
    with pytest.raises(RequestDeadlineExceeded):
        await timed_out
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert calls == 1

    release.set()
    for _ in range(100):
        if not client._request_gate.locked():
            break
        await asyncio.sleep(0.01)
    assert calls == 1
    with pytest.raises(text_mesh.TextPeerUnavailable, match="No community peer"):
        await _collect(client, context=RequestContext.start(1))
    assert calls == 2


@pytest.mark.asyncio
async def test_successful_requests_reuse_same_client_gate(mesh):
    mesh.append(_candidate("peer-a"))
    done = _response(
        {
            "type": "done",
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
        }
    )
    stub = _Stub([_Stream([done]), _Stream([done])])
    p2p = _P2P({"peer-a": stub})
    client = _client(_DHT(p2p))

    await asyncio.wait_for(_collect(client, context=RequestContext.start(1)), 2)
    await asyncio.wait_for(_collect(client, context=RequestContext.start(1)), 2)

    assert len(stub.generated) == 2
    assert p2p.shutdown_calls == 2


@pytest.mark.asyncio
async def test_late_owed_close_failure_keeps_client_gate_closed(mesh, monkeypatch):
    mesh.append(_candidate("peer-a"))
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    class FailingCloseStream(_Stream):
        async def aclose(self):
            self.closed += 1
            close_started.set()
            await release_close.wait()
            raise OSError("PRIVATE CLOSE FAILURE")

    done = _response(
        {
            "type": "done",
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
        }
    )
    responses = FailingCloseStream([done])
    stub = _Stub([responses])
    calls = 0

    def discover(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return list(mesh)

    text_mesh.discover_text_peers = discover
    monkeypatch.setattr(text_mesh, "CLEANUP_SECONDS", 0.05)
    client = _client(_DHT(_P2P({"peer-a": stub})))

    frames = await asyncio.wait_for(_collect(client, context=RequestContext.start(1)), 2)
    assert frames[-1]["type"] == "done"
    await asyncio.wait_for(close_started.wait(), 1)
    with pytest.raises(RequestDeadlineExceeded):
        await _collect(client, context=RequestContext.start(0.05))
    assert calls == 1

    release_close.set()
    await asyncio.sleep(0.05)
    assert client._request_gate.locked()
    with pytest.raises(RequestDeadlineExceeded):
        await _collect(client, context=RequestContext.start(0.05))
    assert calls == 1
    assert responses.closed == 1
