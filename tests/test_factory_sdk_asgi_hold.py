"""Offline SDK-wire-to-ASGI fixture; no original issuer or physical dispatch."""

import hashlib
import json
import unittest
from dataclasses import replace

import httpx

from drift.api.server import create_factory_app
from drift.factory_admission import FactoryAdmission, RequestAdmissionDenied
from drift.node.model_manager import ModelDescriptor, ModelManager, ModelRuntime

MANIFEST = "sha256:" + "1" * 64
# Copied from FACTORY's accepted owner-wire SDK run 438df4a3: actualWire.bodyUtf8
# equals its independently precommitted wire. Keep these bytes and digest fixed;
# constructing a fresh JSON object here would lose the SDK-final wire evidence.
SDK_BODY_UTF8 = (
    b'{"model":"sha256:1111111111111111111111111111111111111111111111111111111111111111",'
    b'"messages":[{"role":"user","content":"offline bridge fixture"}],'
    b'"temperature":1,"stream":true,"max_tokens":8,'
    b'"stream_options":{"include_usage":true}}'
)
SDK_BODY_SHA256 = "44c04671db48d10c164afcdf6558032c4ca68cb8f0a1c5e9ebdaeb7f449843b5"
# Independent R4 expectation for PR35's receiver-normalized, null-excluded body.
# In particular, PR35 inserts n=1 and renders temperature as a Python float.
NORMALIZED_UTF8 = (
    b'{"max_tokens":8,"messages":[{"content":"offline bridge fixture","role":"user"}],'
    b'"model":"sha256:1111111111111111111111111111111111111111111111111111111111111111",'
    b'"n":1,"stream":true,"stream_options":{"include_usage":true},"temperature":1.0}'
)
NORMALIZED_SHA256 = "21a0d9be9d903deba02504bb66506208338bc4423c8669a71db1904d21aa4890"
# Copied byte-for-byte from FACTORY's accepted real Flow R2 case-report
# actualWire.bodyUtf8. The Flow request adds a system message and a cache key;
# the key is only an opaque bound field at this receiver.
FLOW_SDK_BODY_UTF8 = (
    b'{"model":"sha256:1111111111111111111111111111111111111111111111111111111111111111",'
    b'"messages":[{"role":"user","content":"offline bridge fixture"},'
    b'{"role":"system","content":""}],"temperature":1,"stream":true,"max_tokens":8,'
    b'"stream_options":{"include_usage":true},"prompt_cache_key":"flujo-c1c5k0kx"}'
)
FLOW_SDK_BODY_SHA256 = "b86e125b6a031a226acc03147b5d865402ad149a621626fcdbf7629f5541fd03"
FLOW_NORMALIZED_UTF8 = (
    b'{"max_tokens":8,"messages":[{"content":"offline bridge fixture","role":"user"},'
    b'{"content":"","role":"system"}],'
    b'"model":"sha256:1111111111111111111111111111111111111111111111111111111111111111",'
    b'"n":1,"prompt_cache_key":"flujo-c1c5k0kx","stream":true,'
    b'"stream_options":{"include_usage":true},"temperature":1.0}'
)
FLOW_NORMALIZED_SHA256 = "ab288a143d34de92156f01d0d4c4f5d05bbdc51eda4a335e16c8816205cd08b7"
# FACTORY's offline-request-1 is not a valid CommunityAI request identity.
SYNTHETIC_ASGI_REQUEST_ID = "d" * 32


def canonical_bytes(body):
    return json.dumps(body, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


class HeldPeer:
    supports_request_context = True
    supports_single_attempt = True

    def __init__(self):
        self.entries = 0
        self.after_claim = 0
        self.request_ids = []
        self.bodies = []

    async def stream(self, body, *, chat, context):
        self.entries += 1
        self.request_ids.append(context.request_id)
        self.bodies.append(body)
        context.begin_dispatch()  # The fixture claim below always refuses.
        self.after_claim += 1
        yield {"type": "done", "finish_reason": "stop", "usage": {}}


class SDKWireASGIHoldTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = ModelManager()
        self.assertEqual(len(SDK_BODY_UTF8), 232)
        self.assertEqual(hashlib.sha256(SDK_BODY_UTF8).hexdigest(), SDK_BODY_SHA256)
        self.assertEqual(len(NORMALIZED_UTF8), 240)
        self.assertEqual(hashlib.sha256(NORMALIZED_UTF8).hexdigest(), NORMALIZED_SHA256)
        self.assertNotEqual(SDK_BODY_UTF8, NORMALIZED_UTF8)
        self.assertEqual(len(FLOW_SDK_BODY_UTF8), 299)
        self.assertEqual(hashlib.sha256(FLOW_SDK_BODY_UTF8).hexdigest(), FLOW_SDK_BODY_SHA256)
        self.assertEqual(len(FLOW_NORMALIZED_UTF8), 307)
        self.assertEqual(hashlib.sha256(FLOW_NORMALIZED_UTF8).hexdigest(), FLOW_NORMALIZED_SHA256)
        self.assertNotEqual(FLOW_SDK_BODY_UTF8, FLOW_NORMALIZED_UTF8)

        self.peer = HeldPeer()
        self.loads = []
        self.admissions = []
        self.auth_checks = []
        self.claim_attempts = 0

        def load():
            self.loads.append(True)
            return ModelRuntime(None, None, text_client=self.peer)

        self.manager.register(ModelDescriptor("Fixture", aliases=(MANIFEST,), manifest_digest=MANIFEST), load)

        def claim_hold():
            self.claim_attempts += 1
            raise RequestAdmissionDenied()

        self.synthetic_admission = FactoryAdmission(
            request_id=SYNTHETIC_ASGI_REQUEST_ID,
            body_digest=NORMALIZED_SHA256,
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
            claim_dispatch=claim_hold,
        )

        def admission(body):
            self.admissions.append(body)
            return self.synthetic_admission

        def authenticate(key):
            self.auth_checks.append(key)
            return key == "fixture-key"

        self.app = create_factory_app(
            model_manager=self.manager,
            api_key_verifier=authenticate,
            factory_admission=admission,
        )

    async def asyncTearDown(self):
        self.manager.shutdown()

    async def post(self, raw):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
            return await client.post(
                "/v1/chat/completions",
                content=raw,
                headers={"Content-Type": "application/json", "Authorization": "Bearer fixture-key"},
            )

    async def test_exact_sdk_bytes_normalize_and_stop_before_peer_dispatch(self):
        response = await self.post(SDK_BODY_UTF8)

        self.assertEqual(len(self.admissions), 1)
        normalized = canonical_bytes(self.admissions[0])
        self.assertEqual(normalized, NORMALIZED_UTF8)
        self.assertEqual(hashlib.sha256(normalized).hexdigest(), NORMALIZED_SHA256)
        self.assertEqual(self.auth_checks, ["fixture-key"])
        self.assertEqual(self.loads, [True])
        self.assertEqual(response.status_code, 200)  # SSE headers precede the held claim.
        frames = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
        self.assertEqual(frames[-1], "[DONE]")
        error = next(json.loads(frame)["error"] for frame in frames[:-1] if '"error"' in frame)
        self.assertEqual(error["code"], "request_not_dispatched")
        self.assertEqual(error["request_id"], SYNTHETIC_ASGI_REQUEST_ID)
        self.assertIs(error["retryable"], False)
        self.assertEqual(self.claim_attempts, 1)
        self.assertEqual(self.peer.entries, 1)
        self.assertEqual(self.peer.request_ids, [SYNTHETIC_ASGI_REQUEST_ID])
        self.assertEqual(self.peer.after_claim, 0)
        self.assertNotIn('"choices": []', response.text)
        self.assertNotIn('"finish_reason": "stop"', response.text)

    async def test_changed_usage_bit_cannot_reuse_fixed_admission(self):
        changed = SDK_BODY_UTF8.replace(b'"include_usage":true', b'"include_usage":false')
        self.assertNotEqual(changed, SDK_BODY_UTF8)
        response = await self.post(changed)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(self.admissions), 1)
        self.assertNotEqual(canonical_bytes(self.admissions[0]), NORMALIZED_UTF8)
        self.assertEqual(self.auth_checks, [])
        self.assertEqual(self.loads, [])
        self.assertEqual(self.claim_attempts, 0)
        self.assertEqual(self.peer.entries, 0)

    async def test_real_flow_sdk_body_normalizes_and_stops_at_held_claim(self):
        self.synthetic_admission = replace(self.synthetic_admission, body_digest=FLOW_NORMALIZED_SHA256)
        response = await self.post(FLOW_SDK_BODY_UTF8)

        self.assertEqual(len(self.admissions), 1)
        normalized = canonical_bytes(self.admissions[0])
        self.assertEqual(normalized, FLOW_NORMALIZED_UTF8)
        self.assertEqual(hashlib.sha256(normalized).hexdigest(), FLOW_NORMALIZED_SHA256)
        self.assertEqual(self.auth_checks, ["fixture-key"])
        self.assertEqual(self.loads, [True])
        self.assertEqual(response.status_code, 200)  # SSE headers precede the held claim.
        frames = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
        self.assertEqual(frames[-1], "[DONE]")
        error = next(json.loads(frame)["error"] for frame in frames[:-1] if '"error"' in frame)
        self.assertEqual(error["code"], "request_not_dispatched")
        self.assertEqual(error["request_id"], SYNTHETIC_ASGI_REQUEST_ID)
        self.assertIs(error["retryable"], False)
        self.assertEqual(self.claim_attempts, 1)
        self.assertEqual(self.peer.entries, 1)
        self.assertEqual(self.peer.request_ids, [SYNTHETIC_ASGI_REQUEST_ID])
        self.assertEqual(self.peer.bodies, [self.admissions[0]])
        self.assertEqual(self.peer.bodies[0]["prompt_cache_key"], "flujo-c1c5k0kx")
        self.assertEqual(self.peer.after_claim, 0)
        self.assertNotIn('"choices": []', response.text)
        self.assertNotIn('"finish_reason": "stop"', response.text)

    async def test_changed_flow_cache_key_cannot_reuse_fixed_admission(self):
        self.synthetic_admission = replace(self.synthetic_admission, body_digest=FLOW_NORMALIZED_SHA256)
        changed = FLOW_SDK_BODY_UTF8.replace(b'"flujo-c1c5k0kx"', b'"different"')
        response = await self.post(changed)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(self.admissions), 1)
        self.assertNotEqual(canonical_bytes(self.admissions[0]), FLOW_NORMALIZED_UTF8)
        self.assertEqual(self.auth_checks, [])
        self.assertEqual(self.loads, [])
        self.assertEqual(self.claim_attempts, 0)
        self.assertEqual(self.peer.entries, 0)

    async def test_invalid_flow_cache_key_refuses_before_admission(self):
        for invalid in (0, {}, [], "", "x" * 257):
            with self.subTest(invalid=invalid):
                changed = json.loads(FLOW_SDK_BODY_UTF8)
                changed["prompt_cache_key"] = invalid
                response = await self.post(json.dumps(changed).encode("utf-8"))
                self.assertEqual(response.status_code, 422)
        self.assertEqual(self.admissions, [])
        self.assertEqual(self.auth_checks, [])
        self.assertEqual(self.loads, [])
        self.assertEqual(self.claim_attempts, 0)
        self.assertEqual(self.peer.entries, 0)

    async def test_unknown_nested_option_refuses_before_admission(self):
        changed = SDK_BODY_UTF8.replace(
            b'"stream_options":{"include_usage":true}',
            b'"stream_options":{"include_usage":true,"unexpected":true}',
        )
        self.assertNotEqual(changed, SDK_BODY_UTF8)
        response = await self.post(changed)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.admissions, [])
        self.assertEqual(self.auth_checks, [])
        self.assertEqual(self.loads, [])
        self.assertEqual(self.claim_attempts, 0)
        self.assertEqual(self.peer.entries, 0)
