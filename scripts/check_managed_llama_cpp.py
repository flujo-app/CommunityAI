"""Subsecond fake-SSE contract check before managed llama.cpp integration.

No llama.cpp process, weights, GPU, network or CI suite is used here.
"""

# isort: skip_file

import asyncio
import json
import sys
import time
import types
from pathlib import Path

import httpx

PACKAGE = types.ModuleType("drift")
PACKAGE.__path__ = [str(Path(__file__).resolve().parents[1] / "src" / "drift")]
sys.modules["drift"] = PACKAGE

from drift.inference_provider import (  # noqa: E402
    Availability, EventKind, InferenceLimits, InferenceRequest, ProviderIdentity, ProviderProfile,
)
from drift.managed_llama_cpp import ManagedLlamaCppAdapter, ManagedLlamaCppBinding  # noqa: E402
from drift.managed_vllm_text import ManagedVllmTextClient  # noqa: E402


def frame(model, *, text="", finish=None, usage=None):
    return {"id": "cmpl-test-1", "object": "text_completion", "model": model,
            "choices": [] if usage is not None else [{"index": 0, "text": text, "finish_reason": finish}],
            **({"usage": usage} if usage is not None else {})}


async def main():
    profile = ProviderProfile("test/llama-qwen", "test/qwen-llama", Availability.AVAILABLE,
                              qualification_id="a" * 64)
    binding = ManagedLlamaCppBinding(
        profile, profile.model_id, "http://127.0.0.1:18247", "secret", (0,), "none", 256
    )
    assert binding.backend_id == "llama.cpp-b11173"
    for bad in (
        lambda: ManagedLlamaCppBinding(profile, profile.model_id, "http://other-host:18247", "secret", (0,), "none", 256),
        lambda: ManagedLlamaCppBinding(profile, profile.model_id, "http://127.0.0.1:18247", "secret", (0, 0), "layer", 256),
        lambda: ManagedLlamaCppBinding(profile, profile.model_id, "http://127.0.0.1:18247", "secret", (0, 1), "none", 256),
    ):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe llama.cpp binding accepted")

    seen = []

    def transport(request):
        body = json.loads(request.content)
        seen.append((request.url.path, body, request.headers.get("Authorization")))
        model = "wrong/model" if body["prompt"] == "bad" else body["model"]
        frames = [frame(model, text="hello"), frame(model, finish="length"),
                  frame(model, usage={"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3})]
        payload = b"".join(b"data: " + json.dumps(item).encode() + b"\n\n" for item in frames)
        payload += b"data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    try:
        adapter = ManagedLlamaCppAdapter(binding, client=client)
        identity = ProviderIdentity("test/provider", "test/instance")

        def request(prompt):
            now = time.monotonic()
            return InferenceRequest(identity, profile.profile_id, profile.model_id,
                                    "a" * 32, "b" * 32, now, now + 5, prompt,
                                    InferenceLimits(100, 4, 32, 4096))

        events = [event async for event in adapter.stream(request("good"))]
        assert [event.kind for event in events] == [EventKind.STARTED, EventKind.OUTPUT, EventKind.COMPLETED]
        assert events[1].text == "hello" and events[-1].usage.total_units == 3
        assert not adapter.quarantined
        assert seen[0][0] == "/v1/completions" and seen[0][2] == "Bearer secret"
        assert seen[0][1]["stream_options"] == {"include_usage": True}
        bridge = ManagedVllmTextClient(adapter, identity, "sha256:" + "c" * 64)
        assert bridge.adapter is adapter

        bad_adapter = ManagedLlamaCppAdapter(binding, client=client)
        rejected = [event async for event in bad_adapter.stream(request("bad"))]
        assert rejected[-1].kind is EventKind.FAILED and bad_adapter.quarantined
        print("PASS: llama.cpp binding, streaming usage, bridge admission and quarantine")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
