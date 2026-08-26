import asyncio
import logging
import multiprocessing as mp
import queue
import threading
from types import SimpleNamespace

import pytest
from hivemind.p2p import P2P, P2PHandlerError
from hivemind.proto import runtime_pb2
from hivemind.utils.serializer import MSGPackSerializer

from drift.server.admission import AdmissionPolicy, AdmissionRejected, AdmissionState
from drift.server.handler import (
    MAX_INFERENCE_METADATA_BYTES,
    MAX_PUSH_METADATA_BYTES,
    Event,
    TransformerConnectionHandler,
)
from drift.server.rejection_logging import (
    P2P_DAEMON_LOGGER_NAME,
    P2P_HANDLER_FAILURE_MESSAGE,
    PUBLIC_REJECTION_LOG_MESSAGE,
    RoutinePublicRejectionLogFilter,
    install_public_rejection_log_filter,
)
from drift.server.server import ModuleContainer


@pytest.fixture
def event_loop():
    """Own an explicit loop so earlier Windows winloop tests cannot remove this module's loop."""

    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def _spawn_try_acquire(records, lock, policy, result_queue):
    state = AdmissionState(policy, records, lock)
    try:
        state.acquire("spawned-peer").release()
    except AdmissionRejected:
        result_queue.put("rejected")
    else:
        result_queue.put("accepted")


def _policy(**overrides):
    values = {
        "max_active_sessions": 3,
        "max_active_sessions_per_peer": 2,
        "global_session_rate": 2.0,
        "global_session_burst": 3,
        "peer_session_rate": 1.0,
        "peer_session_burst": 2,
        "max_tracked_peers": 4,
        "tracked_peer_ttl": 5.0,
        "max_pending_pushes": 2,
    }
    values.update(overrides)
    return AdmissionPolicy(**values)


def test_policy_requires_finite_consistent_bounds():
    for field, value in (
        ("max_active_sessions", 0),
        ("max_active_sessions_per_peer", True),
        ("global_session_rate", float("inf")),
        ("peer_session_rate", 0),
        ("global_session_burst", 0),
        ("max_tracked_peers", 0),
        ("tracked_peer_ttl", float("nan")),
        ("max_pending_pushes", 0),
    ):
        with pytest.raises(ValueError, match=field):
            _policy(**{field: value})
    with pytest.raises(ValueError, match="cannot exceed"):
        _policy(max_active_sessions_per_peer=4)
    with pytest.raises(ValueError, match="cannot be less"):
        _policy(max_tracked_peers=2)
    with pytest.raises(ValueError, match="complete.*refill"):
        _policy(tracked_peer_ttl=1.0)
    for field, value in (
        ("max_active_sessions", 129),
        ("global_session_burst", 1025),
        ("global_session_rate", 1025.0),
        ("max_tracked_peers", 65_537),
        ("tracked_peer_ttl", 86_401.0),
        ("max_pending_pushes", 65),
    ):
        with pytest.raises(ValueError, match=field):
            _policy(**{field: value})


def test_active_limits_are_global_and_per_peer_and_leases_release():
    clock = Clock()
    state = AdmissionState.local(_policy(max_active_sessions=4, global_session_burst=4), clock=clock)
    first = state.acquire("peer-a")
    second = state.acquire("peer-a")
    third = state.acquire("peer-b")

    with pytest.raises(AdmissionRejected, match="temporarily unavailable"):
        state.acquire("peer-a")
    fourth = state.acquire("peer-c")
    with pytest.raises(AdmissionRejected, match="temporarily unavailable"):
        state.acquire("peer-d")
    assert state.snapshot() == {
        "active_sessions": 4,
        "tracked_peers": 3,
        "active_session_routes": 0,
        "pending_pushes": 0,
        "accepted_sessions": 4,
        "rejected_sessions": 2,
        "healthy": True,
    }

    second.release()
    second.release()
    first.release()
    third.release()
    fourth.release()
    assert state.snapshot()["active_sessions"] == 0


def test_token_buckets_use_injected_monotonic_clock():
    clock = Clock()
    state = AdmissionState.local(
        _policy(
            max_active_sessions=2,
            max_active_sessions_per_peer=2,
            global_session_rate=1.0,
            global_session_burst=1,
            peer_session_rate=0.5,
            peer_session_burst=1,
        ),
        clock=clock,
    )
    state.acquire("peer-a").release()
    with pytest.raises(AdmissionRejected, match="temporarily unavailable"):
        state.acquire("peer-a")
    clock.advance(1.0)
    with pytest.raises(AdmissionRejected, match="temporarily unavailable"):
        state.acquire("peer-a")
    clock.advance(1.0)
    state.acquire("peer-a").release()


def test_backwards_or_invalid_clock_fails_closed():
    clock = Clock()
    state = AdmissionState.local(_policy(), clock=clock)
    state.acquire("peer-a").release()
    clock.value = 99.0
    with pytest.raises(AdmissionRejected, match="state is unavailable"):
        state.acquire("peer-a")


def test_contended_shared_lock_and_failed_release_fail_closed_instead_of_hanging(monkeypatch):
    state = AdmissionState.local(_policy())
    lease = state.acquire("peer-a")
    entered = threading.Event()
    release = threading.Event()

    def hold_lock():
        with state._lock:
            entered.set()
            release.wait()

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert entered.wait(timeout=1.0)
    monkeypatch.setattr("drift.server.admission._LOCK_ACQUIRE_TIMEOUT", 0.01)
    try:
        with pytest.raises(AdmissionRejected, match="state is unavailable"):
            state.require_healthy()
        lease.release()
    finally:
        release.set()
        holder.join(timeout=1.0)
    assert not holder.is_alive()
    assert state.snapshot()["active_sessions"] == 1
    assert state.snapshot()["healthy"] is False
    with pytest.raises(AdmissionRejected, match="state is unavailable"):
        state.acquire("peer-b")


def test_identity_churn_is_bounded_and_only_fully_refilled_peers_expire():
    clock = Clock()
    state = AdmissionState.local(
        _policy(
            max_active_sessions=2,
            max_active_sessions_per_peer=1,
            global_session_rate=10.0,
            global_session_burst=10,
            peer_session_rate=1.0,
            peer_session_burst=1,
            max_tracked_peers=2,
            tracked_peer_ttl=2.0,
        ),
        clock=clock,
    )
    state.acquire("peer-a").release()
    state.acquire("peer-b").release()
    with pytest.raises(AdmissionRejected, match="temporarily unavailable"):
        state.acquire("peer-c")
    assert state.snapshot()["tracked_peers"] == 2

    clock.advance(2.0)
    state.acquire("peer-c").release()
    assert state.snapshot()["tracked_peers"] == 2
    assert all("peer-a" not in key and "peer-b" not in key for key in state._records)


def test_shared_states_enforce_one_quota_across_handlers():
    with mp.Manager() as manager:
        policy = _policy(max_active_sessions=1, max_active_sessions_per_peer=1)
        first_handler = AdmissionState.shared(policy, manager)
        second_handler = AdmissionState(policy, first_handler._records, first_handler._lock)
        lease = first_handler.acquire("peer-a")

        with pytest.raises(AdmissionRejected, match="temporarily unavailable"):
            second_handler.acquire("peer-b")
        assert second_handler.snapshot()["active_sessions"] == 1
        lease.release()
        second_handler.acquire("peer-b").release()


def test_release_corruption_blocks_new_sessions_but_does_not_raise():
    state = AdmissionState.local(_policy())
    lease = state.acquire("peer-a")
    peer_key = lease._peer_key
    state._records.pop(peer_key)

    lease.release()

    assert state.snapshot()["healthy"] is False
    with pytest.raises(AdmissionRejected, match="state is unavailable"):
        state.acquire("peer-b")


def test_legacy_handler_keeps_training_compatibility():
    handler = object.__new__(TransformerConnectionHandler)
    handler._admission_state = None
    handler._require_training_allowed()


def test_training_rpcs_default_off_and_snapshot_is_aggregate_only():
    state = AdmissionState.local(_policy())
    with pytest.raises(AdmissionRejected, match="training RPCs are disabled"):
        state.require_training_allowed()
    state.acquire("secret-peer-identity").release()

    snapshot = state.snapshot()

    assert set(snapshot) == {
        "active_sessions",
        "tracked_peers",
        "active_session_routes",
        "pending_pushes",
        "accepted_sessions",
        "rejected_sessions",
        "healthy",
    }
    assert "secret-peer-identity" not in repr(state._records)
    AdmissionState.local(_policy(allow_training_rpcs=True)).require_training_allowed()


def test_session_registry_is_global_opaque_and_validated():
    state = AdmissionState.local(_policy())
    session_key, route_token = state.register_session("secret-session-id", 1)

    assert state.resolve_session("secret-session-id") == 1
    assert state.snapshot()["active_session_routes"] == 1
    assert "secret-session-id" not in repr(state._records)
    with pytest.raises(AdmissionRejected):
        state.register_session("secret-session-id", 2)
    for invalid in (None, "", "x" * 257, "\ud800"):
        with pytest.raises(AdmissionRejected):
            state.resolve_session(invalid)

    state.unregister_session(session_key, 1, route_token)
    assert state.resolve_session("secret-session-id") is None
    assert state.snapshot()["active_session_routes"] == 0


def test_wrong_session_owner_marks_state_unhealthy():
    state = AdmissionState.local(_policy())
    session_key, route_token = state.register_session("session", 1)

    state.unregister_session(session_key, 2, route_token)

    assert state.snapshot()["healthy"] is False
    with pytest.raises(AdmissionRejected, match="state is unavailable"):
        state.acquire("peer")


def test_pending_push_budget_is_aggregate_and_released():
    state = AdmissionState.local(_policy(max_pending_pushes=2))
    session_key, route_token = state.register_session("session", 0)

    assert state.reserve_push("session") == (0, route_token)
    assert state.reserve_push("session") == (0, route_token)
    with pytest.raises(AdmissionRejected, match="temporarily unavailable"):
        state.reserve_push("session")
    assert state.snapshot()["pending_pushes"] == 2

    state.release_push()
    assert state.reserve_push("session") == (0, route_token)
    state.release_push()
    state.release_push()
    assert state.snapshot()["pending_pushes"] == 0

    state.reserve_outbound_push()
    state.reserve_outbound_push()
    with pytest.raises(AdmissionRejected, match="temporarily unavailable"):
        state.reserve_outbound_push()
    state.release_push()
    state.release_push()
    assert state.snapshot()["pending_pushes"] == 0
    state.unregister_session(session_key, 0, route_token)


def test_spawned_process_shares_the_same_admission_authority():
    context = mp.get_context("spawn")
    with context.Manager() as manager:
        policy = _policy(max_active_sessions=1, max_active_sessions_per_peer=1)
        state = AdmissionState.shared(policy, manager)
        lease = state.acquire("parent-peer")
        result_queue = context.Queue()
        process = context.Process(
            target=_spawn_try_acquire,
            args=(state._records, state._lock, policy, result_queue),
        )
        process.start()
        process.join(15)

        assert process.exitcode == 0
        assert result_queue.get(timeout=2) == "rejected"
        lease.release()
        result_queue.close()
        result_queue.cancel_join_thread()


class _RejectingState:
    def acquire(self, peer_id):
        raise AdmissionRejected("rejected before input")

    def require_training_allowed(self):
        raise AdmissionRejected("training disabled")


@pytest.mark.asyncio
async def test_inference_acquires_before_waiting_for_the_first_stream_message():
    handler = object.__new__(TransformerConnectionHandler)
    handler.session_timeout = 5
    handler._admission_state = _RejectingState()
    consumed = False

    async def requests():
        nonlocal consumed
        consumed = True
        yield runtime_pb2.ExpertRequest()

    with pytest.raises(AdmissionRejected, match="before input"):
        await anext(handler.rpc_inference(requests(), SimpleNamespace(remote_id="peer")))

    assert consumed is False


@pytest.mark.asyncio
async def test_public_inference_size_checks_precede_parsing_and_cover_later_messages(caplog):
    state = AdmissionState.local(_policy())
    handler = object.__new__(TransformerConnectionHandler)
    handler.session_timeout = 5
    handler.step_timeout = 5
    handler._admission_state = state
    handler._log_request = lambda *args, **kwargs: None
    handler._check_uids = lambda value: (_ for _ in ()).throw(AssertionError("uids must not be parsed"))
    oversized = runtime_pb2.ExpertRequest(metadata=b"x" * (MAX_INFERENCE_METADATA_BYTES + 1))

    async def oversized_first():
        yield oversized

    with pytest.raises(AdmissionRejected, match="too large"):
        await anext(handler.rpc_inference(oversized_first(), SimpleNamespace(remote_id="peer")))
    assert state.snapshot()["active_sessions"] == 0

    handler._check_uids = lambda value: ()
    first = runtime_pb2.ExpertRequest(tensors=[runtime_pb2.Tensor()])

    async def oversized_second():
        yield oversized

    steps = handler._iterate_inference_steps(first, oversized_second(), None, (), SimpleNamespace(remote_id="peer"))
    assert await anext(steps) == (first, {})
    with caplog.at_level(logging.WARNING, logger="drift.server.handler"):
        with pytest.raises(AdmissionRejected, match="too large"):
            await anext(steps)
    assert not any("_iterate_inference_steps() exception" in record.getMessage() for record in caplog.records)


def test_public_request_logs_do_not_retain_raw_peer_identity(monkeypatch):
    handler = object.__new__(TransformerConnectionHandler)
    handler._admission_state = AdmissionState.local(_policy())
    messages = []
    monkeypatch.setattr("drift.server.handler.logger.info", messages.append)

    handler._log_request(
        "rpc_inference.open",
        ["model.0"],
        SimpleNamespace(remote_id="secret-peer-identity-ABC123"),
    )

    assert messages == ["rpc_inference.open(blocks=0:1, remote_peer=authenticated)"]
    assert "ABC123" not in messages[0]


@pytest.mark.asyncio
async def test_training_rpcs_reject_before_reading_or_deserializing_inputs():
    handler = object.__new__(TransformerConnectionHandler)
    handler._admission_state = _RejectingState()
    context = SimpleNamespace(remote_id="peer")

    with pytest.raises(AdmissionRejected, match="training disabled"):
        await handler.rpc_forward(None, context)
    with pytest.raises(AdmissionRejected, match="training disabled"):
        await handler.rpc_backward(None, context)

    async def requests():
        raise AssertionError("stream input must not be consumed")
        yield runtime_pb2.ExpertRequest()

    with pytest.raises(AdmissionRejected, match="training disabled"):
        await anext(handler.rpc_forward_stream(requests(), context))
    with pytest.raises(AdmissionRejected, match="training disabled"):
        await anext(handler.rpc_backward_stream(requests(), context))


@pytest.mark.asyncio
async def test_pushes_use_bounded_registry_and_release_budget_on_consumption():
    state = AdmissionState.local(_policy(max_pending_pushes=2))
    session_key, route_token = state.register_session("session", 0)
    handler = object.__new__(TransformerConnectionHandler)
    handler._admission_state = state
    handler._handler_index = 0
    handler._handler_event_queues = [queue.Queue(maxsize=2)]
    handler._session_queues = {"session": asyncio.Queue(maxsize=2)}
    handler._session_handlers = {"session": 0}
    handler._session_tokens = {"session": route_token}
    handler.module_backends = {"model.0": object()}
    handler._log_request = lambda *args, **kwargs: None
    request = runtime_pb2.ExpertRequest(
        uid="model.0",
        metadata=MSGPackSerializer.dumps({"session_id": "session"}),
    )
    context = SimpleNamespace(remote_id="peer")

    await handler.rpc_push(request, context)
    assert state.snapshot()["pending_pushes"] == 1
    assert await handler._get_from_session_queue("session") is request
    assert state.snapshot()["pending_pushes"] == 0

    unknown = runtime_pb2.ExpertRequest(
        uid="model.0",
        metadata=MSGPackSerializer.dumps({"session_id": "unknown"}),
    )
    with pytest.raises(AdmissionRejected, match="push target"):
        await handler.rpc_push(unknown, context)
    oversized = runtime_pb2.ExpertRequest(uid="model.0", metadata=b"x" * (MAX_PUSH_METADATA_BYTES + 1))
    with pytest.raises(AdmissionRejected, match="too large"):
        await handler.rpc_push(oversized, context)

    state.unregister_session(session_key, 0, route_token)


@pytest.mark.asyncio
async def test_outbound_push_tasks_use_the_shared_budget_and_release_on_cancel():
    state = AdmissionState.local(_policy(max_pending_pushes=2))
    handler = object.__new__(TransformerConnectionHandler)
    handler._admission_state = state
    blocked = asyncio.Event()

    async def push_outputs(*args):
        await blocked.wait()

    handler._push_outputs = push_outputs
    request = runtime_pb2.ExpertRequest()
    output = runtime_pb2.Tensor()
    first = handler._create_output_push_task(request, output, {})
    second = handler._create_output_push_task(request, output, {})

    assert first is not None and second is not None
    assert handler._create_output_push_task(request, output, {}) is None
    assert state.snapshot()["pending_pushes"] == 2

    first.cancel()
    second.cancel()
    await asyncio.gather(first, second, return_exceptions=True)
    await asyncio.sleep(0)
    assert state.snapshot()["pending_pushes"] == 0


def test_container_health_includes_the_shared_admission_authority():
    alive = SimpleNamespace(is_alive=lambda: True)
    container = object.__new__(ModuleContainer)
    container.dht_announcer = alive
    container.conn_handlers = [alive]
    container.runtime = SimpleNamespace(pools=[alive])
    container.admission_state = AdmissionState.local(_policy())

    assert container.is_healthy() is True
    container.admission_state.mark_unhealthy()
    assert container.is_healthy() is False


def test_managed_session_unpublishes_and_releases_queued_pushes():
    state = AdmissionState.local(_policy(max_pending_pushes=2))
    handler = object.__new__(TransformerConnectionHandler)
    handler._admission_state = state
    handler._handler_index = 0
    handler._handler_event_queues = [queue.Queue(maxsize=2)]
    handler._session_queues = {}
    handler._session_handlers = {}
    handler._session_tokens = {}
    handler._max_pending_pushes = 2
    request = runtime_pb2.ExpertRequest()

    with handler._managed_session("session"):
        assert state.resolve_session("session") == 0
        state.reserve_push("session")
        handler._session_queues["session"].put_nowait(request)
        assert state.snapshot()["pending_pushes"] == 1

    assert state.resolve_session("session") is None
    assert state.snapshot()["pending_pushes"] == 0
    assert state.snapshot()["healthy"] is True


def test_stale_route_generation_cannot_enter_a_reused_local_session():
    state = AdmissionState.local(_policy(max_pending_pushes=2))
    old_key, old_token = state.register_session("session", 0)
    old_owner, reserved_token = state.reserve_push("session")
    state.unregister_session(old_key, 0, old_token)
    new_key, new_token = state.register_session("session", 0)
    handler = object.__new__(TransformerConnectionHandler)
    handler._admission_state = state
    handler._handler_index = 0
    handler._handler_event_queues = [queue.Queue(maxsize=2)]
    handler._session_queues = {"session": asyncio.Queue(maxsize=2)}
    handler._session_tokens = {"session": new_token}

    with pytest.raises(AdmissionRejected, match="push target"):
        handler._put_into_session_queue(
            "session",
            runtime_pb2.ExpertRequest(),
            handler_index=old_owner,
            route_token=reserved_token,
        )

    assert handler._session_queues["session"].empty()
    assert state.snapshot()["pending_pushes"] == 0
    state.unregister_session(new_key, 0, new_token)


def test_full_remote_queue_releases_the_aggregate_push_reservation():
    state = AdmissionState.local(_policy(max_pending_pushes=2))
    session_key, route_token = state.register_session("session", 1)
    queues = [queue.Queue(maxsize=1), queue.Queue(maxsize=1)]
    queues[1].put_nowait("occupied")
    handler = object.__new__(TransformerConnectionHandler)
    handler._admission_state = state
    handler._handler_index = 0
    handler._handler_event_queues = queues
    handler._session_queues = {}
    handler._session_tokens = {}

    with pytest.raises(AdmissionRejected, match="temporarily unavailable"):
        handler._put_into_session_queue("session", runtime_pb2.ExpertRequest())

    assert state.snapshot()["pending_pushes"] == 0
    state.unregister_session(session_key, 1, route_token)


@pytest.mark.asyncio
async def test_listener_drops_a_stale_cross_handler_generation():
    state = AdmissionState.local(_policy(max_pending_pushes=2))
    old_key, old_token = state.register_session("session", 0)
    _, reserved_token = state.reserve_push("session")
    state.unregister_session(old_key, 0, old_token)
    new_key, new_token = state.register_session("session", 0)
    event_queue = queue.Queue()
    event_queue.put_nowait(
        (
            Event.PUSH,
            "session",
            (reserved_token, runtime_pb2.ExpertRequest()),
        )
    )
    event_queue.put_nowait((Event.SHUTDOWN, None, None))
    handler = object.__new__(TransformerConnectionHandler)
    handler._admission_state = state
    handler._own_event_queue = event_queue
    handler._session_queues = {"session": asyncio.Queue(maxsize=2)}
    handler._session_handlers = {"session": 0}
    handler._session_tokens = {"session": new_token}

    await handler._listen_to_event_queue()

    assert handler._session_queues["session"].empty()
    assert state.snapshot()["pending_pushes"] == 0
    state.unregister_session(new_key, 0, new_token)


def test_full_public_push_queue_cannot_block_shutdown():
    state = AdmissionState.local(_policy(max_pending_pushes=1))
    session_key, route_token = state.register_session("session", 0)
    _, reserved_token = state.reserve_push("session")
    event_queue = queue.Queue(maxsize=1)
    event_queue.put_nowait(
        (
            Event.PUSH,
            "session",
            (reserved_token, runtime_pb2.ExpertRequest()),
        )
    )

    class FakeHandler:
        def __init__(self):
            self._admission_state = state
            self._own_event_queue = event_queue
            self._outer_pipe = SimpleNamespace(send=lambda value: None)
            self.shutdown_timeout = 1
            self.alive = True

        def is_alive(self):
            return self.alive

        def join(self, timeout):
            self.alive = False

        def terminate(self):
            self.alive = False

    fake = FakeHandler()
    TransformerConnectionHandler.shutdown(fake)

    assert event_queue.get_nowait()[0] == Event.SHUTDOWN
    assert state.snapshot()["pending_pushes"] == 0
    state.unregister_session(session_key, 0, route_token)


def _handler_failure_record(exception, *, name=P2P_DAEMON_LOGGER_NAME, message=P2P_HANDLER_FAILURE_MESSAGE):
    return logging.LogRecord(
        name,
        logging.WARNING,
        __file__,
        1,
        message,
        (),
        (type(exception), exception, None),
    )


def test_routine_rejection_filter_bounds_each_category_and_sanitizes_output():
    clock = Clock()
    rejection_filter = RoutinePublicRejectionLogFilter(clock=clock, window_seconds=60, max_suppressed=2)

    first = _handler_failure_record(AdmissionRejected("public worker inference metadata is invalid"))
    assert rejection_filter.filter(first) is True
    assert first.exc_info is None
    assert first.exc_text is None
    assert first.stack_info is None
    assert first.getMessage() == PUBLIC_REJECTION_LOG_MESSAGE % 0

    repeated = _handler_failure_record(AdmissionRejected("public worker inference request is too large"))
    overflow = _handler_failure_record(AdmissionRejected("public worker inference metadata is invalid"))
    assert rejection_filter.filter(repeated) is False
    assert rejection_filter.filter(overflow) is False

    other_category = _handler_failure_record(AdmissionRejected("public worker push target is unavailable"))
    assert rejection_filter.filter(other_category) is True
    assert other_category.getMessage() == PUBLIC_REJECTION_LOG_MESSAGE % 0

    for _ in range(3):
        assert (
            rejection_filter.filter(
                _handler_failure_record(AdmissionRejected("public worker inference request is too large"))
            )
            is False
        )
    clock.advance(60)
    next_window = _handler_failure_record(AdmissionRejected("public worker inference metadata is invalid"))
    assert rejection_filter.filter(next_window) is True
    assert next_window.getMessage() == PUBLIC_REJECTION_LOG_MESSAGE % 2
    assert len(next_window.getMessage()) == len(first.getMessage())
    assert "metadata" not in next_window.getMessage()


def test_invalid_or_backwards_clocks_preserve_diagnostic_tracebacks_and_state():
    clock = Clock()
    rejection_filter = RoutinePublicRejectionLogFilter(clock=clock)
    routine_message = "public worker inference metadata is invalid"
    assert rejection_filter.filter(_handler_failure_record(AdmissionRejected(routine_message))) is True
    assert rejection_filter.filter(_handler_failure_record(AdmissionRejected(routine_message))) is False
    state_before_regression = dict(rejection_filter._categories)

    clock.value -= 1
    backwards = _handler_failure_record(AdmissionRejected(routine_message))
    original_exc_info = backwards.exc_info
    assert rejection_filter.filter(backwards) is True
    assert backwards.getMessage() == P2P_HANDLER_FAILURE_MESSAGE
    assert backwards.exc_info is original_exc_info
    assert rejection_filter._categories == state_before_regression

    def broken_clock():
        raise RuntimeError("clock failed")

    for bad_clock in (broken_clock, lambda: float("nan"), lambda: float("inf")):
        record = _handler_failure_record(AdmissionRejected(routine_message))
        original_exc_info = record.exc_info
        assert RoutinePublicRejectionLogFilter(clock=bad_clock).filter(record) is True
        assert record.getMessage() == P2P_HANDLER_FAILURE_MESSAGE
        assert record.exc_info is original_exc_info


@pytest.mark.parametrize(
    "message",
    [
        "public worker admission is temporarily unavailable",
        "public worker inference request is too large",
        "public worker inference metadata is invalid",
        "public worker session identity is invalid",
        "public worker session identity is already active",
        "public worker push target is unavailable",
        "public worker push request is too large",
        "public worker push metadata is invalid",
        "training RPCs are disabled on manifested public workers",
    ],
)
def test_fixed_routine_rejection_messages_are_sanitized(message):
    record = _handler_failure_record(AdmissionRejected(message))

    assert RoutinePublicRejectionLogFilter().filter(record) is True
    assert record.getMessage() == PUBLIC_REJECTION_LOG_MESSAGE % 0
    assert record.exc_info is None
    assert message not in record.getMessage()


@pytest.mark.parametrize(
    "record",
    [
        _handler_failure_record(AdmissionRejected("public worker admission state is unavailable")),
        _handler_failure_record(AdmissionRejected("future rejection category")),
        _handler_failure_record(AdmissionRejected("session identity is invalid")),
        _handler_failure_record(RuntimeError("unexpected failure")),
        _handler_failure_record(
            AdmissionRejected("public worker admission is temporarily unavailable"),
            name="unrelated.logger",
        ),
        _handler_failure_record(
            AdmissionRejected("public worker admission is temporarily unavailable"),
            message="Different failure message",
        ),
    ],
)
def test_rejection_filter_preserves_unknown_or_internal_tracebacks(record):
    rejection_filter = RoutinePublicRejectionLogFilter()
    original_message = record.getMessage()
    original_exc_info = record.exc_info

    assert rejection_filter.filter(record) is True
    assert record.getMessage() == original_message
    assert record.exc_info is original_exc_info


def test_unexpected_failures_are_never_rate_limited():
    rejection_filter = RoutinePublicRejectionLogFilter()
    for _ in range(3):
        record = _handler_failure_record(RuntimeError("unexpected failure"))
        assert rejection_filter.filter(record) is True
        assert record.exc_info is not None
        assert record.getMessage() == P2P_HANDLER_FAILURE_MESSAGE


def test_public_rejection_filter_installation_is_thread_safe_and_idempotent():
    target = logging.getLogger(P2P_DAEMON_LOGGER_NAME)
    original_filters = list(target.filters)
    target.filters[:] = [
        existing for existing in target.filters if not isinstance(existing, RoutinePublicRejectionLogFilter)
    ]
    installed = []

    try:
        threads = [
            threading.Thread(target=lambda: installed.append(install_public_rejection_log_filter())) for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=1)
        assert all(not thread.is_alive() for thread in threads)
        assert len(installed) == 8
        assert len({id(item) for item in installed}) == 1
        assert sum(isinstance(item, RoutinePublicRejectionLogFilter) for item in target.filters) == 1
    finally:
        target.filters[:] = original_filters


@pytest.mark.asyncio
async def test_only_public_handlers_install_the_rejection_filter(monkeypatch):
    installed = []

    async def no_op_add_p2p_handlers(self, *args, **kwargs):
        return None

    monkeypatch.setattr(
        "hivemind.moe.server.connection_handler.ConnectionHandler.add_p2p_handlers",
        no_op_add_p2p_handlers,
    )
    monkeypatch.setattr(
        "drift.server.handler.install_public_rejection_log_filter",
        lambda: installed.append("installed"),
    )
    handler = object.__new__(TransformerConnectionHandler)
    handler._listener_task = object()
    handler._handler_event_queues = []

    handler._admission_state = None
    await handler.add_p2p_handlers()
    assert installed == []

    handler._admission_state = object()
    await handler.add_p2p_handlers()
    assert installed == ["installed"]


@pytest.mark.asyncio
async def test_hivemind_stream_preserves_rpc_errors_while_bounding_expected_tracebacks():
    target = logging.getLogger(P2P_DAEMON_LOGGER_NAME)
    original_filters = list(target.filters)
    original_level = target.level
    target.filters[:] = [
        existing for existing in target.filters if not isinstance(existing, RoutinePublicRejectionLogFilter)
    ]
    records = []

    class RecordHandler(logging.Handler):
        def emit(self, record):
            records.append(record)

    record_handler = RecordHandler(level=logging.WARNING)
    target.addHandler(record_handler)
    target.setLevel(logging.WARNING)
    install_public_rejection_log_filter()
    server = client = None

    async def one_request():
        yield runtime_pb2.ExpertRequest()

    async def routine_rejection(requests, context):
        async for _ in requests:
            raise AdmissionRejected("public worker admission is temporarily unavailable")
        if False:
            yield runtime_pb2.ExpertResponse()

    async def unexpected_failure(requests, context):
        async for _ in requests:
            raise RuntimeError("unexpected transport failure")
        if False:
            yield runtime_pb2.ExpertResponse()

    async def call(client, server, name):
        responses = await client.iterate_protobuf_handler(
            server.peer_id,
            name,
            one_request(),
            runtime_pb2.ExpertResponse,
        )
        with pytest.raises(P2PHandlerError) as error:
            await asyncio.wait_for(anext(responses), timeout=10)
        return str(error.value)

    try:
        common_options = {
            "host_maddrs": ("/ip4/127.0.0.1/tcp/0",),
            "auto_nat": False,
            "conn_manager": False,
            "nat_port_map": False,
            "use_relay": False,
            "tls": True,
        }
        server = await P2P.create(initial_peers=[], **common_options)
        await server.add_protobuf_handler(
            "routine_rejection",
            routine_rejection,
            runtime_pb2.ExpertRequest,
            stream_input=True,
            stream_output=True,
        )
        await server.add_protobuf_handler(
            "unexpected_failure",
            unexpected_failure,
            runtime_pb2.ExpertRequest,
            stream_input=True,
            stream_output=True,
        )
        client = await P2P.create(initial_peers=await server.get_visible_maddrs(), **common_options)

        first_error = await call(client, server, "routine_rejection")
        second_error = await call(client, server, "routine_rejection")
        unexpected_error = await call(client, server, "unexpected_failure")

        assert "public worker admission is temporarily unavailable" in first_error
        assert second_error == first_error
        assert "unexpected transport failure" in unexpected_error

        handler_failures = [
            record
            for record in records
            if record.name == P2P_DAEMON_LOGGER_NAME
            and (
                record.getMessage().startswith("Routine public-worker request rejected")
                or record.getMessage() == P2P_HANDLER_FAILURE_MESSAGE
            )
        ]
        routine_records = [
            record
            for record in handler_failures
            if record.getMessage().startswith("Routine public-worker request rejected")
        ]
        unexpected_records = [
            record for record in handler_failures if record.getMessage() == P2P_HANDLER_FAILURE_MESSAGE
        ]
        assert len(routine_records) == 1
        assert routine_records[0].exc_info is None
        assert "public-worker" in routine_records[0].getMessage()
        assert len(unexpected_records) == 1
        assert unexpected_records[0].exc_info is not None
        assert isinstance(unexpected_records[0].exc_info[1], RuntimeError)
    finally:
        if client is not None:
            await client.shutdown()
        if server is not None:
            await server.shutdown()
        target.removeHandler(record_handler)
        target.filters[:] = original_filters
        target.setLevel(original_level)
