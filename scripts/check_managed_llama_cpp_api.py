"""Small fake-backend API seam check before real llama.cpp routing.

No model, GPU, server process or broad CI suite is required.
"""

import json

import httpx
from fastapi.testclient import TestClient

from drift.api.server import create_app
from drift.inference_provider import Availability, ProviderIdentity, ProviderProfile
from drift.managed_llama_cpp import ManagedLlamaCppAdapter, ManagedLlamaCppBinding
from drift.managed_vllm_text import ManagedVllmTextClient
from drift.node.model_manager import ModelDescriptor, ModelManager, ModelRuntime


def main() -> None:
    model = "test/qwen-llama-api"
    digest = "sha256:" + "c" * 64
    profile = ProviderProfile("test/qwen-llama-profile", model, Availability.AVAILABLE, qualification_id="a" * 64)
    binding = ManagedLlamaCppBinding(profile, model, "http://127.0.0.1:18247", "backend-key", (0,), "none", 256)
    seen = []

    def backend(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((request.url.path, request.headers.get("Authorization"), body))
        assert body["model"] == model and body["max_tokens"] == 4
        frames = [
            {"id": "llama-1", "model": model, "choices": [{"index": 0, "text": "hello", "finish_reason": None}]},
            {"id": "llama-1", "model": model, "choices": [{"index": 0, "text": "", "finish_reason": "length"}]},
            {"id": "llama-1", "model": model, "choices": [],
             "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}},
        ]
        payload = b"".join(b"data: " + json.dumps(frame).encode() + b"\n\n" for frame in frames) + b"data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=payload)

    backend_client = httpx.AsyncClient(transport=httpx.MockTransport(backend))
    manager = ModelManager()
    adapter = ManagedLlamaCppAdapter(binding, client=backend_client)
    bridge = ManagedVllmTextClient(adapter, ProviderIdentity("test/provider", "test/instance"), digest)
    manager.register(ModelDescriptor(model, manifest_digest=digest),
                     lambda: ModelRuntime(model=None, tokenizer=None, text_client=bridge))
    app = create_app(model_manager=manager, api_keys=["api-key"], request_timeout=5.0)
    try:
        with TestClient(app) as client:
            url = "/v1/completions"
            request = {"model": model, "prompt": "hi", "max_tokens": 4}
            assert client.post(url, json=request).status_code == 401
            headers = {"Authorization": "Bearer api-key"}
            too_large = client.post(url, json={**request, "max_tokens": 256}, headers=headers)
            assert too_large.status_code == 400, too_large.text
            result = client.post(url, json={**request, "stream": False}, headers=headers)
            assert result.status_code == 200, result.text
            body = result.json()
            assert body["choices"][0]["text"] == "hello" and body["usage"]["total_tokens"] == 3
            assert len(seen) == 1 and seen[0][0] == url and seen[0][1] == "Bearer backend-key"
            assert seen[0][2]["stream_options"] == {"include_usage": True}
        print("PASS: llama.cpp fake stream through authenticated API; 256-token context enforced")
    finally:
        import asyncio
        asyncio.run(backend_client.aclose())


if __name__ == "__main__":
    main()
