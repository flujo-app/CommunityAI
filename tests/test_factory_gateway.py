"""Offline HTTP admission/JSON/SSE checks with controlled authority fixtures."""

import json
import unittest
from dataclasses import replace

import httpx
from test_factory_admission import MANIFEST, REQUEST, original

from drift.api.server import ChatCompletionRequest, CompletionRequest, create_app, create_factory_app
from drift.node.model_manager import ModelDescriptor, ModelManager, ModelRuntime
from drift.text_mesh import TextPeerOutcomeUnknown


class Client:
    supports_request_context = True
    supports_single_attempt = True

    def __init__(self):
        self.contexts = []
        self.fail = False

    async def stream(self, body, *, chat, context):
        context.begin_dispatch()
        self.contexts.append(context)
        if self.fail:
            raise TextPeerOutcomeUnknown(context.request_id)
        yield {"type": "delta", "text": "answer"}
        yield {
            "type": "done",
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.peer = Client()
        self.loads = []
        self.auth_checks = []
        self.manager = ModelManager()

        def load():
            self.loads.append(True)
            return ModelRuntime(None, None, text_client=self.peer)

        self.manager.register(ModelDescriptor("Fixture", aliases=(MANIFEST,), manifest_digest=MANIFEST), load)
        self.manager.register(
            ModelDescriptor("Local", execution="local"), lambda: self.fail("local fallback must not load")
        )
        self.body = {"model": MANIFEST, "messages": [{"role": "user", "content": "hello"}]}
        validated = ChatCompletionRequest(**self.body).model_dump(exclude_none=True)
        self.claimed = set()
        self.admission = self.record(validated)

    def record(self, body, request_id=REQUEST):
        def claim():
            if request_id in self.claimed:
                raise RuntimeError("original dispatch right consumed")
            self.claimed.add(request_id)

        return original(body, request_id=request_id, claim_dispatch=claim)

    async def asyncTearDown(self):
        self.manager.shutdown()

    def app(self, adapter=None):
        def verify(key):
            self.auth_checks.append(key)
            return key == "test-key"

        return create_factory_app(
            model_manager=self.manager,
            api_key_verifier=verify,
            factory_admission=adapter if adapter is not None else lambda body: self.admission,
        )

    async def post(self, body=None, *, path="/v1/chat/completions", adapter=None, auth=True):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app(adapter)), base_url="http://test"
        ) as api:
            return await api.post(
                path,
                json=self.body if body is None else body,
                headers={"Authorization": "Bearer test-key"} if auth else {},
            )

    async def test_missing_original_and_protected_class_refuse_before_auth_and_load(self):
        for admission in (None, replace(self.admission, confidentiality_class="required-confidential")):
            result = await self.post(adapter=lambda body: admission)
            self.assertEqual(result.status_code, 403)
        self.assertEqual(self.auth_checks, [])
        self.assertEqual(self.loads, [])
        self.assertEqual(self.peer.contexts, [])

    async def test_async_bootstrap_is_not_executed(self):
        effects = []

        async def bootstrap(body):
            effects.append(True)
            return self.admission

        self.assertEqual((await self.post(adapter=bootstrap)).status_code, 403)
        self.assertEqual(effects, [])
        self.assertEqual(self.auth_checks, [])
        self.assertEqual(self.loads, [])

    async def test_factory_slash_posts_refuse_without_redirect_or_admission(self):
        admissions = []

        def bootstrap(body):
            admissions.append(body)
            return self.admission

        for path, body in (
            ("/v1/chat/completions/", self.body),
            ("/v1/completions/", {"model": MANIFEST, "prompt": "hello"}),
        ):
            for follow_redirects in (False, True):
                for auth in (False, True):
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=self.app(bootstrap)),
                        base_url="http://test",
                        follow_redirects=follow_redirects,
                    ) as api:
                        response = await api.post(
                            path,
                            json=body,
                            headers={"Authorization": "Bearer test-key"} if auth else {},
                        )
                    self.assertEqual(admissions, [])
                    self.assertEqual(self.auth_checks, [])
                    self.assertEqual(self.loads, [])
                    self.assertEqual(self.claimed, set())
                    self.assertEqual(self.peer.contexts, [])
                    self.assertEqual(response.status_code, 404)
                    self.assertNotIn("location", response.headers)
                    self.assertEqual(response.history, [])

    async def test_ordinary_api_retains_slash_redirect_compatibility(self):
        app = create_app(model_manager=self.manager, api_keys=["test-key"])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            response = await api.post("/v1/chat/completions/", json=self.body)
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "http://test/v1/chat/completions")
        self.assertEqual(self.loads, [])
        self.assertEqual(self.peer.contexts, [])

    async def test_unsupported_tools_and_extra_message_fields_refuse_before_load(self):
        for field in ("tools", "tool_choice", "logprobs", "confidentiality_class", "request_id", "arbitrary"):
            with self.subTest(field=field):
                result = await self.post({**self.body, field: []})
                self.assertEqual(result.status_code, 422)
        for message in (
            {"role": "tool", "content": "tool output"},
            {"role": "assistant", "content": "", "tool_calls": []},
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "http://fixture"}}]},
            {"role": "user", "content": [{"type": "text", "text": "hello", "extra": True}]},
        ):
            self.assertEqual((await self.post({**self.body, "messages": [message]})).status_code, 422)
        self.assertEqual(self.auth_checks, [])
        self.assertEqual(self.loads, [])

    async def test_auto_altered_body_and_unauthenticated_requests_do_not_load(self):
        for change in ({"model": "auto"}, {"model": "Local"}, {"temperature": 0.5}):
            self.assertEqual((await self.post({**self.body, **change})).status_code, 403)
        self.assertEqual((await self.post(auth=False)).status_code, 401)
        self.assertEqual(self.loads, [])

    async def test_factory_hook_requires_authentication(self):
        with self.assertRaises(ValueError):
            create_app(model_manager=self.manager, factory_admission=lambda body: self.admission)
        with self.assertRaises(TypeError):
            create_factory_app(model_manager=self.manager, api_keys=["test-key"])
        with self.assertRaises(ValueError):
            create_factory_app(model_manager=self.manager, api_keys=["test-key"], factory_admission=None)

    async def test_json_and_sse_use_original_identity_and_terminal_usage(self):
        response = await self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], "chatcmpl-" + REQUEST)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "answer")
        self.body["stream"] = True
        stream_id = "c" * 32
        self.admission = self.record(ChatCompletionRequest(**self.body).model_dump(exclude_none=True), stream_id)
        response = await self.post()
        frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
        self.assertTrue(all(frame["id"] == "chatcmpl-" + stream_id for frame in frames))
        self.assertEqual(frames[-1]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(frames[-1]["usage"]["total_tokens"], 2)
        self.assertTrue(all(context.single_attempt for context in self.peer.contexts))
        self.assertTrue(all(item.active_requests == 0 for item in self.manager.snapshots()))

    async def test_unknown_json_and_sse_do_not_claim_completion_or_retry(self):
        self.peer.fail = True
        response = await self.post()
        self.assertEqual(response.status_code, 503)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "inference_outcome_unknown")
        self.assertEqual(detail["request_id"], REQUEST)
        self.assertFalse(detail["retryable"])
        self.body["stream"] = True
        self.admission = self.record(ChatCompletionRequest(**self.body).model_dump(exclude_none=True), "c" * 32)
        response = await self.post()
        self.assertIn('"code": "inference_outcome_unknown"', response.text)
        self.assertNotIn('"finish_reason": "stop"', response.text)
        self.assertEqual(len(self.peer.contexts), 2)  # one entry per explicit fixture request

    async def test_plain_completions_share_original_gate_and_identity(self):
        body = {"model": MANIFEST, "prompt": "hello"}
        self.admission = self.record(CompletionRequest(**body).model_dump(exclude_none=True))
        response = await self.post(body, path="/v1/completions")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], "cmpl-" + REQUEST)
        response = await self.post({**body, "tools": []}, path="/v1/completions")
        self.assertEqual(response.status_code, 422)

    async def test_repeated_original_post_cannot_dispatch_again(self):
        self.assertEqual((await self.post()).status_code, 200)
        result = await self.post()
        self.assertEqual(result.status_code, 403)
        self.assertEqual(result.json()["detail"]["code"], "request_not_dispatched")
        self.assertEqual(len(self.peer.contexts), 1)
        self.assertEqual(self.claimed, {REQUEST})

    async def test_unknown_original_cannot_gain_a_second_attempt_by_new_http_context(self):
        self.peer.fail = True
        self.assertEqual((await self.post()).status_code, 503)
        self.assertEqual((await self.post()).status_code, 403)
        self.assertEqual(len(self.peer.contexts), 1)
        self.assertEqual(self.claimed, {REQUEST})

    async def test_missing_blocks_refuse_before_auth_and_loading(self):
        from test_factory_admission import ready_route

        self.admission = replace(
            self.admission, route_snapshot=lambda: {**ready_route(), "covered_blocks": 2, "missing_blocks": [1]}
        )
        self.assertEqual((await self.post()).status_code, 403)
        self.assertEqual(self.auth_checks, [])
        self.assertEqual(self.loads, [])

    async def test_runtime_without_one_attempt_support_refuses_before_inference(self):
        self.peer.supports_single_attempt = False
        self.assertEqual((await self.post()).status_code, 503)
        self.assertEqual(self.peer.contexts, [])
        self.assertEqual(self.claimed, set())
