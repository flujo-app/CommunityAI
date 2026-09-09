"""Consumer acceptance: no local model, full text responses, real peer transport."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from drift.api.server import create_app
from drift.model_manifest import ModelManifest
from drift.node.model_manager import ModelDescriptor, ModelManager, ModelRuntime
from drift.protocol_identity import NodeIdentity, ProtocolSecurityError, RevocationStore
from drift.text_mesh import (
    TextPeerClient,
    TextPeerUnavailable,
    TextPeerProtocol,
    announcement_key,
    create_text_announcement,
    decode,
    verify_text_announcement,
)

MANIFEST = "manifests/candidates/qwen3.8-27b-fp8-dequant-eager.json"


class FakeTextClient:
    def __init__(self):
        self.requests = []
        self.closed_requests = 0

    async def stream(self, body, *, chat):
        self.requests.append((body, chat))
        try:
            yield {"type": "heartbeat"}
            yield {"type": "delta", "text": "Hello "}
            yield {"type": "delta", "text": "from peers."}
            yield {
                "type": "done",
                "finish_reason": "stop",
                "usage": {"prompt_tokens": 3, "completion_tokens": 3, "total_tokens": 6},
            }
        finally:
            self.closed_requests += 1


class ConsumerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = ModelManager()
        self.peer = FakeTextClient()
        self.health = {
            "status": "complete",
            "covered_blocks": 64,
            "total_blocks": 64,
            "peer_count": 4,
            "chat_ready": True,
        }
        self.manager.register(
            ModelDescriptor("Community", selected_whole_shard_bytes=0),
            lambda: ModelRuntime(None, None, text_client=self.peer),
            route_health=lambda: dict(self.health),
        )
        self.manager.register(
            ModelDescriptor("Local fallback", execution="local"),
            lambda: (_ for _ in ()).throw(AssertionError("Local model must not load")),
            route_health=lambda: {
                "status": "complete",
                "covered_blocks": 1,
                "total_blocks": 1,
                "peer_count": 0,
                "source": "local",
            },
        )
        self.manager.configure_auto_selection(["Community", "Local fallback"])
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(model_manager=self.manager)), base_url="http://test"
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        self.manager.shutdown()

    async def test_fresh_consumer_uses_community_with_all_local_weight_loading_forbidden(self):
        with patch(
            "drift.model_manifest.ManifestArtifactVerifier.ensure_path", side_effect=AssertionError("No download")
        ):
            result = await self.client.post(
                "/v1/chat/completions", json={"model": "auto", "messages": [{"role": "user", "content": "Hello"}]}
            )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["model"], "Community")
        self.assertEqual(result.json()["choices"][0]["message"]["content"], "Hello from peers.")
        self.assertEqual(result.json()["usage"]["total_tokens"], 6)
        self.assertEqual(self.manager.snapshots()[0].selected_whole_shard_bytes, 0)
        self.assertTrue(all(item.active_requests == 0 for item in self.manager.snapshots()))

    async def test_streaming_and_plain_completions_keep_openai_shape(self):
        result = await self.client.post(
            "/v1/chat/completions",
            json={"model": "auto", "messages": [{"role": "user", "content": "Hi"}], "stream": True},
        )
        self.assertIn('"content": "Hello "', result.text)
        self.assertIn("data: [DONE]", result.text)
        result = await self.client.post("/v1/completions", json={"model": "auto", "prompt": "Hi"})
        self.assertEqual(result.json()["choices"][0]["text"], "Hello from peers.")
        self.assertEqual(self.peer.closed_requests, 2)

    async def test_fallback_only_when_community_path_unavailable_then_returns(self):
        self.assertEqual(self.manager.resolve("auto").model_id, "Community")
        self.health.update(chat_ready=False)
        self.assertEqual(self.manager.resolve("auto").model_id, "Local fallback")
        self.health.update(chat_ready=True, covered_blocks=63, status="incomplete")
        self.assertEqual(self.manager.resolve("auto").model_id, "Local fallback")
        self.health.update(covered_blocks=64, status="complete")
        self.assertEqual(self.manager.resolve("auto").model_id, "Community")
        self.manager.configure_auto_selection(["Community", "Local fallback"], local_only=True)
        self.assertEqual(self.manager.resolve("auto").model_id, "Local fallback")

    async def test_disconnect_after_role_chunk_releases_unstarted_peer_request(self):
        from drift.api.server import ChatCompletionRequest
        from drift.api.text_response import text_peer_response

        loaded = self.manager.load("auto")
        response = await text_peer_response(
            loaded,
            ChatCompletionRequest(model="auto", messages=[{"role": "user", "content": "Hi"}], stream=True),
            chat=True,
            semaphore=asyncio.Semaphore(1),
        )
        await anext(response.body_iterator)
        await response.body_iterator.aclose()
        self.assertTrue(all(item.active_requests == 0 for item in self.manager.snapshots()))

    async def test_node_status_reaches_desktop_with_no_download_and_live_block_grid(self):
        from dataclasses import replace

        from communityai_desktop.client import NodeClient
        from communityai_desktop.controller import DesktopController
        from drift.node.server import create_node_app

        manifest = ModelManifest.load(MANIFEST)
        manager = ModelManager()
        descriptor = replace(ModelDescriptor.from_manifest(manifest), selected_whole_shard_bytes=0)
        health = {**self.health, "source": "discovery", "replica_counts": [1] * 64, "last_updated_age": 0.0}
        manager.register(
            descriptor, lambda: ModelRuntime(None, None, text_client=self.peer), route_health=lambda: health
        )
        manager.configure_auto_selection([descriptor.model_id])
        try:
            app = create_node_app(manager, api_keys=["api-test"], control_keys=["control-test"])
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
                response = await api.get("/control/v1/status", headers={"Authorization": "Bearer control-test"})
                self.assertEqual(response.status_code, 200)
                control = NodeClient("http://127.0.0.1:8080", "control-test")
                with patch.object(control, "_request", return_value=response.json()), patch.object(
                    control, "list_keys", return_value=[]
                ):
                    view = DesktopController(control).snapshot()
            model = view["models"][0]
            self.assertTrue(model["route_complete"])
            self.assertTrue(model["auto_selected"])
            self.assertIsNone(model["download_progress"])
            self.assertEqual(model["covered_blocks"], 64)
            self.assertEqual(model["health"]["replica_counts"], [1] * 64)
        finally:
            manager.shutdown()


class PeerStartupTests(unittest.TestCase):
    def test_readiness_discovery_starts_without_a_consumer_request(self):
        from drift.server.text_peer import TextPeerService

        discovered = []
        route = SimpleNamespace(start_discovery=lambda: discovered.append(True))
        runtime = SimpleNamespace(
            model=SimpleNamespace(transformer=SimpleNamespace(h=SimpleNamespace(sequence_manager=route))),
            close=lambda: None,
        )
        manifest = ModelManifest.load(MANIFEST)
        service = TextPeerService(
            SimpleNamespace(peer_id="peer"),
            SimpleNamespace(peer_id="peer"),
            manifest,
            initial_peers=[],
            cache_dir="unused",
        )

        async def serve(_runtime):
            self.assertTrue(discovered, "A text peer must discover blocks before advertising readiness")

        with patch("drift.server.text_peer.make_manifest_loader", return_value=lambda: runtime), patch.object(
            service, "_serve", side_effect=serve
        ):
            service._run()
        self.assertIsNone(service.error)


class IdentityTests(unittest.TestCase):
    def test_service_signature_binds_model_identity_expiry_and_revocation(self):
        manifest = ModelManifest.load(MANIFEST)
        with tempfile.TemporaryDirectory() as directory:
            identity = NodeIdentity.ensure(Path(directory) / "peer.key")
            source = create_text_announcement(
                manifest, identity, max_context_tokens=2048, max_output_tokens=512, now=1000
            ).to_dict()
            verify_text_announcement(source, manifest, identity.peer_id, now=1001)
            with self.assertRaises(ProtocolSecurityError):
                verify_text_announcement(source, manifest, identity.peer_id, now=1041)
            with self.assertRaises(ProtocolSecurityError):
                verify_text_announcement(
                    source,
                    manifest,
                    identity.peer_id,
                    now=1001,
                    revocations=RevocationStore(revoked_key_ids={identity.key_id}),
                )
            source["payload"]["max_output_tokens"] = 10000
            with self.assertRaises(ProtocolSecurityError):
                verify_text_announcement(source, manifest, identity.peer_id, now=1001)

    def test_malformed_and_oversized_requests_rejected(self):
        for data in (b'{"x":1,"x":2}', b'{"x":NaN}', b"x" * 65537):
            with self.assertRaises(ValueError):
                decode(data)


class RealTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_only_roundtrip_over_authenticated_peer_transport(self):
        from hivemind import DHT

        manifest = ModelManifest.load(MANIFEST)
        with tempfile.TemporaryDirectory() as directory:
            identity = NodeIdentity.ensure(Path(directory) / "peer.key")
            server = await asyncio.to_thread(
                DHT,
                initial_peers=[],
                host_maddrs=["/ip4/127.0.0.1/tcp/0"],
                identity_path=str(Path(directory) / "peer.key"),
                start=True,
                tls=True,
            )
            p2p = await server.replicate_p2p()

            class Engine:
                def __init__(self):
                    self.cancelled = asyncio.Event()

                async def stream(self, payload, peer):
                    if payload["body"]["prompt"] == "busy":
                        yield {"type": "error", "code": "busy", "message": "This community peer is busy"}
                        return
                    yield {"type": "delta", "text": payload["body"]["prompt"] + " via mesh"}
                    if payload["body"]["prompt"] == "cancel":
                        await self.cancelled.wait()
                        return
                    yield {
                        "type": "done",
                        "finish_reason": "stop",
                        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
                    }

                def cancel(self, request_id, peer):
                    self.cancelled.set()
                    return True

            engine = Engine()
            protocol = TextPeerProtocol(manifest, engine)
            client = None
            try:
                await protocol.add_p2p_handlers(p2p)
                record = create_text_announcement(manifest, identity, max_context_tokens=2048, max_output_tokens=512)
                await asyncio.to_thread(
                    server.store,
                    announcement_key(manifest),
                    record.to_dict(),
                    subkey=str(identity.peer_id),
                    expiration_time=record.payload["expires_at_ms"] / 1000,
                )
                peers = [str(a) for a in server.get_visible_maddrs()]
                client = await asyncio.to_thread(TextPeerClient, manifest, initial_peers=peers)
                frames = [frame async for frame in client.stream({"prompt": "Hello"}, chat=False)]
                self.assertEqual(frames[0]["text"], "Hello via mesh")
                self.assertEqual(frames[-1]["type"], "done")
                with self.assertRaisesRegex(TextPeerUnavailable, "This community peer is busy"):
                    _ = [frame async for frame in client.stream({"prompt": "busy"}, chat=False)]
                stream = client.stream({"prompt": "cancel"}, chat=False)
                first = await anext(stream)
                self.assertEqual(first["text"], "cancel via mesh")
                await stream.aclose()
                await asyncio.wait_for(engine.cancelled.wait(), 3)
            finally:
                if client is not None:
                    await asyncio.to_thread(client.close)
                await protocol.remove_p2p_handlers(p2p)
                await p2p.shutdown()
                await asyncio.to_thread(server.shutdown)


if __name__ == "__main__":
    unittest.main()
