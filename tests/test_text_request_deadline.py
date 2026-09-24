"""Bounded request-deadline regressions for the existing text-v1 API path."""

import asyncio
import gc
import math
import re
import threading
import time
import unittest
import weakref
from unittest.mock import patch

import httpx
from fastapi import HTTPException

from drift import text_request as text_request_module
from drift.api.server import ChatCompletionRequest, create_app
from drift.api.text_response import text_peer_response
from drift.node.model_manager import ModelDescriptor, ModelManager, ModelRuntime
from drift.text_request import MAX_REQUEST_SECONDS, RequestContext, RequestDeadlineExceeded


class _Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


async def _eventually(predicate, *, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0.01)


class _ContextTextClient:
    supports_request_context = True

    def __init__(self):
        self.contexts = []
        self.requests = []
        self.closed = 0

    async def stream(self, body, *, chat, context):
        self.contexts.append(context)
        self.requests.append((body, chat))
        try:
            yield {"type": "heartbeat"}
            yield {"type": "delta", "text": "bounded answer"}
            yield {
                "type": "done",
                "finish_reason": "stop",
                "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
            }
        finally:
            self.closed += 1


class _LegacyHeartbeatClient:
    def __init__(self):
        self.started = 0
        self.closed = 0

    async def stream(self, body, *, chat):
        del body, chat
        self.started += 1
        try:
            while True:
                yield {"type": "heartbeat"}
                await asyncio.sleep(0.005)
        finally:
            self.closed += 1


class _SlowUnwindAfterDoneClient:
    supports_request_context = True

    def __init__(self):
        self.closed = 0
        self.cleanup_started = asyncio.Event()

    async def stream(self, body, *, chat, context):
        del body, chat, context
        try:
            yield {"type": "delta", "text": "complete answer"}
            yield {
                "type": "done",
                "finish_reason": "stop",
                "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
            }
        finally:
            self.cleanup_started.set()
            await asyncio.sleep(1.2)
            self.closed += 1


def _manager_for(client):
    manager = ModelManager()
    manager.register(
        ModelDescriptor("Community", selected_whole_shard_bytes=0),
        lambda: ModelRuntime(None, None, text_client=client),
    )
    return manager


class RequestContextTests(unittest.IsolatedAsyncioTestCase):
    def test_start_uses_exact_finite_budget_and_generated_identity(self):
        clock = _Clock()
        context = RequestContext.start(12.5, clock=clock)
        self.assertRegex(context.request_id, re.compile(r"^[0-9a-f]{32}$"))
        self.assertEqual(context.issued_at, 100.0)
        self.assertEqual(context.deadline, 112.5)
        self.assertEqual(context.remaining(), 12.5)
        self.assertEqual(context.remaining(3), 3.0)

        for invalid in (True, "1", None, 0, -1, math.nan, math.inf, -math.inf, MAX_REQUEST_SECONDS + 0.1):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "Invalid request timeout"):
                RequestContext.start(invalid, clock=clock)

    def test_clock_is_strict_finite_monotonic_and_deadline_is_exclusive(self):
        with self.assertRaisesRegex(ValueError, "Invalid request clock"):
            RequestContext.start(1.0, clock=lambda: 1)
        with self.assertRaisesRegex(ValueError, "Invalid request clock"):
            RequestContext.start(1.0, clock=lambda: math.nan)

        clock = _Clock()
        context = RequestContext.start(2.0, clock=clock)
        clock.now = 101.0
        self.assertEqual(context.remaining(), 1.0)
        clock.now = 100.5
        with self.assertRaisesRegex(ValueError, "Request clock moved backwards"):
            context.require_live()

        clock = _Clock()
        context = RequestContext.start(2.0, clock=clock)
        clock.now = 102.0
        with self.assertRaisesRegex(RequestDeadlineExceeded, "Request deadline exceeded"):
            context.require_live()

    async def test_run_distinguishes_operation_cap_from_absolute_deadline(self):
        context = RequestContext.start(1.0)
        with self.assertRaisesRegex(TimeoutError, "Request operation timed out"):
            await context.run(asyncio.sleep(1), cap=0.01)

        context = RequestContext.start(0.02)
        with self.assertRaisesRegex(RequestDeadlineExceeded, "Request deadline exceeded"):
            await context.run(asyncio.sleep(1))

    async def test_initial_expiry_cancels_precreated_task_before_it_executes(self):
        clock = _Clock()
        context = RequestContext.start(1.0, clock=clock)
        executed = []

        async def operation():
            executed.append(True)

        task = asyncio.create_task(operation())
        clock.now = context.deadline
        try:
            with self.assertRaises(RequestDeadlineExceeded):
                await context.run(task)
            await asyncio.sleep(0)
            self.assertTrue(task.cancelled())
            self.assertEqual(executed, [])
        finally:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def test_initial_expiry_preserves_inflight_resource_for_late_cleanup(self):
        clock = _Clock()
        context = RequestContext.start(1.0, clock=clock)
        resource = asyncio.get_running_loop().create_future()
        released = []
        clock.now = context.deadline

        with self.assertRaises(RequestDeadlineExceeded):
            await context.run(resource, on_abandoned=released.append)
        self.assertFalse(resource.cancelled())
        resource.set_result("lease")
        await _eventually(lambda: released == ["lease"])
        await asyncio.sleep(0)
        self.assertEqual(released, ["lease"])

    async def test_run_releases_late_resource_without_waiting_for_cancellation(self):
        may_finish = asyncio.Event()
        released = []

        async def ignores_cancellation_once():
            try:
                await may_finish.wait()
            except asyncio.CancelledError:
                await may_finish.wait()
            return "lease"

        context = RequestContext.start(0.02)
        with self.assertRaises(RequestDeadlineExceeded):
            await context.run(ignores_cancellation_once(), on_abandoned=released.append)
        self.assertEqual(released, [])
        may_finish.set()
        await _eventually(lambda: released == ["lease"])

    async def test_abandoned_producer_is_strongly_owned_until_late_cleanup(self):
        may_finish = asyncio.Event()
        producer_task = []
        released = []

        async def late_resource():
            producer_task.append(asyncio.current_task())
            await may_finish.wait()
            return "lease"

        context = RequestContext.start(0.02)
        with self.assertRaises(RequestDeadlineExceeded):
            await context.run(late_resource(), on_abandoned=released.append)
        reference = weakref.ref(producer_task.pop())
        gc.collect()
        self.assertIsNotNone(reference())
        self.assertIn(reference(), text_request_module._BACKGROUND_TASKS)
        self.assertEqual(released, [])

        may_finish.set()
        await _eventually(lambda: released == ["lease"])
        await asyncio.sleep(0)
        self.assertNotIn(reference(), text_request_module._BACKGROUND_TASKS)
        self.assertEqual(released, ["lease"])

    async def test_cancelled_pending_acquire_removes_waiter_without_losing_permit(self):
        entered = asyncio.Event()

        class ObservedSemaphore(asyncio.Semaphore):
            async def acquire(self):
                entered.set()
                return await super().acquire()

        semaphore = ObservedSemaphore(0)
        request = asyncio.create_task(RequestContext.start(1.0).acquire(semaphore))
        await asyncio.wait_for(entered.wait(), 1)
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request

        semaphore.release()
        await asyncio.wait_for(semaphore.acquire(), 1)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(semaphore.acquire(), 0.01)

    async def test_acquire_returns_a_grant_completed_at_expiry_exactly_once(self):
        clock = _Clock()
        context = RequestContext.start(1.0, clock=clock)

        class ExpiresOnGrant:
            def __init__(self):
                self.releases = 0

            async def acquire(self):
                clock.now = context.deadline
                return True

            def release(self):
                self.releases += 1

        semaphore = ExpiresOnGrant()
        with self.assertRaises(RequestDeadlineExceeded):
            await context.acquire(semaphore)
        await asyncio.sleep(0)
        self.assertEqual(semaphore.releases, 1)
        await asyncio.sleep(0)
        self.assertEqual(semaphore.releases, 1)


class TextApiDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_load_admission_waiters_never_enter_model_manager(self):
        started = threading.Event()
        may_finish = threading.Event()
        peer = _ContextTextClient()
        manager = ModelManager()

        def load():
            started.set()
            if not may_finish.wait(2):
                raise AssertionError("test did not release loader")
            return ModelRuntime(None, None, text_client=peer)

        manager.register(ModelDescriptor("Community", selected_whole_shard_bytes=0), load)
        original_load = manager.load
        call_count = 0
        call_lock = threading.Lock()

        def counted_load(identifier):
            nonlocal call_count
            with call_lock:
                call_count += 1
            return original_load(identifier)

        with patch.object(manager, "load", side_effect=counted_load):
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(
                    app=create_app(model_manager=manager, max_concurrent=1, request_timeout=1.0)
                ),
                base_url="http://test",
            )
            try:
                requests = [
                    asyncio.create_task(
                        client.post("/v1/completions", json={"model": "Community", "prompt": str(index)})
                    )
                    for index in range(4)
                ]
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                responses = await asyncio.wait_for(asyncio.gather(*requests), 2)
                self.assertTrue(all(response.status_code == 504 for response in responses))
                may_finish.set()
                await _eventually(
                    lambda: manager.snapshots()[0].state.value == "ready"
                    and manager.snapshots()[0].active_requests == 0
                )
                with call_lock:
                    self.assertEqual(call_count, 1)
                self.assertEqual(peer.requests, [])
            finally:
                may_finish.set()
                await client.aclose()
                manager.shutdown()

    async def test_blocked_model_load_times_out_and_late_lease_is_released(self):
        started = threading.Event()
        may_finish = threading.Event()
        loader_finished = threading.Event()
        peer = _ContextTextClient()
        manager = ModelManager()

        def load():
            started.set()
            if not may_finish.wait(2):
                raise AssertionError("test did not release loader")
            loader_finished.set()
            return ModelRuntime(None, None, text_client=peer)

        manager.register(ModelDescriptor("Community", selected_whole_shard_bytes=0), load)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(model_manager=manager, request_timeout=1.0)),
            base_url="http://test",
        )
        try:
            request = asyncio.create_task(
                client.post("/v1/completions", json={"model": "Community", "prompt": "hello"})
            )
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            response = await asyncio.wait_for(request, 2)
            self.assertEqual(response.status_code, 504)
            self.assertEqual(response.json(), {"detail": "Request deadline exceeded"})
            self.assertNotIn("RequestDeadlineExceeded", response.text)
            self.assertEqual(peer.requests, [])

            may_finish.set()
            self.assertTrue(await asyncio.to_thread(loader_finished.wait, 1))
            await _eventually(lambda: manager.snapshots()[0].active_requests == 0)
        finally:
            may_finish.set()
            await client.aclose()
            manager.shutdown()

    async def test_cancelled_load_admission_waiter_never_enters_model_manager(self):
        loader_started = threading.Event()
        may_finish = threading.Event()
        second_waiting = asyncio.Event()
        peer = _ContextTextClient()
        manager = ModelManager()

        def load():
            loader_started.set()
            if not may_finish.wait(2):
                raise AssertionError("test did not release loader")
            return ModelRuntime(None, None, text_client=peer)

        manager.register(ModelDescriptor("Community", selected_whole_shard_bytes=0), load)
        original_load = manager.load
        original_acquire = RequestContext.acquire
        load_calls = 0
        acquire_calls = 0
        call_lock = threading.Lock()

        def counted_load(identifier):
            nonlocal load_calls
            with call_lock:
                load_calls += 1
            return original_load(identifier)

        async def observed_acquire(context, semaphore):
            nonlocal acquire_calls
            acquire_calls += 1
            if acquire_calls == 2:
                second_waiting.set()
            return await original_acquire(context, semaphore)

        with patch.object(manager, "load", side_effect=counted_load), patch.object(
            RequestContext, "acquire", new=observed_acquire
        ):
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(
                    app=create_app(model_manager=manager, max_concurrent=1, request_timeout=1.0)
                ),
                base_url="http://test",
            )
            first = second = None
            try:
                first = asyncio.create_task(
                    client.post("/v1/completions", json={"model": "Community", "prompt": "first"})
                )
                self.assertTrue(await asyncio.to_thread(loader_started.wait, 1))
                second = asyncio.create_task(
                    client.post("/v1/completions", json={"model": "Community", "prompt": "second"})
                )
                await asyncio.wait_for(second_waiting.wait(), 1)
                second.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await second
                with call_lock:
                    self.assertEqual(load_calls, 1)

                may_finish.set()
                response = await asyncio.wait_for(first, 1)
                self.assertEqual(response.status_code, 200)
                await _eventually(lambda: manager.snapshots()[0].active_requests == 0)
                with call_lock:
                    self.assertEqual(load_calls, 1)
                self.assertEqual(len(peer.requests), 1)
            finally:
                may_finish.set()
                for request in (first, second):
                    if request is not None and not request.done():
                        request.cancel()
                await client.aclose()
                manager.shutdown()

    async def test_cancelled_http_load_releases_the_eventual_lease(self):
        started = threading.Event()
        may_finish = threading.Event()
        peer = _ContextTextClient()
        manager = ModelManager()

        def load():
            started.set()
            if not may_finish.wait(2):
                raise AssertionError("test did not release loader")
            return ModelRuntime(None, None, text_client=peer)

        manager.register(ModelDescriptor("Community", selected_whole_shard_bytes=0), load)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(model_manager=manager, request_timeout=1.0)),
            base_url="http://test",
        )
        try:
            request = asyncio.create_task(
                client.post("/v1/completions", json={"model": "Community", "prompt": "hello"})
            )
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request
            may_finish.set()
            await _eventually(
                lambda: manager.snapshots()[0].state.value == "ready" and manager.snapshots()[0].active_requests == 0
            )
            self.assertEqual(peer.requests, [])
        finally:
            may_finish.set()
            await client.aclose()
            manager.shutdown()

    async def test_semaphore_deadline_expires_before_peer_stream_starts(self):
        peer = _LegacyHeartbeatClient()
        manager = _manager_for(peer)
        semaphore = asyncio.Semaphore(1)
        await semaphore.acquire()
        try:
            with patch("drift.api.server.asyncio.Semaphore", return_value=semaphore):
                app = create_app(model_manager=manager, max_concurrent=1, request_timeout=0.05)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/v1/chat/completions",
                    json={"model": "Community", "messages": [{"role": "user", "content": "hello"}]},
                )
            self.assertEqual(response.status_code, 504)
            self.assertEqual(response.json(), {"detail": "Request deadline exceeded"})
            self.assertEqual(peer.started, 0)
            self.assertEqual(manager.snapshots()[0].active_requests, 0)
        finally:
            semaphore.release()
            manager.shutdown()

    async def test_legacy_heartbeat_does_not_extend_absolute_deadline(self):
        peer = _LegacyHeartbeatClient()
        manager = _manager_for(peer)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(model_manager=manager, request_timeout=0.05)),
            base_url="http://test",
        ) as client:
            response = await client.post("/v1/completions", json={"model": "Community", "prompt": "hello"})
        try:
            self.assertEqual(response.status_code, 504)
            self.assertEqual(response.json(), {"detail": "Request deadline exceeded"})
            self.assertNotIn("Traceback", response.text)
            self.assertNotIn("RequestDeadlineExceeded", response.text)
            self.assertEqual(peer.started, 1)
            self.assertEqual(peer.closed, 1)
            self.assertEqual(manager.snapshots()[0].active_requests, 0)
        finally:
            manager.shutdown()

    async def test_streaming_heartbeat_deadline_returns_sse_error_without_success(self):
        peer = _LegacyHeartbeatClient()
        manager = _manager_for(peer)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(model_manager=manager, request_timeout=0.05)),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "Community",
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": True,
                },
            )
        try:
            self.assertEqual(response.status_code, 200)
            self.assertIn('"message": "Request deadline exceeded"', response.text)
            self.assertIn('"type": "server_error"', response.text)
            self.assertTrue(response.text.rstrip().endswith("data: [DONE]"))
            self.assertNotIn('"usage"', response.text)
            self.assertNotIn('"finish_reason": "stop"', response.text)
            self.assertEqual(peer.started, 1)
            self.assertEqual(peer.closed, 1)
            self.assertEqual(manager.snapshots()[0].active_requests, 0)
        finally:
            manager.shutdown()

    async def test_completed_nonstream_response_survives_cleanup_past_execution_deadline(self):
        peer = _SlowUnwindAfterDoneClient()
        manager = _manager_for(peer)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(model_manager=manager, request_timeout=1.0)),
            base_url="http://test",
        ) as client:
            response = await client.post("/v1/completions", json={"model": "Community", "prompt": "hello"})
        try:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["choices"][0]["text"], "complete answer")
            self.assertEqual(
                response.json()["usage"],
                {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
            )
            self.assertTrue(peer.cleanup_started.is_set())
            self.assertEqual(peer.closed, 1)
            self.assertEqual(manager.snapshots()[0].active_requests, 0)
        finally:
            manager.shutdown()

    async def test_completed_stream_response_has_one_success_and_no_late_deadline_error(self):
        peer = _SlowUnwindAfterDoneClient()
        manager = _manager_for(peer)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(model_manager=manager, request_timeout=1.0)),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "Community",
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": True,
                },
            )
        try:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.text.count('"finish_reason": "stop"'), 1)
            self.assertEqual(response.text.count('"usage"'), 1)
            self.assertNotIn('"error"', response.text)
            self.assertNotIn("Request deadline exceeded", response.text)
            self.assertTrue(response.text.rstrip().endswith("data: [DONE]"))
            self.assertTrue(peer.cleanup_started.is_set())
            self.assertEqual(peer.closed, 1)
            self.assertEqual(manager.snapshots()[0].active_requests, 0)
        finally:
            manager.shutdown()

    async def test_context_aware_client_keeps_normal_openai_shape(self):
        peer = _ContextTextClient()
        manager = _manager_for(peer)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(model_manager=manager, request_timeout=1.0)),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "Community", "messages": [{"role": "user", "content": "hello"}]},
            )
        try:
            self.assertEqual(response.status_code, 200)
            result = response.json()
            self.assertEqual(result["model"], "Community")
            self.assertEqual(result["choices"][0]["message"]["content"], "bounded answer")
            self.assertEqual(result["usage"], {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4})
            self.assertEqual(len(peer.contexts), 1)
            self.assertIsInstance(peer.contexts[0], RequestContext)
            self.assertEqual(peer.closed, 1)
            self.assertEqual(manager.snapshots()[0].active_requests, 0)
        finally:
            manager.shutdown()

    async def test_disconnect_after_role_chunk_releases_unstarted_context_request(self):
        peer = _ContextTextClient()
        manager = _manager_for(peer)
        loaded = manager.load("Community")
        response = await text_peer_response(
            loaded,
            ChatCompletionRequest(model="Community", messages=[{"role": "user", "content": "hello"}], stream=True),
            chat=True,
            semaphore=asyncio.Semaphore(1),
            context=RequestContext.start(1.0),
        )
        try:
            role = await anext(response.body_iterator)
            self.assertIn('"role": "assistant"', role)
            await response.body_iterator.aclose()
            self.assertEqual(peer.requests, [])
            self.assertEqual(manager.snapshots()[0].active_requests, 0)
        finally:
            await response.body_iterator.aclose()
            loaded.release()
            manager.shutdown()

    async def test_response_cleanup_outliving_observation_retains_lease_and_permit(self):
        close_started = asyncio.Event()
        may_close = asyncio.Event()
        release_calls = 0

        class BlockingCloseIterator:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

            async def aclose(self):
                close_started.set()
                await may_close.wait()

        class BlockingCloseClient:
            def stream(self, body, *, chat):
                del body, chat
                return BlockingCloseIterator()

        manager = _manager_for(BlockingCloseClient())
        loaded = manager.load("Community")
        original_release = loaded.release
        semaphore = asyncio.Semaphore(1)
        real_wait = asyncio.wait

        def counted_release():
            nonlocal release_calls
            release_calls += 1
            original_release()

        async def short_cleanup_observation(futures, *, timeout=None, **kwargs):
            if timeout == 3.0:
                timeout = 0.01
            return await real_wait(futures, timeout=timeout, **kwargs)

        try:
            with patch.object(loaded, "release", side_effect=counted_release), patch(
                "drift.api.text_response.asyncio.wait", new=short_cleanup_observation
            ):
                with self.assertRaises(HTTPException) as raised:
                    await text_peer_response(
                        loaded,
                        ChatCompletionRequest(model="Community", messages=[{"role": "user", "content": "hello"}]),
                        chat=True,
                        semaphore=semaphore,
                        context=RequestContext.start(1.0),
                    )
                self.assertEqual(raised.exception.status_code, 503)
                self.assertEqual(raised.exception.detail, "The community answer did not finish")
                await asyncio.wait_for(close_started.wait(), 1)
                gc.collect()
                self.assertEqual(manager.snapshots()[0].active_requests, 1)
                self.assertTrue(semaphore.locked())
                self.assertEqual(release_calls, 0)

                may_close.set()
                await _eventually(lambda: manager.snapshots()[0].active_requests == 0 and not semaphore.locked())
                self.assertEqual(release_calls, 1)
                await asyncio.sleep(0)
                self.assertEqual(release_calls, 1)
        finally:
            may_close.set()
            loaded.release()
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
