"""Focused real FastAPI -> managed-provider seam check with a local fake server.

Run with the repository's offline test Python. No vLLM package, model, GPU,
internet, or CI job is used.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient

from drift.api.server import create_app
from drift.inference_provider import REQUIRED_PROFILES, Availability, ProviderIdentity, ProviderProfile
from drift.managed_vllm import ManagedVllmAdapter, ManagedVllmBinding
from drift.managed_vllm_text import ManagedVllmTextClient
from drift.node.model_manager import ModelDescriptor, ModelManager, ModelRuntime

MANIFEST = "sha256:" + "c" * 64
calls = []


class FakeBackend(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers["content-length"])
        payload = json.loads(self.rfile.read(length))
        calls.append((self.path, payload, self.headers.get("Authorization")))
        frames = [
            {
                "id": "backend-1",
                "model": "test/model",
                "choices": [{"index": 0, "text": "hello", "finish_reason": None}],
            },
            {"id": "backend-1", "model": "test/model", "choices": [{"index": 0, "text": "", "finish_reason": "stop"}]},
            {
                "id": "backend-1",
                "model": "test/model",
                "choices": [],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        ]
        body = b"".join(b"data: " + json.dumps(frame).encode() + b"\n\n" for frame in frames)
        body += b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeBackend)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        manager = ModelManager()
        identity = ProviderIdentity("test/provider", "test/instance")
        profile = ProviderProfile("test/profile", "test/model", Availability.AVAILABLE, qualification_id="e" * 64)
        binding = ManagedVllmBinding(
            profile, "test/model", f"http://127.0.0.1:{server.server_port}", "local-test-key", (0,), 1, 1
        )
        bridge = ManagedVllmTextClient(ManagedVllmAdapter(binding), identity, MANIFEST)
        manager.register(
            ModelDescriptor("test/model", manifest_digest=MANIFEST),
            lambda: ModelRuntime(model=None, tokenizer=None, text_client=bridge),
        )
        for model_id, unavailable in REQUIRED_PROFILES.items():
            candidate = ManagedVllmBinding(unavailable, model_id, binding.base_url, "local-test-key", (0,), 1, 1)
            refused = ManagedVllmTextClient(ManagedVllmAdapter(candidate), identity, MANIFEST)
            manager.register(
                ModelDescriptor(model_id, manifest_digest=MANIFEST),
                lambda refused=refused: ModelRuntime(model=None, tokenizer=None, text_client=refused),
            )
        app = create_app(model_manager=manager, api_keys=["api-secret"], request_timeout=5.0)
        with TestClient(app) as client:
            headers = {"Authorization": "Bearer api-secret"}
            unauthorized = client.post("/v1/completions", json={"model": "test/model", "prompt": "hi"})
            assert unauthorized.status_code == 401
            response = client.post(
                "/v1/completions",
                json={"model": "test/model", "prompt": "hi", "max_tokens": 20, "stream": False},
                headers=headers,
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["choices"][0]["text"] == "hello" and body["usage"]["total_tokens"] == 2
            assert len(calls) == 1 and calls[0][0] == "/v1/completions"
            assert calls[0][2] == "Bearer local-test-key"
            assert calls[0][1]["prompt"] == "hi"
            streamed = client.post(
                "/v1/completions",
                json={"model": "test/model", "prompt": "hi", "max_tokens": 20, "stream": True},
                headers=headers,
            )
            assert streamed.status_code == 200
            assert '"text": "hello"' in streamed.text and "data: [DONE]" in streamed.text
            assert len(calls) == 2
            for model_id in REQUIRED_PROFILES:
                refused = client.post("/v1/completions", json={"model": model_id, "prompt": "hi"}, headers=headers)
                assert refused.status_code == 503, (model_id, refused.status_code, refused.text)
            assert len(calls) == 2, "unavailable target reached the backend"
            chat = client.post(
                "/v1/chat/completions",
                json={"model": "test/model", "messages": [{"role": "user", "content": "hi"}]},
                headers=headers,
            )
            assert chat.status_code == 400
            assert len(calls) == 2
        for snapshot in manager.snapshots():
            assert snapshot.active_requests == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    print("managed API check: auth, real HTTP text path, exact-model refusal and lease release PASS")


if __name__ == "__main__":
    main()
