"""Offline receiver-observed ASGI ingress; no Original issuer or physical send."""

import hashlib
import json
import unittest

import httpx
from test_factory_sdk_asgi_hold import (
    FLOW_NORMALIZED_SHA256,
    FLOW_SDK_BODY_SHA256,
    FLOW_SDK_BODY_UTF8,
    MANIFEST,
    HeldPeer,
)

from drift.api.server import create_factory_receiver_app_v2
from drift.factory_admission import FactoryAdmission, RequestAdmissionDenied
from drift.factory_receiver import (
    MAX_FACTORY_RAW_BODY_BYTES,
    FactoryAdmissionV2,
    FactoryBoundedBodyMiddleware,
    FactoryReceiverProfileV2,
    HostVerifiedTransport,
)
from drift.node.model_manager import ModelDescriptor, ModelManager, ModelRuntime

EXPECTED_HEADERS = (
    ("accept", "application/json"),
    ("content-type", "application/json"),
    ("user-agent", "OpenAI/JS 7.3.0"),
    ("x-stainless-arch", "x64"),
    ("x-stainless-lang", "js"),
    ("x-stainless-os", "Windows"),
    ("x-stainless-package-version", "7.3.0"),
    ("x-stainless-retry-count", "0"),
    ("x-stainless-runtime", "node"),
    ("x-stainless-runtime-version", "v24.19.0"),
)


class HostExtension:
    """Test-only outer ASGI host that has already verified a transport."""

    def __init__(self, app, identity):
        self.app = app
        self.identity = identity

    async def __call__(self, scope, receive, send):
        scope = dict(scope)
        scope["extensions"] = dict(scope.get("extensions", {}))
        if self.identity is not None:
            scope["extensions"]["verified_factory_transport"] = self.identity
        await self.app(scope, receive, send)


class FactoryReceiverV2Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = ModelManager()
        self.peer = HeldPeer()
        self.events = []
        self.observations = []
        self.claims = 0
        self.manager.register(
            ModelDescriptor("Fixture", aliases=(MANIFEST,), manifest_digest=MANIFEST),
            lambda: ModelRuntime(None, None, text_client=self.peer),
        )
        self.host_capability = object()
        self.transport = HostVerifiedTransport(
            host_capability=self.host_capability,
            principal_id="fixture-flow-owner",
            channel_binding_sha256="a" * 64,
            generation_sha256="b" * 64,
        )

    async def asyncTearDown(self):
        self.manager.shutdown()

    def app(self, *, identity=True, verifier=None, original_admission=None, expected_raw=FLOW_SDK_BODY_UTF8):
        def verify_transport(extensions):
            self.events.append("transport")
            return extensions.get("verified_factory_transport")

        def admit(observation, transport):
            self.events.append("original")
            self.observations.append(observation)
            if (
                transport != self.transport
                or observation.format != "communityai-factory-asgi-ingress-observation"
                or observation.schema_version != 2
                or observation.method != "POST"
                or observation.route != "/v1/chat/completions"
                or observation.headers != EXPECTED_HEADERS
                or observation.raw_body != expected_raw
                or observation.raw_body_sha256 != hashlib.sha256(expected_raw).hexdigest()
                or observation.normalized_body_sha256 != FLOW_NORMALIZED_SHA256
            ):
                raise RequestAdmissionDenied()
            admission = FactoryAdmission(
                request_id="d" * 32,
                body_digest=FLOW_NORMALIZED_SHA256,
                manifest_digest=MANIFEST,
                num_blocks=1,
                confidentiality_class="ordinary",
                class_version=1,
                authority=lambda: None,
                route_snapshot=lambda: {
                    "manifest_digest": MANIFEST,
                    "status": "complete",
                    "covered_blocks": 1,
                    "total_blocks": 1,
                    "missing_blocks": [],
                    "chat_ready": True,
                    "peer_count": 1,
                    "last_updated_age": 0.0,
                    "replica_counts": [1],
                },
                claim_dispatch=self.hold_claim,
            )
            return FactoryAdmissionV2(
                admission=admission,
                observation_sha256=observation.digest(),
                principal_id=transport.principal_id,
                channel_binding_sha256=transport.channel_binding_sha256,
                generation_sha256=transport.generation_sha256,
            )

        def bearer(candidate):
            self.events.append("bearer")
            return candidate == "fixture-key"

        app = create_factory_receiver_app_v2(
            model_manager=self.manager,
            api_key_verifier=bearer,
            receiver_profile=FactoryReceiverProfileV2(
                generation_sha256="b" * 64,
                host_capability=self.host_capability,
                transport_verifier=verify_transport if verifier is None else verifier,
                original_admission=admit if original_admission is None else original_admission,
            ),
        )
        return HostExtension(app, self.transport if identity else None)

    def hold_claim(self):
        self.claims += 1
        raise RequestAdmissionDenied()

    async def post(self, raw=FLOW_SDK_BODY_UTF8, *, app=None, headers=None, path="/v1/chat/completions"):
        if app is None:
            app = self.app()
        if headers is None:
            headers = {
                "accept": "application/json",
                "content-type": "application/json",
                "user-agent": "OpenAI/JS 7.3.0",
                "x-stainless-arch": "x64",
                "x-stainless-lang": "js",
                "x-stainless-os": "Windows",
                "x-stainless-package-version": "7.3.0",
                "x-stainless-retry-count": "0",
                "x-stainless-runtime": "node",
                "x-stainless-runtime-version": "v24.19.0",
                "authorization": "Bearer fixture-key",
            }
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post(path, content=raw, headers=headers)

    async def test_exact_real_flow_body_and_receiver_headers_reach_host_before_bearer(self):
        self.assertEqual(len(FLOW_SDK_BODY_UTF8), 299)
        self.assertEqual(hashlib.sha256(FLOW_SDK_BODY_UTF8).hexdigest(), FLOW_SDK_BODY_SHA256)
        response = await self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.events, ["transport", "original", "bearer"])
        observation = self.observations[0]
        self.assertEqual(observation.raw_body, FLOW_SDK_BODY_UTF8)
        self.assertNotEqual(observation.raw_body_sha256, observation.normalized_body_sha256)
        self.assertEqual(observation.headers, EXPECTED_HEADERS)
        self.assertEqual(self.claims, 1)
        self.assertEqual(self.peer.after_claim, 0)
        self.assertIn('"code": "request_not_dispatched"', response.text)

    async def test_headers_and_bearer_cannot_supply_missing_host_transport(self):
        response = await self.post(
            app=self.app(identity=False),
            headers={
                "content-type": "application/json",
                "authorization": "Bearer fixture-key",
                "x-factory-principal": "fixture-flow-owner",
                "x-factory-generation": "b" * 64,
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.events, ["transport"])
        self.assertEqual(self.observations, [])
        self.assertEqual(self.claims, 0)
        self.assertEqual(self.peer.entries, 0)

    async def test_stale_host_generation_refuses_before_original_or_bearer(self):
        stale = HostVerifiedTransport(self.host_capability, "fixture-flow-owner", "a" * 64, "c" * 64)
        app = HostExtension(self.app(identity=False), stale)
        response = await self.post(app=app)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.events, ["transport"])
        self.assertEqual(self.observations, [])

    async def test_foreign_host_capability_refuses_before_original_or_bearer(self):
        foreign = HostVerifiedTransport(object(), "fixture-flow-owner", "a" * 64, "b" * 64)
        app = HostExtension(self.app(identity=False), foreign)
        response = await self.post(app=app)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.events, ["transport"])
        self.assertEqual(self.observations, [])

    async def test_bare_v1_admission_cannot_downgrade_v2_profile(self):
        def bare_v1(observation, transport):
            self.events.append("original")
            return FactoryAdmission(
                request_id="d" * 32,
                body_digest=FLOW_NORMALIZED_SHA256,
                manifest_digest=MANIFEST,
                num_blocks=1,
                confidentiality_class="ordinary",
                class_version=1,
                authority=lambda: None,
                route_snapshot=lambda: {},
                claim_dispatch=self.hold_claim,
            )

        response = await self.post(app=self.app(original_admission=bare_v1))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.events, ["transport", "original"])
        self.assertEqual(self.claims, 0)
        self.assertEqual(self.peer.entries, 0)

    async def test_v2_admission_bound_to_another_observation_is_denied(self):
        def wrong_binding(observation, transport):
            self.events.append("original")
            return FactoryAdmissionV2(
                admission=FactoryAdmission(
                    request_id="d" * 32,
                    body_digest=FLOW_NORMALIZED_SHA256,
                    manifest_digest=MANIFEST,
                    num_blocks=1,
                    confidentiality_class="ordinary",
                    class_version=1,
                    authority=lambda: None,
                    route_snapshot=lambda: {},
                    claim_dispatch=self.hold_claim,
                ),
                observation_sha256="0" * 64,
                principal_id=transport.principal_id,
                channel_binding_sha256=transport.channel_binding_sha256,
                generation_sha256=transport.generation_sha256,
            )

        response = await self.post(app=self.app(original_admission=wrong_binding))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.events, ["transport", "original"])
        self.assertEqual(self.claims, 0)

    async def test_same_normalized_body_with_changed_raw_bytes_refuses_before_bearer(self):
        changed = FLOW_SDK_BODY_UTF8.replace(b'"temperature":1,', b'"temperature":1.0,')
        self.assertNotEqual(changed, FLOW_SDK_BODY_UTF8)
        response = await self.post(changed)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.events, ["transport", "original"])
        self.assertEqual(self.observations[0].normalized_body_sha256, FLOW_NORMALIZED_SHA256)
        self.assertNotEqual(self.observations[0].raw_body_sha256, FLOW_SDK_BODY_SHA256)
        self.assertEqual(self.claims, 0)

    async def test_added_routing_header_or_query_refuses_before_bearer(self):
        response = await self.post(
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "user-agent": "OpenAI/JS 7.3.0",
                "x-stainless-arch": "x64",
                "x-stainless-lang": "js",
                "x-stainless-os": "Windows",
                "x-stainless-package-version": "7.3.0",
                "x-stainless-retry-count": "0",
                "x-stainless-runtime": "node",
                "x-stainless-runtime-version": "v24.19.0",
                "openai-organization": "uncommitted",
                "authorization": "Bearer fixture-key",
            }
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.events, ["transport", "original"])
        self.assertIn(("openai-organization", "uncommitted"), self.observations[0].headers)
        self.events.clear()
        self.observations.clear()
        response = await self.post(path="/v1/chat/completions?extra=1")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.events, [])
        self.assertEqual(self.observations, [])

    async def test_decoded_route_with_encoded_raw_path_refuses_before_host_callbacks(self):
        class EncodedRawPath:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                scope = dict(scope)
                scope["raw_path"] = b"/v1/chat%2Fcompletions"
                await self.app(scope, receive, send)

        response = await self.post(app=EncodedRawPath(self.app()))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.events, [])

    async def test_body_overflow_refuses_before_host_or_bearer_callbacks(self):
        expanded = json.loads(FLOW_SDK_BODY_UTF8)
        expanded["messages"][0]["content"] = "x" * MAX_FACTORY_RAW_BODY_BYTES
        response = await self.post(json.dumps(expanded).encode("utf-8"))
        self.assertEqual(response.status_code, 413)
        # Invalid JSON would normally be 422; the ASGI gate must run first.
        invalid = b"{" + b"x" * MAX_FACTORY_RAW_BODY_BYTES
        self.assertEqual((await self.post(invalid)).status_code, 413)
        self.assertEqual(self.events, [])
        self.assertEqual(self.observations, [])

    async def test_exact_body_limit_replays_all_bytes_into_original_adapter(self):
        raw = FLOW_SDK_BODY_UTF8 + b" " * (MAX_FACTORY_RAW_BODY_BYTES - len(FLOW_SDK_BODY_UTF8))
        self.assertEqual(len(raw), MAX_FACTORY_RAW_BODY_BYTES)
        response = await self.post(raw, app=self.app(expected_raw=raw))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.events, ["transport", "original", "bearer"])
        self.assertEqual(self.observations[0].raw_body, raw)
        self.assertEqual(self.observations[0].normalized_body_sha256, FLOW_NORMALIZED_SHA256)
        self.assertEqual(self.claims, 1)
        self.assertEqual(self.peer.after_claim, 0)

    async def test_chunked_overflow_stops_at_first_excess_frame_before_inner_app(self):
        entered = []
        received = []
        sent = []
        frames = iter(
            [
                {"type": "http.request", "body": b"x" * MAX_FACTORY_RAW_BODY_BYTES, "more_body": True},
                {"type": "http.request", "body": b"y", "more_body": True},
                {"type": "http.request", "body": b"z", "more_body": False},
            ]
        )

        async def inner(scope, receive, send):
            entered.append(True)

        async def receive():
            received.append(True)
            return next(frames)

        async def send(message):
            sent.append(message)

        gate = FactoryBoundedBodyMiddleware(inner)
        await gate({"type": "http", "method": "POST", "path": "/v1/chat/completions"}, receive, send)
        self.assertEqual(entered, [])
        self.assertEqual(len(received), 2)
        self.assertEqual(sent[0]["status"], 413)
        self.assertEqual(sent[1]["type"], "http.response.body")

    async def test_chunked_299_byte_fixture_replays_exact_body_once(self):
        frames = iter(
            [
                {"type": "http.request", "body": FLOW_SDK_BODY_UTF8[:100], "more_body": True},
                {"type": "http.request", "body": b"", "more_body": True},
                {"type": "http.request", "body": FLOW_SDK_BODY_UTF8[100:], "more_body": False},
            ]
        )
        received = []
        sent = []

        async def inner(scope, receive, send):
            received.append(await receive())
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def receive():
            return next(frames)

        async def send(message):
            sent.append(message)

        gate = FactoryBoundedBodyMiddleware(inner)
        await gate({"type": "http", "method": "POST", "path": "/v1/chat/completions"}, receive, send)
        self.assertEqual(received, [{"type": "http.request", "body": FLOW_SDK_BODY_UTF8, "more_body": False}])
        self.assertEqual(sent[0]["status"], 204)

    async def test_declared_oversize_refuses_without_reading_any_body_frame(self):
        entered = []
        received = []
        sent = []

        async def inner(scope, receive, send):
            entered.append(True)

        async def receive():
            received.append(True)
            raise AssertionError("body frame should not be requested")

        async def send(message):
            sent.append(message)

        gate = FactoryBoundedBodyMiddleware(inner)
        await gate(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/chat/completions",
                "headers": [(b"content-length", str(MAX_FACTORY_RAW_BODY_BYTES + 1).encode("ascii"))],
            },
            receive,
            send,
        )
        self.assertEqual(entered, [])
        self.assertEqual(received, [])
        self.assertEqual(sent[0]["status"], 413)

    async def test_duplicate_or_oversize_projected_header_refuses_before_callbacks(self):
        for headers in (
            [("content-type", "application/json"), ("Content-Type", "application/json")],
            {"content-type": "application/json", "user-agent": "x" * 1025},
            [("content-type", "application/json")] + [(f"x-unprojected-{index}", "x") for index in range(65)],
        ):
            with self.subTest(headers=headers):
                response = await self.post(headers=headers)
                self.assertEqual(response.status_code, 431)
        self.assertEqual(self.events, [])
        self.assertEqual(self.observations, [])

    async def test_async_host_verifier_is_rejected_without_running_it(self):
        effects = []

        async def invalid_verifier(extensions):
            effects.append(True)
            return self.transport

        response = await self.post(app=self.app(verifier=invalid_verifier))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(effects, [])
        self.assertEqual(self.events, [])

    async def test_slash_route_has_no_redirect_or_original_callback(self):
        response = await self.post(path="/v1/chat/completions/")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("location", response.headers)
        self.assertEqual(self.events, [])

    def test_v2_requires_host_profile_and_api_authentication(self):
        with self.assertRaises(ValueError):
            create_factory_receiver_app_v2(model_manager=self.manager, receiver_profile=None, api_keys=["x"])
        with self.assertRaises(ValueError):
            create_factory_receiver_app_v2(
                model_manager=self.manager,
                receiver_profile=FactoryReceiverProfileV2(
                    "b" * 64, self.host_capability, lambda e: self.transport, lambda o, t: None
                ),
            )
        with self.assertRaises(ValueError):
            create_factory_receiver_app_v2(
                model_manager=self.manager,
                api_keys=["fixture-key"],
                factory_admission=lambda body: None,
                receiver_profile=FactoryReceiverProfileV2(
                    "b" * 64, self.host_capability, lambda e: self.transport, lambda o, t: None
                ),
            )
